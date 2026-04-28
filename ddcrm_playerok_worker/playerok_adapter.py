from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import WorkerConfig
from .errors import ApiError, WorkerErrorCodes, platform_error, runtime_conflict
from .models import (
    WorkerV2AccountInfo,
    WorkerV2ConversationMessage,
    WorkerV2ConversationSummary,
    WorkerV2MediaItem,
    WorkerV2Product,
    WorkerV2ProductCreateRequest,
    WorkerV2ProductUpdateRequest,
)
from .storage import WorkerStateStorage


VENDOR_ROOT = Path(__file__).resolve().parents[1] / "vendor" / "playerok-universal"
if str(VENDOR_ROOT) not in sys.path:
    sys.path.insert(0, str(VENDOR_ROOT))

try:
    from playerokapi import exceptions as po_exceptions
    from playerokapi.account import Account
    from playerokapi.enums import ItemStatuses
except Exception:  # pragma: no cover - guarded at runtime
    po_exceptions = None  # type: ignore[assignment]
    Account = None  # type: ignore[assignment]
    ItemStatuses = None  # type: ignore[assignment]


@dataclass(frozen=True)
class PlayerokCredentials:
    account_id: str
    scheme: str
    token: str | None
    ddg5: str | None
    cookies: str | None
    user_agent: str | None
    proxy: str | None


class PlayerokAdapter:
    def __init__(self, config: WorkerConfig, storage: WorkerStateStorage) -> None:
        self._config = config
        self._storage = storage

    def list_capabilities(self) -> list[str]:
        return [
            "ext.account.proxy-credentials.apply",
            "ext.account.proxy-credentials.reveal",
            "ext.account.marketplace-auth.apply",
        ]

    def resolve_credentials(self, account_id: str) -> PlayerokCredentials:
        normalized_account_id = account_id.strip()
        if not normalized_account_id:
            raise runtime_conflict("Worker account scope не инициализирован (accountId binding отсутствует).")

        auth = self._storage.read_marketplace_auth(normalized_account_id)
        scheme = str(auth["scheme"]).strip().lower() if auth else ""
        credentials = auth.get("credentials", {}) if auth else {}
        normalized_credentials = self._normalize_credentials(credentials)

        if not scheme:
            if self._config.playerok_cookies:
                scheme = "cookies"
            else:
                scheme = "tokens"

        if not normalized_credentials:
            normalized_credentials = self._bootstrap_credentials_from_env(scheme)

        if scheme not in ("tokens", "cookies"):
            raise runtime_conflict(
                f"marketplaceAuth.scheme={scheme} пока не поддерживается в Playerok adapter.",
                details={"supportedSchemes": ["tokens", "cookies"]},
            )

        token: str | None = None
        ddg5: str | None = None
        cookies: str | None = None
        if scheme == "tokens":
            token = normalized_credentials.get("token") or self._config.playerok_token
            ddg5 = normalized_credentials.get("ddg5") or self._config.playerok_ddg5
            if not token or not ddg5:
                raise platform_error(
                    "Для marketplaceAuth.scheme=tokens требуются token и ddg5.",
                    details={"accountId": normalized_account_id},
                )
        else:
            cookies = normalized_credentials.get("cookies") or self._config.playerok_cookies
            if not cookies:
                raise platform_error(
                    "Для marketplaceAuth.scheme=cookies требуется credentials.cookies.",
                    details={"accountId": normalized_account_id},
                )

        user_agent = normalized_credentials.get("user_agent") or self._config.playerok_user_agent
        proxy = self._resolve_proxy_string(normalized_account_id)
        return PlayerokCredentials(
            account_id=normalized_account_id,
            scheme=scheme,
            token=token,
            ddg5=ddg5,
            cookies=cookies,
            user_agent=user_agent,
            proxy=proxy,
        )

    def account_info(self, account_id: str) -> WorkerV2AccountInfo:
        client, credentials = self._create_account_client(account_id)
        try:
            client.get()
        except Exception as exc:  # pragma: no cover - network runtime
            self._translate_exception(exc)

        balance = getattr(getattr(client, "profile", None), "balance", None)
        raw = {
            "email": getattr(client, "email", None),
            "role": getattr(client, "role", None),
            "canPublishItems": getattr(client, "can_publish_items", None),
            "unreadChatsCounter": getattr(client, "unread_chats_counter", None),
            "balance": {
                "value": getattr(balance, "value", None),
                "available": getattr(balance, "available", None),
                "frozen": getattr(balance, "frozen", None),
            }
            if balance is not None
            else None,
        }

        return WorkerV2AccountInfo(
            provider=self._config.provider,
            accountId=str(getattr(client, "id", None) or credentials.account_id),
            nickname=str(getattr(client, "username", None) or "unknown"),
            status="connected",
            profile={
                "authScheme": credentials.scheme,
                "proxyConfigured": credentials.proxy is not None,
                "userAgent": credentials.user_agent,
            },
            raw={k: v for k, v in raw.items() if v is not None},
        )

    def list_conversations(self, account_id: str) -> list[WorkerV2ConversationSummary]:
        client, _ = self._create_account_client(account_id)
        try:
            client.get()
            chats = self._load_all_chats(client)
        except Exception as exc:  # pragma: no cover - network runtime
            self._translate_exception(exc)

        items: list[WorkerV2ConversationSummary] = []
        current_user_id = str(getattr(client, "id", "") or "")
        for chat in chats:
            peer = self._resolve_chat_peer(getattr(chat, "users", []), current_user_id)
            last_message = getattr(chat, "last_message", None)
            last_message_at_raw = getattr(last_message, "created_at", None) or getattr(chat, "started_at", None)
            items.append(
                WorkerV2ConversationSummary(
                    conversationId=str(getattr(chat, "id", "")),
                    peerId=str(getattr(peer, "id", None) or getattr(chat, "id", "")),
                    peerName=str(getattr(peer, "username", None) or "unknown"),
                    unreadCount=int(getattr(chat, "unread_messages_counter", 0) or 0),
                    lastMessagePreview=str(getattr(last_message, "text", "") or ""),
                    lastMessageAt=self._parse_datetime(last_message_at_raw),
                )
            )

        items.sort(key=lambda item: item.lastMessageAt, reverse=True)
        return items

    def list_messages(self, account_id: str, conversation_id: str) -> list[WorkerV2ConversationMessage]:
        normalized_conversation_id = conversation_id.strip()
        if not normalized_conversation_id:
            raise platform_error("conversationId обязателен.")

        client, _ = self._create_account_client(account_id)
        try:
            client.get()
            pages = self._load_all_messages(client, normalized_conversation_id)
        except Exception as exc:  # pragma: no cover - network runtime
            self._translate_exception(exc)

        current_user_id = str(getattr(client, "id", "") or "")
        items = [self._to_conversation_message(message, current_user_id) for message in pages]
        items.sort(key=lambda item: item.createdAt, reverse=True)
        return items

    def send_message(
        self,
        account_id: str,
        conversation_id: str,
        text: str,
        has_attachments: bool,
    ) -> WorkerV2ConversationMessage:
        if has_attachments:
            raise runtime_conflict(
                "attachments в conversations.messages.send пока не поддержаны для Playerok runtime (ожидаются локальные file-path)."
            )

        normalized_conversation_id = conversation_id.strip()
        if not normalized_conversation_id:
            raise platform_error("conversationId обязателен.")

        client, _ = self._create_account_client(account_id)
        try:
            client.get()
            message = client.send_message(chat_id=normalized_conversation_id, text=text)
        except Exception as exc:  # pragma: no cover - network runtime
            self._translate_exception(exc)

        return self._to_conversation_message(message, str(getattr(client, "id", "") or ""))

    def list_products(self, account_id: str) -> list[WorkerV2Product]:
        client, _ = self._create_account_client(account_id)
        try:
            client.get()
            profile = client.get_user(id=str(client.id))
            statuses = self._supported_item_statuses()
            next_cursor: str | None = None
            items: list[WorkerV2Product] = []
            while True:
                page = profile.get_items(count=24, statuses=statuses, after_cursor=next_cursor)
                for item_profile in getattr(page, "items", []):
                    full_item = None
                    try:
                        full_item = client.get_item(id=str(getattr(item_profile, "id", "")))
                    except Exception:
                        full_item = item_profile
                    items.append(self._to_worker_product(full_item))

                page_info = getattr(page, "page_info", None)
                if not page_info or not bool(getattr(page_info, "has_next_page", False)):
                    break
                next_cursor = str(getattr(page_info, "end_cursor", "") or "")
                if not next_cursor:
                    break
        except Exception as exc:  # pragma: no cover - network runtime
            self._translate_exception(exc)

        items.sort(key=lambda item: item.productId, reverse=True)
        return items

    def create_product(self, account_id: str, request: WorkerV2ProductCreateRequest) -> WorkerV2Product:
        if request.schemaId.strip().lower() != "playerok.item.v1":
            raise platform_error(
                "Для Playerok worker поддерживается только schemaId=playerok.item.v1.",
                details={"schemaId": request.schemaId},
            )

        client, _ = self._create_account_client(account_id)
        try:
            client.get()
            attributes = request.attributes or {}
            game_category_id = self._read_required_attr(attributes, "gameCategoryId", aliases=["game_category_id"])
            obtaining_type_id = self._read_optional_attr(attributes, "obtainingTypeId", aliases=["obtaining_type_id"])
            if not obtaining_type_id:
                obtaining_type_id = self._resolve_default_obtaining_type(client, game_category_id)

            category = client.get_game_category(id=game_category_id)
            options = self._resolve_options(category, attributes)
            data_fields = self._resolve_data_fields(client, game_category_id, obtaining_type_id, attributes)
            attachments = self._resolve_media_paths(request.media)
            price = self._normalize_price(request.price.amount)

            created = client.create_item(
                game_category_id=game_category_id,
                obtaining_type_id=obtaining_type_id,
                name=request.title.strip(),
                price=price,
                description=(request.description or "").strip(),
                options=options,
                data_fields=data_fields,
                attachments=attachments,
            )
        except Exception as exc:  # pragma: no cover - network runtime
            self._translate_exception(exc)

        return self._to_worker_product(created)

    def update_product(
        self,
        account_id: str,
        product_id: str,
        request: WorkerV2ProductUpdateRequest,
    ) -> WorkerV2Product:
        normalized_product_id = product_id.strip()
        if not normalized_product_id:
            raise platform_error("productId обязателен.")

        changes = request.changes
        if changes.quantity is not None:
            raise runtime_conflict("Изменение quantity не поддерживается Playerok API напрямую.")

        client, _ = self._create_account_client(account_id)
        try:
            client.get()
            current = client.get_item(id=normalized_product_id)
            if current is None:
                raise platform_error("Товар не найден.", status_code=404)

            current_version = self._version_from_item(current)
            expected_version = (request.expectedVersion or "").strip()
            if expected_version and expected_version != current_version:
                raise runtime_conflict(
                    "expectedVersion не совпадает с текущей версией товара.",
                    details={"expectedVersion": expected_version, "actualVersion": current_version},
                )

            current_status = self._map_playerok_status(getattr(current, "status", None))
            if changes.status is not None:
                requested_status = self._normalize_worker_status(changes.status)
                if requested_status != current_status:
                    raise runtime_conflict(
                        "Изменение status через products.update для Playerok пока не поддерживается.",
                        details={"currentStatus": current_status, "requestedStatus": requested_status},
                    )

            attributes = changes.attributes or {}
            has_attributes_payload = bool(attributes)
            has_options_payload = has_attributes_payload and (
                "options" in attributes
                or any(
                    key not in self._reserved_attribute_keys()
                    for key in attributes
                )
            )
            has_data_fields_payload = has_attributes_payload and "dataFields" in attributes

            game_category_id = (
                self._read_optional_attr(attributes, "gameCategoryId", aliases=["game_category_id"])
                or str(getattr(getattr(current, "category", None), "id", "") or "")
            )
            obtaining_type_id = (
                self._read_optional_attr(attributes, "obtainingTypeId", aliases=["obtaining_type_id"])
                or str(getattr(getattr(current, "obtaining_type", None), "id", "") or "")
            )
            if has_data_fields_payload and not obtaining_type_id:
                if game_category_id:
                    obtaining_type_id = self._resolve_default_obtaining_type(client, game_category_id)
                else:
                    raise runtime_conflict(
                        "Для изменения dataFields требуется attributes.obtainingTypeId или определяемый gameCategoryId."
                    )

            category = client.get_game_category(id=game_category_id) if game_category_id and has_options_payload else None
            options = self._resolve_options(category, attributes) if category is not None else None
            data_fields = (
                self._resolve_data_fields(client, game_category_id, obtaining_type_id, attributes)
                if has_data_fields_payload and game_category_id and obtaining_type_id
                else None
            )

            remove_attachments = self._read_string_list(attributes.get("removeAttachmentIds")) if has_attributes_payload else None
            add_attachments = self._read_string_list(attributes.get("addAttachmentPaths")) if has_attributes_payload else None
            if changes.media is not None:
                media_paths = self._resolve_media_paths(changes.media)
                add_attachments = (add_attachments or []) + media_paths
            if add_attachments:
                add_attachments = list(dict.fromkeys(add_attachments))

            supported_changes_requested = any(
                [
                    changes.title is not None,
                    changes.description is not None,
                    changes.price is not None,
                    options is not None,
                    data_fields is not None,
                    bool(remove_attachments),
                    bool(add_attachments),
                ]
            )
            if not supported_changes_requested:
                raise runtime_conflict(
                    "products.update не содержит поддерживаемых для Playerok полей (title/description/price/options/dataFields/attachments)."
                )

            updated = client.update_item(
                id=normalized_product_id,
                name=changes.title.strip() if changes.title is not None else None,
                price=self._normalize_price(changes.price.amount) if changes.price is not None else None,
                description=(changes.description or "").strip() if changes.description is not None else None,
                options=options,
                data_fields=data_fields,
                remove_attachments=remove_attachments,
                add_attachments=add_attachments,
            )
        except Exception as exc:  # pragma: no cover - network runtime
            self._translate_exception(exc)

        return self._to_worker_product(updated)

    def delete_product(self, account_id: str, product_id: str) -> str:
        normalized_product_id = product_id.strip()
        if not normalized_product_id:
            raise platform_error("productId обязателен.")

        client, _ = self._create_account_client(account_id)
        try:
            client.get()
            removed = client.remove_item(id=normalized_product_id)
            if not removed:
                raise platform_error("Не удалось удалить товар в Playerok.", status_code=502)
        except Exception as exc:  # pragma: no cover - network runtime
            self._translate_exception(exc)

        return f"deleted:{normalized_product_id}:{int(datetime.now(UTC).timestamp())}"

    def _create_account_client(self, account_id: str) -> tuple[Any, PlayerokCredentials]:
        if Account is None:
            raise ApiError(
                status_code=500,
                error_code=WorkerErrorCodes.INTERNAL_ERROR,
                message="playerok-universal не загружен. Проверьте submodule vendor/playerok-universal.",
            )

        credentials = self.resolve_credentials(account_id)
        if credentials.scheme == "tokens":
            client = Account(
                token=credentials.token,
                ddg5=credentials.ddg5 or "",
                user_agent=credentials.user_agent or "",
                proxy=credentials.proxy,
                requests_timeout=self._config.playerok_requests_timeout,
            )
        else:
            client = Account(
                cookies=credentials.cookies,
                user_agent=credentials.user_agent or "",
                proxy=credentials.proxy,
                requests_timeout=self._config.playerok_requests_timeout,
            )
        return client, credentials

    def _normalize_credentials(self, credentials: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, value in credentials.items():
            normalized_key = str(key).strip()
            normalized_value = str(value).strip()
            if normalized_key and normalized_value:
                normalized[normalized_key] = normalized_value
        return normalized

    def _bootstrap_credentials_from_env(self, scheme: str) -> dict[str, str]:
        if scheme == "cookies":
            payload: dict[str, str] = {}
            if self._config.playerok_cookies:
                payload["cookies"] = self._config.playerok_cookies
            if self._config.playerok_user_agent:
                payload["user_agent"] = self._config.playerok_user_agent
            return payload

        payload = {}
        if self._config.playerok_token:
            payload["token"] = self._config.playerok_token
        if self._config.playerok_ddg5:
            payload["ddg5"] = self._config.playerok_ddg5
        if self._config.playerok_user_agent:
            payload["user_agent"] = self._config.playerok_user_agent
        return payload

    def _resolve_proxy_string(self, account_id: str) -> str | None:
        proxy = self._storage.read_proxy_credentials(account_id)
        if proxy is None:
            return None
        host = proxy["host"]
        port = proxy["port"]
        login = proxy["login"]
        password = proxy["password"]
        return f"{login}:{password}@{host}:{port}"

    def _load_all_chats(self, client: Any) -> list[Any]:
        chats: list[Any] = []
        next_cursor: str | None = None
        while True:
            page = client.get_chats(count=24, after_cursor=next_cursor)
            chats.extend(getattr(page, "chats", []))
            page_info = getattr(page, "page_info", None)
            if not page_info or not bool(getattr(page_info, "has_next_page", False)):
                break
            next_cursor = str(getattr(page_info, "end_cursor", "") or "")
            if not next_cursor:
                break
        return chats

    def _load_all_messages(self, client: Any, conversation_id: str) -> list[Any]:
        messages: list[Any] = []
        next_cursor: str | None = None
        while True:
            page = client.get_chat_messages(chat_id=conversation_id, count=24, after_cursor=next_cursor)
            messages.extend(getattr(page, "messages", []))
            page_info = getattr(page, "page_info", None)
            if not page_info or not bool(getattr(page_info, "has_next_page", False)):
                break
            next_cursor = str(getattr(page_info, "end_cursor", "") or "")
            if not next_cursor:
                break
        return messages

    def _resolve_chat_peer(self, users: list[Any], current_user_id: str) -> Any | None:
        for user in users:
            user_id = str(getattr(user, "id", "") or "")
            if current_user_id and user_id and user_id != current_user_id:
                return user
        return users[0] if users else None

    def _to_conversation_message(self, message: Any, current_user_id: str) -> WorkerV2ConversationMessage:
        author_id = str(getattr(getattr(message, "user", None), "id", "") or "")
        attachments: list[dict[str, str]] | None = None
        file_obj = getattr(message, "file", None)
        file_url = getattr(file_obj, "url", None) if file_obj else None
        if file_url:
            attachments = [{"type": "file", "url": str(file_url)}]

        return WorkerV2ConversationMessage(
            messageId=str(getattr(message, "id", "")),
            direction="out" if current_user_id and author_id == current_user_id else "in",
            text=str(getattr(message, "text", "") or ""),
            attachments=attachments,
            createdAt=self._parse_datetime(getattr(message, "created_at", None)),
        )

    def _supported_item_statuses(self) -> list[Any] | None:
        if ItemStatuses is None:
            return None

        ordered_names = (
            "APPROVED",
            "DRAFT",
            "PENDING_APPROVAL",
            "PENDING_MODERATION",
            "DECLINED",
            "BLOCKED",
            "EXPIRED",
            "SOLD",
        )
        statuses: list[Any] = []
        for name in ordered_names:
            member = ItemStatuses.__members__.get(name)
            if member is not None:
                statuses.append(member)
        return statuses or None

    def _to_worker_product(self, item: Any) -> WorkerV2Product:
        product_id = str(getattr(item, "id", "") or "")
        title = str(getattr(item, "name", None) or f"Item {product_id}")
        description = getattr(item, "description", None)

        price_raw = getattr(item, "price", 0) or 0
        try:
            price_value = float(price_raw)
        except (TypeError, ValueError):
            price_value = 0.0

        media: list[WorkerV2MediaItem] = []
        attachment = getattr(item, "attachment", None)
        if attachment is not None:
            attachment_url = getattr(attachment, "url", None)
            if attachment_url:
                media.append(WorkerV2MediaItem(type="image", url=str(attachment_url)))

        for file_obj in getattr(item, "attachments", []) or []:
            file_url = getattr(file_obj, "url", None)
            if file_url:
                media.append(WorkerV2MediaItem(type="image", url=str(file_url)))
        if not media:
            media = []

        attributes: dict[str, Any] = {}
        slug = getattr(item, "slug", None)
        if slug:
            attributes["slug"] = slug

        category = getattr(item, "category", None)
        category_id = getattr(category, "id", None) if category else None
        if category_id:
            attributes["gameCategoryId"] = str(category_id)

        obtaining_type = getattr(item, "obtaining_type", None)
        obtaining_type_id = getattr(obtaining_type, "id", None) if obtaining_type else None
        if obtaining_type_id:
            attributes["obtainingTypeId"] = str(obtaining_type_id)

        if getattr(item, "attributes", None):
            attributes["rawAttributes"] = getattr(item, "attributes")

        data_fields = getattr(item, "data_fields", None) or []
        if data_fields:
            attributes["dataFields"] = {
                str(getattr(field, "id", "")): str(getattr(field, "value", "") or "")
                for field in data_fields
                if getattr(field, "id", None)
            }

        return WorkerV2Product(
            productId=product_id,
            title=title,
            description=description,
            price={"amount": max(0.0, price_value), "currency": "RUB"},
            status=self._map_playerok_status(getattr(item, "status", None)),
            quantity=None,
            media=media or None,
            attributes=attributes or None,
            version=self._version_from_item(item),
            schemaId="playerok.item.v1",
        )

    def _version_from_item(self, item: Any) -> str:
        payload = {
            "id": str(getattr(item, "id", "") or ""),
            "name": getattr(item, "name", None),
            "description": getattr(item, "description", None),
            "price": getattr(item, "price", None),
            "status": getattr(getattr(item, "status", None), "name", getattr(item, "status", None)),
            "slug": getattr(item, "slug", None),
            "categoryId": getattr(getattr(item, "category", None), "id", None),
            "obtainingTypeId": getattr(getattr(item, "obtaining_type", None), "id", None),
            "updatedAt": getattr(item, "updated_at", None),
            "createdAt": getattr(item, "created_at", None),
        }
        digest = hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return digest[:16]

    def _map_playerok_status(self, status: Any) -> str:
        status_name = str(getattr(status, "name", status or "")).upper()
        mapping = {
            "APPROVED": "active",
            "DRAFT": "draft",
            "PENDING_APPROVAL": "pending_approval",
            "PENDING_MODERATION": "pending_moderation",
            "DECLINED": "declined",
            "BLOCKED": "blocked",
            "EXPIRED": "expired",
            "SOLD": "sold",
        }
        return mapping.get(status_name, status_name.lower() if status_name else "unknown")

    def _normalize_worker_status(self, status: str) -> str:
        normalized = status.strip().lower()
        aliases = {
            "approved": "active",
            "pendingapproval": "pending_approval",
            "pendingmoderation": "pending_moderation",
        }
        return aliases.get(normalized.replace("-", "").replace(" ", ""), normalized)

    def _normalize_price(self, amount: float) -> int:
        normalized = int(round(amount))
        if normalized < 1:
            raise platform_error("price.amount должен быть >= 1 для Playerok.")
        return normalized

    def _read_required_attr(self, attributes: dict[str, Any], key: str, *, aliases: list[str] | None = None) -> str:
        value = self._read_optional_attr(attributes, key, aliases=aliases)
        if value:
            return value
        raise platform_error(f"Для Playerok требуется attributes.{key}.")

    def _read_optional_attr(self, attributes: dict[str, Any], key: str, *, aliases: list[str] | None = None) -> str | None:
        candidates = [key] + (aliases or [])
        for candidate in candidates:
            if candidate in attributes and attributes[candidate] is not None:
                value = str(attributes[candidate]).strip()
                if value:
                    return value
        return None

    def _reserved_attribute_keys(self) -> set[str]:
        return {
            "gameCategoryId",
            "game_category_id",
            "obtainingTypeId",
            "obtaining_type_id",
            "options",
            "dataFields",
            "removeAttachmentIds",
            "addAttachmentPaths",
        }

    def _read_option_values(self, attributes: dict[str, Any]) -> dict[str, str]:
        values: dict[str, str] = {}

        options_raw = attributes.get("options")
        if isinstance(options_raw, dict):
            for key, value in options_raw.items():
                normalized_key = str(key).strip()
                normalized_value = str(value).strip()
                if normalized_key and normalized_value:
                    values[normalized_key] = normalized_value
        elif isinstance(options_raw, list):
            for entry in options_raw:
                if not isinstance(entry, dict):
                    continue
                field_name = str(entry.get("field", "")).strip()
                field_value = str(entry.get("value", "")).strip()
                if field_name and field_value:
                    values[field_name] = field_value

        for key, value in attributes.items():
            if key in self._reserved_attribute_keys():
                continue
            if isinstance(value, (str, int, float, bool)):
                normalized_key = str(key).strip()
                normalized_value = str(value).strip()
                if normalized_key and normalized_value:
                    values.setdefault(normalized_key, normalized_value)

        return values

    def _resolve_options(self, category: Any | None, attributes: dict[str, Any]) -> list[Any] | None:
        option_values = self._read_option_values(attributes)
        if not option_values:
            return None

        if category is None:
            raise platform_error("Не удалось определить category для attributes.options.")

        category_options = list(getattr(category, "options", []) or [])
        selected: list[Any] = []
        for field_name, field_value in option_values.items():
            matched = next(
                (
                    option
                    for option in category_options
                    if str(getattr(option, "field", "") or "") == field_name
                    and str(getattr(option, "value", "") or "") == field_value
                ),
                None,
            )
            if matched is None:
                raise platform_error(
                    f"Не найден option для field={field_name}, value={field_value} в выбранной категории Playerok."
                )
            selected.append(matched)
        return selected

    def _read_data_field_values(self, attributes: dict[str, Any]) -> dict[str, str]:
        data_fields_raw = attributes.get("dataFields")
        if data_fields_raw is None:
            return {}

        values: dict[str, str] = {}
        if isinstance(data_fields_raw, dict):
            for key, value in data_fields_raw.items():
                normalized_key = str(key).strip()
                normalized_value = str(value).strip()
                if normalized_key and normalized_value:
                    values[normalized_key] = normalized_value
            return values

        if isinstance(data_fields_raw, list):
            for entry in data_fields_raw:
                if not isinstance(entry, dict):
                    continue
                field_id = str(entry.get("fieldId", "")).strip()
                field_value = str(entry.get("value", "")).strip()
                if field_id and field_value:
                    values[field_id] = field_value
            return values

        raise platform_error("attributes.dataFields должен быть объектом или массивом.")

    def _resolve_data_fields(
        self,
        client: Any,
        game_category_id: str,
        obtaining_type_id: str,
        attributes: dict[str, Any],
    ) -> list[Any]:
        provided_values = self._read_data_field_values(attributes)
        page = client.get_game_category_data_fields(
            game_category_id=game_category_id,
            obtaining_type_id=obtaining_type_id,
            count=24,
        )
        available_fields = list(getattr(page, "data_fields", []) or [])
        if not available_fields:
            return []

        selected: list[Any] = []
        for field in available_fields:
            field_id = str(getattr(field, "id", "") or "")
            if not field_id:
                continue
            required = bool(getattr(field, "required", False))
            field_type_name = str(getattr(getattr(field, "type", None), "name", "") or "")
            value = provided_values.get(field_id)
            if value:
                field.value = value
                selected.append(field)
                continue
            if required and field_type_name == "ITEM_DATA":
                raise platform_error(f"Для Playerok требуется attributes.dataFields.{field_id}.")
        return selected

    def _resolve_default_obtaining_type(self, client: Any, game_category_id: str) -> str:
        page = client.get_game_category_obtaining_types(game_category_id=game_category_id, count=24)
        obtaining_types = list(getattr(page, "obtaining_types", []) or [])
        for obtaining_type in obtaining_types:
            obtaining_type_id = str(getattr(obtaining_type, "id", "") or "").strip()
            if obtaining_type_id:
                return obtaining_type_id
        raise platform_error(
            "Не удалось определить obtainingTypeId для выбранной категории Playerok.",
            details={"gameCategoryId": game_category_id},
        )

    def _resolve_media_paths(self, media: list[WorkerV2MediaItem] | None) -> list[str]:
        if not media:
            return []

        paths: list[str] = []
        for item in media:
            url = (item.url or "").strip()
            if not url:
                continue
            lowered = url.lower()
            if lowered.startswith("http://") or lowered.startswith("https://"):
                raise runtime_conflict(
                    "Playerok media upload поддерживает только локальные file-path в media.url."
                )
            file_path = Path(url)
            if not file_path.is_file():
                raise platform_error(f"Файл media.url не найден: {url}")
            paths.append(str(file_path.resolve()))
        return paths

    def _read_string_list(self, value: Any) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise platform_error("Списочное поле должно быть массивом строк.")
        normalized = [str(item).strip() for item in value if str(item).strip()]
        return normalized or None

    def _parse_datetime(self, value: Any) -> datetime:
        if value is None:
            return datetime.now(UTC)

        raw = str(value).strip()
        if not raw:
            return datetime.now(UTC)

        normalized = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return datetime.now(UTC)

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _translate_exception(self, exc: Exception) -> None:
        if isinstance(exc, ApiError):
            raise exc

        if po_exceptions is not None and isinstance(exc, po_exceptions.UnauthorizedError):
            raise ApiError(401, WorkerErrorCodes.AUTH_FAILED, "Playerok авторизация не удалась.") from exc
        if po_exceptions is not None and isinstance(exc, po_exceptions.BotCheckDetectedException):
            raise ApiError(
                401,
                WorkerErrorCodes.AUTH_FAILED,
                "Playerok bot-check отклонил запрос. Проверьте актуальность ddg5/cookies и привязку к IP.",
            ) from exc
        if po_exceptions is not None and isinstance(exc, po_exceptions.RequestPlayerokError):
            raise ApiError(
                502,
                WorkerErrorCodes.UNAVAILABLE,
                "Playerok вернул платформенную ошибку.",
                details={"reason": str(exc)},
            ) from exc
        if po_exceptions is not None and isinstance(exc, po_exceptions.NotInitiatedError):
            raise ApiError(
                500,
                WorkerErrorCodes.INTERNAL_ERROR,
                "Playerok client не инициализирован перед операцией.",
            ) from exc
        if po_exceptions is not None and isinstance(exc, (po_exceptions.RequestFailedError, po_exceptions.RequestSendingError)):
            raise ApiError(
                502,
                WorkerErrorCodes.UNAVAILABLE,
                "Playerok временно недоступен.",
                details={"reason": str(exc)},
            ) from exc
        if isinstance(exc, (TypeError, ValueError)):
            raise platform_error(str(exc)) from exc

        raise ApiError(
            502,
            WorkerErrorCodes.UNAVAILABLE,
            "Playerok временно недоступен.",
            details={"reason": str(exc)},
        ) from exc

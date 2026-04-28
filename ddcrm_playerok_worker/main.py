from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .config import WorkerConfig
from .errors import ApiError, WorkerErrorCodes, platform_error, runtime_conflict
from .models import (
    ExtensionActionRequest,
    ExtensionActionResponse,
    HealthResponse,
    MarketplaceAuthPayload,
    ProxyCredentialsPayload,
    WorkerV2AccountInfoResponse,
    WorkerV2CapabilitiesResponse,
    WorkerV2ConversationMessagesResponse,
    WorkerV2ConversationsResponse,
    WorkerV2MessageSendRequest,
    WorkerV2MessageSendResponse,
    WorkerV2ProductCreateRequest,
    WorkerV2ProductDeleteRequest,
    WorkerV2ProductMutationResponse,
    WorkerV2ProductSchema,
    WorkerV2ProductSchemaField,
    WorkerV2ProductSchemasResponse,
    WorkerV2ProductUpdateRequest,
    WorkerV2ProductsResponse,
)
from .playerok_adapter import PlayerokAdapter
from .security import enforce_service_token, require_idempotency_key, resolve_request_id
from .storage import WorkerStateStorage


config = WorkerConfig.load()
storage = WorkerStateStorage(
    db_path=config.storage_path,
    proxy_key_source=config.proxy_encryption_key,
    marketplace_key_source=config.marketplace_auth_encryption_key,
)
adapter = PlayerokAdapter(config=config, storage=storage)

app = FastAPI(
    title="DDCRM Playerok Worker",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
)
started_at = time.monotonic()


def _error_payload(request_id: str, error: ApiError) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "errorCode": error.error_code,
        "message": error.message,
        "requestId": request_id,
    }
    if error.details:
        payload["details"] = error.details
    return payload


def _request_id(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if isinstance(existing, str) and existing:
        return existing
    return resolve_request_id(request)


def _ok(model: Any, status_code: int = 200) -> JSONResponse:
    if hasattr(model, "model_dump"):
        content = model.model_dump(mode="json")
    else:
        content = model
    return JSONResponse(status_code=status_code, content=content)


def _normalize_limit(limit: int | None) -> int:
    if limit is None:
        return 50
    if limit < 1 or limit > 200:
        raise platform_error("limit должен быть в диапазоне 1..200.")
    return limit


TItem = TypeVar("TItem")


def _paginate(items: list[TItem], limit: int, cursor: str | None) -> tuple[list[TItem], str | None]:
    start = 0
    if cursor:
        try:
            start = int(cursor)
        except ValueError as exc:
            raise platform_error("cursor должен быть числом.") from exc
        if start < 0:
            raise platform_error("cursor не может быть отрицательным.")

    end = start + limit
    chunk = items[start:end]
    next_cursor = str(end) if end < len(items) else None
    return chunk, next_cursor


def _resolve_bound_account_id() -> str | None:
    configured = config.bound_account_id
    persisted = storage.read_bound_account_id()
    if configured:
        bound = storage.bind_account_if_unset(configured)
        if bound != configured:
            raise runtime_conflict(
                "Worker account binding конфликтует с runtime-конфигурацией.",
                details={"configuredAccountId": configured, "boundAccountId": bound},
            )
        return configured
    return persisted


def _require_bound_account_id() -> str:
    bound = _resolve_bound_account_id()
    if bound:
        return bound
    raise runtime_conflict(
        "Worker account context не инициализирован. Сначала выполните ext.account.marketplace-auth.apply c payload.accountId.",
        details={"hint": "single-tenant worker ожидает account binding"},
    )


def _enforce_account_scope(account_id: str) -> str:
    normalized = account_id.strip()
    if not normalized:
        raise platform_error("payload.accountId обязателен.")

    bound = _resolve_bound_account_id()
    if bound is None:
        bound = storage.bind_account_if_unset(normalized)

    if bound != normalized:
        raise runtime_conflict(
            "Worker уже привязан к другому accountId.",
            details={"boundAccountId": bound, "providedAccountId": normalized},
        )
    return bound


def _idempotent(
    *,
    request: Request,
    scope: str,
    operation: Callable[[], tuple[int, dict[str, Any]]],
) -> JSONResponse:
    idempotency_key = require_idempotency_key(request)
    cached = storage.read_idempotency(scope, idempotency_key)
    if cached is not None:
        return JSONResponse(status_code=cached.status_code, content=cached.payload)

    status_code, payload = operation()
    storage.write_idempotency(scope, idempotency_key, status_code, payload)
    return JSONResponse(status_code=status_code, content=payload)


@app.middleware("http")
async def request_guard(request: Request, call_next):
    request.state.request_id = resolve_request_id(request)
    if request.url.path != "/health":
        try:
            enforce_service_token(request, config)
        except ApiError as error:
            return JSONResponse(
                status_code=error.status_code,
                content=_error_payload(request.state.request_id, error),
            )
    return await call_next(request)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, error: ApiError):
    return JSONResponse(
        status_code=error.status_code,
        content=_error_payload(_request_id(request), error),
    )


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, error: ValidationError):
    payload_error = platform_error(
        "Запрос не прошёл валидацию.",
        details={"errors": error.errors()},
    )
    return JSONResponse(
        status_code=400,
        content=_error_payload(_request_id(request), payload_error),
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, error: Exception):
    payload_error = ApiError(
        status_code=500,
        error_code=WorkerErrorCodes.INTERNAL_ERROR,
        message="Внутренняя ошибка worker runtime.",
        details={"reason": str(error)},
    )
    return JSONResponse(
        status_code=500,
        content=_error_payload(_request_id(request), payload_error),
    )


@app.get("/health")
async def root_health():
    return {"status": "ok"}


@app.get("/internal/v2/worker/health")
async def worker_health(request: Request):
    uptime = int(max(0, time.monotonic() - started_at))
    return _ok(HealthResponse(requestId=_request_id(request), status="ok", uptimeSeconds=uptime))


@app.get("/internal/v2/worker/capabilities")
async def worker_capabilities(request: Request):
    capabilities = adapter.list_capabilities()
    features = {
        "account.info": True,
        "conversations.list": True,
        "conversations.messages.list": True,
        "conversations.messages.send": True,
        "products.list": True,
        "products.create": True,
        "products.update": True,
        "products.delete": True,
        "schemas.products.list": True,
    }
    response = WorkerV2CapabilitiesResponse(
        requestId=_request_id(request),
        provider=config.provider,
        features=features,
        capabilities=[{"key": item, "enabled": True} for item in capabilities],
    )
    return _ok(response)


@app.get("/internal/v2/worker/account")
async def worker_account(request: Request):
    account = adapter.account_info(_require_bound_account_id())
    return _ok(WorkerV2AccountInfoResponse(requestId=_request_id(request), account=account))


@app.get("/internal/v2/worker/conversations")
async def worker_conversations(request: Request, limit: int | None = None, cursor: str | None = None, onlyUnread: bool | None = None):
    normalized_limit = _normalize_limit(limit)
    items = adapter.list_conversations(_require_bound_account_id())
    if onlyUnread:
        items = [item for item in items if item.unreadCount > 0]
    page, next_cursor = _paginate(items, normalized_limit, cursor)
    return _ok(WorkerV2ConversationsResponse(requestId=_request_id(request), items=page, nextCursor=next_cursor))


@app.get("/internal/v2/worker/conversations/{conversation_id}/messages")
async def worker_conversation_messages(
    request: Request,
    conversation_id: str,
    limit: int | None = None,
    cursor: str | None = None,
):
    if not conversation_id.strip():
        raise platform_error("conversationId обязателен.")
    normalized_limit = _normalize_limit(limit)
    items = adapter.list_messages(_require_bound_account_id(), conversation_id)
    page, next_cursor = _paginate(items, normalized_limit, cursor)
    return _ok(WorkerV2ConversationMessagesResponse(requestId=_request_id(request), items=page, nextCursor=next_cursor))


@app.post("/internal/v2/worker/conversations/{conversation_id}/messages")
async def worker_conversation_send_message(request: Request, conversation_id: str, body: WorkerV2MessageSendRequest):
    if not conversation_id.strip():
        raise platform_error("conversationId обязателен.")
    if not body.text.strip():
        raise platform_error("text обязателен.")

    return _idempotent(
        request=request,
        scope=f"worker:conversations:send:{conversation_id}",
        operation=lambda: _build_message_send_response(
            request,
            _require_bound_account_id(),
            conversation_id,
            body,
        ),
    )


def _build_message_send_response(
    request: Request,
    account_id: str,
    conversation_id: str,
    body: WorkerV2MessageSendRequest,
) -> tuple[int, dict[str, Any]]:
    message = adapter.send_message(account_id, conversation_id, body.text.strip(), bool(body.attachments))
    response = WorkerV2MessageSendResponse(
        requestId=_request_id(request),
        messageId=message.messageId,
        status="sent",
        createdAt=message.createdAt,
    )
    return 200, response.model_dump(mode="json")


@app.get("/internal/v2/worker/products")
async def worker_products(request: Request, limit: int | None = None, cursor: str | None = None, status: str | None = None):
    normalized_limit = _normalize_limit(limit)
    items = adapter.list_products(_require_bound_account_id())
    if status and status.strip():
        items = [item for item in items if item.status == status.strip()]
    page, next_cursor = _paginate(items, normalized_limit, cursor)
    return _ok(WorkerV2ProductsResponse(requestId=_request_id(request), items=page, nextCursor=next_cursor))


@app.post("/internal/v2/worker/products")
async def worker_product_create(request: Request, body: WorkerV2ProductCreateRequest):
    if not body.title.strip():
        raise platform_error("title обязателен.")
    if body.price.amount < 0:
        raise platform_error("price.amount не может быть отрицательным.")

    return _idempotent(
        request=request,
        scope="worker:products:create",
        operation=lambda: _build_product_create_response(request, _require_bound_account_id(), body),
    )


def _build_product_create_response(
    request: Request,
    account_id: str,
    body: WorkerV2ProductCreateRequest,
) -> tuple[int, dict[str, Any]]:
    product = adapter.create_product(account_id, body)
    response = WorkerV2ProductMutationResponse(
        requestId=_request_id(request),
        productId=product.productId,
        status=product.status,
        version=product.version,
    )
    return 200, response.model_dump(mode="json")


@app.patch("/internal/v2/worker/products/{product_id}")
async def worker_product_update(request: Request, product_id: str, body: WorkerV2ProductUpdateRequest):
    if not product_id.strip():
        raise platform_error("productId обязателен.")
    return _idempotent(
        request=request,
        scope=f"worker:products:update:{product_id}",
        operation=lambda: _build_product_update_response(request, _require_bound_account_id(), product_id, body),
    )


def _build_product_update_response(
    request: Request,
    account_id: str,
    product_id: str,
    body: WorkerV2ProductUpdateRequest,
) -> tuple[int, dict[str, Any]]:
    if not body.changes.model_dump(exclude_none=True):
        raise platform_error("Требуется минимум одно поле в changes.")
    if body.changes.price is not None and body.changes.price.amount < 0:
        raise platform_error("price.amount не может быть отрицательным.")

    updated = adapter.update_product(account_id, product_id, body)
    response = WorkerV2ProductMutationResponse(
        requestId=_request_id(request),
        productId=updated.productId,
        status=updated.status,
        version=updated.version,
    )
    return 200, response.model_dump(mode="json")


@app.delete("/internal/v2/worker/products/{product_id}")
async def worker_product_delete(request: Request, product_id: str, body: WorkerV2ProductDeleteRequest | None = None):
    if not product_id.strip():
        raise platform_error("productId обязателен.")
    if body and body.mode not in (None, "soft", "hard"):
        raise platform_error("mode должен быть soft или hard.")
    return _idempotent(
        request=request,
        scope=f"worker:products:delete:{product_id}",
        operation=lambda: _build_product_delete_response(request, _require_bound_account_id(), product_id),
    )


def _build_product_delete_response(request: Request, account_id: str, product_id: str) -> tuple[int, dict[str, Any]]:
    version = adapter.delete_product(account_id, product_id)
    response = WorkerV2ProductMutationResponse(
        requestId=_request_id(request),
        productId=product_id,
        status="deleted",
        version=version,
    )
    return 200, response.model_dump(mode="json")


@app.get("/internal/v2/worker/schemas/products")
async def worker_product_schemas(request: Request, schemaId: str | None = None, provider: str | None = None):
    items = [
        WorkerV2ProductSchema(
            schemaId="playerok.item.v1",
            provider="playerok",
            fields=[
                WorkerV2ProductSchemaField(key="title", type="string", required=True),
                WorkerV2ProductSchemaField(key="description", type="string", required=False),
                WorkerV2ProductSchemaField(key="price.amount", type="number", required=True),
                WorkerV2ProductSchemaField(key="price.currency", type="string", required=True),
                WorkerV2ProductSchemaField(key="attributes.gameCategoryId", type="string", required=True),
                WorkerV2ProductSchemaField(key="attributes.obtainingTypeId", type="string", required=False),
                WorkerV2ProductSchemaField(key="attributes.options", type="object", required=False),
                WorkerV2ProductSchemaField(key="attributes.dataFields", type="object", required=False),
                WorkerV2ProductSchemaField(key="attributes.removeAttachmentIds", type="array", required=False),
                WorkerV2ProductSchemaField(key="attributes.addAttachmentPaths", type="array", required=False),
                WorkerV2ProductSchemaField(key="media", type="array", required=False),
            ],
        )
    ]
    if provider and provider.strip():
        items = [item for item in items if item.provider == provider.strip()]
    if schemaId and schemaId.strip():
        items = [item for item in items if item.schemaId == schemaId.strip()]
    return _ok(WorkerV2ProductSchemasResponse(requestId=_request_id(request), items=items))


@app.post("/internal/v2/worker/actions/{action}")
async def worker_actions(request: Request, action: str, body: ExtensionActionRequest | None = None):
    if not action.startswith("ext."):
        raise ApiError(status_code=400, error_code=WorkerErrorCodes.INVALID_ACTION, message="action должен начинаться с ext.")
    payload = body.payload if body else {}
    account_scope_suffix = ""
    account_hint = payload.get("accountId")
    if isinstance(account_hint, str) and account_hint.strip():
        account_scope_suffix = f":{account_hint.strip()}"

    return _idempotent(
        request=request,
        scope=f"worker:action:{action}{account_scope_suffix}",
        operation=lambda: _build_action_response(request, action, payload),
    )


def _build_action_response(request: Request, action: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    if action == "ext.account.proxy-credentials.apply":
        account_id = str(payload.get("accountId", "")).strip()
        if not account_id:
            raise platform_error("Для apply требуется payload.accountId.")
        _enforce_account_scope(account_id)
        proxy_raw = payload.get("proxyConfig")
        if not isinstance(proxy_raw, dict):
            raise platform_error("Для apply требуется payload.proxyConfig.")
        proxy = ProxyCredentialsPayload.model_validate(proxy_raw)
        storage.upsert_proxy_credentials(account_id, proxy.host.strip(), proxy.port, proxy.login.strip(), proxy.password.strip())
        response = ExtensionActionResponse(
            requestId=_request_id(request),
            result={"status": "applied", "accountId": account_id},
            warnings=[],
        )
        return 200, response.model_dump(mode="json")

    if action == "ext.account.proxy-credentials.reveal":
        account_id = str(payload.get("accountId", "")).strip()
        if not account_id:
            raise platform_error("Для reveal требуется payload.accountId.")
        _enforce_account_scope(account_id)
        proxy = storage.read_proxy_credentials(account_id)
        if proxy is None:
            raise platform_error("Для указанного accountId proxy credentials не найдены.", status_code=404)
        response = ExtensionActionResponse(
            requestId=_request_id(request),
            result={
                "accountId": account_id,
                "proxyConfig": proxy,
            },
            warnings=[],
        )
        return 200, response.model_dump(mode="json")

    if action == "ext.account.marketplace-auth.apply":
        account_id = str(payload.get("accountId", "")).strip()
        if not account_id:
            raise platform_error("Для marketplace auth apply требуется payload.accountId.")
        _enforce_account_scope(account_id)
        auth_raw = payload.get("marketplaceAuth")
        if not isinstance(auth_raw, dict):
            raise platform_error("Для marketplace auth apply требуется payload.marketplaceAuth.")
        auth = MarketplaceAuthPayload.model_validate(auth_raw)
        storage.upsert_marketplace_auth(account_id, auth.scheme.strip().lower(), auth.credentials)
        response = ExtensionActionResponse(
            requestId=_request_id(request),
            result={
                "status": "applied",
                "accountId": account_id,
                "scheme": auth.scheme.strip().lower(),
            },
            warnings=[],
        )
        return 200, response.model_dump(mode="json")

    raise ApiError(
        status_code=400,
        error_code=WorkerErrorCodes.INVALID_ACTION,
        message=f"Action {action} не поддерживается данным worker.",
    )


def run() -> None:
    import uvicorn

    uvicorn.run(
        "ddcrm_playerok_worker.main:app",
        host=config.bind_host,
        port=config.bind_port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    run()

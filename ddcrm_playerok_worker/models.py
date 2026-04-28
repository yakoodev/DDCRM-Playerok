from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RequestMeta(BaseModel):
    requestId: str


class CapabilityItem(BaseModel):
    key: str
    enabled: bool


class HealthResponse(RequestMeta):
    status: Literal["ok"]
    uptimeSeconds: int


class WorkerV2CapabilitiesResponse(RequestMeta):
    provider: str
    features: dict[str, bool]
    capabilities: list[CapabilityItem]


class WorkerV2AccountInfo(BaseModel):
    provider: str
    accountId: str
    nickname: str
    status: str
    profile: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class WorkerV2AccountInfoResponse(RequestMeta):
    account: WorkerV2AccountInfo


class WorkerV2ConversationSummary(BaseModel):
    conversationId: str
    peerId: str
    peerName: str
    unreadCount: int
    lastMessagePreview: str | None = None
    lastMessageAt: datetime


class WorkerV2ConversationsResponse(RequestMeta):
    items: list[WorkerV2ConversationSummary]
    nextCursor: str | None = None


class WorkerV2MessageAttachment(BaseModel):
    type: str
    url: str | None = None


class WorkerV2ConversationMessage(BaseModel):
    messageId: str
    direction: Literal["in", "out"]
    text: str
    attachments: list[WorkerV2MessageAttachment] | None = None
    createdAt: datetime


class WorkerV2ConversationMessagesResponse(RequestMeta):
    items: list[WorkerV2ConversationMessage]
    nextCursor: str | None = None


class WorkerV2MessageSendRequest(BaseModel):
    text: str
    attachments: list[WorkerV2MessageAttachment] | None = None


class WorkerV2MessageSendResponse(RequestMeta):
    messageId: str
    status: Literal["sent"]
    createdAt: datetime


class WorkerV2Money(BaseModel):
    amount: float
    currency: str

    @model_validator(mode="after")
    def validate_amount(self) -> "WorkerV2Money":
        if self.amount < 0:
            raise ValueError("price.amount не может быть отрицательным.")
        if not self.currency or len(self.currency.strip()) < 3:
            raise ValueError("currency должен быть длиной от 3 до 8 символов.")
        return self


class WorkerV2MediaItem(BaseModel):
    type: str
    url: str


class WorkerV2Product(BaseModel):
    productId: str
    title: str
    description: str | None = None
    price: WorkerV2Money
    status: str
    quantity: int | None = None
    media: list[WorkerV2MediaItem] | None = None
    attributes: dict[str, Any] | None = None
    version: str
    schemaId: str


class WorkerV2ProductsResponse(RequestMeta):
    items: list[WorkerV2Product]
    nextCursor: str | None = None


class WorkerV2ProductCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaId: str
    title: str
    description: str | None = None
    price: WorkerV2Money
    status: str | None = None
    quantity: int | None = None
    media: list[WorkerV2MediaItem] | None = None
    attributes: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_quantity(self) -> "WorkerV2ProductCreateRequest":
        if self.quantity is not None and self.quantity < 0:
            raise ValueError("quantity не может быть отрицательным.")
        return self


class WorkerV2ProductChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    price: WorkerV2Money | None = None
    status: str | None = None
    quantity: int | None = None
    media: list[WorkerV2MediaItem] | None = None
    attributes: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_changes(self) -> "WorkerV2ProductChanges":
        if self.quantity is not None and self.quantity < 0:
            raise ValueError("quantity не может быть отрицательным.")
        return self


class WorkerV2ProductUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expectedVersion: str | None = None
    changes: WorkerV2ProductChanges


class WorkerV2ProductDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["soft", "hard"] | None = None
    reason: str | None = None
    expectedVersion: str | None = None


class WorkerV2ProductMutationResponse(RequestMeta):
    productId: str
    status: str
    version: str


class WorkerV2ProductSchemaField(BaseModel):
    key: str
    type: str
    required: bool


class WorkerV2ProductSchema(BaseModel):
    schemaId: str
    provider: Literal["funpay", "playerok", "ggsell", "platimarket"]
    fields: list[WorkerV2ProductSchemaField]


class WorkerV2ProductSchemasResponse(RequestMeta):
    items: list[WorkerV2ProductSchema]


class ExtensionActionRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


class ExtensionActionResponse(RequestMeta):
    result: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class ProxyCredentialsPayload(BaseModel):
    host: str
    port: int
    login: str
    password: str

    @model_validator(mode="after")
    def validate_port_and_values(self) -> "ProxyCredentialsPayload":
        if self.port < 1 or self.port > 65535:
            raise ValueError("Поле port должно быть в диапазоне 1..65535.")
        if not self.host.strip():
            raise ValueError("Поле host обязательно и должно быть непустой строкой.")
        if not self.login.strip():
            raise ValueError("Поле login обязательно и должно быть непустой строкой.")
        if not self.password.strip():
            raise ValueError("Поле password обязательно и должно быть непустой строкой.")
        return self


MARKETPLACE_AUTH_SCHEMES: tuple[str, ...] = ("cookies", "tokens")


class MarketplaceAuthPayload(BaseModel):
    scheme: str
    credentials: dict[str, str]

    @model_validator(mode="after")
    def validate_auth(self) -> "MarketplaceAuthPayload":
        validate_marketplace_auth(self.scheme, self.credentials)
        return self


def validate_marketplace_auth(scheme: str, credentials: dict[str, str]) -> None:
    normalized_scheme = (scheme or "").strip().lower()
    if normalized_scheme not in MARKETPLACE_AUTH_SCHEMES:
        raise ValueError("payload.marketplaceAuth.scheme содержит неподдерживаемое значение.")

    if not credentials:
        raise ValueError("payload.marketplaceAuth.credentials должен содержать минимум одно значение.")

    normalized_credentials: dict[str, str] = {}
    for key, value in credentials.items():
        if not key or not key.strip():
            raise ValueError("payload.marketplaceAuth.credentials содержит пустой ключ.")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"payload.marketplaceAuth.credentials.{key} должен быть непустой строкой.")
        normalized_credentials[key.strip()] = value.strip()

    if normalized_scheme == "tokens":
        required_keys = ("token", "ddg5")
        for required_key in required_keys:
            if required_key not in normalized_credentials:
                raise ValueError(
                    f"Для marketplaceAuth.scheme=tokens требуется payload.marketplaceAuth.credentials.{required_key}."
                )

    if normalized_scheme == "cookies" and "cookies" not in normalized_credentials:
        raise ValueError(
            "Для marketplaceAuth.scheme=cookies требуется payload.marketplaceAuth.credentials.cookies."
        )

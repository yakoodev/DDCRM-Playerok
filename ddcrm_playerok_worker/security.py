from __future__ import annotations

import re
import uuid

from fastapi import Request

from .config import WorkerConfig
from .errors import ApiError, WorkerErrorCodes


_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{4,128}$")


def resolve_request_id(request: Request) -> str:
    existing = request.headers.get("X-Request-Id", "").strip()
    if existing and _REQUEST_ID_PATTERN.fullmatch(existing):
        return existing
    return uuid.uuid4().hex


def enforce_service_token(request: Request, config: WorkerConfig) -> None:
    if not config.service_auth_enabled:
        return

    token = request.headers.get("X-Service-Token", "").strip()
    if not token:
        raise ApiError(401, WorkerErrorCodes.AUTH_FAILED, "Отсутствует заголовок X-Service-Token.")

    if token in config.blocked_internal_tokens:
        raise ApiError(403, WorkerErrorCodes.AUTH_FAILED, "Токен internal-контура не может использоваться в worker API.")

    if token not in config.accepted_tokens:
        raise ApiError(401, WorkerErrorCodes.AUTH_FAILED, "Невалидный worker service-auth токен.")


def require_idempotency_key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key", "").strip()
    if len(key) < 8:
        raise ApiError(400, WorkerErrorCodes.PLATFORM_ERROR, "Для mutating endpoint обязателен Idempotency-Key длиной от 8 символов.")
    return key

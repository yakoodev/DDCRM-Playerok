from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class WorkerErrorCodes:
    UNAVAILABLE = "WORKER_UNAVAILABLE"
    INVALID_ACTION = "WORKER_INVALID_ACTION"
    AUTH_FAILED = "WORKER_AUTH_FAILED"
    RUNTIME_CONFLICT = "WORKER_RUNTIME_CONFLICT"
    PLATFORM_ERROR = "WORKER_PLATFORM_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(slots=True)
class ApiError(Exception):
    status_code: int
    error_code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def platform_error(message: str, status_code: int = 400, *, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(
        status_code=status_code,
        error_code=WorkerErrorCodes.PLATFORM_ERROR,
        message=message,
        details=details or {},
    )


def runtime_conflict(message: str, *, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(
        status_code=409,
        error_code=WorkerErrorCodes.RUNTIME_CONFLICT,
        message=message,
        details=details or {},
    )

from __future__ import annotations

import os
from dataclasses import dataclass


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class WorkerConfig:
    bind_host: str
    bind_port: int
    provider: str
    storage_path: str
    service_auth_enabled: bool
    accepted_tokens: tuple[str, ...]
    blocked_internal_tokens: tuple[str, ...]
    proxy_encryption_key: str
    marketplace_auth_encryption_key: str
    bound_account_id: str | None
    playerok_token: str | None
    playerok_ddg5: str | None
    playerok_cookies: str | None
    playerok_user_agent: str | None
    playerok_requests_timeout: int

    @staticmethod
    def load() -> "WorkerConfig":
        bind_host = os.getenv("WORKER_BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"
        bind_port_raw = os.getenv("WORKER_BIND_PORT", "8080").strip()
        try:
            bind_port = int(bind_port_raw)
        except ValueError as exc:
            raise ValueError("WORKER_BIND_PORT должен быть числом.") from exc
        if bind_port < 1 or bind_port > 65535:
            raise ValueError("WORKER_BIND_PORT должен быть в диапазоне 1..65535.")

        accepted_tokens = tuple(_parse_csv(os.getenv("WORKER_API_SERVICE_AUTH_ACCEPTED_TOKENS")))
        if not accepted_tokens:
            accepted_tokens = ("worker-token-a", "worker-token-b")

        blocked_tokens = tuple(_parse_csv(os.getenv("INTERNAL_API_SERVICE_AUTH_ACCEPTED_TOKENS")))
        bound_account_id = (
            (os.getenv("PLAYEROK_WORKER_ACCOUNT_ID") or "").strip()
            or (os.getenv("DDCRM_WORKER_ACCOUNT_ID") or "").strip()
            or None
        )

        timeout_raw = os.getenv("PLAYEROK_REQUESTS_TIMEOUT", "30").strip()
        try:
            timeout_value = int(timeout_raw)
        except ValueError as exc:
            raise ValueError("PLAYEROK_REQUESTS_TIMEOUT должен быть числом.") from exc
        if timeout_value < 3 or timeout_value > 300:
            raise ValueError("PLAYEROK_REQUESTS_TIMEOUT должен быть в диапазоне 3..300.")

        return WorkerConfig(
            bind_host=bind_host,
            bind_port=bind_port,
            provider=(os.getenv("WORKER_PROVIDER", "playerok").strip() or "playerok").lower(),
            storage_path=os.getenv("WORKER_STORAGE_PATH", "./data/worker-state.sqlite3").strip(),
            service_auth_enabled=_parse_bool(os.getenv("WORKER_API_SERVICE_AUTH_ENABLED"), True),
            accepted_tokens=accepted_tokens,
            blocked_internal_tokens=blocked_tokens,
            proxy_encryption_key=os.getenv(
                "WORKER_PROXY_CREDENTIALS_ENCRYPTION_KEY",
                "ddcrm-local-worker-proxy-credentials-key",
            ).strip(),
            marketplace_auth_encryption_key=os.getenv(
                "WORKER_MARKETPLACE_AUTH_ENCRYPTION_KEY",
                "ddcrm-local-worker-marketplace-auth-key",
            ).strip(),
            bound_account_id=bound_account_id,
            playerok_token=(os.getenv("PLAYEROK_TOKEN") or "").strip() or None,
            playerok_ddg5=(os.getenv("PLAYEROK_DDG5") or "").strip() or None,
            playerok_cookies=(os.getenv("PLAYEROK_COOKIES") or "").strip() or None,
            playerok_user_agent=(os.getenv("PLAYEROK_USER_AGENT") or "").strip() or None,
            playerok_requests_timeout=timeout_value,
        )
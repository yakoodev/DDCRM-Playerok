from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient


def _load_worker_module(tmp_path: Path, extra_env: dict[str, str] | None = None):
    env_overrides = {
        "WORKER_BIND_HOST": "127.0.0.1",
        "WORKER_BIND_PORT": "18080",
        "WORKER_PROVIDER": "playerok",
        "WORKER_STORAGE_PATH": str(tmp_path / "worker-state.sqlite3"),
        "WORKER_API_SERVICE_AUTH_ENABLED": "true",
        "WORKER_API_SERVICE_AUTH_ACCEPTED_TOKENS": "worker-token-a,worker-token-b",
        "INTERNAL_API_SERVICE_AUTH_ACCEPTED_TOKENS": "internal-token-a,internal-token-b",
        "WORKER_PROXY_CREDENTIALS_ENCRYPTION_KEY": "proxy-key-for-tests-0123456789",
        "WORKER_MARKETPLACE_AUTH_ENCRYPTION_KEY": "market-key-for-tests-0123456789",
    }
    if extra_env:
        env_overrides.update(extra_env)

    os.environ.pop("PLAYEROK_WORKER_ACCOUNT_ID", None)
    os.environ.pop("DDCRM_WORKER_ACCOUNT_ID", None)
    for key, value in env_overrides.items():
        os.environ[key] = value

    for module_name in [
        "ddcrm_playerok_worker.main",
        "ddcrm_playerok_worker.playerok_adapter",
        "ddcrm_playerok_worker.config",
    ]:
        sys.modules.pop(module_name, None)

    module = importlib.import_module("ddcrm_playerok_worker.main")
    return module


def test_service_auth_enforced(tmp_path: Path) -> None:
    module = _load_worker_module(tmp_path)
    with TestClient(module.app) as client:
        response_missing = client.get("/internal/v2/worker/health")
        assert response_missing.status_code == 401

        response_forbidden = client.get(
            "/internal/v2/worker/health",
            headers={"X-Service-Token": "internal-token-a"},
        )
        assert response_forbidden.status_code == 403

        response_ok = client.get(
            "/internal/v2/worker/health",
            headers={"X-Service-Token": "worker-token-a"},
        )
        assert response_ok.status_code == 200
        assert response_ok.json()["status"] == "ok"


def test_read_endpoints_require_account_binding(tmp_path: Path) -> None:
    module = _load_worker_module(tmp_path)
    with TestClient(module.app) as client:
        response = client.get(
            "/internal/v2/worker/account",
            headers={"X-Service-Token": "worker-token-a"},
        )
        assert response.status_code == 409
        assert response.json()["errorCode"] == "WORKER_RUNTIME_CONFLICT"


def test_action_idempotency_replay_for_marketplace_auth_apply(tmp_path: Path) -> None:
    module = _load_worker_module(tmp_path)
    with TestClient(module.app) as client:
        headers = {
            "X-Service-Token": "worker-token-a",
            "Idempotency-Key": "idem-market-0001",
        }
        payload = {
            "payload": {
                "accountId": "acc-1",
                "marketplaceAuth": {
                    "scheme": "tokens",
                    "credentials": {
                        "token": "token-a",
                        "ddg5": "ddg5-a",
                        "user_agent": "ua-1",
                    },
                },
            }
        }
        response_first = client.post(
            "/internal/v2/worker/actions/ext.account.marketplace-auth.apply",
            headers=headers,
            json=payload,
        )
        assert response_first.status_code == 200

        replay_payload = {
            "payload": {
                "accountId": "acc-1",
                "marketplaceAuth": {
                    "scheme": "tokens",
                    "credentials": {
                        "token": "token-b",
                        "ddg5": "ddg5-b",
                    },
                },
            }
        }
        response_replay = client.post(
            "/internal/v2/worker/actions/ext.account.marketplace-auth.apply",
            headers=headers,
            json=replay_payload,
        )
        assert response_replay.status_code == 200
        assert response_replay.json() == response_first.json()

        stored = module.storage.read_marketplace_auth("acc-1")
        assert stored is not None
        assert stored["credentials"]["token"] == "token-a"
        assert stored["credentials"]["ddg5"] == "ddg5-a"


def test_worker_scope_is_locked_to_single_account(tmp_path: Path) -> None:
    module = _load_worker_module(tmp_path)
    with TestClient(module.app) as client:
        first_response = client.post(
            "/internal/v2/worker/actions/ext.account.marketplace-auth.apply",
            headers={
                "X-Service-Token": "worker-token-a",
                "Idempotency-Key": "idem-lock-1",
            },
            json={
                "payload": {
                    "accountId": "acc-1",
                    "marketplaceAuth": {
                        "scheme": "tokens",
                        "credentials": {"token": "token-1", "ddg5": "ddg5-1"},
                    },
                }
            },
        )
        assert first_response.status_code == 200

        conflict_response = client.post(
            "/internal/v2/worker/actions/ext.account.proxy-credentials.apply",
            headers={
                "X-Service-Token": "worker-token-a",
                "Idempotency-Key": "idem-lock-2",
            },
            json={
                "payload": {
                    "accountId": "acc-2",
                    "proxyConfig": {
                        "host": "127.0.0.1",
                        "port": 1080,
                        "login": "u",
                        "password": "p",
                    },
                }
            },
        )
        assert conflict_response.status_code == 409
        assert conflict_response.json()["errorCode"] == "WORKER_RUNTIME_CONFLICT"


def test_env_bound_account_rejects_other_account_payload(tmp_path: Path) -> None:
    module = _load_worker_module(
        tmp_path,
        {"PLAYEROK_WORKER_ACCOUNT_ID": "acc-env"},
    )
    with TestClient(module.app) as client:
        response = client.post(
            "/internal/v2/worker/actions/ext.account.marketplace-auth.apply",
            headers={
                "X-Service-Token": "worker-token-a",
                "Idempotency-Key": "idem-env-1",
            },
            json={
                "payload": {
                    "accountId": "acc-other",
                    "marketplaceAuth": {
                        "scheme": "tokens",
                        "credentials": {"token": "token-1", "ddg5": "ddg5-1"},
                    },
                }
            },
        )
        assert response.status_code == 409
        assert response.json()["errorCode"] == "WORKER_RUNTIME_CONFLICT"


def test_worker_capabilities_include_playerok_metadata_action(tmp_path: Path) -> None:
    module = _load_worker_module(tmp_path)
    with TestClient(module.app) as client:
        response = client.get(
            "/internal/v2/worker/capabilities",
            headers={"X-Service-Token": "worker-token-a"},
        )
        assert response.status_code == 200
        capability_keys = {
            item.get("key")
            for item in response.json().get("capabilities", [])
            if isinstance(item, dict)
        }
        assert "ext.playerok.products.metadata" in capability_keys


def test_playerok_metadata_action_uses_bound_account_scope(tmp_path: Path) -> None:
    module = _load_worker_module(tmp_path)
    captured: dict[str, str | None] = {}

    def _fake_metadata(
        account_id: str,
        game_category_id: str | None = None,
        obtaining_type_id: str | None = None,
    ) -> dict[str, object]:
        captured["accountId"] = account_id
        captured["gameCategoryId"] = game_category_id
        captured["obtainingTypeId"] = obtaining_type_id
        return {
            "provider": "playerok",
            "games": [],
            "categories": [],
            "options": [],
            "obtainingTypes": [],
            "dataFields": [],
            "query": {
                "gameCategoryId": game_category_id,
                "obtainingTypeId": obtaining_type_id,
            },
        }

    module.adapter.get_product_metadata = _fake_metadata

    with TestClient(module.app) as client:
        bind_response = client.post(
            "/internal/v2/worker/actions/ext.account.marketplace-auth.apply",
            headers={
                "X-Service-Token": "worker-token-a",
                "Idempotency-Key": "idem-meta-bind-1",
            },
            json={
                "payload": {
                    "accountId": "acc-meta-1",
                    "marketplaceAuth": {
                        "scheme": "tokens",
                        "credentials": {"token": "token-1", "ddg5": "ddg5-1"},
                    },
                }
            },
        )
        assert bind_response.status_code == 200

        metadata_response = client.post(
            "/internal/v2/worker/actions/ext.playerok.products.metadata",
            headers={
                "X-Service-Token": "worker-token-a",
                "Idempotency-Key": "idem-meta-call-1",
            },
            json={
                "payload": {
                    "gameCategoryId": "cat-101",
                    "obtainingTypeId": "obt-201",
                }
            },
        )
        assert metadata_response.status_code == 200
        body = metadata_response.json()
        assert body["result"]["provider"] == "playerok"
        assert body["result"]["query"]["gameCategoryId"] == "cat-101"
        assert body["result"]["query"]["obtainingTypeId"] == "obt-201"

    assert captured == {
        "accountId": "acc-meta-1",
        "gameCategoryId": "cat-101",
        "obtainingTypeId": "obt-201",
    }

from __future__ import annotations

import pytest

from ddcrm_playerok_worker.models import validate_marketplace_auth


def test_validate_marketplace_auth_accepts_tokens() -> None:
    validate_marketplace_auth("tokens", {"token": "secret-token", "ddg5": "cookie-value"})


def test_validate_marketplace_auth_rejects_unknown_scheme() -> None:
    with pytest.raises(ValueError):
        validate_marketplace_auth("unknown_scheme", {"a": "b"})


def test_validate_marketplace_auth_requires_token_and_ddg5() -> None:
    with pytest.raises(ValueError):
        validate_marketplace_auth("tokens", {"token": "secret-token"})


def test_validate_marketplace_auth_requires_cookies_value_for_cookies_scheme() -> None:
    with pytest.raises(ValueError):
        validate_marketplace_auth("cookies", {"token": "x"})

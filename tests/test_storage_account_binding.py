from __future__ import annotations

from pathlib import Path

from ddcrm_playerok_worker.storage import WorkerStateStorage


def _create_storage(tmp_path: Path) -> WorkerStateStorage:
    return WorkerStateStorage(
        db_path=str(tmp_path / "worker-state.sqlite3"),
        proxy_key_source="proxy-key-for-tests-0123456789",
        marketplace_key_source="market-key-for-tests-0123456789",
    )


def test_bind_account_if_unset_sets_first_account_id(tmp_path: Path) -> None:
    storage = _create_storage(tmp_path)

    assert storage.read_bound_account_id() is None

    bound = storage.bind_account_if_unset("acc-1")
    assert bound == "acc-1"
    assert storage.read_bound_account_id() == "acc-1"


def test_bind_account_if_unset_does_not_replace_existing_account_id(tmp_path: Path) -> None:
    storage = _create_storage(tmp_path)
    storage.bind_account_if_unset("acc-1")

    rebound = storage.bind_account_if_unset("acc-2")
    assert rebound == "acc-1"
    assert storage.read_bound_account_id() == "acc-1"

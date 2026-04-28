from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True)
class IdempotencyEntry:
    status_code: int
    payload: dict[str, Any]


class WorkerStateStorage:
    def __init__(self, db_path: str, proxy_key_source: str, marketplace_key_source: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._proxy_key = hashlib.sha256(proxy_key_source.encode("utf-8")).digest()
        self._marketplace_key = hashlib.sha256(marketplace_key_source.encode("utf-8")).digest()
        self._ensure_parent_dir()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_parent_dir(self) -> None:
        parent = os.path.dirname(os.path.abspath(self._db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_records (
                    scope TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    PRIMARY KEY (scope, idempotency_key)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS proxy_credentials (
                    account_id TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    login TEXT NOT NULL,
                    password_encrypted TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS marketplace_auth (
                    account_id TEXT PRIMARY KEY,
                    scheme TEXT NOT NULL,
                    credentials_encrypted TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_runtime_binding (
                    binding_key TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def read_idempotency(self, scope: str, idempotency_key: str) -> IdempotencyEntry | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT status_code, payload_json
                FROM idempotency_records
                WHERE scope = ? AND idempotency_key = ?
                """,
                (scope, idempotency_key),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload_json"])
            return IdempotencyEntry(status_code=int(row["status_code"]), payload=payload)

    def write_idempotency(self, scope: str, idempotency_key: str, status_code: int, payload: dict[str, Any]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO idempotency_records(scope, idempotency_key, status_code, payload_json, created_at_utc)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    idempotency_key,
                    status_code,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()

    def read_bound_account_id(self) -> str | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT account_id
                FROM worker_runtime_binding
                WHERE binding_key = ?
                """,
                ("runtime",),
            ).fetchone()
            if row is None:
                return None
            account_id = str(row["account_id"]).strip()
            return account_id or None

    def bind_account_if_unset(self, account_id: str) -> str:
        normalized = account_id.strip()
        if not normalized:
            raise ValueError("account_id не может быть пустым.")

        with self._lock, self._connect() as connection:
            existing_row = connection.execute(
                """
                SELECT account_id
                FROM worker_runtime_binding
                WHERE binding_key = ?
                """,
                ("runtime",),
            ).fetchone()
            if existing_row is not None:
                return str(existing_row["account_id"]).strip()

            connection.execute(
                """
                INSERT INTO worker_runtime_binding(binding_key, account_id, updated_at_utc)
                VALUES (?, ?, ?)
                """,
                ("runtime", normalized, datetime.now(UTC).isoformat()),
            )
            connection.commit()
            return normalized

    def upsert_proxy_credentials(self, account_id: str, host: str, port: int, login: str, password: str) -> None:
        encrypted_password = self._encrypt(password, self._proxy_key)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO proxy_credentials(account_id, host, port, login, password_encrypted, updated_at_utc)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (account_id, host, port, login, encrypted_password, datetime.now(UTC).isoformat()),
            )
            connection.commit()

    def read_proxy_credentials(self, account_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT host, port, login, password_encrypted
                FROM proxy_credentials
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "host": row["host"],
                "port": int(row["port"]),
                "login": row["login"],
                "password": self._decrypt(row["password_encrypted"], self._proxy_key),
            }

    def upsert_marketplace_auth(self, account_id: str, scheme: str, credentials: dict[str, str]) -> None:
        credentials_json = json.dumps(credentials, ensure_ascii=False, separators=(",", ":"))
        encrypted_credentials = self._encrypt(credentials_json, self._marketplace_key)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO marketplace_auth(account_id, scheme, credentials_encrypted, updated_at_utc)
                VALUES (?, ?, ?, ?)
                """,
                (account_id, scheme, encrypted_credentials, datetime.now(UTC).isoformat()),
            )
            connection.commit()

    def read_marketplace_auth(self, account_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT scheme, credentials_encrypted
                FROM marketplace_auth
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
            if row is None:
                return None

            credentials_raw = self._decrypt(row["credentials_encrypted"], self._marketplace_key)
            credentials = json.loads(credentials_raw)
            if not isinstance(credentials, dict):
                return None
            string_credentials = {
                str(key): str(value)
                for key, value in credentials.items()
                if isinstance(value, str) and value.strip()
            }
            if not string_credentials:
                return None
            return {
                "scheme": row["scheme"],
                "credentials": string_credentials,
            }

    def _encrypt(self, plaintext: str, key: bytes) -> str:
        nonce = os.urandom(12)
        aes = AESGCM(key)
        encrypted = aes.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
        payload = nonce + encrypted
        return base64.b64encode(payload).decode("ascii")

    def _decrypt(self, ciphertext: str, key: bytes) -> str:
        raw = base64.b64decode(ciphertext.encode("ascii"))
        nonce = raw[:12]
        encrypted = raw[12:]
        aes = AESGCM(key)
        plaintext = aes.decrypt(nonce, encrypted, associated_data=None)
        return plaintext.decode("utf-8")

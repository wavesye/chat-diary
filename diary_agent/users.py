"""Persistent channel identities and per-user business service ownership."""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .config import Settings
from .service import DiaryService


def _channel_name(channel: str) -> str:
    if not isinstance(channel, str):
        raise ValueError("channel must be a non-empty channel name")
    value = channel.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", value):
        raise ValueError("channel must contain letters, numbers, '_' or '-'")
    return value


def _external_id(external_user_id: str) -> str:
    if not isinstance(external_user_id, (str, int)) or isinstance(external_user_id, bool):
        raise ValueError("external_user_id must be a non-empty string")
    value = str(external_user_id).strip()
    if not value:
        raise ValueError("external_user_id must be a non-empty string")
    return value


def _valid_user_id(user_id: str) -> bool:
    return isinstance(user_id, str) and re.fullmatch(r"[0-9a-f]{32}", user_id) is not None


class UserRegistry:
    """Map platform identities to opaque internal UUIDs, never to data paths.

    The local owner retains the pre-channel single-user database and vault.
    Bindings must be explicitly removed before assigning an identity elsewhere.
    """

    def __init__(self, path: Path):
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._closed = False
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY CHECK(length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'),
                name TEXT NOT NULL DEFAULT '',
                is_local INTEGER NOT NULL DEFAULT 0 CHECK(is_local IN (0, 1))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_local_owner
                ON users(is_local) WHERE is_local = 1;
            CREATE TABLE IF NOT EXISTS bindings (
                channel TEXT NOT NULL,
                external_user_id TEXT NOT NULL,
                user_id TEXT NOT NULL REFERENCES users(id),
                PRIMARY KEY(channel, external_user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_bindings_user ON bindings(user_id);
            CREATE TABLE IF NOT EXISTS channel_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

    def local_user_id(self) -> str:
        """Return the one persistent owner of the original single-user data."""
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO users(id, name, is_local) VALUES (?, '', 1)",
                (uuid.uuid4().hex,),
            )
            row = self.connection.execute(
                "SELECT id FROM users WHERE is_local=1"
            ).fetchone()
        return str(row["id"])

    def create_user(self, name: str = "") -> str:
        if not isinstance(name, str):
            raise ValueError("name must be a string")
        user_id = uuid.uuid4().hex
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO users(id, name) VALUES (?, ?)", (user_id, name.strip())
            )
        return user_id

    def get_user(self, user_id: str) -> dict | None:
        if not _valid_user_id(user_id):
            return None
        with self._lock:
            row = self.connection.execute(
                "SELECT id, name, is_local FROM users WHERE id=?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "name": row["name"], "is_local": bool(row["is_local"])}

    def list_users(self) -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT id, name, is_local FROM users ORDER BY is_local DESC, name, id"
            ).fetchall()
        return [
            {"id": row["id"], "name": row["name"], "is_local": bool(row["is_local"])}
            for row in rows
        ]

    def bind(self, channel: str, external_user_id: str, user_id: str) -> None:
        channel, external_user_id = _channel_name(channel), _external_id(external_user_id)
        with self._lock, self.connection:
            if self.get_user(user_id) is None:
                raise ValueError("Unknown internal user_id; create a user before binding")
            # INSERT OR IGNORE + read works for competing processes as well as threads.
            self.connection.execute(
                "INSERT OR IGNORE INTO bindings(channel, external_user_id, user_id) "
                "VALUES (?, ?, ?)", (channel, external_user_id, user_id),
            )
            existing = self.connection.execute(
                "SELECT user_id FROM bindings WHERE channel=? AND external_user_id=?",
                (channel, external_user_id),
            ).fetchone()
            if existing["user_id"] != user_id:
                raise ValueError("Channel identity already bound to another user; unbind it first")

    def resolve(self, channel: str, external_user_id: str) -> str | None:
        channel, external_user_id = _channel_name(channel), _external_id(external_user_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT user_id FROM bindings WHERE channel=? AND external_user_id=?",
                (channel, external_user_id),
            ).fetchone()
        return str(row["user_id"]) if row else None

    def bindings(self, channel: str | None = None) -> list[dict]:
        query = "SELECT channel, external_user_id, user_id FROM bindings"
        parameters = ()
        if channel is not None:
            query += " WHERE channel=?"
            parameters = (_channel_name(channel),)
        query += " ORDER BY channel, external_user_id"
        with self._lock:
            rows = self.connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def unbind(self, channel: str, external_user_id: str) -> bool:
        channel, external_user_id = _channel_name(channel), _external_id(external_user_id)
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "DELETE FROM bindings WHERE channel=? AND external_user_id=?",
                (channel, external_user_id),
            )
        return bool(cursor.rowcount)

    def get_state(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.connection.execute(
                "SELECT value FROM channel_state WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO channel_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)),
            )

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self.connection.close()
                self._closed = True


class ServicePool:
    """Reuse business services by internal user ID with isolated persistence."""

    def __init__(
        self, settings: Settings, registry: UserRegistry,
        factory: Callable[[Settings], DiaryService] = DiaryService,
    ):
        self.settings = settings
        self.registry = registry
        self.factory = factory
        self._services: dict[str, DiaryService] = {}
        self._lock = threading.RLock()
        self._closed = False

    def get(self, user_id: str) -> DiaryService:
        with self._lock:
            if self._closed:
                raise RuntimeError("ServicePool is closed")
            user = self.registry.get_user(user_id)
            if user is None:
                raise ValueError("Unknown internal user_id")
            if user_id not in self._services:
                settings = self.settings
                if not user["is_local"]:
                    settings = replace(
                        settings,
                        database_path=settings.database_path.parent / "users" / user_id / "diary.sqlite",
                        vault_path=settings.vault_path / "Users" / user_id,
                        history_path=None,
                        user_name=user["name"] or settings.user_name,
                    )
                    settings.vault_path.mkdir(parents=True, exist_ok=True)
                    # The legacy owner's history root can contain this subtree.
                    # History importers use this marker to exclude private user vaults.
                    (settings.vault_path / ".chat-diary-user").write_text(
                        user_id + "\n", encoding="utf-8"
                    )
                self._services[user_id] = self.factory(settings)
            return self._services[user_id]

    __call__ = get

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            for service in self._services.values():
                store = getattr(service, "store", None)
                connection = getattr(store, "connection", None)
                if connection is not None:
                    with getattr(store, "_lock", self._lock):
                        connection.close()
            self._services.clear()
            self._closed = True

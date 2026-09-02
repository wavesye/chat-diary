from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class DiaryStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_messages_day ON messages(day, id);
            CREATE TABLE IF NOT EXISTS entries (
                day TEXT PRIMARY KEY,
                markdown_path TEXT NOT NULL,
                finalized_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    def add_message(self, day: str, role: str, content: str) -> None:
        with self._lock:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO messages(day, role, content) VALUES (?, ?, ?)",
                    (day, role, content),
                )

    def messages(self, day: str) -> list[dict[str, str]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT role, content FROM messages WHERE day = ? ORDER BY id", (day,)
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def user_message_count(self, day: str) -> int:
        with self._lock:
            row = self.connection.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE day = ? AND role = 'user'", (day,)
            ).fetchone()
        return int(row["n"])

    def mark_finalized(self, day: str, path: Path) -> None:
        with self._lock:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO entries(day, markdown_path) VALUES (?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET markdown_path=excluded.markdown_path, "
                    "finalized_at=CURRENT_TIMESTAMP",
                    (day, str(path)),
                )

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
        self.connection.execute("PRAGMA foreign_keys=ON")
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
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('pending_confirmation', 'active', 'completed', 'cancelled')),
                planned_date TEXT,
                due_at TEXT,
                priority INTEGER NOT NULL DEFAULT 3 CHECK(priority BETWEEN 1 AND 5),
                source_message_id INTEGER,
                creation_method TEXT NOT NULL DEFAULT 'manual',
                confirmed INTEGER NOT NULL DEFAULT 1 CHECK(confirmed IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT,
                cancelled_at TEXT,
                FOREIGN KEY(source_message_id) REFERENCES messages(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_todos_status_date
                ON todos(status, planned_date, due_at);
            CREATE TABLE IF NOT EXISTS todo_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                todo_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(todo_id) REFERENCES todos(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL
                    CHECK(source_type IN ('todo', 'chat', 'obsidian')),
                todo_id INTEGER,
                source_message_id INTEGER,
                source_file TEXT,
                tags TEXT NOT NULL DEFAULT '[]',
                confidence REAL NOT NULL DEFAULT 1.0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(todo_id) REFERENCES todos(id) ON DELETE SET NULL,
                FOREIGN KEY(source_message_id) REFERENCES messages(id) ON DELETE SET NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_activity_todo_unique
                ON activities(todo_id) WHERE todo_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_activities_day ON activities(day, id);
            CREATE TABLE IF NOT EXISTS reminder_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reminder_day TEXT NOT NULL,
                reminder_type TEXT NOT NULL CHECK(reminder_type IN ('morning', 'evening')),
                recipient_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(reminder_day, reminder_type, recipient_id)
            );
            """
        )
        self._ensure_column("entries", "title", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("entries", "tags", "TEXT NOT NULL DEFAULT '[]'")
        self.connection.execute("PRAGMA optimize")

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        with self._lock:
            columns = {
                row["name"] for row in self.connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            if column not in columns:
                with self.connection:
                    self.connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    )

    def add_message(self, day: str, role: str, content: str) -> int:
        with self._lock:
            with self.connection:
                cursor = self.connection.execute(
                    "INSERT INTO messages(day, role, content) VALUES (?, ?, ?)",
                    (day, role, content),
                )
        return int(cursor.lastrowid)

    def add_messages(self, day: str, role: str, contents: list[str]) -> list[int]:
        """Insert one message burst atomically while preserving message boundaries."""
        ids = []
        with self._lock, self.connection:
            for content in contents:
                cursor = self.connection.execute(
                    "INSERT INTO messages(day, role, content) VALUES (?, ?, ?)",
                    (day, role, content),
                )
                ids.append(int(cursor.lastrowid))
        return ids

    def messages(self, day: str) -> list[dict[str, str]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT role, content FROM messages WHERE day = ? ORDER BY id", (day,)
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def message_records(self, day: str) -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT id, role, content, created_at FROM messages "
                "WHERE day = ? ORDER BY id", (day,)
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_message(self, day: str, message_id: int) -> bool:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "DELETE FROM messages WHERE day = ? AND id = ?", (day, message_id)
            )
        return bool(cursor.rowcount)

    def user_message_count(self, day: str) -> int:
        with self._lock:
            row = self.connection.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE day = ? AND role = 'user'", (day,)
            ).fetchone()
        return int(row["n"])

    def mark_finalized(
        self, day: str, path: Path, *, title: str = "", tags: str = "[]"
    ) -> None:
        with self._lock:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO entries(day, markdown_path, title, tags) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET markdown_path=excluded.markdown_path, "
                    "title=excluded.title, tags=excluded.tags, finalized_at=CURRENT_TIMESTAMP",
                    (day, str(path), title, tags),
                )

    def get_state(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.connection.execute(
                "SELECT value FROM app_state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO app_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def add_activity(
        self, day: str, title: str, *, description: str = "",
        source_type: str, todo_id: int | None = None,
        source_message_id: int | None = None, source_file: str | None = None,
        tags: str = "[]", confidence: float = 1.0,
    ) -> int:
        with self._lock, self.connection:
            if todo_id is not None:
                existing = self.connection.execute(
                    "SELECT id FROM activities WHERE todo_id = ?", (todo_id,)
                ).fetchone()
                if existing:
                    return int(existing["id"])
            if source_file is not None:
                existing = self.connection.execute(
                    "SELECT id FROM activities WHERE day=? AND title=? AND source_type=? "
                    "AND source_file=?", (day, title.strip(), source_type, source_file),
                ).fetchone()
                if existing:
                    return int(existing["id"])
            cursor = self.connection.execute(
                "INSERT INTO activities(day, title, description, source_type, todo_id, "
                "source_message_id, source_file, tags, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (day, title.strip(), description.strip(), source_type, todo_id,
                 source_message_id, source_file, tags, confidence),
            )
        return int(cursor.lastrowid)

    def add_activity(
        self, day: str, title: str, *, description: str = "",
        source_type: str, todo_id: int | None = None,
        source_message_id: int | None = None, source_file: str | None = None,
        tags: str = "[]", confidence: float = 1.0,
    ) -> int:
        with self._lock, self.connection:
            if todo_id is not None:
                existing = self.connection.execute(
                    "SELECT id FROM activities WHERE todo_id = ?", (todo_id,)
                ).fetchone()
                if existing:
                    return int(existing["id"])
            cursor = self.connection.execute(
                "INSERT INTO activities(day, title, description, source_type, todo_id, "
                "source_message_id, source_file, tags, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (day, title, description, source_type, todo_id, source_message_id,
                 source_file, tags, max(0.0, min(float(confidence), 1.0))),
            )
        return int(cursor.lastrowid)

    def activities(self, day: str) -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM activities WHERE day = ? ORDER BY id", (day,)
            ).fetchall()
        return [dict(row) for row in rows]

    def reminder_sent(self, day: str, kind: str, recipient_id: int) -> bool:
        with self._lock:
            row = self.connection.execute(
                "SELECT 1 FROM reminder_log WHERE reminder_day=? AND reminder_type=? "
                "AND recipient_id=?", (day, kind, recipient_id),
            ).fetchone()
        return row is not None

    def mark_reminder_sent(self, day: str, kind: str, recipient_id: int) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO reminder_log(reminder_day, reminder_type, recipient_id) "
                "VALUES (?, ?, ?)", (day, kind, recipient_id),
            )

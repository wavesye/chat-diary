from __future__ import annotations

from datetime import datetime


class ReminderService:
    """Decides when reminders are due; the adapter owns actual delivery."""

    def __init__(self, store, timezone, morning_time: str, evening_time: str,
                 enabled: bool = True):
        self.store = store
        self.timezone = timezone
        self.morning_time = morning_time
        self.evening_time = evening_time
        self.enabled = enabled

    def due(self, recipient_id: int, now: datetime | None = None) -> str | None:
        if not self.enabled:
            return None
        now = now or datetime.now(self.timezone)
        if now.tzinfo is None:
            now = now.replace(tzinfo=self.timezone)
        day = now.astimezone(self.timezone).date().isoformat()
        clock = now.astimezone(self.timezone).strftime("%H:%M")
        reminder_type = None
        if clock >= self.evening_time:
            reminder_type = "evening"
        elif clock >= self.morning_time:
            reminder_type = "morning"
        if not reminder_type:
            return None
        with self.store._lock:
            sent = self.store.connection.execute(
                "SELECT 1 FROM reminder_log WHERE reminder_day=? AND reminder_type=? "
                "AND recipient_id=?", (day, reminder_type, recipient_id),
            ).fetchone()
        return None if sent else reminder_type

    def mark_sent(self, recipient_id: int, reminder_type: str,
                  now: datetime | None = None) -> bool:
        now = now or datetime.now(self.timezone)
        if now.tzinfo is None:
            now = now.replace(tzinfo=self.timezone)
        day = now.astimezone(self.timezone).date().isoformat()
        with self.store._lock, self.store.connection:
            cursor = self.store.connection.execute(
                "INSERT OR IGNORE INTO reminder_log(reminder_day, reminder_type, recipient_id, sent_at) "
                "VALUES (?, ?, ?, ?)",
                (day, reminder_type, recipient_id, now.isoformat()),
            )
        return bool(cursor.rowcount)

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


TODO_ACTIONS = {"create", "complete", "postpone", "update", "cancel", "activity", "none"}


@dataclass(frozen=True)
class TaskIntent:
    action: str
    title: str = ""
    description: str = ""
    planned_date: str | None = None
    due_at: str | None = None
    priority: int = 3
    confidence: float = 0.0
    needs_confirmation: bool = False
    source_message_id: int | None = None


def parse_relative_date(text: str, today: date) -> str | None:
    """Parse a deliberately small, deterministic set of Chinese/ISO date forms."""
    lowered = text.strip().lower()
    relative = (
        ("day after tomorrow", 2), ("tomorrow", 1), ("today", 0),
        ("大后天", 3), ("后天", 2), ("明天", 1), ("今天", 0),
    )
    for token, days in relative:
        if token in lowered:
            return (today + timedelta(days=days)).isoformat()
    match = re.search(r"\b(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?\b", lowered)
    if match:
        try:
            return date(*(int(value) for value in match.groups())).isoformat()
        except ValueError:
            return None
    match = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日?", lowered)
    if match:
        try:
            candidate = date(today.year, int(match.group(1)), int(match.group(2)))
            if candidate < today - timedelta(days=1):
                candidate = candidate.replace(year=today.year + 1)
            return candidate.isoformat()
        except ValueError:
            return None
    weekdays = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    match = re.search(r"(?:下周|周|星期)([一二三四五六日天])", lowered)
    if match:
        target = weekdays[match.group(1)]
        delta = (target - today.weekday()) % 7
        if "下周" in match.group(0):
            delta = 7 - today.weekday() + target
        elif delta == 0:
            delta = 7
        return (today + timedelta(days=delta)).isoformat()
    english_weekdays = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    match = re.search(r"\b(next\s+)?(" + "|".join(english_weekdays) + r")\b", lowered)
    if match:
        target = english_weekdays[match.group(2)]
        delta = (target - today.weekday()) % 7
        if match.group(1):
            delta = delta + 7 if delta else 7
        elif delta == 0:
            delta = 7
        return (today + timedelta(days=delta)).isoformat()
    return None


def parse_due_at(text: str, today: date, timezone: ZoneInfo) -> str | None:
    marker = re.search(r"截止(?:到)?|最晚(?:在)?|deadline|due", text, re.I)
    if not marker:
        return None
    due_text = text[marker.end():]
    due_day = parse_relative_date(due_text, today)
    if not due_day:
        return None
    clock = re.search(
        r"(?:(上午|中午|下午|晚上|早上)\s*(\d{1,2})(?::(\d{1,2})|点(半|\d{1,2}分)?)?"
        r"|(?<![-\d])(\d{1,2}):(\d{2}))",
        due_text,
    )
    if not clock:
        return due_day
    hour = int(clock.group(2) or clock.group(5))
    minute = int(clock.group(3) or clock.group(6) or
                 ("30" if clock.group(4) == "半" else
                  (clock.group(4) or "0").rstrip("分")))
    if clock.group(1) in {"下午", "晚上"} and hour < 12:
        hour += 12
    if clock.group(1) == "中午" and hour < 11:
        hour += 12
    try:
        value = datetime.combine(date.fromisoformat(due_day), datetime.min.time(), timezone)
        return value.replace(hour=hour, minute=minute).isoformat()
    except ValueError:
        return due_day


def clean_task_title(text: str) -> str:
    value = text.strip(" ，,。.!！?？：:")
    value = re.sub(r"^(?:请|帮我|记得|提醒我|我要|我需要|我得|计划|安排|准备)\s*", "", value)
    value = re.sub(r"(?:今天|明天|后天|大后天|下周[一二三四五六日天]?)", "", value)
    value = re.sub(r"\b20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?\b|\d{1,2}月\d{1,2}日?", "", value)
    value = re.sub(
        r"(?:(?:上午|中午|下午|晚上|早上)\s*\d{1,2}(?::\d{1,2}|点(?:半|\d{1,2}分)?)?"
        r"|(?<![-\d])\d{1,2}(?::\d{1,2}|点(?:半|\d{1,2}分)?))", "", value,
    )
    value = re.sub(r"^(?:把|将)\s*", "", value)
    value = re.sub(r"截止(?:到)?|最晚(?:在)?|deadline|due", "", value, flags=re.I)
    return value.strip(" ，,。.!！?？：:")[:200]


class TodoService:
    STATUSES = {"pending_confirmation", "active", "completed", "cancelled"}

    def __init__(self, store, timezone: ZoneInfo):
        self.store = store
        self.timezone = timezone

    @property
    def now(self) -> datetime:
        return datetime.now(self.timezone)

    @staticmethod
    def _row(row) -> dict | None:
        return dict(row) if row else None

    def create(
        self, title: str, *, description: str = "", planned_date: str | None = None,
        due_at: str | None = None, priority: int = 3,
        source_message_id: int | None = None, creation_method: str = "manual",
        confirmed: bool = True,
    ) -> dict:
        title = title.strip()
        if not title:
            raise ValueError("Todo 标题不能为空")
        if planned_date:
            date.fromisoformat(planned_date)
        if due_at:
            if len(due_at) == 10:
                date.fromisoformat(due_at)
            else:
                parsed_due = datetime.fromisoformat(due_at)
                if parsed_due.tzinfo is None:
                    due_at = parsed_due.replace(tzinfo=self.timezone).isoformat()
        priority = max(1, min(int(priority), 5))
        status = "active" if confirmed else "pending_confirmation"
        with self.store._lock, self.store.connection:
            cursor = self.store.connection.execute(
                "INSERT INTO todos(title, description, status, planned_date, due_at, "
                "priority, source_message_id, creation_method, confirmed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, description.strip(), status, planned_date, due_at, priority,
                 source_message_id, creation_method, int(confirmed)),
            )
            todo_id = int(cursor.lastrowid)
            self._event(todo_id, "create", {"confirmed": confirmed})
        return self.get(todo_id)

    def _event(self, todo_id: int, action: str, detail: dict[str, Any] | None = None) -> None:
        self.store.connection.execute(
            "INSERT INTO todo_events(todo_id, action, detail) VALUES (?, ?, ?)",
            (todo_id, action, json.dumps(detail or {}, ensure_ascii=False)),
        )

    def get(self, todo_id: int) -> dict | None:
        with self.store._lock:
            row = self.store.connection.execute(
                "SELECT * FROM todos WHERE id = ?", (todo_id,)
            ).fetchone()
        return self._row(row)

    def list(
        self, *, statuses: tuple[str, ...] = ("active",),
        start: str | None = None, end: str | None = None,
        include_unscheduled: bool = False,
    ) -> list[dict]:
        statuses = tuple(status for status in statuses if status in self.STATUSES)
        if not statuses:
            return []
        clauses = [f"status IN ({','.join('?' for _ in statuses)})"]
        params: list[Any] = list(statuses)
        if start is not None:
            clauses.append("(planned_date >= ? OR (planned_date IS NULL AND due_at >= ?))")
            params.extend([start, start])
        if end is not None:
            date_clause = "(planned_date <= ? OR (planned_date IS NULL AND substr(due_at,1,10) <= ?))"
            if include_unscheduled:
                date_clause = f"({date_clause} OR (planned_date IS NULL AND due_at IS NULL))"
            clauses.append(date_clause)
            params.extend([end, end])
        with self.store._lock:
            rows = self.store.connection.execute(
                "SELECT * FROM todos WHERE " + " AND ".join(clauses) +
                " ORDER BY CASE WHEN planned_date IS NULL THEN 1 ELSE 0 END, planned_date, "
                "CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at, priority DESC, id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def today(self, day: str | None = None) -> list[dict]:
        day = day or self.now.date().isoformat()
        with self.store._lock:
            rows = self.store.connection.execute(
                "SELECT * FROM todos WHERE status='active' AND (planned_date=? OR "
                "(planned_date IS NULL AND due_at IS NOT NULL AND substr(due_at,1,10)<=?) OR "
                "(planned_date<?)) ORDER BY priority DESC, due_at, id", (day, day, day),
            ).fetchall()
        return [dict(row) for row in rows]

    def week(self, day: str | None = None) -> list[dict]:
        start = date.fromisoformat(day) if day else self.now.date()
        return self.list(start=start.isoformat(), end=(start + timedelta(days=6)).isoformat())

    def confirm(self, todo_id: int) -> dict:
        todo = self._require(todo_id, "pending_confirmation")
        with self.store._lock, self.store.connection:
            self.store.connection.execute(
                "UPDATE todos SET status='active', confirmed=1, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=?", (todo_id,),
            )
            self._event(todo_id, "confirm")
        return self.get(todo_id)

    def complete(
        self, todo_id: int, *, completed_at: datetime | None = None,
        source_message_id: int | None = None,
    ) -> dict:
        todo = self._require(todo_id, "active")
        when = completed_at or self.now
        with self.store._lock, self.store.connection:
            self.store.connection.execute(
                "UPDATE todos SET status='completed', completed_at=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?", (when.isoformat(), todo_id),
            )
            self._event(todo_id, "complete")
            self.store.add_activity(
                when.date().isoformat(), todo["title"], description=todo["description"],
                source_type="todo", todo_id=todo_id,
                source_message_id=source_message_id or todo["source_message_id"], confidence=1.0,
            )
        return self.get(todo_id)

    def postpone(self, todo_id: int, planned_date: str) -> dict:
        self._require(todo_id, "active")
        date.fromisoformat(planned_date)
        with self.store._lock, self.store.connection:
            self.store.connection.execute(
                "UPDATE todos SET planned_date=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (planned_date, todo_id),
            )
            self._event(todo_id, "postpone", {"planned_date": planned_date})
        return self.get(todo_id)

    def update(self, todo_id: int, **changes) -> dict:
        self._require(todo_id)
        allowed = {"title", "description", "planned_date", "due_at", "priority"}
        updates = {key: value for key, value in changes.items() if key in allowed}
        if not updates:
            raise ValueError("没有可修改的 Todo 字段")
        if "title" in updates and not str(updates["title"]).strip():
            raise ValueError("Todo 标题不能为空")
        if updates.get("planned_date"):
            date.fromisoformat(updates["planned_date"])
        if updates.get("due_at"):
            if len(updates["due_at"]) == 10:
                date.fromisoformat(updates["due_at"])
            else:
                datetime.fromisoformat(updates["due_at"])
        if "priority" in updates:
            updates["priority"] = max(1, min(int(updates["priority"]), 5))
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self.store._lock, self.store.connection:
            self.store.connection.execute(
                f"UPDATE todos SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                [*updates.values(), todo_id],
            )
            self._event(todo_id, "update", updates)
        return self.get(todo_id)

    def cancel(self, todo_id: int) -> dict:
        self._require(todo_id, ("active", "pending_confirmation"))
        with self.store._lock, self.store.connection:
            self.store.connection.execute(
                "UPDATE todos SET status='cancelled', cancelled_at=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (self.now.isoformat(), todo_id),
            )
            self._event(todo_id, "cancel")
        return self.get(todo_id)

    def find_match(self, title: str) -> dict | None:
        needle = re.sub(r"\s+", "", title).casefold()
        if not needle:
            return None
        candidates = self.list(statuses=("active", "pending_confirmation"))
        scored = []
        for todo in candidates:
            haystack = re.sub(r"\s+", "", todo["title"]).casefold()
            overlap = len(set(needle) & set(haystack)) / max(1, len(set(needle) | set(haystack)))
            score = 1.0 if needle in haystack or haystack in needle else overlap
            scored.append((score, todo))
        return max(scored, key=lambda item: item[0])[1] if scored and max(x[0] for x in scored) >= 0.35 else None

    def _require(
        self, todo_id: int, status: str | tuple[str, ...] | None = None
    ) -> dict:
        todo = self.get(todo_id)
        if not todo:
            raise ValueError(f"没有找到 Todo #{todo_id}")
        allowed = (status,) if isinstance(status, str) else status
        if allowed and todo["status"] not in allowed:
            raise ValueError(f"Todo #{todo_id} 当前状态是 {todo['status']}，不能执行此操作")
        return todo


class TaskExtractor:
    """Prefilter first; model extraction and deterministic fallback stay outside chat."""

    SIGNAL = re.compile(
        r"提醒我|待办|todo|要做|需要|记得|计划|安排|准备|我想|想要|希望|完成了|"
        r"做完了|改完了|搞定了|已完成|延期|推迟|挪到|改到|取消|删除任务|不用做了|截止|最晚",
        re.I,
    )

    def __init__(self, provider, timezone: ZoneInfo):
        self.provider = provider
        self.timezone = timezone

    def looks_relevant(self, text: str) -> bool:
        return bool(self.SIGNAL.search(text))

    def extract(self, text: str, source_message_id: int | None = None) -> TaskIntent | None:
        if not self.looks_relevant(text):
            return None
        now = datetime.now(self.timezone)
        prompt = (
            "你是 Todo 意图提取器，只返回 JSON。action 只能是 create、complete、postpone、"
            "update、cancel、activity、none。区分 planned_date（计划哪天做）和 due_at（明确的"
            "最晚完成时间）；用户没明确说截止时间时 due_at 必须为 null。普通愿望不能擅自"
            "安排日期，模糊愿望 needs_confirmation=true。用户明确要求添加、完成、延期、修改或"
            "取消时 needs_confirmation=false。已经做完但没有对应任务可用 activity。字段："
            "action,title,description,planned_date,due_at,priority,confidence,needs_confirmation。"
            f"当前时区 {self.timezone.key}，当前时间 {now.isoformat()}。原消息：{text}"
        )
        raw = None
        try:
            candidate = self.provider.json([
                {"role": "system", "content": "只提取任务意图，不回复用户，也不写日记。"},
                {"role": "user", "content": prompt},
            ])
            if candidate.get("action") in TODO_ACTIONS:
                raw = candidate
        except Exception:
            raw = None
        fallback = self._rules(text, now.date())
        if fallback and source_message_id is not None:
            fallback = TaskIntent(**{
                **fallback.__dict__, "source_message_id": source_message_id
            })
        if raw is None:
            return fallback
        return self._normalize(raw, text, now.date(), source_message_id) or fallback

    def _normalize(self, raw: dict, source: str, today: date,
                   source_message_id: int | None) -> TaskIntent | None:
        action = str(raw.get("action", "none")).lower()
        if action not in TODO_ACTIONS or action == "none":
            return None
        title = clean_task_title(str(raw.get("title", "")))
        if not title:
            return None
        due_marker = re.search(r"截止(?:到)?|最晚(?:在)?|deadline|due", source, re.I)
        planning_text = source[:due_marker.start()] if due_marker else source
        temporal = parse_relative_date(planning_text, today)
        planned = raw.get("planned_date")
        if planned:
            try:
                planned = date.fromisoformat(str(planned)[:10]).isoformat()
            except ValueError:
                planned = temporal
        if not temporal and not re.search(r"\b20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b|\d{1,2}月\d{1,2}日", source):
            planned = None
        source_due = parse_due_at(source, today, self.timezone)
        due = raw.get("due_at")
        if not due_marker or not source_due:
            due = None
        elif due:
            try:
                due_text = str(due)
                if len(due_text) == 10:
                    due = date.fromisoformat(due_text).isoformat()
                else:
                    parsed = datetime.fromisoformat(due_text)
                    due = (parsed if parsed.tzinfo else parsed.replace(tzinfo=self.timezone)).isoformat()
            except ValueError:
                due = None
        elif due_marker:
            due = source_due
        planning_signal = re.search(
            r"提醒我|计划|安排|准备|打算|要做|开始|挪到|改到|延期|推迟", source
        )
        if not planning_signal and due:
            planned = None
        confidence = max(0.0, min(float(raw.get("confidence", 0.5)), 1.0))
        ambiguous = bool(re.search(r"也许|可能|有空|希望|想(?:要)?(?!提醒|安排)", source))
        return TaskIntent(
            action=action, title=title, description=str(raw.get("description", "")).strip(),
            planned_date=planned or (temporal if planning_signal else None), due_at=due,
            priority=max(1, min(int(raw.get("priority", 3) or 3), 5)),
            confidence=confidence, needs_confirmation=bool(raw.get("needs_confirmation")) or ambiguous,
            source_message_id=source_message_id,
        )

    def _rules(self, text: str, today: date) -> TaskIntent | None:
        due_marker = re.search(r"截止(?:到)?|最晚(?:在)?|deadline|due", text, re.I)
        planned = parse_relative_date(text[:due_marker.start()] if due_marker else text, today)
        due = parse_due_at(text, today, self.timezone)
        ambiguous = bool(re.search(r"也许|可能|有空|希望|想(?:要)?(?!提醒|安排)", text))
        action = "none"
        title = text
        if re.search(r"挪到|改到|延期|推迟", text):
            action = "postpone"
            title = re.split(r"挪到|改到|延期|推迟", text, maxsplit=1)[0]
        elif re.search(r"取消|删除(?:这个|这条)?任务|不用做了", text):
            action = "cancel"
            title = re.sub(r"取消|删除(?:这个|这条)?任务|不用做了", "", text)
        elif re.search(r"完成了|做完了|改完了|搞定了|已完成", text):
            action = "complete"
            title = re.sub(r"已经?|完成了|做完了|改完了|搞定了|已完成", "", text)
        elif re.search(r"提醒我|待办|todo|要做|需要|记得|计划|安排|准备|截止|最晚", text, re.I):
            action = "create"
        elif ambiguous:
            action = "create"
        if action == "none":
            return None
        cleaned = clean_task_title(title)
        if not cleaned:
            return None
        if due and not re.search(
            r"提醒我|计划|安排|准备|打算|要做|开始|挪到|改到|延期|推迟", text
        ):
            planned = None
        return TaskIntent(
            action=action, title=cleaned, planned_date=planned, due_at=due,
            confidence=0.72 if not ambiguous else 0.52,
            needs_confirmation=ambiguous, source_message_id=None,
        )

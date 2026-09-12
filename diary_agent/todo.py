from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


TODO_ACTIONS = {
    "create", "list", "complete", "postpone", "update", "cancel", "activity", "none"
}

TODO_REFERENCE = re.compile(r"todo|to-do|待办(?:事项)?|待辦(?:事項)?|任务|任務", re.I)
EXPLICIT_CREATE = re.compile(
    r"提醒我|(?:加入|加到|添加到|新增到|记到|記到|记录到|記錄到|列入)\s*(?:我的)?\s*"
    r"(?:todo|to-do|待办(?:事项)?|待辦(?:事項)?|任务|任務)|"
    r"\bremind\s+me\b|\b(?:add|put|save|record)\b[\s\S]*\b(?:todo|to-do|task)\b",
    re.I,
)
EXPLICIT_COMPLETION_STATUS = re.compile(
    r"(?:已完成|做完了|改完了|搞定了)\s*[。.!！]?$|"
    r"\b(?:is|was|has\s+been)\s+(?:done|finished|completed)\s*[.!]?$",
    re.I,
)

DUE_MARKER = re.compile(
    r"截止(?:到)?|最晚(?:在)?|\bdeadline\b|\bdue(?:\s+(?:on|by))?\b|"
    r"\bno\s+later\s+than\b|"
    r"\bby\b(?=\s+(?:today|tonight|tomorrow|the\s+day\s+after\s+tomorrow|"
    r"next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"in\s+\d+\s+days?|20\d{2}[-/.]|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)))",
    re.I,
)

ENGLISH_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

ENGLISH_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8,
    "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


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
    """Parse deterministic Chinese and English date forms."""
    lowered = text.strip().lower()
    relative = (
        ("day after tomorrow", 2), ("tomorrow", 1), ("today", 0),
        ("tonight", 0),
        ("大后天", 3), ("后天", 2), ("明天", 1), ("今天", 0),
    )
    for token, days in relative:
        if token in lowered:
            return (today + timedelta(days=days)).isoformat()
    match = re.search(r"\bin\s+(\d{1,3})\s+days?\b", lowered)
    if match:
        return (today + timedelta(days=int(match.group(1)))).isoformat()
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
    match = re.search(
        r"\b(next\s+|this\s+)?(" + "|".join(ENGLISH_WEEKDAYS) + r")\b",
        lowered,
    )
    if match:
        target = ENGLISH_WEEKDAYS[match.group(2)]
        delta = (target - today.weekday()) % 7
        if match.group(1) and match.group(1).strip() == "next":
            delta = delta + 7 if delta else 7
        elif delta == 0:
            delta = 7
        return (today + timedelta(days=delta)).isoformat()
    month_pattern = "|".join(ENGLISH_MONTHS)
    match = re.search(
        rf"\b({month_pattern})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d{{2}}))?\b",
        lowered,
    )
    if not match:
        reversed_match = re.search(
            rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})\.?(?:\s+(20\d{{2}}))?\b",
            lowered,
        )
        if reversed_match:
            month, day_value, year = (
                ENGLISH_MONTHS[reversed_match.group(2)],
                int(reversed_match.group(1)),
                int(reversed_match.group(3) or today.year),
            )
            explicit_year = bool(reversed_match.group(3))
        else:
            return None
    else:
        month, day_value, year = (
            ENGLISH_MONTHS[match.group(1)], int(match.group(2)),
            int(match.group(3) or today.year),
        )
        explicit_year = bool(match.group(3))
    try:
        candidate = date(year, month, day_value)
        if not explicit_year and candidate < today - timedelta(days=1):
            candidate = candidate.replace(year=today.year + 1)
        return candidate.isoformat()
    except ValueError:
        return None


def parse_due_at(text: str, today: date, timezone: ZoneInfo) -> str | None:
    marker = DUE_MARKER.search(text)
    if not marker:
        return None
    due_text = text[marker.end():]
    due_day = parse_relative_date(due_text, today)
    if not due_day:
        return None
    english_clock = re.search(
        r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b",
        due_text, re.I,
    )
    clock = re.search(
        r"(?:(上午|中午|下午|晚上|早上)\s*(\d{1,2})(?::(\d{1,2})|点(半|\d{1,2}分)?)?"
        r"|(?<![-\d])(\d{1,2}):(\d{2}))",
        due_text,
    )
    if not clock and not english_clock:
        return due_day
    if english_clock:
        hour = int(english_clock.group(1))
        minute = int(english_clock.group(2) or 0)
        period = english_clock.group(3).lower().replace(".", "")
        if period == "pm" and hour < 12:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0
    else:
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
    value = re.sub(
        r"^(?:please\s+)?(?:remind\s+me\s+to|add\s+(?:this\s+|a\s+)?(?:to-?do|todo|task)\s*(?:to\s+)?|"
        r"i\s+(?:need|have|want|plan)\s+to|i\s+must|remember\s+to|plan\s+to|schedule\s+|"
        r"i(?:'d|\s+would)\s+like\s+to)\b\s*",
        "", value, flags=re.I,
    )
    value = re.sub(r"(?:今天|明天|后天|大后天|下周[一二三四五六日天]?)", "", value)
    value = re.sub(
        r"\b(?:today|tonight|tomorrow|the\s+day\s+after\s+tomorrow|in\s+\d+\s+days?|"
        r"(?:next|this)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
        "", value, flags=re.I,
    )
    value = re.sub(
        r"^(?:please\s+)?remind\s+me\s+(?:to\s+)?|"
        r"^(?:maybe|perhaps|i\s+might|i\s+hope\s+to)\s+",
        "", value, flags=re.I,
    )
    value = re.sub(
        r"(?:[，,；;：:]\s*)?(?:加入|加到|添加到|新增到|记到|記到|记录到|記錄到|列入)"
        r"\s*(?:我的)?\s*(?:todo|to-do|待办(?:事项)?|待辦(?:事項)?|任务|任務)"
        r"(?:列表|清单|清單)?\s*$",
        "", value, flags=re.I,
    )
    value = re.sub(
        r"\s*(?:,|，)?\s*(?:add|put|save|record)\s+(?:this\s+)?(?:to|in)\s+"
        r"(?:my\s+)?(?:todo|to-do|task)(?:\s+list)?\s*$",
        "", value, flags=re.I,
    )
    value = re.sub(r"\s+(?:someday|when\s+i\s+have\s+time)\s*$", "", value, flags=re.I)
    value = re.sub(r"\b20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?\b|\d{1,2}月\d{1,2}日?", "", value)
    month_pattern = "|".join(ENGLISH_MONTHS)
    value = re.sub(
        rf"\b(?:{month_pattern})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+20\d{{2}})?\b|"
        rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{month_pattern})\.?(?:\s+20\d{{2}})?\b",
        "", value, flags=re.I,
    )
    value = re.sub(
        r"(?:(?:上午|中午|下午|晚上|早上)\s*\d{1,2}(?::\d{1,2}|点(?:半|\d{1,2}分)?)?"
        r"|(?<![-\d])\d{1,2}(?::\d{1,2}|点(?:半|\d{1,2}分)?))", "", value,
    )
    value = re.sub(
        r"\b(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b",
        "", value, flags=re.I,
    )
    value = re.sub(r"^(?:把|将)\s*", "", value)
    value = DUE_MARKER.sub("", value)
    value = re.sub(r"\b(?:on|by|until|for|at)\s*$", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value)
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
        r"提醒我|待办|要做|需要|记得|计划|計劃|安排|准备|準備|我想|想要|希望|完成了|"
        r"做完了|改完了|搞定了|已完成|延期|推迟|推遲|挪到|改到|取消|删除任务|刪除任務|"
        r"不用做了|截止|最晚|"
        r"\b(?:todo|to-do|task|remind\s+me|need\s+to|have\s+to|must|should|"
        r"remember\s+to|plan(?:ning)?\s+to|schedule|going\s+to|want\s+to|"
        r"would\s+like\s+to|finished|completed|done|submitted|wrapped\s+up|"
        r"postpone|reschedule|delay|push\s+back|cancel|"
        r"(?:update|edit|rename|change)\s+(?:the\s+)?(?:todo|task)|"
        r"delete\s+(?:the\s+)?(?:todo|task)|"
        r"remove\s+(?:the\s+)?(?:todo|task)|no\s+longer\s+need|deadline|due)\b",
        re.I,
    )

    DATE_SIGNAL = re.compile(
        r"\b(?:today|tonight|tomorrow|the\s+day\s+after\s+tomorrow|"
        r"in\s+\d+\s+days?|(?:next|this)\s+(?:monday|tuesday|wednesday|thursday|"
        r"friday|saturday|sunday)|20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|"
        r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?)\.?\s+\d{1,2})\b",
        re.I,
    )

    def __init__(self, provider, timezone: ZoneInfo,
                 multilingual_model_fallback: bool = True):
        self.provider = provider
        self.timezone = timezone
        self.multilingual_model_fallback = multilingual_model_fallback

    def looks_relevant(self, text: str) -> bool:
        return bool(self.SIGNAL.search(text) or self.DATE_SIGNAL.search(text))

    @staticmethod
    def is_list_query(text: str) -> bool:
        """Recognize read-only Todo questions before a model can turn them into writes."""
        if not TODO_REFERENCE.search(text):
            return False
        chinese = re.search(
            r"有什么|有哪些|有啥|查看|查一下|看一下|看看|列出|显示|顯示|"
            r"(?:列表|清单|清單)(?:是什么|是什麼|呢|吗|嗎|[？?])?\s*$",
            text, re.I,
        )
        english = re.search(
            r"\b(?:what|which)\b[\s\S]*\b(?:todos?|tasks?)\b|"
            r"\b(?:show|list|view|check)\b[\s\S]*\b(?:my\s+)?(?:todos?|tasks?)\b|"
            r"\b(?:my|current|today(?:'s)?)\s+(?:todo|to-do|task)(?:s|\s+list)?\s*[?？]?$",
            text, re.I,
        )
        return bool(chinese or english)

    @staticmethod
    def is_direct_management(text: str, intent: TaskIntent | None) -> bool:
        """Return true when Telegram can safely route a pure task operation locally."""
        if intent is None:
            return False
        if intent.action == "list":
            return True
        if intent.action == "create":
            return bool(EXPLICIT_CREATE.search(text))
        if intent.action in {"postpone", "cancel"}:
            return True
        if intent.action == "complete":
            return bool(
                TODO_REFERENCE.search(text) or EXPLICIT_COMPLETION_STATUS.search(text)
            )
        if intent.action == "update":
            return bool(TODO_REFERENCE.search(text))
        return False

    def rule_intent(
        self, text: str, today: date | None = None,
        source_message_id: int | None = None,
    ) -> TaskIntent | None:
        intent = self._rules(text.strip(), today or datetime.now(self.timezone).date())
        if intent and source_message_id is not None:
            intent = TaskIntent(**{
                **intent.__dict__, "source_message_id": source_message_id
            })
        return intent

    def _use_multilingual_fallback(self, text: str) -> bool:
        if not self.multilingual_model_fallback:
            return False
        # Chinese task expressions have a fast deterministic gate. If another
        # writing system or Latin text is present, let the model decide instead
        # of trying to enumerate every language in regexes.
        return any(
            char.isalpha() and not ("\u3400" <= char <= "\u9fff")
            for char in text
        )

    def extract(self, text: str, source_message_id: int | None = None) -> TaskIntent | None:
        text = text.strip()
        now = datetime.now(self.timezone)
        fallback = self.rule_intent(text, now.date(), source_message_id)
        # Explicit local task operations are authoritative. A less reliable model
        # must not turn a list question into a write or veto a clear mutation.
        if fallback and (
            fallback.action in {"list", "complete", "postpone", "cancel", "update"}
            or (fallback.action == "create" and EXPLICIT_CREATE.search(text))
        ):
            return fallback
        if not text or (
            not self.looks_relevant(text) and not self._use_multilingual_fallback(text)
        ):
            return None
        prompt = (
            "你是 Todo 意图提取器，只返回 JSON。action 只能是 create、complete、postpone、"
            "update、cancel、activity、list、none。用户询问自己当前有哪些 Todo、要求查看或列出"
            "Todo 时 action=list，绝不能返回 create。区分 planned_date（计划哪天做）和 due_at（明确的"
            "最晚完成时间）；用户没明确说截止时间时 due_at 必须为 null。普通愿望不能擅自"
            "安排日期，模糊愿望 needs_confirmation=true。用户明确要求添加、完成、延期、修改或"
            "取消时 needs_confirmation=false。已经做完但没有对应任务可用 activity。字段："
            "action,title,description,planned_date,due_at,priority,confidence,needs_confirmation、"
            "planned_date_evidence、due_at_evidence。输入可能使用任意语言，title 保留用户原本的"
            "语言；两个 evidence 字段必须逐字引用原消息中的日期或截止表达，没有就返回 null。"
            "只有任务管理意图或明确完成的具体行动才提取；普通经历、感受和知识问题返回 none。"
            f"当前时区 {self.timezone.key}，当前时间 {now.isoformat()}。原消息：{text}"
        )
        raw = None
        try:
            candidate = self.provider.json([
                {"role": "system", "content": "只提取任务意图，不回复用户，也不写日记。"},
                {"role": "user", "content": prompt},
            ])
            candidate_action = (
                str(candidate.get("action")).strip().lower()
                if "action" in candidate else ""
            )
            if candidate_action in TODO_ACTIONS:
                raw = {**candidate, "action": candidate_action}
        except Exception:
            raw = None
        if raw is None:
            return fallback
        if str(raw.get("action", "none")).strip().lower() == "none":
            return None
        return self._normalize(raw, text, now.date(), source_message_id) or fallback

    def _normalize(self, raw: dict, source: str, today: date,
                   source_message_id: int | None) -> TaskIntent | None:
        action = str(raw.get("action", "none")).strip().lower()
        if action not in TODO_ACTIONS or action == "none":
            return None
        if action == "list":
            return TaskIntent(
                action="list", confidence=1.0, source_message_id=source_message_id
            )
        title = clean_task_title(str(raw.get("title") or ""))
        if not title:
            return None
        due_marker = DUE_MARKER.search(source)
        planning_text = source[:due_marker.start()] if due_marker else source
        temporal = parse_relative_date(planning_text, today)
        planned_evidence = str(raw.get("planned_date_evidence") or "").strip()
        planned_evidence_valid = bool(
            planned_evidence and planned_evidence.casefold() in source.casefold()
        )
        planned = raw.get("planned_date")
        if temporal:
            planned = temporal
        elif planned:
            try:
                planned = date.fromisoformat(str(planned)[:10]).isoformat()
            except ValueError:
                planned = None
        if not temporal and not planned_evidence_valid and not re.search(
            r"\b20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b|\d{1,2}月\d{1,2}日", source
        ):
            planned = None
        source_due = parse_due_at(source, today, self.timezone)
        due_evidence = str(raw.get("due_at_evidence") or "").strip()
        due_evidence_valid = bool(
            due_evidence and due_evidence.casefold() in source.casefold()
        )
        due = raw.get("due_at")
        if source_due:
            due = source_due
        elif not due_evidence_valid:
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
        planning_signal = re.search(
            r"提醒我|计划|計劃|安排|准备|準備|打算|要做|开始|挪到|改到|延期|推迟|推遲|"
            r"\b(?:remind\s+me|need\s+to|have\s+to|must|should|remember\s+to|"
            r"plan(?:ning)?\s+to|schedule|going\s+to|postpone|reschedule|push\s+back)\b",
            source, re.I,
        )
        if not planning_signal and due and not planned_evidence_valid:
            planned = None
        try:
            confidence = max(0.0, min(float(raw.get("confidence", 0.5)), 1.0))
        except (TypeError, ValueError):
            confidence = 0.5
        try:
            priority = max(1, min(int(raw.get("priority", 3) or 3), 5))
        except (TypeError, ValueError):
            priority = 3
        confirmation = raw.get("needs_confirmation", False)
        if isinstance(confirmation, str):
            confirmation = confirmation.strip().lower() in {"1", "true", "yes", "on"}
        ambiguous = bool(re.search(
            r"也许|可能|有空|希望|想(?:要)?(?!提醒|安排)|"
            r"\b(?:maybe|might|perhaps|someday|when\s+i\s+have\s+time|hope\s+to|"
            r"want\s+to|would\s+like\s+to)\b",
            source, re.I,
        ))
        return TaskIntent(
            action=action, title=title,
            description=str(raw.get("description") or "").strip(),
            planned_date=planned or (temporal if planning_signal else None), due_at=due,
            priority=priority, confidence=confidence,
            needs_confirmation=bool(confirmation) or ambiguous,
            source_message_id=source_message_id,
        )

    def _rules(self, text: str, today: date) -> TaskIntent | None:
        if self.is_list_query(text):
            return TaskIntent(action="list", confidence=1.0)
        due_marker = DUE_MARKER.search(text)
        planned = parse_relative_date(text[:due_marker.start()] if due_marker else text, today)
        due = parse_due_at(text, today, self.timezone)
        ambiguous = bool(re.search(
            r"也许|可能|有空|希望|想(?:要)?(?!提醒|安排)|"
            r"\b(?:maybe|might|perhaps|someday|when\s+i\s+have\s+time|hope\s+to|"
            r"want\s+to|would\s+like\s+to)\b",
            text, re.I,
        ))
        action = "none"
        title = text
        english_postpone = re.search(
            r"\b(?:postpone|reschedule|delay|push\s+back)\b", text, re.I
        ) or (planned and re.search(r"^\s*move\b", text, re.I))
        if re.search(r"挪到|改到|延期|推迟|推遲", text) or english_postpone:
            action = "postpone"
            if english_postpone:
                title = re.sub(
                    r"^\s*(?:please\s+)?(?:postpone|reschedule|delay|push\s+back|move)\s+",
                    "", text, flags=re.I,
                )
                separators = list(re.finditer(r"\b(?:to|until|for)\b", title, re.I))
                for separator in reversed(separators):
                    if parse_relative_date(title[separator.end():], today):
                        title = title[:separator.start()]
                        break
            else:
                title = re.split(r"挪到|改到|延期|推迟|推遲", text, maxsplit=1)[0]
        elif re.search(
            r"取消|删除(?:这个|这条)?任务|刪除(?:這個|這條)?任務|不用做了|"
            r"^\s*(?:please\s+)?(?:cancel|delete|remove|drop)\b|\bno\s+longer\s+need\b",
            text, re.I,
        ):
            action = "cancel"
            title = re.sub(
                r"取消|删除(?:这个|这条)?任务|刪除(?:這個|這條)?任務|不用做了|"
                r"^\s*(?:please\s+)?(?:cancel|delete|remove|drop)\s+|"
                r"^\s*i\s+no\s+longer\s+need\s+to\s+",
                "", text, flags=re.I,
            )
            title = re.sub(r"\b(?:todo|task)\s*$", "", title, flags=re.I)
            title = re.sub(r"^the\s+", "", title, flags=re.I)
        elif re.search(
            r"已经完成|已完成|完成了|做完了|改完了|搞定了|"
            r"^\s*i(?:'m|\s+am)\s+done\b|"
            r"^\s*i\s+(?:have\s+)?(?:just\s+)?(?:finished|completed|submitted)\b|"
            r"\b(?:is|was|has\s+been)\s+(?:done|finished|completed)\s*$",
            text, re.I,
        ):
            action = "complete"
            title = re.sub(
                r"已经完成|已完成|完成了|做完了|改完了|搞定了|已经?|"
                r"^\s*i(?:'m|\s+am)\s+done\s*(?:with\s+)?|"
                r"^\s*i\s+(?:have\s+)?(?:just\s+)?(?:finished|completed|submitted)\s+|"
                r"\s+(?:is|was|has\s+been)\s+(?:done|finished|completed)\s*$",
                "", text, flags=re.I,
            )
        elif EXPLICIT_CREATE.search(text) or re.search(
            r"提醒我|待办|要做|需要|记得|计划|計劃|安排|准备|準備|截止|最晚|"
            r"\b(?:todo|to-do|task|remind\s+me|need\s+to|have\s+to|must|should|"
            r"remember\s+to|plan(?:ning)?\s+to|schedule|going\s+to|deadline|due)\b",
            text, re.I,
        ):
            action = "create"
        elif due:
            action = "create"
        elif ambiguous:
            action = "create"
        if action == "none":
            return None
        cleaned = clean_task_title(title)
        if not cleaned:
            return None
        if due and not re.search(
            r"提醒我|计划|計劃|安排|准备|準備|打算|要做|开始|挪到|改到|延期|推迟|推遲|"
            r"\b(?:remind\s+me|need\s+to|have\s+to|must|should|remember\s+to|"
            r"plan(?:ning)?\s+to|schedule|going\s+to|postpone|reschedule|push\s+back)\b",
            text, re.I,
        ):
            planned = None
        return TaskIntent(
            action=action, title=cleaned, planned_date=planned, due_at=due,
            confidence=0.72 if not ambiguous else 0.52,
            needs_confirmation=ambiguous, source_message_id=None,
        )

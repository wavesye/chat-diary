from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path


class ActivityService:
    def __init__(self, store):
        self.store = store

    def replace_chat_summary(self, day: str, items: list[dict]) -> int:
        cleaned = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            try:
                confidence = max(0.0, min(float(item.get("confidence", 0.8)), 1.0))
            except (TypeError, ValueError):
                confidence = 0.8
            tags = item.get("tags", [])
            tags = [str(tag).strip() for tag in tags if str(tag).strip()] if isinstance(tags, list) else []
            cleaned.append((title, str(item.get("description", "")).strip(), tags, confidence))
        with self.store._lock, self.store.connection:
            # Natural-language completion records carry source_message_id and are preserved.
            self.store.connection.execute(
                "DELETE FROM activities WHERE day=? AND source_type='chat' "
                "AND source_message_id IS NULL", (day,),
            )
            for title, description, tags, confidence in cleaned:
                self.store.add_activity(
                    day, title, description=description, source_type="chat",
                    tags=json.dumps(tags, ensure_ascii=False), confidence=confidence,
                )
        return len(cleaned)

    def list_day(self, day: str) -> list[dict]:
        with self.store._lock:
            rows = self.store.connection.execute(
                "SELECT * FROM activities WHERE day=? ORDER BY id", (day,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["tags"] = json.loads(item["tags"] or "[]")
            except json.JSONDecodeError:
                item["tags"] = []
            result.append(item)
        return result

    def invalidate_chat_day(self, day: str) -> int:
        with self.store._lock, self.store.connection:
            cursor = self.store.connection.execute(
                "DELETE FROM activities WHERE day=? AND source_type='chat'", (day,)
            )
        return int(cursor.rowcount)

    def month(self, month: str, today: str) -> list[dict]:
        start = date.fromisoformat(month + "-01")
        end = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
        with self.store._lock:
            rows = self.store.connection.execute(
                "SELECT day, COUNT(*) AS activity_count FROM activities "
                "WHERE day>=? AND day<? AND day<=? GROUP BY day ORDER BY day",
                (start.isoformat(), end.isoformat(), today),
            ).fetchall()
        return [dict(row) for row in rows]

    def review_day(self, day: str) -> dict:
        with self.store._lock:
            entry = self.store.connection.execute(
                "SELECT day, title, tags, markdown_path FROM entries WHERE day=?", (day,)
            ).fetchone()
            has_history = self.store.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='historical_documents'"
            ).fetchone()
            historical = self.store.connection.execute(
                "SELECT title, frontmatter, source_file FROM historical_documents "
                "WHERE diary_date=? ORDER BY id LIMIT 1", (day,)
            ).fetchone() if has_history else None
        tags = []
        title = entry["title"] if entry else ""
        markdown_path = entry["markdown_path"] if entry else None
        if entry:
            try:
                tags = json.loads(entry["tags"] or "[]")
            except json.JSONDecodeError:
                tags = []
            if not title and markdown_path:
                try:
                    content = Path(markdown_path).read_text(encoding="utf-8")
                    heading = re.search(r"^#\s+(.+)$", content, re.M)
                    title = heading.group(1).strip() if heading else ""
                except OSError:
                    pass
        if historical and not title:
            title = historical["title"]
            markdown_path = markdown_path or historical["source_file"]
            try:
                frontmatter = json.loads(historical["frontmatter"] or "{}")
                historical_tags = frontmatter.get("tags", [])
                if isinstance(historical_tags, list):
                    tags.extend(str(tag) for tag in historical_tags)
            except json.JSONDecodeError:
                pass
        activities = self.list_day(day)
        completed = [item for item in activities if item["source_type"] == "todo"]
        other = [item for item in activities if item["source_type"] != "todo"]
        all_tags = list(dict.fromkeys(tags + [tag for item in activities for tag in item["tags"]]))
        return {
            "day": day, "title": title,
            "markdown_path": markdown_path,
            "completed_todos": completed, "other_activities": other, "tags": all_tags,
        }

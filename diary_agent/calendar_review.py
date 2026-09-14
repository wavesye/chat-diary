"""Read-only, paginated activity reviews for text chat channels."""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from .channels.base import Button


class CalendarReview:
    PAGE_SIZE = 7
    USAGE = (
        "用法：/calendar（最近30天）、/calendar 2026-09（月）、"
        "/calendar 2026-09-14（日）；翻页：/calendar recent 2 或 /calendar 2026-09 2"
    )

    def __init__(self, activities, today: str):
        self.activities = activities
        self.today = date.fromisoformat(today)

    @staticmethod
    def _short(text: str, limit: int = 60) -> str:
        text = " ".join(str(text).split())
        return text if len(text) <= limit else text[:limit - 1] + "…"

    @classmethod
    def _summary(cls, items: list[dict], marker: str) -> str:
        titles = "；".join(cls._short(item["title"]) for item in items[:2])
        remaining = f"（另有 {len(items) - 2} 项，查看当天了解详情）" if len(items) > 2 else ""
        return marker + titles + remaining

    def _day_lines(self, review: dict, *, detail: bool) -> list[str]:
        day = date.fromisoformat(review["day"])
        lines = [f"{day.isoformat()} 周{'一二三四五六日'[day.weekday()]}"]
        if review["title"]:
            title = review["title"] if detail else self._short(review["title"])
            lines.append(f"日记：{title}")
        for key, label, marker in (
            ("other_activities", "做了什么", "• "),
            ("completed_todos", "已完成 Todo", "✓ "),
        ):
            items = review[key]
            if not items:
                continue
            if detail:
                lines.append(label + "：")
                for item in items:
                    lines.append(marker + item["title"])
                    if item.get("description"):
                        lines.append("  " + item["description"])
            else:
                lines.append(self._summary(items, marker))
        if not review["other_activities"] and not review["completed_todos"]:
            if review["title"] or review.get("markdown_path"):
                lines.append("已记录日记，暂无已整理的活动或完成 Todo。")
            else:
                lines.append("暂无已整理的活动或日记记录。")
        if detail and review.get("tags"):
            lines.append("标签：" + "、".join(review["tags"]))
        return lines

    def render(self, query: str = "") -> tuple[str, tuple[tuple[Button, ...], ...]]:
        parts = query.split()
        if len(parts) > 2:
            raise ValueError(self.USAGE)
        period = parts[0] if parts else "recent"
        if period in {"最近", "30天"}:
            period = "recent"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", period):
            if len(parts) > 1:
                raise ValueError("单日回顾不需要页码。" + self.USAGE)
            try:
                selected = date.fromisoformat(period)
            except ValueError as error:
                raise ValueError("日期无效，请使用 YYYY-MM-DD，例如 2026-09-14。") from error
            if selected > self.today:
                raise ValueError("回顾日历只查看今天及以前的记录；未来安排请用 /todo 或 /week。")
            review = self.activities.review_day(period)
            text = "回顾日历\n\n" + "\n".join(self._day_lines(review, detail=True))
            text += f"\n\n查看本月：/calendar {period[:7]}"
            return text, ((Button("查看本月", f"calendar:{period[:7]}:1"),),)
        if period == "recent":
            end = self.today
            start = date.fromordinal(max(1, end.toordinal() - 29))
            label = "最近30天"
        elif re.fullmatch(r"\d{4}-\d{2}", period):
            try:
                start = date.fromisoformat(period + "-01")
            except ValueError as error:
                raise ValueError("月份无效，请使用 YYYY-MM，例如 2026-09。") from error
            if start > self.today:
                raise ValueError("这个月份还未到来；未来安排请用 /todo 或 /week。")
            end = min(self.today, start.replace(day=calendar.monthrange(start.year, start.month)[1]))
            label = period
        else:
            raise ValueError(self.USAGE)
        if len(parts) == 2 and not re.fullmatch(r"[1-9]\d{0,2}", parts[1]):
            raise ValueError("页码应为从 1 开始的整数。" + self.USAGE)
        page = int(parts[1]) if len(parts) == 2 else 1
        pages = ((end - start).days + self.PAGE_SIZE) // self.PAGE_SIZE
        if page > pages:
            raise ValueError(f"{label}共有 {pages} 页，请选择 1 到 {pages}。")
        page_end = end - timedelta(days=(page - 1) * self.PAGE_SIZE)
        page_start = date.fromordinal(max(start.toordinal(), page_end.toordinal() - self.PAGE_SIZE + 1))
        blocks = [
            f"回顾日历 · {label}（第 {page}/{pages} 页）\n"
            f"{start.isoformat()} — {end.isoformat()} · 每页最多7天，最近在前"
        ]
        day = page_end
        while day >= page_start:
            review = self.activities.review_day(day.isoformat())
            blocks.append("\n".join(self._day_lines(review, detail=False)))
            if day == page_start:
                break
            day -= timedelta(days=1)
        links = []
        buttons = []
        for target, label in ((page - 1, "上一页（更近）"), (page + 1, "下一页（更早）")):
            if 1 <= target <= pages:
                links.append(f"{label}：/calendar {period} {target}")
                buttons.append(Button(label, f"calendar:{period}:{target}"))
        blocks.append(
            "\n".join(links + ["查看某天详情：/calendar YYYY-MM-DD", "这里只列已发生的活动和已完成 Todo。"])
        )
        return "\n\n".join(blocks), (tuple(buttons),) if buttons else ()

import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from diary_agent.activities import ActivityService
from diary_agent.calendar_review import CalendarReview
from diary_agent.chat_handler import ChatHandler, HELP
from diary_agent.store import DiaryStore
from diary_agent.todo import TodoService


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DiaryStore(Path(self.temp.name) / "diary.sqlite")
        self.addCleanup(self.store.connection.close)
        self.activities = ActivityService(self.store)
        self.calendar = CalendarReview(self.activities, "2026-09-14")

    def test_recent_pages_cover_exactly_thirty_days_with_navigation(self):
        days = []
        for page in range(1, 6):
            text, buttons = self.calendar.render(f"recent {page}")
            page_days = re.findall(r"^(\d{4}-\d{2}-\d{2}) 周", text, re.M)
            days.extend(page_days)
            self.assertLessEqual(len(page_days), 7)
            self.assertIn(f"第 {page}/5 页", text)
            values = [button.value for row in buttons for button in row]
            if page < 5:
                self.assertIn(f"calendar:recent:{page + 1}", values)
                self.assertIn(f"/calendar recent {page + 1}", text)
            if page > 1:
                self.assertIn(f"calendar:recent:{page - 1}", values)
        self.assertEqual(len(days), 30)
        self.assertEqual(len(set(days)), 30)
        self.assertEqual((days[0], days[-1]), ("2026-09-14", "2026-08-16"))

    def test_review_shows_real_activities_and_completed_todos_without_pending_plans(self):
        todos = TodoService(self.store, ZoneInfo("Asia/Shanghai"))
        todos.create("还没发生的旅行", planned_date="2026-09-14")
        finished = todos.create("提交论文", planned_date="2026-09-12")
        todos.complete(finished["id"], completed_at=datetime(2026, 9, 14, 10, tzinfo=ZoneInfo("Asia/Shanghai")))
        self.store.add_activity("2026-09-14", "沿河散步", description="走了半小时", source_type="chat")
        text, _ = self.calendar.render()
        self.assertIn("• 沿河散步", text)
        self.assertIn("✓ 提交论文", text)
        self.assertNotIn("还没发生的旅行", text)
        text, buttons = self.calendar.render("2026-09-14")
        self.assertIn("做了什么：", text)
        self.assertIn("走了半小时", text)
        self.assertIn("已完成 Todo：", text)
        self.assertEqual(buttons[0][0].value, "calendar:2026-09:1")
        self.assertEqual(self.store.messages("2026-09-14"), [])

    def test_current_month_stops_at_today_and_past_leap_month_has_all_days(self):
        text, _ = self.calendar.render("2026-09")
        self.assertIn("2026-09-01 — 2026-09-14", text)
        self.assertIn("第 1/2 页", text)
        text, _ = self.calendar.render("2024-02 5")
        self.assertIn("2024-02-01 — 2024-02-29", text)
        self.assertEqual(re.findall(r"^(\d{4}-\d{2}-\d{2}) 周", text, re.M), ["2024-02-01"])

    def test_empty_day_explains_absence_of_summarized_records(self):
        text, _ = self.calendar.render("2026-08-16")
        self.assertIn("暂无已整理的活动或日记记录", text)

    def test_day_detail_keeps_items_omitted_from_compact_pages(self):
        for index in range(5):
            self.store.add_activity("2026-09-14", f"活动{index}", source_type="chat")
        text, _ = self.calendar.render()
        self.assertIn("另有 3 项", text)
        self.assertNotIn("活动4", text)
        detail, _ = self.calendar.render("2026-09-14")
        self.assertIn("活动4", detail)

    def test_invalid_dates_future_dates_and_invalid_pages_are_rejected(self):
        for query in (
            "2026-02-30", "2026-13", "2026-09-15", "2026-10", "2026-9",
            "yesterday", "recent 0", "recent -1", "recent foo", "recent 6",
            "recent 1 extra", "2026-09-14 1", "0000-01",
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.calendar.render(query)

    def test_extreme_valid_dates_do_not_overflow(self):
        text, _ = CalendarReview(self.activities, "0001-01-01").render()
        self.assertIn("0001-01-01", text)
        text, _ = CalendarReview(self.activities, "9999-12-31").render("9999-12")
        self.assertIn("9999-12-31", text)

    def test_summary_deduplicates_completed_todos_and_preserved_chat_activities(self):
        todos = TodoService(self.store, ZoneInfo("Asia/Shanghai"))
        todo = todos.create("提交论文")
        todos.complete(todo["id"], completed_at=datetime(2026, 9, 14, 10, tzinfo=ZoneInfo("Asia/Shanghai")))
        source_id = self.store.add_message("2026-09-14", "user", "今天散步了")
        self.store.add_activity("2026-09-14", "散步", source_type="chat", source_message_id=source_id)
        self.store.add_activity("2026-09-13", "买菜", source_type="chat", source_message_id=source_id)
        self.activities.replace_chat_summary("2026-09-14", [{"title": "旧摘要活动"}])
        items = [
            {"title": "提交 论文。"}, {"title": "散步！"},
            {"title": "买菜", "description": "今天又去了一次"},
            {"title": "Review ＰＲ"}, {"title": "review pr."},
        ]
        self.assertEqual(self.activities.replace_chat_summary("2026-09-14", items), 2)
        # Replacing the same summary stays idempotent; real events keep their IDs.
        self.assertEqual(self.activities.replace_chat_summary("2026-09-14", items), 2)
        review = self.activities.review_day("2026-09-14")
        self.assertEqual([item["title"] for item in review["completed_todos"]], ["提交论文"])
        self.assertEqual([item["title"] for item in review["other_activities"]], ["散步", "买菜", "Review ＰＲ"])
        self.assertEqual(review["other_activities"][0]["source_message_id"], source_id)
        text, _ = self.calendar.render("2026-09-14")
        self.assertEqual(text.count("提交论文"), 1)

    def test_summary_ignores_empty_normalized_titles_and_duplicate_spellings(self):
        self.assertEqual(self.activities.replace_chat_summary("2026-09-14", [
            {"title": "..."}, {"title": "READ BOOK"}, {"title": "Read book！"},
        ]), 1)
        self.assertEqual([item["title"] for item in self.activities.list_day("2026-09-14")], ["READ BOOK"])


class CalendarCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DiaryStore(Path(self.temp.name) / "diary.sqlite")
        self.addCleanup(self.store.connection.close)
        self.service = SimpleNamespace(
            activities=ActivityService(self.store), day="2026-09-14",
            journal_instruction=lambda text: None,
        )
        self.adapter = SimpleNamespace(send_message=AsyncMock())
        self.handler = ChatHandler(self.service, self.adapter, "owner", "123")

    async def test_command_and_callback_route_without_a_model(self):
        self.assertIn("/calendar", HELP)
        await self.handler.handle_text("/calendar@my_diary_bot")
        self.assertIn("最近30天", self.adapter.send_message.call_args.args[1])
        await self.handler.handle_action("calendar:recent:2")
        self.assertIn("第 2/5 页", self.adapter.send_message.call_args.args[1])
        await self.handler.handle_text("/calendar 2026-09-12")
        self.assertIn("2026-09-12 周六", self.adapter.send_message.call_args.args[1])

    async def test_malformed_callback_and_future_command_return_friendly_errors(self):
        for action in ("calendar:recent:99", "calendar:recent", "calendar:recent:1:extra"):
            await self.handler.handle_action(action)
            self.assertIn("操作没有完成", self.adapter.send_message.call_args.args[1])
        await self.handler.handle_text("/calendar 2026-10")
        self.assertIn("还未到来", self.adapter.send_message.call_args.args[1])


if __name__ == "__main__":
    unittest.main()

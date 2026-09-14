import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from diary_agent.store import DiaryStore
from diary_agent.todo import TodoService


class TodoDiaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DiaryStore(Path(self.temp.name) / "diary.sqlite")
        self.todos = TodoService(self.store, ZoneInfo("Asia/Shanghai"))

    def tearDown(self):
        self.store.connection.close()
        self.temp.cleanup()

    def task(self, title, *, created="2026-09-01 00:00:00", **kwargs):
        todo = self.todos.create(title, **kwargs)
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE todos SET created_at=?, updated_at=? WHERE id=?",
                (created, created, todo["id"]),
            )
            self.store.connection.execute(
                "UPDATE todo_events SET created_at=? WHERE todo_id=?", (created, todo["id"])
            )
        return todo

    def ids(self, day="2026-09-14"):
        return {item["id"] for item in self.todos.diary_items(day)}

    def test_local_creation_day_and_future_plan_are_included_without_messages(self):
        included = self.task("明天交报告", created="2026-09-13 16:00:00", planned_date="2026-09-15")
        self.task("昨天临时想到的事", created="2026-09-13 15:59:59")
        self.task("下一天新建", created="2026-09-14 16:00:00")
        self.assertEqual(self.ids(), {included["id"]})
        self.assertEqual(self.store.messages("2026-09-14"), [])

    def test_pending_and_cancelled_are_excluded(self):
        self.task("还没确认", created="2026-09-14 00:00:00", confirmed=False)
        cancelled = self.task("取消的任务", created="2026-09-14 00:00:00")
        self.todos.cancel(cancelled["id"])
        self.assertEqual(self.ids(), set())

    def test_confirm_update_and_postpone_events_count_in_user_timezone(self):
        confirmed = self.task("确认任务", confirmed=False)
        edited = self.task("修改任务")
        postponed = self.task("延期任务")
        self.todos.confirm(confirmed["id"])
        self.todos.update(edited["id"], title="改过的标题")
        self.todos.postpone(postponed["id"], "2026-10-01")
        with self.store.connection:
            self.store.connection.execute("UPDATE todos SET updated_at='2026-09-01 00:00:00'")
            self.store.connection.execute(
                "UPDATE todo_events SET created_at='2026-09-13 16:30:00' WHERE action != 'create'"
            )
        self.assertEqual(self.ids(), {confirmed["id"], edited["id"], postponed["id"]})
        self.assertEqual(next(item for item in self.todos.diary_items("2026-09-14")
                              if item["id"] == edited["id"])["title"], "改过的标题")

    def test_scheduled_due_and_overdue_active_tasks_are_included(self):
        planned = self.task("今日计划", planned_date="2026-09-14")
        overdue = self.task("逾期计划", planned_date="2026-09-13")
        due = self.task("今日到期", due_at="2026-09-14")
        independently_due = self.task("计划在后天但今日到期", planned_date="2026-09-16", due_at="2026-09-14")
        self.task("明天才做", planned_date="2026-09-15")
        self.task("无日期旧任务")
        self.assertEqual(self.ids(), {planned["id"], overdue["id"], due["id"], independently_due["id"]})

    def test_due_datetime_is_converted_to_local_day(self):
        due = self.task("当地今天截止", due_at="2026-09-13T17:00:00Z")
        self.task("当地明天截止", due_at="2026-09-14T17:00:00+00:00")
        self.assertEqual(self.ids(), {due["id"]})

    def test_cross_day_completion_uses_completion_day_and_does_not_repeat(self):
        done = self.task("跨天完成", planned_date="2026-09-12")
        self.todos.complete(done["id"], completed_at=datetime(2026, 9, 13, 17, tzinfo=timezone.utc))
        self.assertEqual(self.ids(), {done["id"]})
        self.assertEqual(self.ids("2026-09-15"), set())
        self.assertEqual(self.todos.diary_items("2026-09-14")[0]["status"], "completed")
        activity = self.store.connection.execute("SELECT day FROM activities WHERE todo_id=?", (done["id"],)).fetchone()
        self.assertEqual(activity["day"], "2026-09-14")

    def test_eventless_legacy_rows_use_task_timestamps(self):
        added = self.task("旧库新增", created="2026-09-13 16:00:00")
        done = self.task("旧库完成")
        with self.store.connection:
            self.store.connection.execute("DELETE FROM todo_events")
            self.store.connection.execute(
                "UPDATE todos SET status='completed', completed_at='2026-09-14T01:00:00+08:00' WHERE id=?", (done["id"],)
            )
        self.assertEqual(self.ids(), {added["id"], done["id"]})

    def test_missing_completion_timestamp_falls_back_to_completion_event(self):
        done = self.task("旧完成事件")
        with self.store.connection:
            self.store.connection.execute("UPDATE todos SET status='completed' WHERE id=?", (done["id"],))
            self.store.connection.execute(
                "INSERT INTO todo_events(todo_id, action, detail, created_at) VALUES (?, 'complete', '{}', '2026-09-13 16:00:00')",
                (done["id"],),
            )
        self.assertEqual(self.ids(), {done["id"]})
        self.assertEqual(self.ids("2026-09-15"), set())


if __name__ == "__main__":
    unittest.main()

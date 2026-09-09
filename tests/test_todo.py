import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from diary_agent.store import DiaryStore
from diary_agent.reminders import ReminderService
from diary_agent.todo import TaskExtractor, TodoService, parse_due_at, parse_relative_date


class BrokenProvider:
    def json(self, messages):
        raise RuntimeError("model unavailable")


class TodoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DiaryStore(Path(self.temp.name) / "diary.sqlite")
        self.todos = TodoService(self.store, ZoneInfo("Asia/Shanghai"))

    def tearDown(self):
        self.temp.cleanup()

    def test_create_modify_postpone_complete_and_activity(self):
        todo = self.todos.create(
            "修改论文第二节", description="补充实验", planned_date="2026-09-10",
            due_at="2026-09-12T18:00:00+08:00", priority=5,
            creation_method="natural_language", confirmed=True,
        )
        self.assertEqual(todo["planned_date"], "2026-09-10")
        self.assertEqual(todo["due_at"], "2026-09-12T18:00:00+08:00")
        changed = self.todos.update(todo["id"], description="补充两组实验")
        self.assertIn("两组", changed["description"])
        moved = self.todos.postpone(todo["id"], "2026-09-11")
        self.assertEqual(moved["planned_date"], "2026-09-11")
        done = self.todos.complete(
            todo["id"], completed_at=datetime(2026, 9, 11, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
        self.assertEqual(done["status"], "completed")
        activity = self.store.connection.execute(
            "SELECT * FROM activities WHERE todo_id=?", (todo["id"],)
        ).fetchone()
        self.assertEqual(activity["day"], "2026-09-11")

    def test_pending_todo_must_be_confirmed(self):
        todo = self.todos.create("学习陶艺", confirmed=False, creation_method="inferred")
        self.assertEqual(todo["status"], "pending_confirmation")
        confirmed = self.todos.confirm(todo["id"])
        self.assertEqual(confirmed["status"], "active")
        self.assertEqual(confirmed["confirmed"], 1)

    def test_rule_extraction_uses_timezone_date_and_does_not_make_due_date(self):
        extractor = TaskExtractor(BrokenProvider(), ZoneInfo("Asia/Shanghai"))
        intent = extractor._rules("明天提醒我修改论文第二节", date(2026, 9, 9))
        self.assertEqual(intent.action, "create")
        self.assertEqual(intent.planned_date, "2026-09-10")
        self.assertIsNone(intent.due_at)
        moved = extractor._rules("BMS 分析挪到后天", date(2026, 9, 9))
        self.assertEqual(moved.action, "postpone")
        self.assertEqual(moved.planned_date, "2026-09-11")

    def test_deadline_is_not_mistaken_for_planned_date(self):
        extractor = TaskExtractor(BrokenProvider(), ZoneInfo("Asia/Shanghai"))
        intent = extractor._rules("论文最晚明天下午3点提交", date(2026, 9, 9))
        self.assertEqual(intent.action, "create")
        self.assertIsNone(intent.planned_date)
        self.assertEqual(intent.due_at, "2026-09-10T15:00:00+08:00")
        both = extractor._rules(
            "明天开始修改论文，最晚后天提交", date(2026, 9, 9)
        )
        self.assertEqual(both.planned_date, "2026-09-10")
        self.assertEqual(both.due_at, "2026-09-11")
        self.assertEqual(
            parse_relative_date("next Friday", date(2026, 9, 9)), "2026-09-18"
        )
        self.assertEqual(
            parse_relative_date("下周一", date(2026, 9, 9)), "2026-09-14"
        )

    def test_existing_database_data_survives_additive_migration(self):
        legacy_path = Path(self.temp.name) / "legacy.sqlite"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, day TEXT, role TEXT, content TEXT, created_at TEXT);"
            "CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO messages VALUES (1, '2026-09-01', 'user', '旧消息', 'now');"
            "INSERT INTO app_state VALUES ('telegram_update_offset:1', '88');"
        )
        connection.commit()
        connection.close()
        migrated = DiaryStore(legacy_path)
        self.assertEqual(migrated.message_records("2026-09-01")[0]["content"], "旧消息")
        self.assertEqual(migrated.get_state("telegram_update_offset:1"), "88")
        self.assertIsNotNone(migrated.connection.execute(
            "SELECT name FROM sqlite_master WHERE name='todos'"
        ).fetchone())

    def test_reminder_log_prevents_duplicates_after_restart(self):
        reminder = ReminderService(
            self.store, ZoneInfo("Asia/Shanghai"), "08:30", "21:30"
        )
        morning = datetime(2026, 9, 9, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(reminder.due(123, morning), "morning")
        self.assertTrue(reminder.mark_sent(123, "morning", morning))
        restarted = ReminderService(
            self.store, ZoneInfo("Asia/Shanghai"), "08:30", "21:30"
        )
        self.assertIsNone(restarted.due(123, morning))
        evening = morning.replace(hour=22)
        self.assertEqual(restarted.due(123, evening), "evening")


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from diary_agent.config import Settings
from diary_agent.service import DiaryService
from diary_agent.store import DiaryStore
from telegram_bot import TelegramDiaryBot
from test_diary import FakeProvider


class FakeTelegramAPI:
    def __init__(self):
        self.sent = []
        self.calls = []

    async def call(self, method, **payload):
        self.calls.append((method, payload))
        return True

    async def send(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))


def update(user_id, text, update_id=1):
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": user_id, "type": "private"},
            "text": text,
        },
    }


def callback(user_id, data, update_id=2):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "callback-1", "from": {"id": user_id}, "data": data,
            "message": {"chat": {"id": user_id, "type": "private"}},
        },
    }


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        settings = Settings(root, "Daily", root / "diary.sqlite", ZoneInfo("UTC"),
                            "fake", "fake", "", "", "小叶")
        self.service = DiaryService(settings, provider=FakeProvider(),
                                    store=DiaryStore(settings.database_path))
        self.api = FakeTelegramAPI()

    def tearDown(self):
        self.temp.cleanup()

    async def test_pairing_mode_reports_user_id_without_saving_chat(self):
        bot = TelegramDiaryBot(self.service, self.api, None)
        await bot.handle_update(update(12345, "/start"))
        self.assertIn("12345", self.api.sent[0][1])
        self.assertEqual(self.service.store.messages(self.service.day), [])

    async def test_unauthorized_user_is_ignored(self):
        bot = TelegramDiaryBot(self.service, self.api, 12345)
        await bot.handle_update(update(99999, "hello"))
        self.assertEqual(self.api.sent, [])

    async def test_chat_and_commands_use_existing_diary_service(self):
        bot = TelegramDiaryBot(self.service, self.api, 12345)
        await bot.handle_update(update(12345, "今天完成了方案"))
        self.assertTrue(any("什么感受" in message[1] for message in self.api.sent))
        self.assertTrue(any("完成的活动" in message[1] for message in self.api.sent))
        await bot.handle_update(update(12345, "/preview", 2))
        self.assertIn("# 雨里的好消息", self.api.sent[-1][1])
        await bot.handle_update(update(12345, "/done", 3))
        self.assertIn("AIGEN.md", self.api.sent[-1][1])

    async def test_todo_commands_and_inline_callbacks(self):
        bot = TelegramDiaryBot(self.service, self.api, 12345)
        await bot.handle_update(update(12345, "/todo 添加 明天修改论文第二节"))
        todo = self.service.todos.list()[0]
        self.assertEqual(
            todo["planned_date"],
            (self.service.todos.now.date() + timedelta(days=1)).isoformat(),
        )
        self.assertEqual(
            self.api.sent[-1][2]["inline_keyboard"][0][0]["callback_data"],
            f"todo:done:{todo['id']}",
        )
        await bot.handle_update(callback(12345, f"todo:done:{todo['id']}"))
        self.assertEqual(self.service.todos.get(todo["id"])["status"], "completed")
        self.assertTrue(any(call[0] == "answerCallbackQuery" for call in self.api.calls))

    async def test_ambiguous_natural_wish_needs_confirmation(self):
        bot = TelegramDiaryBot(self.service, self.api, 12345)
        await bot.handle_update(update(12345, "我希望有空学习陶艺"))
        pending = self.service.todos.list(statuses=("pending_confirmation",))
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["confirmed"], 0)
        self.assertIn("确认加入", str(self.api.sent[-1][2]))

    def test_only_temporary_telegram_errors_are_retried(self):
        request = httpx.Request("POST", "https://api.telegram.org/bot/test/getUpdates")
        self.assertTrue(TelegramDiaryBot._is_retryable(httpx.ReadTimeout("offline", request=request)))
        self.assertTrue(TelegramDiaryBot._is_retryable(httpx.HTTPStatusError(
            "server error", request=request, response=httpx.Response(502, request=request)
        )))
        self.assertFalse(TelegramDiaryBot._is_retryable(httpx.HTTPStatusError(
            "unauthorized", request=request, response=httpx.Response(401, request=request)
        )))


if __name__ == "__main__":
    unittest.main()

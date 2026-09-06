import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

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

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))


def update(user_id, text, update_id=1):
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": user_id, "type": "private"},
            "text": text,
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
        self.assertIn("什么感受", self.api.sent[-1][1])
        await bot.handle_update(update(12345, "/preview", 2))
        self.assertIn("# 雨里的好消息", self.api.sent[-1][1])
        await bot.handle_update(update(12345, "/done", 3))
        self.assertIn("AIGEN.md", self.api.sent[-1][1])


if __name__ == "__main__":
    unittest.main()

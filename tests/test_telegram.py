import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx

from diary_agent.config import Settings
from diary_agent.service import DiaryService
from diary_agent.store import DiaryStore
from telegram_bot import TelegramAPI, TelegramDiaryBot
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


class TypingFailureAPI(FakeTelegramAPI):
    async def call(self, method, **payload):
        if method == "sendChatAction":
            request = httpx.Request("POST", "https://api.telegram.org/bot/test/sendChatAction")
            raise httpx.ReadTimeout("", request=request)
        return await super().call(method, **payload)


class FlakyHTTPClient:
    def __init__(self):
        self.calls = 0
        self.payloads = []

    async def post(self, url, json):
        self.calls += 1
        self.payloads.append(json)
        request = httpx.Request("POST", url)
        if self.calls == 1:
            raise httpx.ReadTimeout("", request=request)
        return httpx.Response(
            200, request=request, json={"ok": True, "result": {"message_id": 1}}
        )

    async def aclose(self):
        return None


class StopBot(Exception):
    pass


class BufferedRunAPI(FakeTelegramAPI):
    def __init__(self):
        super().__init__()
        self.polls = 0

    async def call(self, method, **payload):
        self.calls.append((method, payload))
        if method == "getMe":
            return {"id": 99, "username": "test_diary_bot"}
        if method == "deleteWebhook":
            return True
        if method == "getUpdates":
            self.polls += 1
            if self.polls == 1:
                return [
                    update(12345, "今天开会了", 1),
                    update(12345, "讨论了新方案", 2),
                ]
            if self.polls == 2:
                await asyncio.sleep(0.02)
                return []
            raise StopBot
        return True


class OneUpdatePerPollAPI(FakeTelegramAPI):
    """Deliver a burst the way Telegram often does: one update per poll."""

    def __init__(self):
        super().__init__()
        self.items = [
            update(12345, "test1", 1),
            update(12345, "test2", 2),
            update(12345, "test3", 3),
            update(12345, "tell me next", 4),
        ]
        self.empty_poll_returned = False

    async def call(self, method, **payload):
        self.calls.append((method, payload))
        if method == "getMe":
            return {"id": 100, "username": "test_diary_bot"}
        if method == "deleteWebhook":
            return True
        if method == "getUpdates":
            if self.items:
                await asyncio.sleep(0.002)
                return [self.items.pop(0)]
            if not self.empty_poll_returned:
                self.empty_poll_returned = True
                await asyncio.sleep(0.02)
                return []
            raise StopBot
        return True


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

    async def test_english_natural_language_creates_scheduled_todo(self):
        bot = TelegramDiaryBot(self.service, self.api, 12345)
        await bot.handle_update(
            update(12345, "Remind me tomorrow to revise section two")
        )
        todo = self.service.todos.list()[0]
        self.assertEqual(todo["title"], "revise section two")
        self.assertEqual(
            todo["planned_date"],
            (self.service.todos.now.date() + timedelta(days=1)).isoformat(),
        )
        self.assertTrue(any("已加入 Todo" in item[1] for item in self.api.sent))

    async def test_natural_list_query_does_not_create_a_todo(self):
        self.service.todos.create("原有任务")
        bot = TelegramDiaryBot(self.service, self.api, 12345)
        await bot.handle_update(update(12345, "我现在的todo list有什么"))
        todos = self.service.todos.list(statuses=("active",))
        self.assertEqual([item["title"] for item in todos], ["原有任务"])
        self.assertTrue(any("原有任务" in item[1] for item in self.api.sent))
        self.assertFalse(any("已加入 Todo" in item[1] for item in self.api.sent))

    async def test_explicit_natural_todo_is_persisted_before_listing(self):
        bot = TelegramDiaryBot(self.service, self.api, 12345)
        await bot.handle_update(update(12345, "今天做10个俯卧撑，加入todo"))
        todos = self.service.todos.list(statuses=("active",))
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["title"], "做10个俯卧撑")
        self.assertTrue(any("已加入 Todo" in item[1] for item in self.api.sent))

        await bot.handle_update(update(12345, "/todo", 2))
        self.assertTrue(any("做10个俯卧撑" in item[1] for item in self.api.sent))

        await bot.handle_update(update(12345, "10个俯卧撑已完成", 3))
        self.assertEqual(self.service.todos.get(todos[0]["id"])["status"], "completed")
        self.assertTrue(any("已完成 Todo" in item[1] for item in self.api.sent))
        self.assertEqual(
            self.service.store.message_records(self.service.day)[-1]["content"],
            "10个俯卧撑已完成",
        )
        self.assertEqual(self.service.provider.last_chat_messages, [])

    async def test_run_debounces_message_burst_into_one_reply(self):
        self.service.reminders.enabled = False
        api = BufferedRunAPI()
        bot = TelegramDiaryBot(
            self.service, api, 12345, message_debounce_seconds=0.01
        )
        with self.assertRaises(StopBot):
            await bot.run()
        records = self.service.store.message_records(self.service.day)
        self.assertEqual(
            [(item["role"], item["content"]) for item in records],
            [
                ("user", "今天开会了"),
                ("user", "讨论了新方案"),
                ("assistant", "听起来这件事对你挺重要。那一刻你是什么感受？"),
            ],
        )
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(
            self.service.store.get_state("telegram_update_offset:99"), "3"
        )

    async def test_run_debounces_updates_arriving_across_separate_polls(self):
        self.service.reminders.enabled = False
        api = OneUpdatePerPollAPI()
        bot = TelegramDiaryBot(
            self.service, api, 12345, message_debounce_seconds=0.01
        )
        with self.assertRaises(StopBot):
            await bot.run()
        records = self.service.store.message_records(self.service.day)
        self.assertEqual(
            [item["content"] for item in records if item["role"] == "user"],
            ["test1", "test2", "test3", "tell me next"],
        )
        self.assertEqual(
            len([item for item in records if item["role"] == "assistant"]), 1
        )
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(
            self.service.store.get_state("telegram_update_offset:100"), "5"
        )

    async def test_status_reports_effective_debounce_window(self):
        bot = TelegramDiaryBot(
            self.service, self.api, 12345, message_debounce_seconds=4.5
        )
        await bot.handle_update(update(12345, "/status"))
        status = self.api.sent[-1][1]
        self.assertIn("消息合并窗口：4.5 秒", status)
        self.assertIn("当前降级为 keyword", status)
        self.assertNotIn("运行 import_history.py embed", status)

    def test_commands_are_never_debounced(self):
        bot = TelegramDiaryBot(
            self.service, self.api, 12345, message_debounce_seconds=3
        )
        self.assertTrue(bot._is_batchable_update(update(12345, "第一段")))
        self.assertFalse(bot._is_batchable_update(update(12345, "/done")))

    async def test_typing_failure_does_not_abort_chat(self):
        api = TypingFailureAPI()
        bot = TelegramDiaryBot(self.service, api, 12345)
        await bot.handle_update(update(12345, "只是记一下今天散步了"))
        self.assertTrue(any("什么感受" in message[1] for message in api.sent))
        self.assertFalse(any("处理失败" in message[1] for message in api.sent))

    async def test_telegram_api_retries_one_temporary_disconnect(self):
        client = FlakyHTTPClient()
        api = TelegramAPI("test", client=client)
        with patch("telegram_bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await api.call("sendMessage", chat_id=12345, text="hello")
        self.assertEqual(result["message_id"], 1)
        self.assertEqual(client.calls, 2)
        self.assertEqual(client.payloads[-1], {"chat_id": 12345, "text": "hello"})
        sleep.assert_awaited_once_with(1)

    def test_empty_exception_still_gets_a_useful_error_message(self):
        message = TelegramDiaryBot._friendly_error(RuntimeError())
        self.assertIn("RuntimeError", message)
        self.assertNotEqual(message, "处理失败：")

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

"""Offline transport regression tests, independent of the diary business layer."""

import asyncio
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx

from diary_agent.channels.base import Button
from diary_agent.channels.telegram import TelegramAPI, TelegramAdapter


class MemoryState:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_state(self, key, default=None):
        return self.values.get(key, default)

    def set_state(self, key, value):
        self.values[key] = value


class StopPolling(Exception):
    pass


class FakeAPI:
    def __init__(self, updates=None, *, block_poll=False):
        self.updates = updates
        self.block_poll = block_poll
        self.poll_started = asyncio.Event()
        self.calls = []
        self.sent = []
        self.close_count = 0

    async def call(self, method, **payload):
        self.calls.append((method, payload))
        if method == "getMe":
            return {"id": 99, "username": "offline_diary_bot"}
        if method == "getUpdates":
            self.poll_started.set()
            if self.block_poll:
                await asyncio.Event().wait()
            if self.updates is not None:
                updates, self.updates = self.updates, None
                return updates
            raise StopPolling
        return True

    async def send(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))

    async def close(self):
        self.close_count += 1


def message_update(user_id=101, text="今天完成了工作", update_id=1, *, chat_type="private", chat_id=None):
    return {"update_id": update_id, "message": {
        "message_id": 1000 + update_id,
        "from": {"id": user_id},
        "chat": {"id": user_id if chat_id is None else chat_id, "type": chat_type},
        "date": 1700000000, "text": text,
    }}


def callback_update(user_id=101, *, chat_type="private", chat_id=None):
    return {"update_id": 2, "callback_query": {
        "id": "callback-123", "from": {"id": user_id}, "data": "todo:confirm:7",
        "message": {"message_id": 500, "date": 1700000010,
                    "chat": {"id": user_id if chat_id is None else chat_id, "type": chat_type}},
    }}


class TelegramAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = FakeAPI()
        self.state = MemoryState()
        self.received = AsyncMock()
        self.adapter = TelegramAdapter(
            self.received, api=self.api, state_store=self.state,
            is_authorized=lambda external_id: external_id in {"101", "202"},
        )

    async def asyncTearDown(self):
        await self.adapter.stop()

    async def test_private_text_normalizes_sender_message_id_and_utc_timestamp(self):
        await self.adapter.handle_update(message_update(text="  今天完成了工作 🙂  "))
        self.received.assert_awaited_once()
        message = self.received.call_args.args[0]
        self.assertEqual((message.channel, message.external_user_id, message.message_id,
                          message.text, message.kind),
                         ("telegram", "101", "1001", "今天完成了工作 🙂", "text"))
        self.assertEqual(message.timestamp, datetime.fromtimestamp(1700000000, timezone.utc))

    async def test_group_and_mismatched_private_identity_never_route(self):
        for update in (
            message_update(chat_type="group", chat_id=-10001),
            message_update(chat_type="supergroup", chat_id=-10002),
            message_update(chat_id=202),
            callback_update(chat_type="group", chat_id=-10001),
            callback_update(chat_id=202),
        ):
            with self.subTest(update=update):
                self.assertIsNone(TelegramAdapter.normalize_update(update))
                await self.adapter.handle_update(update)
        self.received.assert_not_awaited()
        self.assertEqual(self.api.sent, [])

    async def test_authorized_private_callback_becomes_action_and_is_acknowledged(self):
        await self.adapter.handle_update(callback_update())
        message = self.received.call_args.args[0]
        self.assertEqual((message.external_user_id, message.message_id, message.kind, message.text),
                         ("101", "callback-123", "action", "todo:confirm:7"))
        self.assertEqual(message.timestamp, datetime.fromtimestamp(1700000010, timezone.utc))
        self.assertIn(("answerCallbackQuery", {"callback_query_id": "callback-123"}), self.api.calls)

    async def test_unbound_messages_and_callbacks_do_not_route(self):
        await self.adapter.handle_update(message_update(user_id=303))
        await self.adapter.handle_update(callback_update(user_id=303))
        self.assertFalse(self.adapter._is_batchable_update(message_update(user_id=303)))
        self.received.assert_not_awaited()
        self.assertEqual(self.api.sent, [])

    async def test_pairing_mode_only_reports_external_identity_without_routing(self):
        self.adapter.pairing_mode = True
        await self.adapter.handle_update(message_update(user_id=303, text="/start"))
        self.received.assert_not_awaited()
        self.assertEqual(self.api.sent[0][0], 303)
        self.assertIn("TELEGRAM_ALLOWED_USER_ID=303", self.api.sent[0][1])

    async def test_send_message_renders_shared_buttons_as_inline_keyboard(self):
        await self.adapter.send_message("101", "待确认任务", buttons=(
            (Button("确认", "todo:confirm:7"), Button("取消", "todo:cancel:7")),
        ))
        self.assertEqual(self.api.sent, [(101, "待确认任务", {"inline_keyboard": [[
            {"text": "确认", "callback_data": "todo:confirm:7"},
            {"text": "取消", "callback_data": "todo:cancel:7"},
        ]]})])

    async def test_long_unicode_sends_stay_within_telegram_utf16_limit(self):
        payloads = []

        def capture(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        api = TelegramAPI("offline-test", client=httpx.AsyncClient(transport=httpx.MockTransport(capture)))
        adapter = TelegramAdapter(self.received, api=api, state_store=self.state,
                                  is_authorized=lambda external_id: True)
        text = "🙂" * 4500 + "中文" * 1000 + "done"
        try:
            await adapter.send_message("101", text, buttons=((Button("完成", "todo:done:7"),),))
        finally:
            await adapter.stop()
        self.assertGreater(len(payloads), 1)
        self.assertEqual("".join(item["text"] for item in payloads), text)
        for item in payloads:
            with self.subTest(chunk_length=len(item["text"])):
                self.assertLessEqual(len(item["text"].encode("utf-16-le")) // 2, 4096)
                self.assertEqual(item["chat_id"], 101)
        self.assertTrue(all("reply_markup" not in item for item in payloads[:-1]))
        self.assertIn("reply_markup", payloads[-1])

    async def test_stop_cancels_polling_and_closes_once(self):
        self.api.block_poll = True
        task = asyncio.create_task(self.adapter.start())
        await asyncio.wait_for(self.api.poll_started.wait(), timeout=1)
        self.assertFalse(task.done())
        await asyncio.wait_for(self.adapter.stop(), timeout=1)
        await self.adapter.stop()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.api.close_count, 1)
        with self.assertRaisesRegex(RuntimeError, "new TelegramAdapter"):
            await self.adapter.start()

    async def test_bot_scoped_legacy_offset_is_used_when_new_store_has_none(self):
        self.adapter.legacy_state = MemoryState({"telegram_update_offset:99": "41",
                                                  "telegram_update_offset": "999"})
        with self.assertRaises(StopPolling):
            await self.adapter.start()
        poll, = [payload for method, payload in self.api.calls if method == "getUpdates"]
        self.assertEqual(poll["offset"], 41)
        self.assertEqual(self.adapter.state_key, "telegram_update_offset:99")
        self.assertIn(("deleteWebhook", {"drop_pending_updates": False}), self.api.calls)

    async def test_new_bot_scoped_offset_takes_precedence_over_legacy(self):
        self.state.set_state("telegram_update_offset:99", "73")
        self.adapter.legacy_state = MemoryState({"telegram_update_offset:99": "41"})
        with self.assertRaises(StopPolling):
            await self.adapter.start()
        poll, = [payload for method, payload in self.api.calls if method == "getUpdates"]
        self.assertEqual(poll["offset"], 73)

    async def test_two_users_burst_is_split_into_identity_scoped_batches(self):
        self.api.updates = [
            message_update(101, "A first", 1), message_update(101, "A second", 2),
            message_update(202, "B first", 3), message_update(202, "B second", 4),
            message_update(101, "A third", 5), message_update(101, "/help", 6),
        ]
        batch_handler = AsyncMock()
        self.adapter.on_batch = batch_handler
        with self.assertRaises(StopPolling):
            await self.adapter.start()
        batches = [call.args[0] for call in batch_handler.await_args_list]
        self.assertEqual([[message.text for message in batch] for batch in batches],
                         [["A first", "A second"], ["B first", "B second"], ["A third"]])
        self.assertEqual([{message.external_user_id for message in batch} for batch in batches],
                         [{"101"}, {"202"}, {"101"}])
        self.received.assert_awaited_once()
        self.assertEqual(self.received.call_args.args[0].text, "/help")
        self.assertEqual(self.state.get_state("telegram_update_offset:99"), "7")

    async def test_stop_preserves_debounced_inbox_and_restart_replays_it(self):
        pending = message_update(101, "must survive restart", 1)

        class PendingAPI(FakeAPI):
            async def call(self, method, **payload):
                if method == "getUpdates":
                    self.calls.append((method, payload))
                    if self.updates is not None:
                        updates, self.updates = self.updates, None
                        return updates
                    # Entering the second poll proves the update is already
                    # buffered, with Telegram's next offset advanced past it.
                    self.poll_started.set()
                    await asyncio.Event().wait()
                return await super().call(method, **payload)

        pending_api = PendingAPI([pending])
        adapter = TelegramAdapter(self.received, api=pending_api, state_store=self.state,
                                  is_authorized=lambda user_id: user_id == "101",
                                  message_debounce_seconds=30)
        task = asyncio.create_task(adapter.start())
        try:
            await asyncio.wait_for(pending_api.poll_started.wait(), timeout=1)
            self.received.assert_not_awaited()
            self.assertEqual(json.loads(self.state.get_state("telegram_update_offset:99:inbox")), [pending])
        finally:
            await adapter.stop()
        self.assertTrue(task.cancelled())
        self.assertEqual(json.loads(self.state.get_state("telegram_update_offset:99:inbox")), [pending])

        replay_api = FakeAPI()
        replay = TelegramAdapter(self.received, api=replay_api, state_store=self.state,
                                 is_authorized=lambda user_id: user_id == "101",
                                 message_debounce_seconds=0)
        try:
            with self.assertRaises(StopPolling):
                await replay.start()
        finally:
            await replay.stop()
        self.received.assert_awaited_once()
        self.assertEqual(self.received.call_args.args[0].text, "must survive restart")
        self.assertEqual(self.state.get_state("telegram_update_offset:99"), "2")
        self.assertEqual(json.loads(self.state.get_state("telegram_update_offset:99:inbox")), [])

import asyncio
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from diary_agent.channels.base import ChannelAdapter, IncomingMessage
from diary_agent.chat_handler import MessageProcessingError
from diary_agent.config import Settings
from diary_agent.message_router import MessageRouter
from diary_agent.service import DiaryService
from diary_agent.users import ServicePool, UserRegistry
from test_diary import FakeProvider


class RecordingAdapter(ChannelAdapter):
    def __init__(self, name, on_message):
        super().__init__(on_message)
        self.name = name
        self.sent = []
        self.stopped = asyncio.Event()

    async def start(self):
        await self.stopped.wait()

    async def stop(self):
        self.stopped.set()

    async def send_message(self, user_id, text, *, buttons=()):
        self.sent.append((user_id, text, buttons))


def incoming(channel, external_id, text, message_id='1', kind='text'):
    return IncomingMessage(channel, external_id, message_id, text,
                           datetime.now(timezone.utc), kind=kind)


class MessageRouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(root, 'Daily', root / 'diary.sqlite', ZoneInfo('UTC'),
                                 'fake', 'fake', '', '', 'Local', todo_reminders_enabled=False,
                                 history_path=root)
        self.registry = UserRegistry(root / 'identities.sqlite')
        self.pool = ServicePool(self.settings, self.registry,
                                factory=lambda settings: DiaryService(settings, provider=FakeProvider()))
        self.router = MessageRouter(self.registry, self.pool)
        self.telegram = RecordingAdapter('telegram', self.router.route)
        self.line = RecordingAdapter('line', self.router.route)
        self.router.register(self.telegram)
        self.router.register(self.line)
        self.owner = self.registry.local_user_id()
        self.other = self.registry.create_user('Other')
        self.registry.bind('telegram', '42', self.owner)
        self.registry.bind('line', 'U-owner', self.owner)
        self.registry.bind('line', '42', self.other)

    def tearDown(self):
        self.pool.close()
        self.registry.close()
        self.temp.cleanup()

    async def test_unknown_identity_or_disabled_channel_never_opens_a_service(self):
        self.assertFalse(await self.router.route(incoming('telegram', 'unknown', '/todo add secret')))
        self.registry.bind('qq', '42', self.owner)
        self.assertFalse(await self.router.route(incoming('qq', '42', '/todo add secret')))
        self.assertEqual(self.pool._services, {})
        self.assertEqual(self.telegram.sent, [])

    async def test_same_external_id_in_different_channels_has_separate_business_data(self):
        await self.router.route(incoming('telegram', '42', '/todo add owner-only'))
        await self.router.route(incoming('line', '42', '/todo add other-only'))
        self.assertEqual(self.pool.get(self.owner).todos.list()[0]['title'], 'owner-only')
        self.assertEqual(self.pool.get(self.other).todos.list()[0]['title'], 'other-only')
        self.assertEqual(self.telegram.sent[0][0], '42')
        self.assertEqual(self.line.sent[0][0], '42')

    async def test_linked_channels_share_commands_and_transcript(self):
        await self.router.route(incoming('telegram', '42', 'a quiet morning'))
        await self.router.route(incoming('line', 'U-owner', 'a quiet evening'))
        service = self.pool.get(self.owner)
        self.assertEqual([item['content'] for item in service.store.messages(service.day)
                          if item['role'] == 'user'], ['a quiet morning', 'a quiet evening'])
        self.assertNotIn(self.other, self.pool._services)

    async def test_receipts_survive_router_recreation_and_do_not_repeat_commands(self):
        message = incoming('telegram', '42', '/todo add once')
        await self.router.route(message)
        router = MessageRouter(self.registry, self.pool)
        router.register(self.telegram)
        self.assertTrue(await router.route(message))
        self.assertEqual(len(self.pool.get(self.owner).todos.list()), 1)
        self.assertEqual(len(self.telegram.sent), 1)

    async def test_batch_preserves_messages_and_removes_duplicate_ids(self):
        first = incoming('telegram', '42', 'morning', '10')
        second = incoming('telegram', '42', 'evening', '11')
        await self.router.route_many([first, first, second])
        service = self.pool.get(self.owner)
        records = service.store.messages(service.day)
        self.assertEqual([item['content'] for item in records if item['role'] == 'user'],
                         ['morning', 'evening'])
        self.assertEqual(len(self.telegram.sent), 1)

    async def test_mixed_identity_batch_rejected_before_mutation(self):
        with self.assertRaises(ValueError):
            await self.router.route_many([incoming('telegram', '42', 'one'),
                                          incoming('line', '42', 'two')])
        self.assertEqual(self.pool._services, {})

    async def test_action_id_cannot_modify_another_users_todo(self):
        todo = self.pool.get(self.owner).todos.create('owner-only')
        await self.router.route(incoming('line', '42', f"todo:done:{todo['id']}", kind='action'))
        self.assertEqual(self.pool.get(self.owner).todos.get(todo['id'])['status'], 'active')
        self.assertEqual(self.pool.get(self.other).todos.list(), [])

    async def test_text_confirmation_works_for_channels_without_inline_buttons(self):
        service = self.pool.get(self.owner)
        todo = service.todos.create('learn pottery', confirmed=False)
        await self.router.route(incoming('line', 'U-owner', f"/todo confirm {todo['id']}"))
        self.assertEqual(service.todos.get(todo['id'])['status'], 'active')

    async def test_unbound_identity_is_rejected_immediately(self):
        self.registry.unbind('telegram', '42')
        self.assertFalse(await self.router.route(incoming('telegram', '42', '/memory')))
        self.assertEqual(self.pool._services, {})

    async def test_cross_channel_requests_for_same_user_are_serialized(self):
        service = self.pool.get(self.owner)
        original = service.provider.chat
        active = 0
        maximum = 0
        lock = threading.Lock()
        def slow_chat(messages):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            result = original(messages)
            with lock:
                active -= 1
            return result
        service.provider.chat = slow_chat
        await asyncio.gather(
            self.router.route(incoming('telegram', '42', 'morning')),
            self.router.route(incoming('line', 'U-owner', 'evening')),
        )
        self.assertEqual(maximum, 1)
        self.assertEqual([item['role'] for item in service.store.messages(service.day)],
                         ['user', 'assistant', 'user', 'assistant'])

    async def test_cancellation_finishes_inflight_processing_before_returning(self):
        service = self.pool.get(self.owner)
        original = service.provider.chat
        entered = threading.Event()
        def slow_chat(messages):
            entered.set()
            time.sleep(0.03)
            return original(messages)
        service.provider.chat = slow_chat
        message = incoming('telegram', '42', 'morning')
        task = asyncio.create_task(self.router.route(message))
        await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.registry.get_state(self.router._receipt_key(message)))
        self.assertEqual(len(self.telegram.sent), 1)

    async def test_owner_history_scan_excludes_new_users_diary(self):
        other = self.pool.get(self.other)
        other.reply('other private diary')
        path = other.finalize()
        self.assertTrue(path.is_file())
        owner = self.pool.get(self.owner)
        result = owner.history_importer.scan()
        self.assertEqual(result['imported'], 0)
        self.assertEqual(owner.history_index.status()['documents'], 0)

    async def test_processing_failure_does_not_create_a_success_receipt(self):
        service = self.pool.get(self.owner)
        original = service.provider.chat
        def fail(messages):
            raise RuntimeError('temporary failure')
        service.provider.chat = fail
        message = incoming('telegram', '42', 'morning')
        with self.assertRaises(MessageProcessingError):
            await self.router.route(message)
        self.assertFalse(self.registry.get_state(self.router._receipt_key(message)))
        service.provider.chat = original
        self.assertTrue(await self.router.route(message))
        self.assertTrue(self.registry.get_state(self.router._receipt_key(message)))

    async def test_rebinding_during_model_call_prevents_old_users_reply(self):
        service = self.pool.get(self.owner)
        original = service.provider.chat
        def rebind(messages):
            self.registry.unbind('telegram', '42')
            self.registry.bind('telegram', '42', self.other)
            return original(messages)
        service.provider.chat = rebind
        with self.assertRaises(PermissionError):
            await self.router.route(incoming('telegram', '42', 'private morning'))
        self.assertEqual(self.telegram.sent, [])

    async def test_reminder_failure_does_not_block_other_recipients(self):
        self.registry.bind('telegram', '99', self.other)
        for user_id in (self.owner, self.other):
            service = self.pool.get(user_id)
            service.reminders.due = lambda recipient: 'morning'
            service.todos.create('private task')
        original = self.telegram.send_message
        async def fail_one(user_id, text, **options):
            if user_id == '42':
                raise RuntimeError('recipient blocked bot')
            await original(user_id, text, **options)
        self.telegram.send_message = fail_one
        await self.router.remind('telegram')
        self.assertTrue(self.telegram.sent)
        self.assertEqual({item[0] for item in self.telegram.sent}, {'99'})
        count = self.pool.get(self.owner).store.connection.execute(
            'SELECT COUNT(*) FROM reminder_log').fetchone()[0]
        self.assertEqual(count, 0)

    async def test_reminder_rechecks_binding_after_waiting_for_user_lock(self):
        lock = self.router._locks.setdefault(self.owner, asyncio.Lock())
        await lock.acquire()
        task = asyncio.create_task(self.router.remind('telegram'))
        await asyncio.sleep(0)
        self.registry.unbind('telegram', '42')
        self.registry.bind('telegram', '42', self.other)
        lock.release()
        await task
        self.assertEqual(self.pool._services, {})
        self.assertEqual(self.telegram.sent, [])

    def test_incoming_message_requires_timezone_and_string_identifiers(self):
        with self.assertRaises(ValueError):
            IncomingMessage('telegram', '42', '1', 'hello', datetime.now())
        with self.assertRaises(ValueError):
            incoming('telegram', 42, 'hello')


if __name__ == '__main__':
    unittest.main()

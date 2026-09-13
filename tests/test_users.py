import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from diary_agent.config import Settings
from diary_agent.service import DiaryService
from diary_agent.store import DiaryStore
from diary_agent.users import ServicePool, UserRegistry
from manage_users import main


class FakeProvider:
    def chat(self, messages):
        return "已记录"

    def json(self, messages):
        return {"title": "今日记录", "diary": "我的日记", "memories": [], "activities": []}


class UserRegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "channel_users.sqlite"
        self.registry = UserRegistry(self.path)
        self.addCleanup(self.registry.close)

    def test_local_owner_uuid_bindings_and_state_survive_restart(self):
        local_id = self.registry.local_user_id()
        self.assertEqual(uuid.UUID(local_id).hex, local_id)
        self.assertEqual(self.registry.local_user_id(), local_id)
        self.registry.bind("telegram", "123456", local_id)
        self.registry.set_state("telegram_offset", "42")
        self.registry.close()
        registry = UserRegistry(self.path)
        self.addCleanup(registry.close)
        self.assertEqual(registry.local_user_id(), local_id)
        self.assertTrue(registry.get_user(local_id)["is_local"])
        self.assertEqual(registry.resolve("telegram", "123456"), local_id)
        self.assertNotEqual(local_id, "123456")
        self.assertEqual(registry.get_state("telegram_offset"), "42")
        self.assertEqual(registry.get_state("unknown", "0"), "0")

    def test_bind_is_idempotent_but_cannot_silently_reassign_identity(self):
        first = self.registry.create_user("Alice")
        second = self.registry.create_user("Bob")
        self.registry.bind("telegram", "123", first)
        self.registry.bind("telegram", "123", first)
        with self.assertRaisesRegex(ValueError, "already bound"):
            self.registry.bind("telegram", "123", second)
        self.assertEqual(self.registry.resolve("telegram", "123"), first)
        self.assertTrue(self.registry.unbind("telegram", "123"))
        self.assertFalse(self.registry.unbind("telegram", "123"))
        self.registry.bind("telegram", "123", second)
        self.assertEqual(self.registry.resolve("telegram", "123"), second)
        self.assertIsNotNone(self.registry.get_user(first))

    def test_multi_channel_binding_and_external_ids_are_namespaced(self):
        first = self.registry.create_user()
        second = self.registry.create_user()
        self.registry.bind("telegram", "123", first)
        self.registry.bind("line", "line-user-1", first)
        self.registry.bind("qq", "123", second)
        self.assertEqual(self.registry.resolve("telegram", "123"), first)
        self.assertEqual(self.registry.resolve("line", "line-user-1"), first)
        self.assertEqual(self.registry.resolve("qq", "123"), second)
        self.assertEqual(len(self.registry.bindings()), 3)
        self.assertEqual(self.registry.bindings("line"), [{
            "channel": "line", "external_user_id": "line-user-1", "user_id": first,
        }])
        self.assertIsNone(self.registry.resolve("whatsapp", "123"))

    def test_invalid_identity_or_missing_user_is_rejected(self):
        for user_id in ("external-platform-id", "../another-user", "f" * 32):
            with self.subTest(user_id=user_id):
                with self.assertRaisesRegex(ValueError, "Unknown internal"):
                    self.registry.bind("telegram", "123", user_id)
        user_id = self.registry.create_user()
        for channel, external in (("", "123"), ("telegram", ""), ("telegram", None)):
            with self.subTest(channel=channel, external=external):
                with self.assertRaises(ValueError):
                    self.registry.bind(channel, external, user_id)
        self.assertEqual(self.registry.bindings(), [])

    def test_local_owner_and_binding_are_safe_across_connections(self):
        other = UserRegistry(self.path)
        self.addCleanup(other.close)
        with ThreadPoolExecutor(max_workers=8) as executor:
            ids = list(executor.map(
                lambda i: (self.registry if i % 2 else other).local_user_id(), range(24),
            ))
        self.assertEqual(len(set(ids)), 1)
        first, second = self.registry.create_user(), other.create_user()

        def bind(registry, user_id):
            try:
                registry.bind("telegram", "123", user_id)
                return user_id
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            attempts = [executor.submit(bind, self.registry, first),
                        executor.submit(bind, other, second)]
            successful = [result for future in attempts if (result := future.result())]
        self.assertEqual(len(successful), 1)
        self.assertEqual(self.registry.resolve("telegram", "123"), successful[0])


class ServicePoolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.registry = UserRegistry(self.root / "channel_users.sqlite")
        self.addCleanup(self.registry.close)
        self.settings = Settings(
            vault_path=self.root / "vault", journal_folder="Daily",
            database_path=self.root / "data" / "diary.sqlite", timezone=ZoneInfo("UTC"),
            provider="fake", model="fake", base_url="", api_key="", user_name="Owner",
            history_path=self.root / "history", embedding_provider="none",
        )
        self.pool = ServicePool(
            self.settings, self.registry,
            factory=lambda settings: DiaryService(settings, provider=FakeProvider()),
        )
        self.addCleanup(self.pool.close)

    def test_local_owner_reuses_existing_database_vault_and_history(self):
        store = DiaryStore(self.settings.database_path)
        store.set_state("legacy_marker", "retained")
        store.connection.close()
        owner = self.registry.local_user_id()
        service = self.pool.get(owner)
        self.assertIs(service.settings, self.settings)
        self.assertEqual(service.store.get_state("legacy_marker"), "retained")
        self.assertIsNotNone(service.history_importer)
        self.assertIs(self.pool(owner), service)

    def test_distinct_users_isolate_chat_todo_memory_activities_and_vault(self):
        first, second = self.registry.create_user("Alice"), self.registry.create_user("Bob")
        alice, bob = self.pool(first), self.pool(second)
        alice.reply("Alice 的秘密")
        alice.todos.create("Alice 的待办")
        alice.memory.remember([{"content": "Alice 喜欢写日记", "category": "preference"}], alice.day)
        alice.activities.replace_chat_summary(alice.day, [{"title": "Alice 的活动"}])
        alice.store.set_state("draft", "Alice 的草稿")
        self.assertEqual(bob.store.messages(bob.day), [])
        self.assertEqual(bob.todos.list(), [])
        self.assertEqual(bob.memory.list(), [])
        self.assertEqual(bob.activities.list_day(bob.day), [])
        self.assertEqual(bob.store.get_state("draft"), "")
        self.assertIsNone(alice.history_importer)
        self.assertIsNone(bob.history_importer)
        self.assertEqual(alice.settings.user_name, "Alice")
        self.assertEqual(alice.settings.database_path,
                         self.root / "data" / "users" / first / "diary.sqlite")
        self.assertEqual(bob.settings.database_path,
                         self.root / "data" / "users" / second / "diary.sqlite")
        self.assertEqual(
            (alice.settings.vault_path / ".chat-diary-user").read_text().strip(), first,
        )
        alice_entry = alice.finalize()
        bob.reply("Bob 的秘密")
        bob_entry = bob.finalize()
        self.assertNotEqual(alice_entry, bob_entry)
        self.assertTrue(alice_entry.is_relative_to((self.settings.vault_path / "Users" / first).resolve()))
        self.assertTrue(bob_entry.is_relative_to((self.settings.vault_path / "Users" / second).resolve()))
        self.assertEqual(alice.store.messages(alice.day)[0]["content"], "Alice 的秘密")
        self.assertEqual(bob.store.messages(bob.day)[0]["content"], "Bob 的秘密")

    def test_same_user_on_two_channels_has_one_service_and_shared_data(self):
        user = self.registry.create_user()
        self.registry.bind("telegram", "123", user)
        self.registry.bind("line", "different-external-id", user)
        first = self.pool(self.registry.resolve("telegram", "123"))
        second = self.pool(self.registry.resolve("line", "different-external-id"))
        self.assertIs(first, second)
        first.reply("从 Telegram 写入")
        self.assertEqual(second.store.messages(second.day)[0]["content"], "从 Telegram 写入")

    def test_unknown_user_and_traversal_are_rejected_before_service_creation(self):
        for user_id in ("12345", "../../outside", "f" * 32):
            with self.subTest(user_id=user_id):
                with self.assertRaisesRegex(ValueError, "Unknown internal"):
                    self.pool.get(user_id)
        self.assertFalse((self.root / "data" / "users").exists())

    def test_service_construction_is_cached_under_concurrent_access_and_close_releases_db(self):
        user = self.registry.create_user()
        with ThreadPoolExecutor(max_workers=8) as executor:
            services = list(executor.map(lambda _: self.pool(user), range(16)))
        self.assertTrue(all(service is services[0] for service in services))
        self.pool.close()
        self.pool.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            services[0].store.connection.execute("SELECT 1")
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.pool.get(user)


class UserManagementCLITests(unittest.TestCase):
    def run_cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(list(args)), 0)
        return output.getvalue().strip()

    def test_management_works_without_llm_credentials_and_uses_database_environment(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            path = Path(temporary) / "custom" / "diary.sqlite"
            os.environ["DIARY_DB_PATH"] = str(path)
            local_id = self.run_cli("local")
            self.assertTrue((path.parent / "channel_users.sqlite").exists())
            user_id = self.run_cli("create", "--name", "Alice")
            self.run_cli("bind", "--channel", "line", "--external-user-id", "u-alice",
                         "--user-id", user_id)
            output = json.loads(self.run_cli("list"))
            self.assertEqual({user["id"] for user in output["users"]}, {local_id, user_id})
            self.assertEqual(output["bindings"][0]["user_id"], user_id)
            self.run_cli("unbind", "--channel", "line", "--external-user-id", "u-alice")
            self.assertEqual(json.loads(self.run_cli("list"))["bindings"], [])
            os.environ["CHANNEL_BINDINGS_DB"] = str(Path(temporary) / "other.sqlite")
            self.assertNotEqual(self.run_cli("local"), local_id)


if __name__ == "__main__":
    unittest.main()

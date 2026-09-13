import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from diary_agent.channels.config import ChannelsConfig
from diary_agent.channels.runtime import ChannelRuntime, run_channels
from diary_agent.config import Settings
from diary_agent.service import DiaryService
from diary_agent.users import ServicePool, UserRegistry


class FakeProvider:
    def chat(self, messages):
        return "已记录"

    def json(self, messages):
        return {}


class OfflineTelegramAPI:
    """Fail at a controlled poll boundary; never open a network connection."""

    def __init__(self, *, fail_on="getUpdates"):
        self.fail_on = fail_on
        self.calls = []
        self.closed = False

    async def call(self, method, **payload):
        self.calls.append((method, payload))
        if method == self.fail_on:
            raise RuntimeError("offline transport stopped")
        if method == "getMe":
            return {"id": 777, "username": "offline_test"}
        return True

    async def send(self, chat_id, text, reply_markup=None):
        raise AssertionError("This runtime test must not send messages")

    async def close(self):
        self.closed = True


class ChannelRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.settings = Settings(
            root / "vault", "Daily", root / "data" / "diary.sqlite", ZoneInfo("UTC"),
            "fake", "fake", "", "", "Owner", embedding_provider="none",
            todo_reminders_enabled=False,
        )
        self.registry = UserRegistry(root / "channel_users.sqlite")
        self.addCleanup(self.registry.close)
        self.services = ServicePool(
            self.settings, self.registry,
            factory=lambda settings: DiaryService(settings, provider=FakeProvider()),
        )
        self.addCleanup(self.services.close)
        self.runtimes = []

    async def asyncTearDown(self):
        for runtime in self.runtimes:
            for adapter in runtime.adapters.values():
                await adapter.stop()

    def runtime(self, env):
        runtime = ChannelRuntime(self.settings, ChannelsConfig.from_env(env),
                                 registry=self.registry, services=self.services)
        self.runtimes.append(runtime)
        return runtime

    def assert_storage_closed(self, store=None):
        with self.assertRaises(sqlite3.ProgrammingError):
            self.registry.connection.execute("SELECT 1")
        if store is not None:
            with self.assertRaises(sqlite3.ProgrammingError):
                store.connection.execute("SELECT 1")

    async def test_build_constructs_only_enabled_adapters(self):
        api = OfflineTelegramAPI()
        runtime = self.runtime({
            "TELEGRAM_BOT_TOKEN": "offline-token", "TELEGRAM_ALLOWED_USER_ID": "42",
            "CHANNEL_LINE_ENABLED": "true", "LINE_CHANNEL_ACCESS_TOKEN": "offline-token",
            "LINE_CHANNEL_SECRET": "offline-secret", "QQ_APP_ID": "disabled-qq",
        })
        with patch("diary_agent.channels.runtime.TelegramAPI", return_value=api), \
                patch("diary_agent.channels.runtime.QQAdapter") as qq, \
                patch("diary_agent.channels.runtime.WeChatAdapter") as wechat, \
                patch("diary_agent.channels.runtime.WhatsAppAdapter") as whatsapp:
            runtime.build()
        self.assertEqual(set(runtime.adapters), {"telegram", "line"})
        qq.assert_not_called()
        wechat.assert_not_called()
        whatsapp.assert_not_called()
        self.assertEqual(api.calls, [])

    async def test_legacy_telegram_env_binds_local_uuid_and_reuses_original_offset(self):
        owner = self.registry.local_user_id()
        service = self.services.get(owner)
        service.store.set_state("telegram_update_offset:777", "91")
        api = OfflineTelegramAPI()
        runtime = self.runtime({
            "TELEGRAM_BOT_TOKEN": "offline-token", "TELEGRAM_ALLOWED_USER_ID": "42",
        })
        with patch("diary_agent.channels.runtime.TelegramAPI", return_value=api):
            runtime.build()
        adapter = runtime.adapters["telegram"]
        self.assertEqual(self.registry.resolve("telegram", "42"), owner)
        self.assertRegex(owner, r"^[0-9a-f]{32}$")
        self.assertNotEqual(owner, "42")
        self.assertIs(adapter.legacy_state, service.store)
        self.assertIs(adapter.state_store, self.registry)
        self.assertIs(service.settings, self.settings)
        self.assertTrue(adapter.is_authorized("42"))
        self.assertFalse(adapter.is_authorized("43"))
        with self.assertRaisesRegex(RuntimeError, "offline transport stopped"):
            await adapter.start()
        polls = [payload for method, payload in api.calls if method == "getUpdates"]
        self.assertEqual(polls[0]["offset"], 91)

    async def test_channel_offset_takes_precedence_over_legacy_owner_offset(self):
        owner = self.registry.local_user_id()
        self.services.get(owner).store.set_state("telegram_update_offset:777", "91")
        self.registry.set_state("telegram_update_offset:777", "105")
        api = OfflineTelegramAPI()
        runtime = self.runtime({"TELEGRAM_BOT_TOKEN": "offline-token"})
        with patch("diary_agent.channels.runtime.TelegramAPI", return_value=api):
            runtime.build()
        with self.assertRaisesRegex(RuntimeError, "offline transport stopped"):
            await runtime.adapters["telegram"].start()
        polls = [payload for method, payload in api.calls if method == "getUpdates"]
        self.assertEqual(polls[0]["offset"], 105)

    async def test_telegram_startup_failure_closes_transport_and_open_storage(self):
        owner = self.registry.local_user_id()
        store = self.services.get(owner).store
        api = OfflineTelegramAPI(fail_on="getMe")
        runtime = self.runtime({"TELEGRAM_BOT_TOKEN": "offline-token"})
        with patch("diary_agent.channels.runtime.TelegramAPI", return_value=api):
            with self.assertRaisesRegex(RuntimeError, "offline transport stopped"):
                await runtime.run()
        self.assertTrue(api.closed)
        self.assert_storage_closed(store)

    async def test_later_adapter_construction_failure_closes_previously_opened_transport(self):
        owner = self.registry.local_user_id()
        store = self.services.get(owner).store
        api = OfflineTelegramAPI()
        runtime = self.runtime({
            "TELEGRAM_BOT_TOKEN": "offline-token", "CHANNEL_LINE_ENABLED": "true",
            "LINE_CHANNEL_ACCESS_TOKEN": "offline-token", "LINE_CHANNEL_SECRET": "offline-secret",
        })
        with patch("diary_agent.channels.runtime.TelegramAPI", return_value=api), \
                patch("diary_agent.channels.runtime.LineAdapter",
                      side_effect=ValueError("invalid LINE configuration")):
            with self.assertRaisesRegex(ValueError, "invalid LINE configuration"):
                await runtime.run()
        self.assertEqual(api.calls, [])
        self.assertTrue(api.closed)
        self.assert_storage_closed(store)

    async def test_service_initialization_failure_does_not_leak_telegram_client(self):
        api = OfflineTelegramAPI()
        runtime = self.runtime({"TELEGRAM_BOT_TOKEN": "offline-token"})
        with patch("diary_agent.channels.runtime.TelegramAPI", return_value=api) as api_factory, \
                patch.object(self.services, "get", side_effect=RuntimeError("service unavailable")):
            with self.assertRaisesRegex(RuntimeError, "service unavailable"):
                await runtime.run()
        # Either create the service before opening transport, or explicitly close
        # the opened client if adapter construction cannot complete.
        if api_factory.called:
            self.assertTrue(api.closed)
        self.assert_storage_closed()

    async def assert_stub_fails_before_telegram(self, name, credentials):
        runtime = self.runtime({
            "TELEGRAM_BOT_TOKEN": "offline-token", f"CHANNEL_{name.upper()}_ENABLED": "true",
            **credentials,
        })
        with patch("diary_agent.channels.runtime.TelegramAPI") as api:
            with self.assertRaisesRegex(NotImplementedError, "not connected"):
                await runtime.run()
        api.assert_not_called()
        self.assertEqual(runtime.adapters, {})
        self.assert_storage_closed()

    async def test_wechat_fails_before_opening_any_telegram_transport(self):
        await self.assert_stub_fails_before_telegram("wechat", {
            "WECHAT_APP_ID": "offline-app", "WECHAT_APP_SECRET": "offline-secret",
            "WECHAT_TOKEN": "offline-token",
        })

    async def test_qq_fails_before_opening_any_telegram_transport(self):
        await self.assert_stub_fails_before_telegram("qq", {
            "QQ_APP_ID": "offline-app", "QQ_APP_SECRET": "offline-secret",
        })

    async def test_no_enabled_channels_is_a_clear_error_and_closes_storage(self):
        runtime = self.runtime({"CHANNEL_TELEGRAM_ENABLED": "false"})
        with self.assertRaisesRegex(ValueError, "No chat channels are enabled"):
            await runtime.run()
        self.assert_storage_closed()

    async def test_webhook_server_failure_closes_webhook_clients_and_storage(self):
        runtime = self.runtime({
            "CHANNEL_TELEGRAM_ENABLED": "false", "CHANNEL_LINE_ENABLED": "true",
            "LINE_CHANNEL_ACCESS_TOKEN": "offline-token", "LINE_CHANNEL_SECRET": "offline-secret",
        })
        server = SimpleNamespace(should_exit=False,
                                 serve=AsyncMock(side_effect=RuntimeError("offline server failure")))
        with patch("diary_agent.channels.runtime.uvicorn.Server", return_value=server):
            with self.assertRaisesRegex(RuntimeError, "offline server failure"):
                await runtime.run()
        self.assertTrue(server.should_exit)
        self.assertFalse(runtime.adapters["line"].running)
        self.assertTrue(runtime.adapters["line"].client.is_closed)
        self.assert_storage_closed()

    async def test_invalid_webhook_port_closes_constructed_clients_without_starting_server(self):
        runtime = self.runtime({
            "CHANNEL_TELEGRAM_ENABLED": "false", "CHANNEL_LINE_ENABLED": "true",
            "LINE_CHANNEL_ACCESS_TOKEN": "offline-token", "LINE_CHANNEL_SECRET": "offline-secret",
        })
        with patch.dict(os.environ, {"CHANNEL_WEBHOOK_PORT": "invalid"}), \
                patch("diary_agent.channels.runtime.uvicorn.Server") as server:
            with self.assertRaisesRegex(ValueError, "must be an integer"):
                await runtime.run()
        server.assert_not_called()
        self.assertTrue(runtime.adapters["line"].client.is_closed)
        self.assert_storage_closed()

    async def test_telegram_entry_point_ignores_other_enabled_channels_credentials(self):
        env = {"TELEGRAM_BOT_TOKEN": "offline-token", "CHANNEL_LINE_ENABLED": "true",
               "CHANNEL_WHATSAPP_ENABLED": "true", "CHANNEL_WECHAT_ENABLED": "true",
               "CHANNEL_QQ_ENABLED": "true"}
        run = AsyncMock()
        with patch.dict(os.environ, env), \
                patch("diary_agent.channels.runtime.Settings.from_env", return_value=self.settings), \
                patch("diary_agent.channels.runtime.ChannelRuntime",
                      return_value=SimpleNamespace(run=run)) as runtime_factory:
            await run_channels(only="telegram")
            self.assertEqual(os.environ["CHANNEL_LINE_ENABLED"], "true")
        config = runtime_factory.call_args.args[1]
        self.assertEqual([name for name, channel in config.channels.items() if channel.enabled],
                         ["telegram"])
        run.assert_awaited_once()

    async def test_unknown_single_channel_is_rejected_before_loading_llm_settings(self):
        with patch("diary_agent.channels.runtime.Settings.from_env") as settings:
            with self.assertRaisesRegex(ValueError, "Unknown channel"):
                await run_channels(only="unknown")
        settings.assert_not_called()


if __name__ == "__main__":
    unittest.main()

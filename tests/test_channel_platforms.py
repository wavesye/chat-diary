import asyncio
import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from diary_agent.channels.base import Button
from diary_agent.channels.config import ChannelsConfig
from diary_agent.channels.line import LineAdapter
from diary_agent.channels.qq import QQAdapter
from diary_agent.channels.wechat import WeChatAdapter
from diary_agent.channels.webhook import WebhookSignatureError, create_webhook_app
from diary_agent.channels.whatsapp import WhatsAppAdapter


class ChannelConfigTests(unittest.TestCase):
    def test_defaults_keep_only_telegram_and_require_only_its_credentials(self):
        config = ChannelsConfig.from_env({"TELEGRAM_BOT_TOKEN": "test-secret"})
        self.assertEqual([item.name for item in config.channels.values() if item.enabled], ["telegram"])
        self.assertNotIn("test-secret", repr(config))
        self.assertEqual(config.channels["telegram"].credentials["message_debounce_seconds"], "3")

    def test_env_switches_override_json_and_disabled_credentials_are_not_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channels.json"
            path.write_text(json.dumps({"channels": {"telegram": {"enabled": True},
                                                    "line": {"enabled": False}}}))
            config = ChannelsConfig.from_env({
                "CHANNELS_CONFIG": str(path), "CHANNEL_TELEGRAM_ENABLED": "false",
                "TELEGRAM_ALLOWED_USER_ID": "invalid-but-disabled",
                "CHANNEL_LINE_ENABLED": "true", "LINE_CHANNEL_ACCESS_TOKEN": "test-token",
                "LINE_CHANNEL_SECRET": "test-secret",
            })
            self.assertFalse(config.channels["telegram"].enabled)
            self.assertTrue(config.channels["line"].enabled)

    def test_json_cannot_contain_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channels.json"
            path.write_text(json.dumps({"channels": {"line": {"channel_secret": "secret"}}}))
            with self.assertRaisesRegex(ValueError, "environment variables"):
                ChannelsConfig.from_env({"CHANNELS_CONFIG": str(path), "CHANNEL_TELEGRAM_ENABLED": "false"})

    def test_invalid_switches_and_missing_credentials_fail_clearly(self):
        for env, expected in [
            ({}, "TELEGRAM_BOT_TOKEN"),
            ({"CHANNEL_TELEGRAM_ENABLED": "maybe"}, "must be true or false"),
            ({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_MESSAGE_DEBOUNCE_SECONDS": "nan"}, "between 0 and 30"),
            ({"CHANNEL_TELEGRAM_ENABLED": "false", "CHANNEL_LINE_ENABLED": "true"}, "LINE_CHANNEL_ACCESS_TOKEN"),
        ]:
            with self.subTest(env=env), self.assertRaisesRegex(ValueError, expected):
                ChannelsConfig.from_env(env)

    def test_whatsapp_api_version_is_explicit(self):
        env = {"CHANNEL_TELEGRAM_ENABLED": "false", "CHANNEL_WHATSAPP_ENABLED": "true",
               "WHATSAPP_ACCESS_TOKEN": "token", "WHATSAPP_PHONE_NUMBER_ID": "123",
               "WHATSAPP_APP_SECRET": "secret", "WHATSAPP_VERIFY_TOKEN": "verify"}
        with self.assertRaisesRegex(ValueError, "WHATSAPP_API_VERSION"):
            ChannelsConfig.from_env(env)
        env["WHATSAPP_API_VERSION"] = "v123.0"
        self.assertTrue(ChannelsConfig.from_env(env).channels["whatsapp"].enabled)


class PlatformAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.received = AsyncMock()

        def capture(request):
            self.requests.append(request)
            return httpx.Response(200, json={"messages": [{"id": "sent-1"}]})

        self.line = LineAdapter(self.received, channel_access_token="line-token",
                                channel_secret="line-secret",
                                client=httpx.AsyncClient(transport=httpx.MockTransport(capture)))
        self.whatsapp = WhatsAppAdapter(
            self.received, access_token="wa-token", phone_number_id="123",
            app_secret="wa-secret", verify_token="wa-verify", api_version="v123.0",
            client=httpx.AsyncClient(transport=httpx.MockTransport(capture)),
        )

    async def asyncTearDown(self):
        await self.line.stop()
        await self.whatsapp.stop()

    @staticmethod
    def line_payload(*, source_type="user"):
        return json.dumps({"events": [{"type": "message", "timestamp": 1700000000000,
                         "source": {"type": source_type, "userId": "line-user"},
                         "message": {"type": "text", "id": "line-message", "text": "今天很好🙂"}}]},
                          ensure_ascii=False).encode()

    @staticmethod
    def line_headers(payload):
        signature = base64.b64encode(hmac.new(b"line-secret", payload, hashlib.sha256).digest()).decode()
        return {"X-Line-Signature": signature}

    @staticmethod
    def whatsapp_payload(*, phone_number_id="123", message_type="text"):
        return json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{
            "field": "messages", "value": {"metadata": {"phone_number_id": phone_number_id},
            "messages": [{"from": "8613800000000", "id": "wamid.example", "timestamp": "1700000000",
                          "type": message_type, "text": {"body": "今天很好"}}]},
        }]}]}, ensure_ascii=False).encode()

    @staticmethod
    def whatsapp_headers(payload):
        return {"X-Hub-Signature-256": "sha256=" + hmac.new(b"wa-secret", payload, hashlib.sha256).hexdigest()}

    async def test_line_normalizes_signed_text_and_rejects_tampered_body(self):
        payload = self.line_payload()
        message, = self.line.parse_request(payload, self.line_headers(payload))
        self.assertEqual((message.channel, message.external_user_id, message.message_id, message.text),
                         ("line", "line-user", "line-message", "今天很好🙂"))
        self.assertEqual(message.timestamp, datetime.fromtimestamp(1700000000, timezone.utc))
        with self.assertRaises(WebhookSignatureError):
            self.line.parse_request(payload + b" ", self.line_headers(payload))
        with self.assertRaises(WebhookSignatureError):
            self.line.parse_request(b"invalid JSON", {})

    async def test_line_does_not_route_group_journals_and_accepts_empty_verification(self):
        for payload in (self.line_payload(source_type="group"), b'{"events":[]}'):
            self.assertEqual(self.line.parse_request(payload, self.line_headers(payload)), [])

    async def test_line_push_splits_long_text_and_renders_action_commands(self):
        await self.line.send_message("line-user", "x" * 12000,
                                     buttons=((Button("确认", "todo:confirm:7"),),))
        self.assertEqual(len(self.requests), 2)
        first = self.requests[0]
        self.assertEqual(str(first.url), "https://api.line.me/v2/bot/message/push")
        self.assertEqual(first.headers["authorization"], "Bearer line-token")
        self.assertEqual(json.loads(first.content)["to"], "line-user")
        combined = "".join(message["text"] for request in self.requests
                           for message in json.loads(request.content)["messages"])
        self.assertIn("/todo confirm 7", combined)
        self.assertTrue(combined.startswith("x" * 12000))

    async def test_whatsapp_normalizes_text_and_checks_account(self):
        payload = self.whatsapp_payload()
        message, = self.whatsapp.parse_request(payload, self.whatsapp_headers(payload))
        self.assertEqual((message.channel, message.external_user_id, message.message_id),
                         ("whatsapp", "8613800000000", "wamid.example"))
        self.assertEqual(message.timestamp, datetime.fromtimestamp(1700000000, timezone.utc))
        for payload in (self.whatsapp_payload(phone_number_id="999"),
                        self.whatsapp_payload(message_type="image")):
            self.assertEqual(self.whatsapp.parse_request(payload, self.whatsapp_headers(payload)), [])
        with self.assertRaises(WebhookSignatureError):
            self.whatsapp.parse_request(payload, {"X-Hub-Signature-256": "sha256=bad"})

    async def test_whatsapp_handshake_and_send(self):
        self.assertEqual(self.whatsapp.verify_challenge({"hub.mode": "subscribe",
                         "hub.verify_token": "wa-verify", "hub.challenge": "123456"}), "123456")
        with self.assertRaises(WebhookSignatureError):
            self.whatsapp.verify_challenge({"hub.mode": "subscribe", "hub.verify_token": "wrong",
                                            "hub.challenge": "123456"})
        await self.whatsapp.send_message("8613800000000", "reply",
                                        buttons=((Button("明天", "todo:tomorrow:7"),),))
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://graph.facebook.com/v123.0/123/messages")
        self.assertEqual(request.headers["authorization"], "Bearer wa-token")
        self.assertEqual(json.loads(request.content)["messaging_product"], "whatsapp")
        self.assertIn("/todo postpone 7 明天", json.loads(request.content)["text"]["body"])

    async def test_signed_malformed_payloads_raise_value_error(self):
        for adapter, signer in ((self.line, self.line_headers), (self.whatsapp, self.whatsapp_headers)):
            for payload in (b"no json", b"[]", b"null", b"{}"):
                with self.subTest(channel=adapter.name, payload=payload), self.assertRaises(ValueError):
                    adapter.parse_request(payload, signer(payload))

    async def test_delivery_errors_do_not_echo_provider_body_or_credentials(self):
        await self.line.client.aclose()
        self.line.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": "line-token secret-body"})))
        with self.assertRaisesRegex(RuntimeError, "HTTP 401") as captured:
            await self.line.send_message("line-user", "test")
        self.assertNotIn("line-token", str(captured.exception))
        self.assertNotIn("secret-body", str(captured.exception))

    async def test_lifecycle_runs_until_stopped(self):
        task = asyncio.create_task(self.line.start())
        await asyncio.sleep(0)
        self.assertTrue(self.line.running)
        self.assertFalse(task.done())
        await self.line.stop()
        await task
        self.assertFalse(self.line.running)

    async def test_unfinished_platforms_fail_fast(self):
        for adapter in (WeChatAdapter(self.received), QQAdapter(self.received)):
            with self.subTest(channel=adapter.name), self.assertRaises(NotImplementedError):
                await adapter.start()
            with self.assertRaises(NotImplementedError):
                await adapter.send_message("user", "text")
            await adapter.stop()

    async def test_webhook_ingress_authenticates_before_routing_and_hides_errors(self):
        app = create_webhook_app({"line": self.line, "whatsapp": self.whatsapp})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            payload = self.line_payload()
            response = await client.post("/webhooks/line", content=payload)
            self.assertEqual(response.status_code, 403)
            self.received.assert_not_awaited()
            response = await client.post("/webhooks/line", content=payload, headers=self.line_headers(payload))
            self.assertEqual(response.status_code, 200)
            self.received.assert_awaited_once()
            self.assertEqual(self.received.call_args.args[0].channel, "line")
            response = await client.post("/webhooks/wechat", content=payload)
            self.assertEqual(response.status_code, 404)
            response = await client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe",
                                        "hub.verify_token": "wa-verify", "hub.challenge": "challenge"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "challenge")
            self.received.side_effect = RuntimeError("secret-internal-error")
            response = await client.post("/webhooks/line", content=payload, headers=self.line_headers(payload))
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("secret-internal-error", response.text)

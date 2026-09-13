"""LINE Messaging API: private text webhooks and push replies.

Official protocol: https://developers.line.biz/en/reference/messaging-api/
TODO: reply-token delivery, rich media, and interactive postback events.
Push messages require an eligible recipient and consume the account's quota.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping

from .base import IncomingMessage
from .webhook import WebhookChannelAdapter, WebhookSignatureError, button_text, event_timestamp, header_value


class LineAdapter(WebhookChannelAdapter):
    name = "line"

    def __init__(self, on_message, *, channel_access_token: str,
                 channel_secret: str, client=None):
        if not channel_access_token or not channel_secret:
            raise ValueError("LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET are required")
        super().__init__(on_message, client=client)
        self.channel_access_token = channel_access_token
        self.channel_secret = channel_secret

    def parse_request(self, raw_body: bytes, headers: Mapping[str, str]) -> list[IncomingMessage]:
        supplied = header_value(headers, "x-line-signature")
        expected = base64.b64encode(hmac.new(
            self.channel_secret.encode(), raw_body, hashlib.sha256,
        ).digest()).decode("ascii")
        if not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise WebhookSignatureError("Invalid LINE webhook signature")
        try:
            document = json.loads(raw_body)
        except (ValueError, UnicodeError):
            raise ValueError("Invalid LINE webhook JSON") from None
        if not isinstance(document, dict) or not isinstance(document.get("events"), list):
            raise ValueError("Invalid LINE webhook events")
        messages = []
        for event in document["events"]:
            if not isinstance(event, dict):
                raise ValueError("Invalid LINE event")
            if event.get("type") != "message":
                continue
            source, content = event.get("source"), event.get("message")
            if not isinstance(source, dict) or not isinstance(content, dict):
                raise ValueError("Invalid LINE message")
            # Journals are private: never answer a group with personal history.
            if source.get("type") != "user" or content.get("type") != "text":
                continue
            user_id, message_id, text = source.get("userId"), content.get("id"), content.get("text")
            if not all(isinstance(value, str) and value for value in (user_id, message_id, text)):
                raise ValueError("Invalid LINE text fields")
            messages.append(IncomingMessage(
                channel=self.name, external_user_id=user_id, message_id=message_id,
                text=text, timestamp=event_timestamp(event.get("timestamp"), milliseconds=True),
            ))
        return messages

    async def send_message(self, user_id: str, text: str, *, buttons=()) -> None:
        text = button_text(text or "（没有可发送的内容）", buttons)
        chunks = [{"type": "text", "text": text[start:start + 2000]}
                  for start in range(0, len(text), 2000)]
        # At most five message objects per push request. The conservative chunk
        # size also remains below LINE's limit when every character is astral.
        for start in range(0, len(chunks), 5):
            await self._post(
                "https://api.line.me/v2/bot/message/push", token=self.channel_access_token,
                payload={"to": user_id, "messages": chunks[start:start + 5]},
            )

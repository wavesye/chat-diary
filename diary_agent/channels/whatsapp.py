"""WhatsApp Cloud API text transport with verified Meta webhooks.

Official examples: https://github.com/fbsamples/whatsapp-api-examples
TODO: approved templates for proactive delivery outside the customer-service
window, rich media, interactive replies, and provider account onboarding.
The operator must select a currently supported Graph API version explicitly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping

from .base import IncomingMessage
from .webhook import WebhookChannelAdapter, WebhookSignatureError, button_text, event_timestamp, header_value


class WhatsAppAdapter(WebhookChannelAdapter):
    name = "whatsapp"

    def __init__(self, on_message, *, access_token: str, phone_number_id: str,
                 app_secret: str, verify_token: str, api_version: str, client=None):
        if not all((access_token, phone_number_id, app_secret, verify_token, api_version)):
            raise ValueError("WhatsApp access token, phone number ID, app secret, verify token and API version are required")
        if not re.fullmatch(r"v[0-9]+\.[0-9]+", api_version) or not phone_number_id.isdigit():
            raise ValueError("Invalid WHATSAPP_API_VERSION or WHATSAPP_PHONE_NUMBER_ID")
        super().__init__(on_message, client=client)
        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.app_secret = app_secret
        self.verify_token = verify_token
        self.api_version = api_version

    def verify_challenge(self, params: Mapping[str, str]) -> str:
        supplied = params.get("hub.verify_token", "")
        if (params.get("hub.mode") != "subscribe" or not supplied
                or not hmac.compare_digest(supplied.encode(), self.verify_token.encode())
                or not params.get("hub.challenge")):
            raise WebhookSignatureError("Invalid WhatsApp webhook verification")
        return params["hub.challenge"]

    def parse_request(self, raw_body: bytes, headers: Mapping[str, str]) -> list[IncomingMessage]:
        supplied = header_value(headers, "x-hub-signature-256")
        expected = "sha256=" + hmac.new(self.app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise WebhookSignatureError("Invalid WhatsApp webhook signature")
        try:
            document = json.loads(raw_body)
        except (ValueError, UnicodeError):
            raise ValueError("Invalid WhatsApp webhook JSON") from None
        if (not isinstance(document, dict) or document.get("object") != "whatsapp_business_account"
                or not isinstance(document.get("entry"), list)):
            raise ValueError("Invalid WhatsApp webhook envelope")
        messages = []
        for entry in document["entry"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("changes"), list):
                raise ValueError("Invalid WhatsApp webhook entry")
            for change in entry["changes"]:
                if not isinstance(change, dict):
                    raise ValueError("Invalid WhatsApp webhook change")
                if change.get("field") != "messages":
                    continue
                value = change.get("value")
                if not isinstance(value, dict) or not isinstance(value.get("metadata"), dict):
                    raise ValueError("Invalid WhatsApp webhook message metadata")
                # A Meta app can receive multiple business phone numbers. Do not
                # accept another account's events on this adapter's credentials.
                if value["metadata"].get("phone_number_id") != self.phone_number_id:
                    continue
                items = value.get("messages", [])
                if not isinstance(items, list):
                    raise ValueError("Invalid WhatsApp messages")
                for item in items:
                    if not isinstance(item, dict):
                        raise ValueError("Invalid WhatsApp message")
                    if item.get("type") != "text":
                        continue
                    content = item.get("text")
                    if not isinstance(content, dict):
                        raise ValueError("Invalid WhatsApp text message")
                    user_id, message_id, text = item.get("from"), item.get("id"), content.get("body")
                    if not all(isinstance(field, str) and field for field in (user_id, message_id, text)):
                        raise ValueError("Invalid WhatsApp text fields")
                    messages.append(IncomingMessage(
                        channel=self.name, external_user_id=user_id, message_id=message_id,
                        text=text, timestamp=event_timestamp(item.get("timestamp")),
                    ))
        return messages

    async def send_message(self, user_id: str, text: str, *, buttons=()) -> None:
        text = button_text(text or "（没有可发送的内容）", buttons)
        for start in range(0, len(text), 2000):
            await self._post(
                f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages",
                token=self.access_token,
                payload={"messaging_product": "whatsapp", "recipient_type": "individual",
                         "to": user_id, "type": "text",
                         "text": {"body": text[start:start + 2000], "preview_url": False}},
            )

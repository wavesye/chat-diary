"""Official Telegram Bot API transport (private chats, long polling)."""
from __future__ import annotations

import asyncio
import json
import math
import sys
from datetime import datetime, timezone

import httpx

from .base import ChannelAdapter, IncomingMessage, MessageProcessingError

class TelegramAPIError(RuntimeError):
    def __init__(self, description: str, error_code: int | None = None,
                 retry_after: int | None = None):
        super().__init__(description)
        self.error_code = error_code
        self.retry_after = retry_after


class TelegramAPI:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(45, connect=15))

    @staticmethod
    def is_retryable(error: Exception) -> bool:
        if isinstance(error, httpx.RequestError):
            return True
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code == 429 or error.response.status_code >= 500
        if isinstance(error, TelegramAPIError):
            return error.error_code == 429 or bool(
                error.error_code and error.error_code >= 500
            )
        return False

    async def call(self, method: str, *, retry_attempts: int = 3, **payload):
        attempts = max(1, retry_attempts)
        for attempt in range(attempts):
            try:
                response = await self.client.post(
                    f"{self.base_url}/{method}", json=payload
                )
                try:
                    data = response.json()
                except ValueError:
                    response.raise_for_status()
                    raise TelegramAPIError(f"Telegram {method} 返回了无效响应")
                if not data.get("ok"):
                    parameters = data.get("parameters") or {}
                    raise TelegramAPIError(
                        data.get("description", f"Telegram {method} failed"),
                        error_code=data.get("error_code") or response.status_code,
                        retry_after=parameters.get("retry_after"),
                    )
                response.raise_for_status()
                return data.get("result")
            except Exception as error:
                if attempt + 1 >= attempts or not self.is_retryable(error):
                    raise
                retry_after = getattr(error, "retry_after", None)
                delay = min(max(float(retry_after or 2 ** attempt), 1), 30)
                print(
                    f"Telegram 请求 {method} 暂时失败（{type(error).__name__}），"
                    f"{delay:g} 秒后重试",
                    file=sys.stderr, flush=True,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def send(self, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
        text = text or "（没有可发送的内容）"
        # 2,000 Unicode code points fit even if each consumes two UTF-16 units.
        for start in range(0, len(text), 2000):
            payload = {"chat_id": chat_id, "text": text[start:start + 2000]}
            if reply_markup and start + 2000 >= len(text):
                payload["reply_markup"] = reply_markup
            await self.call("sendMessage", **payload)

    async def close(self) -> None:
        await self.client.aclose()


class TelegramAdapter(ChannelAdapter):
    name = "telegram"

    def __init__(self, on_message, *, api: TelegramAPI, state_store,
                 is_authorized, on_batch=None, on_tick=None,
                 pairing_mode: bool = False, message_debounce_seconds: float = 3.0,
                 legacy_state=None):
        super().__init__(on_message)
        if not math.isfinite(message_debounce_seconds) or not 0 <= message_debounce_seconds <= 30:
            raise ValueError("TELEGRAM_MESSAGE_DEBOUNCE_SECONDS 必须是 0 到 30 之间的数字")
        self.api = api
        self.state_store = state_store
        self.legacy_state = legacy_state
        self.is_authorized = is_authorized
        self.on_batch = on_batch
        self.on_tick = on_tick
        self.pairing_mode = pairing_mode
        self.message_debounce_seconds = message_debounce_seconds
        self.state_key = "telegram_update_offset"
        self.offset = 0
        self._task = None
        self._closed = False
        self._inbox: list[dict] = []

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Create a new TelegramAdapter after stop()")
        self._task = asyncio.current_task()
        try:
            await self.run()
        finally:
            self._task = None

    async def stop(self) -> None:
        if self._task and self._task is not asyncio.current_task():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if not self._closed:
            self._closed = True
            await self.api.close()

    async def send_message(self, user_id: str, text: str, *, buttons=()) -> None:
        markup = None
        if buttons:
            markup = {"inline_keyboard": [
                [{"text": button.text, "callback_data": button.value} for button in row]
                for row in buttons
            ]}
        await self.api.send(int(user_id), text, reply_markup=markup)

    async def show_typing(self, user_id: str) -> None:
        await self.api.call("sendChatAction", retry_attempts=1,
                            chat_id=int(user_id), action="typing")

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        return TelegramAPI.is_retryable(error)

    @staticmethod
    def normalize_update(update: dict) -> IncomingMessage | None:
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        sender = (callback or message).get("from", {}).get("id")
        chat = message.get("chat", {})
        # A personal diary must never answer a private command in a group chat.
        if (chat.get("type") != "private" or not isinstance(sender, int)
                or sender != chat.get("id")):
            return None
        text = callback.get("data") if callback else message.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        raw_id = callback.get("id") if callback else message.get("message_id")
        if raw_id is None:
            raw_id = update.get("update_id")
        if raw_id is None:
            return None
        timestamp = datetime.fromtimestamp(message.get("date", 0), timezone.utc)
        return IncomingMessage("telegram", str(sender), str(raw_id), text.strip(),
                               timestamp, kind="action" if callback else "text")

    def _is_batchable_update(self, update: dict) -> bool:
        message = self.normalize_update(update)
        return bool(self.message_debounce_seconds > 0 and message
                    and self.is_authorized(message.external_user_id)
                    and message.kind == "text" and not message.text.startswith("/"))

    async def handle_update(self, update: dict) -> None:
        message = self.normalize_update(update)
        callback_id = update.get("callback_query", {}).get("id")
        if callback_id:
            # Clear the client spinner before potentially slow model or disk work.
            await self.api.call("answerCallbackQuery", callback_query_id=callback_id)
        if message is None:
            return
        if not self.is_authorized(message.external_user_id):
            if self.pairing_mode and message.kind == "text":
                await self.send_message(
                    message.external_user_id,
                    f"你的 Telegram user ID 是 {message.external_user_id}。\n"
                    "把它填入 .env：\nTELEGRAM_ALLOWED_USER_ID=" + message.external_user_id
                    + "\n或请管理员通过 manage_users.py 绑定此身份。",
                )
            return
        try:
            await self.on_message(message)
        except MessageProcessingError:
            # Preserve the personal bot's explicit retry UX after a friendly
            # error response. Unlike webhooks, long polling has no redelivery SLA.
            pass

    def _ack_update(self, update: dict) -> None:
        offset = int(update["update_id"]) + 1
        self.state_store.set_state(self.state_key, str(offset))
        self._inbox = [item for item in self._inbox if int(item["update_id"]) >= offset]
        self.state_store.set_state(self.state_key + ":inbox", json.dumps(self._inbox))

    def _save_inbox(self, updates: list[dict]) -> None:
        by_id = {int(item["update_id"]): item for item in self._inbox}
        by_id.update((int(item["update_id"]), item) for item in updates)
        self._inbox = [by_id[key] for key in sorted(by_id)]
        self.state_store.set_state(self.state_key + ":inbox", json.dumps(self._inbox))

    async def _flush_message_updates(self, updates: list[dict]) -> None:
        messages = [self.normalize_update(update) for update in updates]
        messages = [message for message in messages if message is not None]
        if messages:
            try:
                if self.on_batch:
                    await self.on_batch(messages)
                else:
                    for message in messages:
                        await self.on_message(message)
            except MessageProcessingError:
                pass
            self._ack_update(updates[-1])

    async def _maybe_send_reminder(self) -> None:
        if self.on_tick:
            await self.on_tick()

    async def _call_until_connected(self, method: str, **payload):
        retry_delay = 5
        while True:
            try:
                return await self.api.call(method, **payload)
            except Exception as error:
                if not self._is_retryable(error):
                    raise
                print(
                    f"Telegram 连接中断：{type(error).__name__}；{retry_delay} 秒后重试",
                    file=sys.stderr, flush=True,
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

    async def run(self) -> None:
        me = await self._call_until_connected("getMe")
        self.state_key = f"telegram_update_offset:{me['id']}"
        self.offset = int(self.state_store.get_state(self.state_key, self.legacy_state.get_state(self.state_key, "0") if self.legacy_state else "0") or 0)
        restored = json.loads(self.state_store.get_state(self.state_key + ":inbox", "[]") or "[]")
        self._inbox = list(restored)
        await self._call_until_connected("deleteWebhook", drop_pending_updates=False)
        print(f"Telegram Bot @{me.get('username', '')} 已启动")
        if self.pairing_mode:
            print("当前为配对模式：给 Bot 发送 /start 获取你的 user ID")
        elif self.message_debounce_seconds > 0:
            print(
                f"普通消息会在静默 {self.message_debounce_seconds:g} 秒后合并回复"
            )
        retry_delay = 5
        pending_updates: list[dict] = []
        pending_deadline: float | None = None
        while not self._closed:
            try:
                await self._maybe_send_reminder()
                loop = asyncio.get_running_loop()
                if (
                    pending_updates and pending_deadline is not None
                    and loop.time() >= pending_deadline
                ):
                    await self._flush_message_updates(pending_updates)
                    pending_updates = []
                    pending_deadline = None
                poll_timeout = 30
                if pending_deadline is not None:
                    poll_timeout = max(
                        1, min(30, math.ceil(pending_deadline - loop.time()))
                    )
                if restored:
                    updates, restored = restored, []
                else:
                    updates = await self.api.call(
                        "getUpdates", offset=self.offset, timeout=poll_timeout,
                        allowed_updates=["message", "callback_query"],
                        retry_attempts=1,
                    )
                    # Telegram confirms updates as soon as the next request's
                    # offset advances. Persist bursts before that can happen.
                    if updates:
                        self._save_inbox(updates)
                if retry_delay > 5:
                    print("Telegram 连接已恢复")
                retry_delay = 5
                for update in updates:
                    self.offset = int(update["update_id"]) + 1
                    if self._is_batchable_update(update):
                        chat_id = update["message"]["chat"]["id"]
                        if pending_updates and (
                            pending_updates[0]["message"]["chat"]["id"] != chat_id
                            or pending_updates[0]["message"]["from"]["id"] != update["message"]["from"]["id"]
                        ):
                            await self._flush_message_updates(pending_updates)
                            pending_updates = []
                        pending_updates.append(update)
                        pending_deadline = (
                            loop.time() + self.message_debounce_seconds
                        )
                        continue
                    if pending_updates:
                        await self._flush_message_updates(pending_updates)
                        pending_updates = []
                        pending_deadline = None
                    await self.handle_update(update)
                    self._ack_update(update)
            except Exception as error:
                if not self._is_retryable(error):
                    raise
                print(
                    f"Telegram 连接中断：{type(error).__name__}；{retry_delay} 秒后重试",
                    file=sys.stderr,
                    flush=True,
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
                continue

"""Personal Telegram adapter using the official Bot API long polling."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

from diary_agent.config import Settings
from diary_agent.service import DiaryService


HELP = (
    "直接发消息就可以开始记录。\n\n"
    "/preview — 预览今天的日记\n"
    "/done — 写入 Obsidian\n"
    "/memory — 查看长期记忆\n"
    "/help — 查看帮助"
)


class TelegramAPI:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(45, connect=15))

    async def call(self, method: str, **payload):
        response = await self.client.post(f"{self.base_url}/{method}", json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", f"Telegram {method} failed"))
        return data.get("result")

    async def send(self, chat_id: int, text: str) -> None:
        text = text or "（没有可发送的内容）"
        for start in range(0, len(text), 4000):
            await self.call("sendMessage", chat_id=chat_id, text=text[start:start + 4000])

    async def close(self) -> None:
        await self.client.aclose()


class TelegramDiaryBot:
    def __init__(self, service: DiaryService, api: TelegramAPI,
                 allowed_user_id: int | None):
        self.service = service
        self.api = api
        self.allowed_user_id = allowed_user_id
        self.state_key = "telegram_update_offset"
        self.offset = 0

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        """Only retry temporary network/server failures."""
        if isinstance(error, httpx.RequestError):
            return True
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code == 429 or error.response.status_code >= 500
        return False

    async def handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not message or not isinstance(message.get("text"), str):
            return
        sender = message.get("from", {})
        user_id = sender.get("id")
        chat_id = message.get("chat", {}).get("id")
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            return
        if self.allowed_user_id is None:
            await self.api.send(
                chat_id,
                f"你的 Telegram user ID 是 {user_id}。\n"
                "把它填入 .env：\nTELEGRAM_ALLOWED_USER_ID=" + str(user_id),
            )
            return
        if user_id != self.allowed_user_id:
            return

        text = message["text"].strip()
        if not text:
            return
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        try:
            if command in {"/start", "/help"}:
                await self.api.send(chat_id, self.service.greeting() + "\n\n" + HELP)
                return
            if command == "/preview":
                await self.api.call("sendChatAction", chat_id=chat_id, action="typing")
                preview = await asyncio.to_thread(self.service.preview)
                await self.api.send(chat_id, preview)
                return
            if command == "/done":
                await self.api.call("sendChatAction", chat_id=chat_id, action="typing")
                path = await asyncio.to_thread(self.service.finalize)
                await self.api.send(chat_id, f"今天的日记已写入 Obsidian：{Path(path).name}")
                return
            if command == "/memory":
                memories = self.service.memory.list()
                if not memories:
                    await self.api.send(chat_id, "目前还没有长期记忆。完成日记后会逐渐积累。")
                    return
                lines = [f"长期记忆（{self.service.memory.mode}）"]
                lines.extend(
                    f"{index}. [{item['category']}] {item['content']}"
                    for index, item in enumerate(memories[:20], 1)
                )
                await self.api.send(chat_id, "\n".join(lines))
                return
            if command.startswith("/"):
                await self.api.send(chat_id, "不认识这个命令。\n\n" + HELP)
                return

            await self.api.call("sendChatAction", chat_id=chat_id, action="typing")
            answer = await asyncio.to_thread(self.service.reply, text)
            await self.api.send(chat_id, answer)
        except Exception as error:
            await self.api.send(chat_id, f"处理失败：{error}")

    async def run(self) -> None:
        me = await self.api.call("getMe")
        self.state_key = f"telegram_update_offset:{me['id']}"
        self.offset = int(self.service.store.get_state(self.state_key, "0") or 0)
        await self.api.call("deleteWebhook", drop_pending_updates=False)
        print(f"Telegram Bot @{me.get('username', '')} 已启动")
        if self.allowed_user_id is None:
            print("当前为配对模式：给 Bot 发送 /start 获取你的 user ID")
        retry_delay = 5
        while True:
            try:
                updates = await self.api.call(
                    "getUpdates", offset=self.offset, timeout=30,
                    allowed_updates=["message"],
                )
                if retry_delay > 5:
                    print("Telegram 连接已恢复")
                retry_delay = 5
            except Exception as error:
                if not self._is_retryable(error):
                    raise
                print(
                    f"Telegram 连接中断：{error}；{retry_delay} 秒后重试",
                    file=sys.stderr,
                    flush=True,
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
                continue
            for update in updates:
                await self.handle_update(update)
                self.offset = int(update["update_id"]) + 1
                self.service.store.set_state(
                    self.state_key, str(self.offset)
                )


async def async_main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("请在 .env 中设置 TELEGRAM_BOT_TOKEN")
    raw_user_id = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
    try:
        allowed_user_id = int(raw_user_id) if raw_user_id else None
    except ValueError as error:
        raise SystemExit("TELEGRAM_ALLOWED_USER_ID 必须是整数") from error
    api = TelegramAPI(token)
    try:
        await TelegramDiaryBot(
            DiaryService(Settings.from_env()), api, allowed_user_id
        ).run()
    finally:
        await api.close()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\nTelegram Bot 已停止")
    except httpx.HTTPError as error:
        print(f"Telegram 网络请求失败：{error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()

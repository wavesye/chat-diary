from __future__ import annotations

import asyncio
from prompt_toolkit import PromptSession

from diary_agent.channels.base import ChannelAdapter
from diary_agent.chat_handler import ChatHandler, HELP as CHAT_HELP
from diary_agent.config import Settings
from diary_agent.service import DiaryService


HELP = CHAT_HELP + "\n/quit — 退出（聊天和草稿保留）"


class ConsoleAdapter(ChannelAdapter):
    name = "console"

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send_message(self, user_id, text, *, buttons=()):
        print("\n" + text)


def main() -> None:
    asyncio.run(run())


async def run() -> None:
    try:
        service = DiaryService(Settings.from_env())
    except Exception as error:
        raise SystemExit(f"配置错误：{error}") from error
    print("Chat Diary · 每天聊一聊，结束后写入 Obsidian")
    print(HELP)
    print(f"\n日记伙伴：{service.greeting()}")
    prompt = PromptSession()
    handler = ChatHandler(service, ConsoleAdapter(None), "local", "local")
    while True:
        try:
            if service.editing_draft:
                print("粘贴完整 Markdown，按 Esc 后按 Enter 提交。")
            text = await prompt.prompt_async("\n你：", multiline=service.editing_draft)
        except (EOFError, KeyboardInterrupt):
            print("\n聊天已保存在本地，回头见。")
            return
        if not text.strip():
            continue
        command = text.strip().lower()
        try:
            if command in {"/quit", "/exit"}:
                print("聊天已保存在本地，回头见。")
                return
            if command == "/help":
                print(HELP)
            else:
                await handler.handle_text(text)
        except Exception as error:
            print(f"\n操作失败：{error}")


if __name__ == "__main__":
    main()

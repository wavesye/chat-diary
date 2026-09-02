from __future__ import annotations

from prompt_toolkit import PromptSession

from diary_agent.config import Settings
from diary_agent.service import DiaryService


HELP = "命令：/done 生成并写入日记；/preview 预览；/help 查看帮助；/quit 退出"


def main() -> None:
    try:
        service = DiaryService(Settings.from_env())
    except Exception as error:
        raise SystemExit(f"配置错误：{error}") from error
    print("Chat Diary · 每天聊一聊，结束后写入 Obsidian")
    print(HELP)
    print(f"\n日记伙伴：{service.greeting()}")
    prompt = PromptSession()
    while True:
        try:
            text = prompt.prompt("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n聊天已保存在本地，回头见。")
            return
        if not text:
            continue
        command = text.lower()
        try:
            if command in {"/quit", "/exit"}:
                print("聊天已保存在本地，回头见。")
                return
            if command == "/help":
                print(HELP)
            elif command == "/preview":
                print("\n" + service.preview())
            elif command == "/done":
                path = service.finalize()
                print(f"\n日记已写入：{path}")
                return
            else:
                print(f"\n日记伙伴：{service.reply(text)}")
        except Exception as error:
            print(f"\n操作失败：{error}")


if __name__ == "__main__":
    main()


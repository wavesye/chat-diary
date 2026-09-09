"""Personal Telegram adapter using the official Bot API long polling."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx

from diary_agent.config import Settings
from diary_agent.service import DiaryService
from diary_agent.todo import TaskIntent, clean_task_title, parse_relative_date


HELP = (
    "直接发消息就可以开始记录。\n\n"
    "/todo — 查看或管理 Todo\n"
    "/today — 今天需要做什么\n"
    "/week — 未来七天的 Todo\n"
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

    async def send(self, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
        text = text or "（没有可发送的内容）"
        for start in range(0, len(text), 4000):
            payload = {"chat_id": chat_id, "text": text[start:start + 4000]}
            if reply_markup and start + 4000 >= len(text):
                payload["reply_markup"] = reply_markup
            await self.call("sendMessage", **payload)

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
    def _todo_keyboard(todo: dict) -> dict:
        todo_id = todo["id"]
        if todo["status"] == "pending_confirmation":
            buttons = [[
                {"text": "✓ 确认加入", "callback_data": f"todo:confirm:{todo_id}"},
                {"text": "取消", "callback_data": f"todo:cancel:{todo_id}"},
            ]]
        else:
            buttons = [
                [
                    {"text": "✓ 完成", "callback_data": f"todo:done:{todo_id}"},
                    {"text": "明天", "callback_data": f"todo:tomorrow:{todo_id}"},
                    {"text": "后天", "callback_data": f"todo:day2:{todo_id}"},
                ],
                [
                    {"text": "✎ 修改", "callback_data": f"todo:edit:{todo_id}"},
                    {"text": "取消任务", "callback_data": f"todo:cancel:{todo_id}"},
                ],
            ]
        return {"inline_keyboard": buttons}

    @staticmethod
    def _todo_text(todo: dict) -> str:
        schedule = []
        if todo.get("planned_date"):
            schedule.append(f"计划 {todo['planned_date']}")
        if todo.get("due_at"):
            schedule.append(f"截止 {todo['due_at'].replace('T', ' ')[:16]}")
        schedule_text = " · ".join(schedule) or "未安排日期"
        priority = "!" * max(1, int(todo.get("priority", 3)) - 2)
        description = f"\n{todo['description']}" if todo.get("description") else ""
        return f"#{todo['id']}  {todo['title']}  {priority}\n{schedule_text}{description}"

    async def _send_todo(self, chat_id: int, todo: dict, prefix: str = "") -> None:
        await self.api.send(
            chat_id, (prefix + "\n" if prefix else "") + self._todo_text(todo),
            reply_markup=self._todo_keyboard(todo),
        )

    async def _send_todo_list(self, chat_id: int, todos: list[dict], heading: str) -> None:
        if not todos:
            await self.api.send(chat_id, heading + "\n目前没有任务。")
            return
        await self.api.send(chat_id, f"{heading}（{len(todos)} 项）")
        for todo in todos[:30]:
            await self._send_todo(chat_id, todo)

    async def _handle_callback(self, callback: dict) -> None:
        sender_id = callback.get("from", {}).get("id")
        data = str(callback.get("data", ""))
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        callback_id = callback.get("id")
        if sender_id != self.allowed_user_id or not isinstance(chat_id, int):
            if callback_id:
                await self.api.call("answerCallbackQuery", callback_query_id=callback_id)
            return
        try:
            _, action, raw_id = data.split(":", 2)
            todo_id = int(raw_id)
            if action == "confirm":
                todo = self.service.todos.confirm(todo_id)
                text = "已确认加入 Todo。"
            elif action == "done":
                todo = self.service.todos.complete(todo_id)
                text = "完成得漂亮，已经记入今天的活动。"
            elif action in {"tomorrow", "day2"}:
                days = 1 if action == "tomorrow" else 2
                planned = (self.service.todos.now.date() + timedelta(days=days)).isoformat()
                todo = self.service.todos.postpone(todo_id, planned)
                text = f"已安排到 {planned}。"
            elif action == "cancel":
                todo = self.service.todos.cancel(todo_id)
                text = "已取消这项 Todo。"
            elif action == "edit":
                todo = self.service.todos.get(todo_id)
                if not todo:
                    raise ValueError("这项 Todo 已不存在")
                text = (
                    f"请发送：/todo 修改 {todo_id} 新标题\n"
                    f"也可以发送：/todo 延期 {todo_id} 明天"
                )
            else:
                raise ValueError("无法识别这个按钮")
            await self.api.send(chat_id, text)
            if action not in {"done", "cancel", "edit"}:
                await self._send_todo(chat_id, todo)
            if callback_id:
                await self.api.call("answerCallbackQuery", callback_query_id=callback_id)
        except Exception as error:
            if callback_id:
                await self.api.call(
                    "answerCallbackQuery", callback_query_id=callback_id,
                    text=str(error)[:180], show_alert=True,
                )

    async def _handle_todo_command(self, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=2)
        if len(parts) == 1:
            await self._send_todo_list(
                chat_id, self.service.todos.list(statuses=("active",)), "全部进行中的 Todo"
            )
            return
        action = parts[1].lower()
        rest = parts[2].strip() if len(parts) > 2 else ""
        if action in {"添加", "add", "新建"}:
            if not rest:
                raise ValueError("用法：/todo 添加 明天修改论文第二节")
            intent = self.service.task_extractor._rules(
                rest, self.service.todos.now.date()
            )
            planned = intent.planned_date if intent else parse_relative_date(
                rest, self.service.todos.now.date()
            )
            due_at = intent.due_at if intent else None
            title = intent.title if intent else clean_task_title(rest)
            todo = self.service.todos.create(
                title.strip(), planned_date=planned, due_at=due_at,
                creation_method="telegram_command"
            )
            await self._send_todo(chat_id, todo, "已加入 Todo。")
            return
        match = re.match(r"#?(\d+)(?:\s+(.+))?$", rest)
        if not match:
            raise ValueError("请提供 Todo 编号，例如：/todo 完成 3")
        todo_id, value = int(match.group(1)), (match.group(2) or "").strip()
        if action in {"完成", "done", "complete"}:
            todo = self.service.todos.complete(todo_id)
            await self.api.send(chat_id, f"✓ 已完成：{todo['title']}，并记入今天的活动。")
        elif action in {"延期", "postpone", "挪到"}:
            planned = parse_relative_date(value, self.service.todos.now.date())
            if not planned:
                try:
                    planned = value[:10]
                    date.fromisoformat(planned)
                except ValueError as error:
                    raise ValueError("请提供日期，例如：/todo 延期 3 后天") from error
            todo = self.service.todos.postpone(todo_id, planned)
            await self._send_todo(chat_id, todo, "已重新安排。")
        elif action in {"修改", "edit", "update"}:
            if not value:
                raise ValueError("用法：/todo 修改 3 新标题")
            todo = self.service.todos.update(todo_id, title=value)
            await self._send_todo(chat_id, todo, "已修改。")
        elif action in {"取消", "删除", "cancel", "delete"}:
            todo = self.service.todos.cancel(todo_id)
            await self.api.send(chat_id, f"已取消：{todo['title']}")
        else:
            raise ValueError(
                "支持：/todo 添加 内容、/todo 完成 ID、/todo 延期 ID 日期、"
                "/todo 修改 ID 标题、/todo 取消 ID"
            )

    async def _apply_task_intent(self, chat_id: int, intent: TaskIntent) -> None:
        if intent.action == "create":
            todo = self.service.todos.create(
                intent.title, description=intent.description,
                planned_date=intent.planned_date, due_at=intent.due_at,
                priority=intent.priority, source_message_id=intent.source_message_id,
                creation_method="inferred" if intent.needs_confirmation else "natural_language",
                confirmed=not intent.needs_confirmation,
            )
            prefix = "这听起来可能是一项 Todo，要加入吗？" if intent.needs_confirmation else "已加入 Todo。"
            await self._send_todo(chat_id, todo, prefix)
            return
        matched = self.service.todos.find_match(intent.title)
        if intent.action in {"complete", "activity"}:
            if matched and matched["status"] == "active":
                todo = self.service.todos.complete(
                    matched["id"], source_message_id=intent.source_message_id
                )
                await self.api.send(chat_id, f"✓ 已完成 Todo：{todo['title']}，也记入了今天的活动。")
            else:
                self.service.store.add_activity(
                    self.service.day, intent.title, description=intent.description,
                    source_type="chat", source_message_id=intent.source_message_id,
                    confidence=intent.confidence,
                )
                await self.api.send(chat_id, f"已把“{intent.title}”记为今天完成的活动。")
            return
        if not matched:
            await self.api.send(chat_id, f"我没找到和“{intent.title}”对应的 Todo。发送 /todo 可以查看编号。")
            return
        if intent.action == "postpone":
            if not intent.planned_date:
                await self.api.send(chat_id, "要延期到哪一天？例如：/todo 延期 3 后天")
                return
            todo = self.service.todos.postpone(matched["id"], intent.planned_date)
            await self._send_todo(chat_id, todo, "已重新安排。")
        elif intent.action == "cancel":
            todo = self.service.todos.cancel(matched["id"])
            await self.api.send(chat_id, f"已取消 Todo：{todo['title']}")
        elif intent.action == "update":
            todo = self.service.todos.update(
                matched["id"], title=intent.title,
                **({"planned_date": intent.planned_date} if intent.planned_date else {}),
            )
            await self._send_todo(chat_id, todo, "已修改。")

    async def _maybe_send_reminder(self) -> None:
        if self.allowed_user_id is None:
            return
        reminder_type = self.service.reminders.due(self.allowed_user_id)
        if not reminder_type:
            return
        todos = self.service.todos.today()
        if reminder_type == "morning":
            heading = "早上好，这是今天的 Todo"
        else:
            heading = "晚间检查：这些 Todo 还没有完成"
        await self._send_todo_list(self.allowed_user_id, todos, heading)
        self.service.reminders.mark_sent(self.allowed_user_id, reminder_type)

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        """Only retry temporary network/server failures."""
        if isinstance(error, httpx.RequestError):
            return True
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code == 429 or error.response.status_code >= 500
        return False

    async def _call_until_connected(self, method: str, **payload):
        retry_delay = 5
        while True:
            try:
                return await self.api.call(method, **payload)
            except Exception as error:
                if not self._is_retryable(error):
                    raise
                print(
                    f"Telegram 连接中断：{error}；{retry_delay} 秒后重试",
                    file=sys.stderr, flush=True,
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

    async def handle_update(self, update: dict) -> None:
        callback = update.get("callback_query")
        if callback:
            await self._handle_callback(callback)
            return
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
            if command == "/todo":
                await self._handle_todo_command(chat_id, text)
                return
            if command == "/today":
                await self._send_todo_list(chat_id, self.service.todos.today(), "今天的 Todo")
                return
            if command == "/week":
                await self._send_todo_list(chat_id, self.service.todos.week(), "未来七天的 Todo")
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
                    f"{index}. [{item['category']} / {item.get('temporal_status', 'current')}] "
                    f"{item['content']}"
                    + (f"（来源 {item['source_date']}）" if item.get("source_date") else "")
                    for index, item in enumerate(memories[:20], 1)
                )
                await self.api.send(chat_id, "\n".join(lines))
                return
            if command.startswith("/"):
                await self.api.send(chat_id, "不认识这个命令。\n\n" + HELP)
                return

            await self.api.call("sendChatAction", chat_id=chat_id, action="typing")
            answer, source_message_id = await asyncio.to_thread(
                self.service.reply_with_record, text
            )
            await self.api.send(chat_id, answer)
            try:
                intent = await asyncio.to_thread(
                    self.service.task_extractor.extract, text, source_message_id
                )
                if intent:
                    await self._apply_task_intent(chat_id, intent)
            except Exception as error:
                print(f"Todo 识别失败（不影响聊天）：{error}", file=sys.stderr)
        except Exception as error:
            await self.api.send(chat_id, f"处理失败：{error}")

    async def run(self) -> None:
        me = await self._call_until_connected("getMe")
        self.state_key = f"telegram_update_offset:{me['id']}"
        self.offset = int(self.service.store.get_state(self.state_key, "0") or 0)
        await self._call_until_connected("deleteWebhook", drop_pending_updates=False)
        print(f"Telegram Bot @{me.get('username', '')} 已启动")
        if self.allowed_user_id is None:
            print("当前为配对模式：给 Bot 发送 /start 获取你的 user ID")
        retry_delay = 5
        while True:
            try:
                await self._maybe_send_reminder()
                updates = await self.api.call(
                    "getUpdates", offset=self.offset, timeout=30,
                    allowed_updates=["message", "callback_query"],
                )
                if retry_delay > 5:
                    print("Telegram 连接已恢复")
                retry_delay = 5
                for update in updates:
                    await self.handle_update(update)
                    self.offset = int(update["update_id"]) + 1
                    self.service.store.set_state(
                        self.state_key, str(self.offset)
                    )
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

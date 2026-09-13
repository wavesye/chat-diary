"""Shared diary commands and conversation workflow, independent of chat platforms."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, timedelta
from pathlib import Path

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from .channels.base import Button, ChannelAdapter, MessageProcessingError
from .provider import ProviderResponseError
from .service import DiaryService
from .todo import TaskIntent, clean_task_title, parse_relative_date

HELP = (
    "直接发消息就可以开始记录。\n\n"
    "/todo — 查看或管理 Todo\n"
    "/today — 今天需要做什么\n"
    "/week — 未来七天的 Todo\n"
    "/preview — 预览今天的日记\n"
    "/done — 写入 Obsidian\n"
    "/memory — 查看长期记忆\n"
    "/status — 查看 Bot 当前运行配置\n"
    "/help — 查看帮助"
)


class ChatHandler:
    def __init__(self, service: DiaryService, adapter: ChannelAdapter,
                 user_id: str, external_user_id: str, binding_is_current=lambda: True):
        self.service = service
        self.adapter = adapter
        self.user_id = user_id
        self.external_user_id = external_user_id
        self.message_debounce_seconds = getattr(adapter, "message_debounce_seconds", 0)
        self.binding_is_current = binding_is_current

    async def _send(self, chat_id: str, text: str, **options) -> None:
        if chat_id != self.external_user_id or not self.binding_is_current():
            raise PermissionError("Channel binding changed during processing")
        await self.adapter.send_message(chat_id, text, **options)

    def _status_text(self) -> str:
        history = self.service.history_index.status()
        memory = self.service.memory.status()
        text = (
            "Bot 运行状态\n"
            f"消息合并窗口：{self.message_debounce_seconds:g} 秒\n"
            f"时区：{self.service.settings.timezone.key}\n"
            f"长期记忆检索：{memory['search_mode']} "
            f"（向量 {memory['embedded_memories']}/{memory['memories']}）\n"
            f"历史原文检索：{history['search_mode']} "
            f"（向量 {history['embedded_chunks']}/{history['chunks']}）\n"
            f"已导入历史文档：{history['documents']}"
        )
        if history["fallback_reason"] == "embedding_not_configured":
            text += (
                f"\n提示：已配置 {history['configured_search_mode']}，但没有可用的 "
                "Embedding 模型，当前降级为 keyword。"
            )
        elif history["fallback_reason"] == "embeddings_not_built":
            text += (
                "\nEmbedding 已配置，但历史向量尚未补建；当前实际使用 keyword。"
            )
        elif (
            history["fallback_reason"] == "embedding_runtime_error"
            or memory["fallback_reason"] == "embedding_runtime_error"
        ):
            detail = (
                history.get("embedding_runtime_error")
                or memory.get("embedding_runtime_error")
                or "未知错误"
            )
            text += (
                "\n本地 Embedding 初始化失败，当前降级为 keyword："
                f"{detail}。修复依赖或网络后请重启 Bot 再试。"
            )
        runtime_state = history.get("embedding_runtime_state")
        if runtime_state in {"not_loaded", "ready"}:
            if runtime_state == "ready":
                label = "已加载"
            elif history.get("embedding_model_cached"):
                label = "已缓存，尚未载入内存"
            else:
                label = "尚未下载（首次生成向量时自动下载）"
            text += f"\n本地 ONNX 模型：{label}"
        if (
            history["configured_search_mode"] != "keyword"
            and self.service.embedder
            and (history["pending_embeddings"] or memory["pending_embeddings"])
        ):
            text += (
                f"\n待补建向量：历史 {history['pending_embeddings']}、"
                f"长期记忆 {memory['pending_embeddings']}；"
                "运行 import_history.py embed 后才会全部参与语义检索。"
            )
        return text

    @staticmethod
    def _todo_buttons(todo: dict) -> tuple[tuple[Button, ...], ...]:
        todo_id = todo["id"]
        if todo["status"] == "pending_confirmation":
            return ((Button("✓ 确认加入", f"todo:confirm:{todo_id}"),
                     Button("取消", f"todo:cancel:{todo_id}")),)
        return (
            (Button("✓ 完成", f"todo:done:{todo_id}"),
             Button("明天", f"todo:tomorrow:{todo_id}"),
             Button("后天", f"todo:day2:{todo_id}")),
            (Button("✎ 修改", f"todo:edit:{todo_id}"),
             Button("取消任务", f"todo:cancel:{todo_id}")),
        )

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

    async def _send_todo(self, chat_id: str, todo: dict, prefix: str = "") -> None:
        await self._send(
            chat_id, (prefix + "\n" if prefix else "") + self._todo_text(todo),
            buttons=self._todo_buttons(todo),
        )

    async def _send_todo_list(self, chat_id: str, todos: list[dict], heading: str) -> None:
        if not todos:
            await self._send(chat_id, heading + "\n目前没有任务。")
            return
        await self._send(chat_id, f"{heading}（{len(todos)} 项）")
        for todo in todos[:30]:
            await self._send_todo(chat_id, todo)

    async def handle_action(self, data: str) -> None:
        chat_id = self.external_user_id
        try:
            prefix, action, raw_id = data.split(":", 2)
            if prefix != "todo":
                raise ValueError("无法识别这个按钮")
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
            await self._send(chat_id, text)
            if action not in {"done", "cancel", "edit"}:
                await self._send_todo(chat_id, todo)
        except Exception as error:
            await self._report_processing_error(chat_id, error)

    async def _handle_todo_command(self, chat_id: str, text: str) -> None:
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
                creation_method="channel_command"
            )
            await self._send_todo(chat_id, todo, "已加入 Todo。")
            return
        match = re.match(r"#?(\d+)(?:\s+(.+))?$", rest)
        if not match:
            raise ValueError("请提供 Todo 编号，例如：/todo 完成 3")
        todo_id, value = int(match.group(1)), (match.group(2) or "").strip()
        if action in {"确认", "confirm"}:
            todo = self.service.todos.confirm(todo_id)
            await self._send_todo(chat_id, todo, "已确认加入 Todo。")
        elif action in {"完成", "done", "complete"}:
            todo = self.service.todos.complete(todo_id)
            await self._send(chat_id, f"✓ 已完成：{todo['title']}，并记入今天的活动。")
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
            await self._send(chat_id, f"已取消：{todo['title']}")
        else:
            raise ValueError(
                "支持：/todo 添加 内容、/todo 完成 ID、/todo 延期 ID 日期、"
                "/todo 修改 ID 标题、/todo 取消 ID"
            )

    async def _apply_task_intent(self, chat_id: str, intent: TaskIntent) -> None:
        if intent.action == "list":
            await self._send_todo_list(
                chat_id, self.service.todos.list(statuses=("active",)),
                "全部进行中的 Todo",
            )
            return
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
                await self._send(chat_id, f"✓ 已完成 Todo：{todo['title']}，也记入了今天的活动。")
            else:
                self.service.store.add_activity(
                    self.service.day, intent.title, description=intent.description,
                    source_type="chat", source_message_id=intent.source_message_id,
                    confidence=intent.confidence,
                )
                await self._send(chat_id, f"已把“{intent.title}”记为今天完成的活动。")
            return
        if not matched:
            await self._send(chat_id, f"我没找到和“{intent.title}”对应的 Todo。发送 /todo 可以查看编号。")
            return
        if intent.action == "postpone":
            if not intent.planned_date:
                await self._send(chat_id, "要延期到哪一天？例如：/todo 延期 3 后天")
                return
            todo = self.service.todos.postpone(matched["id"], intent.planned_date)
            await self._send_todo(chat_id, todo, "已重新安排。")
        elif intent.action == "cancel":
            todo = self.service.todos.cancel(matched["id"])
            await self._send(chat_id, f"已取消 Todo：{todo['title']}")
        elif intent.action == "update":
            todo = self.service.todos.update(
                matched["id"], title=intent.title,
                **({"planned_date": intent.planned_date} if intent.planned_date else {}),
            )
            await self._send_todo(chat_id, todo, "已修改。")

    @staticmethod
    def _friendly_error(error: Exception) -> str:
        """Return an actionable message even when an exception has no text."""
        if isinstance(error, (APIConnectionError, APITimeoutError)):
            return "模型服务暂时连接不上，这条消息还没有处理完成。请稍后再试。"
        if isinstance(error, RateLimitError) or (
            isinstance(error, APIStatusError) and error.status_code == 429
        ):
            return "模型服务现在有些繁忙，这条消息还没有处理完成。请稍后再试。"
        if isinstance(error, APIStatusError):
            return f"模型服务返回了错误（HTTP {error.status_code}），请检查模型配置或稍后再试。"
        if isinstance(error, ProviderResponseError):
            return f"模型回复的格式暂时无法处理：{error}。请重试一次。"
        if isinstance(error, (httpx.RequestError, httpx.HTTPStatusError)):
            return "聊天渠道网络暂时不稳定，刚才的操作未能确认，请稍后再试。"
        if isinstance(error, OSError):
            return "写入本地文件失败，请检查 Obsidian 路径、磁盘空间和文件权限。"
        detail = str(error).strip()
        if detail:
            return f"操作没有完成：{detail}"
        return f"操作没有完成（{type(error).__name__}），请稍后重试。"

    async def _report_processing_error(self, chat_id: str, error: Exception) -> None:
        logging.getLogger(__name__).warning("Message processing failed: %s", type(error).__name__)
        await self._send(chat_id, self._friendly_error(error))
        if not isinstance(error, ValueError):
            raise MessageProcessingError("Message processing failed") from error

    async def handle_texts(self, texts: list[str]) -> None:
        chat_id = self.external_user_id
        texts = [text.strip() for text in texts if text.strip()]
        if not texts:
            return
        combined = "\n".join(texts)
        try:
            # A single, explicit task-management message does not need a second
            # conversational answer. Route it locally so only the database-backed
            # result is presented as authoritative.
            rule_intent = self.service.task_extractor.rule_intent(combined)
            if len(texts) == 1 and self.service.task_extractor.is_direct_management(
                combined, rule_intent
            ):
                if rule_intent.action != "list":
                    source_ids = self.service.store.add_messages(
                        self.service.day, "user", texts
                    )
                    rule_intent = TaskIntent(**{
                        **rule_intent.__dict__, "source_message_id": source_ids[-1]
                    })
                await self._apply_task_intent(chat_id, rule_intent)
                return
            await self._show_typing(chat_id)
            answer, source_message_ids = await asyncio.to_thread(
                self.service.reply_many_with_record, texts
            )
            await self._send(chat_id, answer)
            try:
                intent = await asyncio.to_thread(
                    self.service.task_extractor.extract,
                    combined,
                    source_message_ids[-1],
                )
                if intent:
                    await self._apply_task_intent(chat_id, intent)
            except Exception as error:
                logging.getLogger(__name__).warning("Todo extraction failed: %s", type(error).__name__)
        except Exception as error:
            await self._report_processing_error(chat_id, error)

    async def handle_text(self, text: str) -> None:
        chat_id = self.external_user_id
        text = text.strip()
        if not text:
            return
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        try:
            if command in {"/start", "/help"}:
                await self._send(chat_id, self.service.greeting() + "\n\n" + HELP)
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
                await self._show_typing(chat_id)
                preview = await asyncio.to_thread(self.service.preview)
                await self._send(chat_id, preview)
                return
            if command == "/done":
                await self._show_typing(chat_id)
                path = await asyncio.to_thread(self.service.finalize)
                await self._send(chat_id, f"今天的日记已写入 Obsidian：{Path(path).name}")
                return
            if command == "/memory":
                memories = self.service.memory.list()
                if not memories:
                    await self._send(chat_id, "目前还没有长期记忆。完成日记后会逐渐积累。")
                    return
                lines = [f"长期记忆（{self.service.memory.mode}）"]
                lines.extend(
                    f"{index}. [{item['category']} / {item.get('temporal_status', 'current')}] "
                    f"{item['content']}"
                    + (f"（来源 {item['source_date']}）" if item.get("source_date") else "")
                    for index, item in enumerate(memories[:20], 1)
                )
                await self._send(chat_id, "\n".join(lines))
                return
            if command == "/status":
                await self._send(chat_id, self._status_text())
                return
            if command.startswith("/"):
                await self._send(chat_id, "不认识这个命令。\n\n" + HELP)
                return

            await self.handle_texts([text])
        except MessageProcessingError:
            raise
        except Exception as error:
            await self._report_processing_error(chat_id, error)

    async def maybe_send_reminder(self) -> None:
        # Each service has its own database; a constant internal slot avoids using
        # external platform IDs in reminder business state and duplicate delivery.
        recipient = 1
        reminder_type = self.service.reminders.due(recipient)
        if not reminder_type:
            return
        heading = ("早上好，这是今天的 Todo" if reminder_type == "morning"
                   else "晚间检查：这些 Todo 还没有完成")
        await self._send_todo_list(self.external_user_id, self.service.todos.today(), heading)
        self.service.reminders.mark_sent(recipient, reminder_type)

    async def _show_typing(self, chat_id: str) -> None:
        try:
            await self.adapter.show_typing(chat_id)
        except Exception as error:
            logging.getLogger(__name__).warning("Typing status failed: %s", type(error).__name__)

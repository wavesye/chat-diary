from __future__ import annotations

import json
from datetime import datetime

from .config import Settings
from .prompts import CHAT_SYSTEM, SUMMARY_PROMPT, SUMMARY_SYSTEM
from .provider import ChatProvider
from .store import DiaryStore
from .writer import render_markdown, write_entry


class DiaryService:
    def __init__(self, settings: Settings, provider=None, store=None):
        self.settings = settings
        self.provider = provider or ChatProvider(
            api_key=settings.api_key, base_url=settings.base_url, model=settings.model
        )
        self.store = store or DiaryStore(settings.database_path)

    @property
    def day(self) -> str:
        return datetime.now(self.settings.timezone).date().isoformat()

    def greeting(self) -> str:
        if self.store.user_message_count(self.day):
            return "我们接着聊今天吧。刚才之后，又发生了什么？"
        return f"晚上好，{self.settings.user_name}。今天最想从哪一刻说起？"

    def reply(self, text: str) -> str:
        clean = text.strip()
        if not clean:
            raise ValueError("消息不能为空")
        self.store.add_message(self.day, "user", clean)
        history = self.store.messages(self.day)
        answer = self.provider.chat([{"role": "system", "content": CHAT_SYSTEM}, *history])
        self.store.add_message(self.day, "assistant", answer)
        return answer

    def preview(self) -> str:
        return render_markdown(self.day, self._summarize())

    def finalize(self):
        markdown = self.preview()
        path = write_entry(
            self.settings.vault_path, self.settings.journal_folder, self.day, markdown
        )
        self.store.mark_finalized(self.day, path)
        return path

    def _summarize(self) -> dict:
        history = self.store.messages(self.day)
        user_messages = [m for m in history if m["role"] == "user"]
        if not user_messages:
            raise ValueError("今天还没有可整理的聊天内容")
        transcript = json.dumps(history, ensure_ascii=False, indent=2)
        return self.provider.json([
            {"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": SUMMARY_PROMPT + transcript},
        ])


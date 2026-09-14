import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from api import create_app
from api import WebReplyAdapter
from diary_agent.chat_handler import ChatHandler
from diary_agent.config import Settings
from diary_agent.service import DiaryService
from diary_agent.store import DiaryStore
from diary_agent.verbatim import preservation_request
from test_diary import FakeProvider


class ChangingProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.summary_messages = []

    def json(self, messages):
        self.calls += 1
        self.summary_messages = messages
        result = super().json(messages)
        result["diary"] += f"版本 {self.calls}。"
        return result


class DraftAndQuoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(root, "Daily", root / "diary.sqlite", ZoneInfo("Asia/Shanghai"),
                                 "fake", "fake", "", "", "小叶")
        self.provider = ChangingProvider()
        self.service = DiaryService(self.settings, provider=self.provider)

    def tearDown(self):
        self.service.store.connection.close()
        self.temp.cleanup()

    def test_preview_is_persistent_and_done_never_regenerates(self):
        self.service.reply("今天散步了")
        preview = self.service.preview()
        self.assertEqual(self.service.preview(), preview)
        other = DiaryService(self.settings, provider=self.provider)
        try:
            path = other.finalize()
            self.assertEqual(path.read_bytes(), preview.encode("utf-8"))
            self.assertEqual(other.finalize().read_bytes(), preview.encode("utf-8"))
            self.assertEqual(self.provider.calls, 1)
        finally:
            other.store.connection.close()

    def test_manual_edits_preserve_every_character_and_do_not_index_removed_facts(self):
        self.service.reply("今天散步了")
        self.service.preview()
        manual = "  \n# 我自己改的标题\n\n第一段。  \n\n    第二段\n- [x] 喝水\n\n"
        self.service.edit_draft(manual)
        self.assertEqual(self.service.preview(), manual)
        self.assertEqual(self.service.finalize().read_bytes(), manual.encode("utf-8"))
        self.assertFalse(self.service.memory.list())
        self.assertEqual(self.service.activities.review_day(self.service.day)["title"], "我自己改的标题")
        self.assertEqual(self.provider.calls, 1)

    def test_new_sources_only_refresh_on_explicit_request(self):
        self.service.reply("今天散步了")
        original = self.service.preview()
        self.service.todos.create("回复邮件")
        self.assertTrue(self.service.draft_status()["stale"])
        self.assertEqual(self.service.preview(), original)
        self.assertEqual(self.service.finalize().read_text(), original)
        refreshed = self.service.preview(refresh=True)
        self.assertIn("- [ ] 回复邮件", refreshed)
        self.assertFalse(self.service.draft_status()["stale"])
        self.assertEqual(self.provider.calls, 2)

    def test_todos_and_quotes_can_generate_without_conversation(self):
        todo = self.service.todos.create("写测试")
        completed = self.service.todos.create("修好Bug")
        self.service.todos.complete(completed["id"])
        self.service.todos.create("还没确认", confirmed=False)
        self.service.journal_instruction("/quote  原样的句子\n第二行。  \n")
        result = self.service.preview()
        self.assertIn("- [ ] 写测试", result)
        self.assertIn("- [x] 修好Bug", result)
        self.assertNotIn("还没确认", result)
        self.assertIn(" 原样的句子\n第二行。  \n", result)
        request = self.provider.summary_messages[-1]["content"]
        self.assertIn('"status": "completed"', request)
        self.assertIn('"verbatim_quotes"', request)

    def test_natural_request_reuses_previous_user_text_exactly(self):
        original = "  我不想给今天下结论。\n\nMaybe tomorrow.  \n"
        self.service.reply(original)
        self.service.reply("这段话要原封不动放到日记里")
        quotes = self.service.store.quotes(self.service.day)
        self.assertEqual([item["content"] for item in quotes], [original])
        self.assertIn(original, self.service.preview())
        self.assertTrue(self.service.delete_message(quotes[0]["source_message_id"]))
        self.assertEqual(self.service.store.quotes(self.service.day), [])

    def test_inline_and_suffix_natural_requests_preserve_multiline_text(self):
        self.assertEqual(preservation_request("这段话要原封不动放到日记里：  原文\n换行。 "),
                         (True, "  原文\n换行。 "))
        self.assertEqual(preservation_request("原文\n第二段。\n这段话要原封不动放到日记里"),
                         (True, "原文\n第二段。"))
        self.assertFalse(preservation_request("不要把这段话原封不动放到日记里")[0])
        self.assertEqual(preservation_request("  原文。  \r\n\r\n这段话要原封不动放到日记里"),
                         (True, "  原文。  \r\n"))

    def test_quote_reference_cannot_read_other_days_or_assistant_text(self):
        old = self.service.store.add_message("2000-01-01", "user", "旧的原话")
        assistant = self.service.store.add_message(self.service.day, "assistant", "助手说的话")
        for source_id in (old, assistant):
            with self.assertRaises(ValueError):
                self.service.journal_instruction(f"/quote message {source_id}")

    def test_edit_mode_replaces_draft_without_saving_as_conversation(self):
        self.service.reply("今天散步了")
        self.service.preview()
        original = self.service.store.messages(self.service.day)
        self.service.journal_instruction("/edit")
        self.service.reply("# 手动修改\n\n记得提醒我，原文不是一项 Todo。  \n")
        self.assertEqual(self.service.store.messages(self.service.day), original)
        self.assertFalse(self.service.editing_draft)
        self.assertEqual(self.service.preview(), "# 手动修改\n\n记得提醒我，原文不是一项 Todo。  \n")

    def test_api_manual_draft_roundtrip_and_commands(self):
        with TestClient(create_app(self.service)) as client:
            added = client.post("/v1/chat", json={"message": "/todo add 写测试"})
            self.assertEqual(added.status_code, 200)
            self.assertIn("已加入", added.json()["reply"])
            quoted = client.post("/v1/chat", json={"message": "/quote  原文\n保留换行  \n"})
            self.assertIn("已保留原话", quoted.json()["reply"])
            preview = client.post("/v1/preview").json()
            self.assertIn("- [ ] 写测试", preview["markdown"])
            manual = "\n# 手动的日记\n\n不要改动。  \n"
            edited = client.put("/v1/preview", json={"markdown": manual})
            self.assertEqual(edited.json()["markdown"], manual)
            self.assertTrue(edited.json()["edited"])
            output = client.post("/v1/finalize").json()
            self.assertEqual(Path(output["path"]).read_bytes(), manual.encode())
            self.assertEqual(self.provider.calls, 1)

    def test_empty_edit_does_not_destroy_existing_draft(self):
        self.service.reply("记录")
        preview = self.service.preview()
        with self.assertRaises(ValueError):
            self.service.edit_draft(" \n")
        self.assertEqual(self.service.preview(), preview)

    def test_model_cannot_turn_unfinished_todo_into_calendar_activity(self):
        self.service.todos.create("收到好消息")
        self.service.finalize()
        self.assertEqual(self.service.activities.list_day(self.service.day), [])


class JournalCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        settings = Settings(root, "Daily", root / "diary.sqlite", ZoneInfo("UTC"),
                            "fake", "fake", "", "", "小叶")
        self.provider = ChangingProvider()
        self.service = DiaryService(settings, provider=self.provider)
        self.addCleanup(self.service.store.connection.close)
        self.adapter = WebReplyAdapter()
        self.handler = ChatHandler(self.service, self.adapter, "owner", "owner")

    async def test_command_roundtrip_preserves_raw_edit_and_never_extracts_tasks(self):
        await self.handler.handle_text("/todo add 完成实验")
        await self.handler.handle_text("/preview")
        original = self.adapter.replies[-1]
        await self.handler.handle_text("/preview")
        self.assertEqual(self.adapter.replies[-1], original)
        await self.handler.handle_text("/edit")
        manual = "  \n# 自己写的标题\n\n明天提醒我去散步。\n\n- [x] 完成实验  \n"
        await self.handler.handle_text(manual)
        self.assertEqual(self.service.preview(), manual)
        self.assertEqual(len(self.service.todos.list()), 1)
        self.assertEqual(self.service.store.messages(self.service.day), [])
        await self.handler.handle_text("/done")
        self.assertEqual(self.service.finalize().read_bytes(), manual.encode())
        self.assertEqual(self.provider.calls, 1)

    async def test_batched_previous_quote_preserves_message_boundaries(self):
        original = "  平常的下午。\n第二行。  \n"
        await self.handler.handle_texts([original, "这段话要原封不动放到日记里"])
        self.assertEqual(self.service.store.quotes(self.service.day)[0]["content"], original)
        await self.handler.handle_text("/quote@diary_bot 提醒我明天跑步  \n")
        self.assertEqual(self.service.store.quotes(self.service.day)[1]["content"], "提醒我明天跑步  \n")
        self.assertFalse(self.service.todos.list())

    async def test_refresh_is_explicit_and_invalid_option_keeps_draft(self):
        await self.handler.handle_text("/quote 今天下雨了")
        await self.handler.handle_text("/preview")
        before = self.service.preview()
        await self.handler.handle_text("/preview nonsense")
        self.assertEqual(self.service.preview(), before)
        await self.handler.handle_text("/preview refresh")
        self.assertNotEqual(self.service.preview(), before)


if __name__ == "__main__":
    unittest.main()

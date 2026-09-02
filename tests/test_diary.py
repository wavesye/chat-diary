import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from api import create_app
from diary_agent.config import Settings
from diary_agent.service import DiaryService
from diary_agent.store import DiaryStore
from diary_agent.writer import render_markdown, write_entry


class FakeProvider:
    def chat(self, messages):
        return "听起来这件事对你挺重要。那一刻你是什么感受？"

    def json(self, messages):
        return {
            "title": "雨里的好消息", "summary": "今天我收到一个好消息。",
            "moments": ["下班时收到消息"], "emotions": ["开心"],
            "insights": [], "gratitude": [], "tomorrow": ["回复邮件"],
            "quotes": ["我终于松了口气"], "tags": ["工作", "日记"],
        }


class DiaryTests(unittest.TestCase):
    def test_service_persists_chat_and_writes_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(root, "Daily", root / "diary.sqlite", ZoneInfo("UTC"),
                                "fake", "fake", "", "", "小叶")
            service = DiaryService(settings, provider=FakeProvider(),
                                   store=DiaryStore(settings.database_path))
            answer = service.reply("我终于松了口气")
            self.assertIn("什么感受", answer)
            path = service.finalize()
            self.assertEqual(path, (root / "Daily" / f"{service.day}.md").resolve())
            content = path.read_text(encoding="utf-8")
            self.assertIn("# 雨里的好消息", content)
            self.assertIn("> 我终于松了口气", content)

    def test_writer_rejects_folder_outside_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            with self.assertRaises(ValueError):
                write_entry(vault, "../../elsewhere", "2026-09-02", "bad")

    def test_empty_sections_are_omitted(self):
        result = render_markdown("2026-09-02", {"title": "一天", "summary": "正文", "tags": []})
        self.assertNotIn("## 感谢", result)
        self.assertIn("tags: [日记]", result)

    def test_http_api_chat_preview_and_finalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(root, "Daily", root / "diary.sqlite", ZoneInfo("UTC"),
                                "fake", "fake", "", "", "小叶")
            service = DiaryService(settings, provider=FakeProvider(),
                                   store=DiaryStore(settings.database_path))
            with TestClient(create_app(service)) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                response = client.post("/v1/chat", json={"message": "我终于松了口气"})
                self.assertEqual(response.status_code, 200)
                self.assertIn("什么感受", response.json()["reply"])
                session = client.get("/v1/session").json()
                self.assertEqual(len(session["messages"]), 2)
                preview = client.post("/v1/preview")
                self.assertIn("# 雨里的好消息", preview.json()["markdown"])
                finalized = client.post("/v1/finalize")
                self.assertEqual(finalized.status_code, 200)
                self.assertTrue(Path(finalized.json()["path"]).is_file())


if __name__ == "__main__":
    unittest.main()

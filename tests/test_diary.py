import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from api import create_app
from diary_agent.config import Settings
from diary_agent.memory import LongTermMemory
from diary_agent.service import DiaryService
from diary_agent.store import DiaryStore
from diary_agent.writer import entry_filename, render_markdown, write_entry


class FakeProvider:
    def __init__(self):
        self.last_chat_messages = []

    def chat(self, messages):
        self.last_chat_messages = messages
        return "听起来这件事对你挺重要。那一刻你是什么感受？"

    def json(self, messages):
        return {
            "title": "雨里的好消息", "diary": "今天我收到一个好消息。心里一直绷着的弦终于松了下来。",
            "moments": ["下班时收到消息"], "emotions": ["开心"],
            "insights": [], "gratitude": [], "tomorrow": ["回复邮件"],
            "quote": "我终于松了口气", "tags": ["工作", "日记"],
            "activities": [{
                "title": "收到好消息", "description": "下班时收到消息",
                "tags": ["工作"], "confidence": 0.95,
            }],
            "memories": [{"content": "用户正在开发对话式日记应用", "category": "project", "importance": 0.8}],
        }


class FakeEmbedder:
    signature = "fake:semantic-v1"

    def embed(self, texts):
        return [
            [1.0, 0.0] if ("日记" in text or "项目" in text) else [0.0, 1.0]
            for text in texts
        ]


class DiaryTests(unittest.TestCase):
    def test_chat_prompt_has_no_forced_question_or_round_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(root, "Daily", root / "diary.sqlite", ZoneInfo("UTC"),
                                "fake", "fake", "", "", "小叶")
            provider = FakeProvider()
            service = DiaryService(settings, provider=provider,
                                   store=DiaryStore(settings.database_path))
            service.reply("我只是记录一下今天看了场电影")
            system = provider.last_chat_messages[0]["content"]
            self.assertIn("不要为了\n延长对话而硬问问题", system)
            self.assertNotIn("6 至 10 轮", system)

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
            self.assertEqual(
                path, (root / "Daily" / entry_filename(service.day)).resolve()
            )
            content = path.read_text(encoding="utf-8")
            self.assertIn("# 雨里的好消息", content)
            self.assertIn("> 我终于松了口气", content)
            self.assertEqual(service.memory.list()[0]["category"], "project")
            self.assertEqual(service.activities.list_day(service.day)[0]["title"], "收到好消息")

    def test_writer_rejects_folder_outside_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            with self.assertRaises(ValueError):
                write_entry(vault, "../../elsewhere", "2026-09-02", "bad")

    def test_empty_sections_are_omitted(self):
        result = render_markdown("2026-09-02", {"title": "一天", "diary": "正文", "tags": []})
        self.assertNotIn("## 感谢", result)
        self.assertIn("tags: [日记]", result)

    def test_entry_filename_uses_unpadded_date_and_suffix(self):
        self.assertEqual(entry_filename("2026-09-01"), "2026-9-1-AIGEN.md")

    def test_hybrid_memory_uses_semantic_vector_without_keyword_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DiaryStore(Path(tmp) / "diary.sqlite")
            memory = LongTermMemory(store, FakeEmbedder())
            memory.remember([
                {"content": "用户正在开发对话式日记应用", "category": "project", "importance": 0.9},
                {"content": "用户每周末去户外跑步", "category": "routine", "importance": 0.6},
            ], "2026-09-01")
            hits = memory.search("那个项目最近怎么样", top_k=1)
            self.assertEqual(memory.mode, "hybrid")
            self.assertIn("日记应用", hits[0].content)

    def test_deleting_message_invalidates_only_that_days_memory_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(root, "Daily", root / "diary.sqlite", ZoneInfo("UTC"),
                                "fake", "fake", "", "", "小叶")
            provider = FakeProvider()
            service = DiaryService(settings, provider=provider,
                                   store=DiaryStore(settings.database_path))
            service.reply("我终于松了口气")
            service.finalize()
            records = service.store.message_records(service.day)
            self.assertTrue(service.memory.list())
            self.assertTrue(service.delete_message(records[0]["id"]))
            self.assertFalse(service.memory.list())
            self.assertFalse(service.activities.list_day(service.day))

    def test_memory_shared_by_two_days_survives_one_day_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = LongTermMemory(DiaryStore(Path(tmp) / "diary.sqlite"))
            item = [{"content": "用户正在开发日记应用", "category": "project", "importance": 0.8}]
            memory.remember(item, "2026-09-01")
            memory.remember(item, "2026-09-02")
            memory.forget_day("2026-09-02")
            remaining = memory.list()
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["last_seen"], "2026-09-01")

    def test_http_api_chat_preview_and_finalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(root, "Daily", root / "diary.sqlite", ZoneInfo("UTC"),
                                "fake", "fake", "", "", "小叶")
            service = DiaryService(settings, provider=FakeProvider(),
                                   store=DiaryStore(settings.database_path))
            with TestClient(create_app(service)) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.get("/").status_code, 200)
                response = client.post("/v1/chat", json={"message": "我终于松了口气"})
                self.assertEqual(response.status_code, 200)
                self.assertIn("什么感受", response.json()["reply"])
                session = client.get("/v1/session").json()
                self.assertEqual(len(session["messages"]), 2)
                self.assertIsInstance(session["messages"][0]["id"], int)
                preview = client.post("/v1/preview")
                self.assertIn("# 雨里的好消息", preview.json()["markdown"])
                finalized = client.post("/v1/finalize")
                self.assertEqual(finalized.status_code, 200)
                self.assertTrue(Path(finalized.json()["path"]).is_file())
                memories = client.get("/v1/memories").json()
                self.assertEqual(memories["mode"], "keyword")
                self.assertEqual(len(memories["memories"]), 1)
                memory_id = memories["memories"][0]["id"]
                self.assertEqual(
                    client.delete(f"/v1/memories/{memory_id}").status_code, 200
                )
                self.assertEqual(client.get("/v1/memories").json()["memories"], [])

    def test_todo_api_completion_feeds_review_calendar_not_future_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(root, "Daily", root / "diary.sqlite", ZoneInfo("UTC"),
                                "fake", "fake", "", "", "小叶")
            service = DiaryService(settings, provider=FakeProvider(),
                                   store=DiaryStore(settings.database_path))
            with TestClient(create_app(service)) as client:
                future = client.post("/v1/todos", json={
                    "title": "未来计划", "planned_date": "2099-01-01"
                }).json()
                completed = client.post("/v1/todos", json={"title": "已经做完的事"}).json()
                self.assertEqual(
                    client.post(f"/v1/todos/{completed['id']}/complete").status_code, 200
                )
                month = client.get(
                    "/v1/review/month", params={"month": service.day[:7]}
                ).json()
                self.assertEqual(month["days"][0]["activity_count"], 1)
                detail = client.get(f"/v1/review/day/{service.day}").json()
                self.assertEqual(detail["completed_todos"][0]["title"], "已经做完的事")
                self.assertNotIn("未来计划", str(detail))
                self.assertEqual(service.todos.get(future["id"])["status"], "active")
                self.assertEqual(client.get("/v1/review/day/2099-01-01").status_code, 404)

    def test_history_scan_and_search_api_use_temporary_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "History"
            history.mkdir()
            (history / "2020-2-3.md").write_text(
                "# 旧项目\n\n第一次整理长期项目资料。", encoding="utf-8"
            )
            settings = Settings(
                root, "Daily", root / "diary.sqlite", ZoneInfo("UTC"),
                "fake", "fake", "", "", "小叶", history_path=history,
            )
            service = DiaryService(settings, provider=FakeProvider(),
                                   store=DiaryStore(settings.database_path))
            with TestClient(create_app(service)) as client:
                scanned = client.post("/v1/history/scan")
                self.assertEqual(scanned.status_code, 200)
                self.assertEqual(scanned.json()["imported"], 1)
                found = client.get(
                    "/v1/history/search", params={"q": "长期项目资料"}
                ).json()
                self.assertEqual(found["results"][0]["diary_date"], "2020-02-03")
                self.assertEqual(client.post(
                    "/v1/history/extract", json={"max_batches": 1}
                ).status_code, 409)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from diary_agent.activities import ActivityService
from diary_agent.history import HistoricalMemoryIndex, ObsidianHistoryImporter
from diary_agent.memory import LongTermMemory
from diary_agent.store import DiaryStore


class LocalEmbedder:
    signature = "local:test-v1"

    def embed(self, texts):
        return [[1.0, float("散步" in text)] for text in texts]


class HistoryProvider:
    def __init__(self):
        self.calls = 0

    def json(self, messages):
        self.calls += 1
        return {
            "memories": [{
                "content": "用户在 2021 年曾持续写晨间日记",
                "category": "routine", "importance": 0.7, "confidence": 0.85,
                "status": "historical", "time_start": "2021-01-01",
                "time_end": "2021-12-31", "evidence": "连续写了晨间日记",
            }],
            "activities": [{
                "title": "完成历史日记整理", "description": "整理旧笔记",
                "tags": ["整理"], "confidence": 0.9,
            }],
        }


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.store = DiaryStore(self.root / "diary.sqlite")
        self.index = HistoricalMemoryIndex(self.store, LocalEmbedder())
        self.memory = LongTermMemory(self.store, LocalEmbedder())
        self.activities = ActivityService(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_scan_prefers_frontmatter_date_is_incremental_and_preserves_file(self):
        path = self.vault / "wrong-2020-01-01.md"
        original = "---\ndate: 2021-03-04\ntags: [生活, 城市]\n---\n# 春日\n\n去了咖啡店写作。\n\n- [x] 整理书桌\n"
        path.write_text(original, encoding="utf-8")
        importer = ObsidianHistoryImporter(
            self.index, self.memory, self.activities, self.vault
        )
        first = importer.scan()
        self.assertEqual(first["imported"], 1)
        document = self.store.connection.execute(
            "SELECT * FROM historical_documents"
        ).fetchone()
        self.assertEqual(document["diary_date"], "2021-03-04")
        self.assertEqual(document["raw_content"], original)
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual(importer.scan()["unchanged"], 1)
        self.assertIn("咖啡店", self.index.search("那家咖啡店", 1)[0].content)
        review = self.activities.review_day("2021-03-04")
        self.assertEqual(review["title"], "春日")
        self.assertEqual(review["other_activities"][0]["title"], "整理书桌")

        path.write_text(original + "\n后来又去散步。", encoding="utf-8")
        changed = importer.scan()
        self.assertEqual(changed["updated"], 1)
        self.assertIn("散步", "".join(
            row[0] for row in self.store.connection.execute(
                "SELECT content FROM historical_chunks"
            ).fetchall()
        ))

    def test_filename_date_fallback_and_skips_undated_files(self):
        (self.vault / "2022-7-8 日记.md").write_text("普通一天", encoding="utf-8")
        (self.vault / "notes.md").write_text("没有日期", encoding="utf-8")
        importer = ObsidianHistoryImporter(
            self.index, self.memory, self.activities, self.vault
        )
        result = importer.scan()
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], 1)

    def test_model_extraction_is_explicit_batched_and_resumable(self):
        path = self.vault / "2021-05-06.md"
        path.write_text("A" * 1800 + "\n\n" + "B" * 1800, encoding="utf-8")
        provider = HistoryProvider()
        importer = ObsidianHistoryImporter(
            self.index, self.memory, self.activities, self.vault, provider
        )
        importer.scan()
        first = importer.extract(max_batches=1, batch_size=1)
        self.assertEqual(first["processed_batches"], 1)
        self.assertEqual(first["pending_extraction"], 1)
        cursor = self.store.connection.execute(
            "SELECT extraction_cursor FROM historical_documents"
        ).fetchone()[0]
        self.assertEqual(cursor, 1)
        while importer.extract(max_batches=2, batch_size=1)["pending_extraction"]:
            pass
        document = self.store.connection.execute(
            "SELECT extraction_status FROM historical_documents"
        ).fetchone()[0]
        self.assertEqual(document, "done")
        memory = self.memory.list()[0]
        self.assertEqual(memory["temporal_status"], "historical")
        self.assertEqual(memory["source_date"], "2021-05-06")
        evidence = self.store.connection.execute(
            "SELECT * FROM historical_memory_evidence"
        ).fetchone()
        self.assertEqual(evidence["source_file"], str(path.resolve()))

    def test_without_explicit_model_only_local_indexing_runs(self):
        (self.vault / "2020-01-02.md").write_text("过去在这里生活。", encoding="utf-8")
        importer = ObsidianHistoryImporter(
            self.index, self.memory, self.activities, self.vault
        )
        importer.scan()
        with self.assertRaisesRegex(ValueError, "未配置 HISTORY_LLM_MODEL"):
            importer.extract()

        provider = HistoryProvider()
        enabled_later = ObsidianHistoryImporter(
            self.index, self.memory, self.activities, self.vault, provider
        )
        enabled_later.scan()
        status = self.store.connection.execute(
            "SELECT extraction_status FROM historical_documents"
        ).fetchone()[0]
        self.assertEqual(status, "pending")

    def test_current_source_upgrade_and_removal_preserves_historical_evidence(self):
        path = self.vault / "2020-01-03.md"
        path.write_text("过去每天晨跑。", encoding="utf-8")
        provider = HistoryProvider()
        importer = ObsidianHistoryImporter(
            self.index, self.memory, self.activities, self.vault, provider
        )
        importer.scan()
        while importer.extract(max_batches=2, batch_size=2)["pending_extraction"]:
            pass
        content = "用户在 2021 年曾持续写晨间日记"
        self.memory.remember([{
            "content": content, "category": "routine", "importance": 0.8
        }], "2026-09-09")
        self.assertEqual(self.memory.list()[0]["temporal_status"], "current")
        self.memory.forget_day("2026-09-09")
        remaining = self.memory.list()[0]
        self.assertEqual(remaining["temporal_status"], "historical")
        self.assertEqual(remaining["time_start"], "2021-01-01")


if __name__ == "__main__":
    unittest.main()

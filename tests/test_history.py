import tempfile
import unittest
from os import environ
from pathlib import Path
from unittest.mock import patch

from diary_agent.activities import ActivityService
from diary_agent.config import Settings
from diary_agent.history import HistoricalMemoryIndex, ObsidianHistoryImporter
from diary_agent.memory import EmbeddingProvider, LongTermMemory
from diary_agent.store import DiaryStore


class LocalEmbedder:
    signature = "local:test-v1"

    def embed(self, texts):
        return [[1.0, float("散步" in text)] for text in texts]


class CountingSemanticEmbedder:
    signature = "local:semantic-v2"

    def __init__(self):
        self.batches = []

    def embed(self, texts):
        self.batches.append(list(texts))
        vectors = []
        for text in texts:
            if any(word in text for word in ("沿江晨跑", "锻炼习惯")):
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors


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

    def test_embedding_backfill_is_explicit_batched_and_resumable(self):
        (self.vault / "2020-01-01.md").write_text("第一篇历史日记。", encoding="utf-8")
        (self.vault / "2020-01-02.md").write_text("第二篇历史日记。", encoding="utf-8")
        keyword_index = HistoricalMemoryIndex(self.store)
        importer = ObsidianHistoryImporter(
            keyword_index, LongTermMemory(self.store), self.activities, self.vault
        )
        importer.scan()
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM historical_chunks WHERE embedding IS NULL"
            ).fetchone()[0],
            2,
        )

        embedder = CountingSemanticEmbedder()
        hybrid_index = HistoricalMemoryIndex(
            self.store, embedder, search_mode="hybrid"
        )
        first = hybrid_index.backfill_embeddings(batch_size=1, max_batches=1)
        self.assertEqual(first["processed_embeddings"], 1)
        self.assertEqual(first["pending_embeddings"], 1)
        self.assertEqual(len(embedder.batches), 1)

        # A fresh index instance simulates stopping and rerunning the CLI command.
        resumed_embedder = CountingSemanticEmbedder()
        resumed = HistoricalMemoryIndex(
            self.store, resumed_embedder, search_mode="hybrid"
        ).backfill_embeddings(batch_size=1, max_batches=10)
        self.assertEqual(resumed["processed_embeddings"], 1)
        self.assertEqual(resumed["pending_embeddings"], 0)
        self.assertEqual(len(resumed_embedder.batches), 1)
        signatures = {
            row[0] for row in self.store.connection.execute(
                "SELECT embedding_signature FROM historical_chunks"
            ).fetchall()
        }
        self.assertEqual(signatures, {CountingSemanticEmbedder.signature})

    def test_historical_hybrid_search_finds_semantic_match_without_lexical_overlap(self):
        (self.vault / "2020-02-03.md").write_text(
            "今天沿江晨跑五公里，回来后状态很好。", encoding="utf-8"
        )
        (self.vault / "2020-02-04.md").write_text(
            "晚上给朋友做了一顿饭。", encoding="utf-8"
        )
        keyword_index = HistoricalMemoryIndex(self.store)
        importer = ObsidianHistoryImporter(
            keyword_index, LongTermMemory(self.store), self.activities, self.vault
        )
        importer.scan()

        embedder = CountingSemanticEmbedder()
        index = HistoricalMemoryIndex(self.store, embedder, search_mode="hybrid")
        self.assertEqual(index.search("锻炼习惯", top_k=1), [])
        index.backfill_embeddings(batch_size=10, max_batches=10)
        hits = index.search("锻炼习惯", top_k=10)
        self.assertEqual(len(hits), 1)
        self.assertIn("沿江晨跑", hits[0].content)

    def test_corrupt_vector_is_skipped_without_disabling_other_vector_hits(self):
        rows = [
            {"id": 1, "embedding": "not-json", "content": "损坏数据"},
            {"id": 2, "embedding": "[1.0, 0.0]", "content": "有效数据"},
            {"id": 3, "embedding": "[0.0, 1.0]", "content": "无关数据"},
        ]
        ranked = LongTermMemory._rank_vectors(
            rows, [1.0, 0.0], limit=10, min_similarity=0.1
        )
        self.assertEqual([row["id"] for row in ranked], [2])

    def test_history_status_reports_effective_mode_and_vector_progress(self):
        (self.vault / "2020-03-01.md").write_text("今天读了一本书。", encoding="utf-8")
        keyword_index = HistoricalMemoryIndex(self.store)
        ObsidianHistoryImporter(
            keyword_index, LongTermMemory(self.store), self.activities, self.vault
        ).scan()

        keyword_status = keyword_index.status()
        self.assertEqual(keyword_status["search_mode"], "keyword")
        self.assertEqual(keyword_status["embedded_chunks"], 0)
        self.assertEqual(keyword_status["pending_embeddings"], 1)

        index = HistoricalMemoryIndex(
            self.store, CountingSemanticEmbedder(), search_mode="hybrid"
        )
        before = index.status()
        self.assertEqual(before["configured_search_mode"], "hybrid")
        self.assertEqual(before["search_mode"], "keyword")
        self.assertEqual(before["fallback_reason"], "embeddings_not_built")
        self.assertEqual(before["embedded_chunks"], 0)
        self.assertEqual(before["pending_embeddings"], 1)
        index.backfill_embeddings(batch_size=10, max_batches=10)
        after = index.status()
        self.assertEqual(after["embedded_chunks"], 1)
        self.assertEqual(after["pending_embeddings"], 0)
        self.assertEqual(after["embedding_signature"], CountingSemanticEmbedder.signature)


class ConfigTests(unittest.TestCase):
    def test_openai_compatible_signature_keeps_existing_vectors_usable(self):
        embedder = EmbeddingProvider(
            base_url="https://embedding.example/v1",
            api_key="test-key",
            model="multilingual-v1",
            provider="openai-compatible",
        )
        self.assertEqual(
            embedder.signature,
            "https://embedding.example/v1:multilingual-v1",
        )

    def test_ollama_embedding_preset_selects_local_hybrid_search(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(environ, {
            "OBSIDIAN_VAULT_PATH": tmp,
            "LLM_PROVIDER": "ollama",
            "OLLAMA_MODEL": "chat-model",
            "EMBEDDING_PROVIDER": "ollama",
            "OLLAMA_EMBEDDING_MODEL": "embeddinggemma",
            "MEMORY_SEARCH_MODE": "hybrid",
        }, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.embedding_provider, "ollama")
        self.assertEqual(settings.embedding_base_url, "http://127.0.0.1:11434/v1")
        self.assertEqual(settings.embedding_api_key, "ollama")
        self.assertEqual(settings.embedding_model, "embeddinggemma")
        self.assertEqual(settings.memory_search_mode, "hybrid")

    def test_ollama_embedding_never_inherits_a_cloud_embedding_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(environ, {
            "OBSIDIAN_VAULT_PATH": tmp,
            "LLM_PROVIDER": "ollama",
            "OLLAMA_MODEL": "chat-model",
            "EMBEDDING_PROVIDER": "ollama",
            "OLLAMA_EMBEDDING_MODEL": "embeddinggemma",
            "EMBEDDING_BASE_URL": "https://remote.example/v1",
            "EMBEDDING_API_KEY": "remote-secret",
            "EMBEDDING_MODEL": "remote-model",
        }, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.embedding_base_url, "http://127.0.0.1:11434/v1")
        self.assertEqual(settings.embedding_api_key, "ollama")
        self.assertEqual(settings.embedding_model, "embeddinggemma")


if __name__ == "__main__":
    unittest.main()

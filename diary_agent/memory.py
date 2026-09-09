from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass

from openai import OpenAI


@dataclass(frozen=True)
class MemoryHit:
    id: int
    content: str
    category: str
    importance: float
    first_seen: str
    last_seen: str
    score: float
    temporal_status: str = "current"
    confidence: float = 1.0
    source_file: str | None = None
    source_date: str | None = None


class EmbeddingProvider:
    def __init__(self, *, base_url: str, api_key: str, model: str):
        if not (base_url and api_key and model):
            raise ValueError(
                "Hybrid 长期记忆需要设置 EMBEDDING_BASE_URL、"
                "EMBEDDING_API_KEY 和 EMBEDDING_MODEL"
            )
        self.model = model
        self.base_url = base_url
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    @property
    def signature(self) -> str:
        return f"{self.base_url}:{self.model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self.client.embeddings.create(model=self.model, input=texts)
        return [item.embedding for item in response.data]


class LongTermMemory:
    """SQLite FTS5 + exact cosine vector search with reciprocal-rank fusion."""

    CATEGORIES = {
        "person", "project", "goal", "preference", "routine",
        "commitment", "life_event", "other",
    }

    def __init__(self, store, embedder=None):
        self.store = store
        self.embedder = embedder
        with self.store._lock:
            self.store.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_key TEXT NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    category TEXT NOT NULL,
                    importance REAL NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    embedding TEXT,
                    embedding_signature TEXT
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    memory_id UNINDEXED,
                    content,
                    tokenize='trigram'
                );
                CREATE TABLE IF NOT EXISTS memory_sources (
                    memory_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    PRIMARY KEY(memory_id, day),
                    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS historical_memory_evidence (
                    memory_id INTEGER NOT NULL,
                    document_id INTEGER NOT NULL,
                    source_file TEXT NOT NULL,
                    source_date TEXT NOT NULL,
                    time_start TEXT,
                    time_end TEXT,
                    confidence REAL NOT NULL,
                    temporal_status TEXT NOT NULL,
                    snippet TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(memory_id, document_id, snippet),
                    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
                );
                """
            )
        self.store._ensure_column("memories", "temporal_status", "TEXT NOT NULL DEFAULT 'current'")
        self.store._ensure_column("memories", "confidence", "REAL NOT NULL DEFAULT 1.0")
        self.store._ensure_column("memories", "source_file", "TEXT")
        self.store._ensure_column("memories", "source_date", "TEXT")
        self.store._ensure_column("memories", "time_start", "TEXT")
        self.store._ensure_column("memories", "time_end", "TEXT")
        self.store.connection.execute("PRAGMA optimize")

    @property
    def mode(self) -> str:
        return "hybrid" if self.embedder else "keyword"

    @staticmethod
    def _key(content: str) -> str:
        return re.sub(r"\s+", "", content).casefold()

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right) or not left:
            return -1.0
        dot = sum(a * b for a, b in zip(left, right))
        norm = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
        return dot / norm if norm else -1.0

    @staticmethod
    def _match_expression(query: str) -> str | None:
        compact = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "", query)
        terms = [compact[i:i + 3] for i in range(max(0, len(compact) - 2))]
        terms.extend(re.findall(r"[A-Za-z0-9_-]{3,}", query))
        unique = list(dict.fromkeys(terms))[:48]
        return " OR ".join(f'"{term}"' for term in unique) if unique else None

    def remember(self, items: list[dict], day: str) -> int:
        cleaned = []
        for item in items:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            category = str(item.get("category", "other")).strip().lower()
            if category not in self.CATEGORIES:
                category = "other"
            try:
                importance = max(0.0, min(float(item.get("importance", 0.5)), 1.0))
            except (TypeError, ValueError):
                importance = 0.5
            cleaned.append((self._key(content), content, category, importance))
        self.forget_day(day)
        if not cleaned:
            return 0

        vectors = [None] * len(cleaned)
        signature = None
        if self.embedder:
            try:
                vectors = self.embedder.embed([item[1] for item in cleaned])
                if len(vectors) != len(cleaned):
                    raise ValueError("Embedding 返回数量与输入不一致")
                signature = self.embedder.signature
            except Exception as error:
                logging.getLogger(__name__).warning(
                    "Embedding 写入失败，本次记忆仅建立 FTS5 索引：%s", error
                )

        with self.store._lock, self.store.connection:
            for (key, content, category, importance), vector in zip(cleaned, vectors):
                existing = self.store.connection.execute(
                    "SELECT id, importance FROM memories WHERE memory_key = ?", (key,)
                ).fetchone()
                encoded = json.dumps(vector) if vector is not None else None
                if existing:
                    memory_id = int(existing["id"])
                    self.store.connection.execute(
                        "UPDATE memories SET content=?, category=?, importance=?, "
                        "last_seen=?, embedding=COALESCE(?, embedding), "
                        "embedding_signature=COALESCE(?, embedding_signature), "
                        "temporal_status='current', confidence=1.0 WHERE id=?",
                        (content, category, max(importance, existing["importance"]), day,
                         encoded, signature, memory_id),
                    )
                    self.store.connection.execute(
                        "DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,)
                    )
                else:
                    cursor = self.store.connection.execute(
                        "INSERT INTO memories(memory_key, content, category, importance, "
                        "first_seen, last_seen, embedding, embedding_signature) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (key, content, category, importance, day, day, encoded, signature),
                    )
                    memory_id = int(cursor.lastrowid)
                self.store.connection.execute(
                    "INSERT INTO memory_fts(memory_id, content) VALUES (?, ?)",
                    (memory_id, content),
                )
                self.store.connection.execute(
                    "INSERT OR IGNORE INTO memory_sources(memory_id, day) VALUES (?, ?)",
                    (memory_id, day),
                )
        return len(cleaned)

    def begin_historical_document(self, document_id: int) -> None:
        """Remove the previous extraction for one changed source without touching current memory."""
        with self.store._lock, self.store.connection:
            ids = [row[0] for row in self.store.connection.execute(
                "SELECT DISTINCT memory_id FROM historical_memory_evidence WHERE document_id=?",
                (document_id,),
            ).fetchall()]
            self.store.connection.execute(
                "DELETE FROM historical_memory_evidence WHERE document_id=?", (document_id,)
            )
            for memory_id in ids:
                current = self.store.connection.execute(
                    "SELECT 1 FROM memory_sources WHERE memory_id=? LIMIT 1", (memory_id,)
                ).fetchone()
                historical = self.store.connection.execute(
                    "SELECT 1 FROM historical_memory_evidence WHERE memory_id=? LIMIT 1",
                    (memory_id,),
                ).fetchone()
                if not current and not historical:
                    self.store.connection.execute(
                        "DELETE FROM memory_fts WHERE memory_id=?", (memory_id,)
                    )
                    self.store.connection.execute("DELETE FROM memories WHERE id=?", (memory_id,))

    def remember_historical(self, items: list[dict], document: dict) -> int:
        statuses = {"historical", "current", "uncertain"}
        cleaned = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            category = str(item.get("category", "other")).lower()
            if category not in self.CATEGORIES:
                category = "other"
            status = str(item.get("status", "historical")).lower()
            if status not in statuses:
                status = "uncertain"
            try:
                importance = max(0.0, min(float(item.get("importance", 0.5)), 1.0))
                confidence = max(0.0, min(float(item.get("confidence", 0.7)), 1.0))
            except (TypeError, ValueError):
                importance, confidence = 0.5, 0.7
            cleaned.append((item, content, category, status, importance, confidence))
        vectors = [None] * len(cleaned)
        signature = None
        if self.embedder and cleaned:
            try:
                vectors = self.embedder.embed([item[1] for item in cleaned])
                if len(vectors) != len(cleaned):
                    raise ValueError("Embedding 返回数量与输入不一致")
                signature = self.embedder.signature
            except Exception as error:
                logging.getLogger(__name__).warning("历史长期记忆向量化失败：%s", error)
        with self.store._lock, self.store.connection:
            for (raw, content, category, status, importance, confidence), vector in zip(cleaned, vectors):
                time_start = raw.get("time_start") or document["diary_date"]
                time_end = raw.get("time_end") or document["diary_date"]
                key = self._key(content)
                existing = self.store.connection.execute(
                    "SELECT id, importance, temporal_status FROM memories WHERE memory_key=?", (key,)
                ).fetchone()
                encoded = json.dumps(vector) if vector is not None else None
                if existing:
                    memory_id = int(existing["id"])
                    merged_status = "current" if existing["temporal_status"] == "current" else status
                    self.store.connection.execute(
                        "UPDATE memories SET importance=?, confidence=MAX(confidence, ?), "
                        "temporal_status=?, embedding=COALESCE(?, embedding), "
                        "embedding_signature=COALESCE(?, embedding_signature), "
                        "source_file=COALESCE(source_file, ?), source_date=COALESCE(source_date, ?), "
                        "time_start=COALESCE(time_start, ?), time_end=COALESCE(time_end, ?) WHERE id=?",
                        (max(importance, existing["importance"]), confidence, merged_status,
                         encoded, signature, document["source_file"], document["diary_date"],
                         time_start, time_end, memory_id),
                    )
                else:
                    cursor = self.store.connection.execute(
                        "INSERT INTO memories(memory_key, content, category, importance, first_seen, "
                        "last_seen, embedding, embedding_signature, temporal_status, confidence, "
                        "source_file, source_date, time_start, time_end) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (key, content, category, importance, document["diary_date"],
                         document["diary_date"], encoded, signature, status, confidence,
                         document["source_file"], document["diary_date"],
                         time_start, time_end),
                    )
                    memory_id = int(cursor.lastrowid)
                    self.store.connection.execute(
                        "INSERT INTO memory_fts(memory_id, content) VALUES (?, ?)",
                        (memory_id, content),
                    )
                self.store.connection.execute(
                    "INSERT OR IGNORE INTO historical_memory_evidence(memory_id, document_id, "
                    "source_file, source_date, time_start, time_end, confidence, temporal_status, snippet) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (memory_id, document["id"], document["source_file"], document["diary_date"],
                     time_start, time_end, confidence, status,
                     str(raw.get("evidence", ""))[:500]),
                )
        return len(cleaned)

    def forget_day(self, day: str) -> int:
        """Remove one day's contribution and delete memories with no other source day."""
        with self.store._lock, self.store.connection:
            ids = [row[0] for row in self.store.connection.execute(
                "SELECT memory_id FROM memory_sources WHERE day = ?", (day,)
            ).fetchall()]
            self.store.connection.execute(
                "DELETE FROM memory_sources WHERE day = ?", (day,)
            )
            for memory_id in ids:
                dates = self.store.connection.execute(
                    "SELECT MIN(day), MAX(day) FROM memory_sources WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone()
                if dates[0] is None:
                    evidence = self.store.connection.execute(
                        "SELECT MIN(source_date), MAX(source_date), "
                        "CASE WHEN SUM(temporal_status='current')>0 THEN 'current' "
                        "WHEN SUM(temporal_status='uncertain')>0 THEN 'uncertain' ELSE 'historical' END "
                        "FROM historical_memory_evidence WHERE memory_id=?", (memory_id,)
                    ).fetchone()
                    if evidence and evidence[0] is not None:
                        self.store.connection.execute(
                            "UPDATE memories SET first_seen=?, last_seen=?, temporal_status=? WHERE id=?",
                            (evidence[0], evidence[1], evidence[2], memory_id),
                        )
                    else:
                        self.store.connection.execute(
                            "DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,)
                        )
                        self.store.connection.execute(
                            "DELETE FROM memories WHERE id = ?", (memory_id,)
                        )
                else:
                    self.store.connection.execute(
                        "UPDATE memories SET first_seen=?, last_seen=? WHERE id=?",
                        (dates[0], dates[1], memory_id),
                    )
        return len(ids)

    def search(self, query: str, top_k: int = 5) -> list[MemoryHit]:
        top_k = max(1, min(top_k, 10))
        candidate_count = top_k * 4
        lexical = []
        expression = self._match_expression(query)
        with self.store._lock:
            if expression:
                rows = self.store.connection.execute(
                    "SELECT m.* FROM memory_fts f JOIN memories m ON m.id=f.memory_id "
                    "WHERE memory_fts MATCH ? ORDER BY bm25(memory_fts) LIMIT ?",
                    (expression, candidate_count),
                ).fetchall()
                lexical = [dict(row) for row in rows]
            all_rows = [dict(row) for row in self.store.connection.execute(
                "SELECT * FROM memories ORDER BY importance DESC"
            ).fetchall()]

        vector = []
        if self.embedder and all_rows:
            try:
                missing = [
                    row for row in all_rows
                    if not row["embedding"]
                    or row["embedding_signature"] != self.embedder.signature
                ]
                if missing:
                    embeddings = self.embedder.embed(
                        [row["content"] for row in missing]
                    )
                    if len(embeddings) != len(missing):
                        raise ValueError("Embedding 返回数量与输入不一致")
                    with self.store._lock, self.store.connection:
                        for row, embedding in zip(missing, embeddings):
                            encoded = json.dumps(embedding)
                            self.store.connection.execute(
                                "UPDATE memories SET embedding=?, "
                                "embedding_signature=? WHERE id=?",
                                (encoded, self.embedder.signature, row["id"]),
                            )
                            row["embedding"] = encoded
                            row["embedding_signature"] = self.embedder.signature
                query_vector = self.embedder.embed([query])[0]
                compatible = [row for row in all_rows if row["embedding"] and
                              row["embedding_signature"] == self.embedder.signature]
                vector = sorted(
                    compatible,
                    key=lambda row: self._cosine(
                        query_vector, json.loads(row["embedding"])
                    ),
                    reverse=True,
                )[:candidate_count]
            except Exception as error:
                logging.getLogger(__name__).warning(
                    "Embedding 检索失败，本次降级为 FTS5：%s", error
                )

        fused: dict[int, dict] = {}
        for weight, candidates in ((0.4, lexical), (0.6, vector)):
            for rank, item in enumerate(candidates, 1):
                entry = fused.setdefault(item["id"], dict(item, score=0.0))
                entry["score"] += weight / (60 + rank)
        ranked = sorted(
            fused.values(),
            key=lambda item: (item["score"] * (0.75 + 0.25 * item["importance"])),
            reverse=True,
        )[:top_k]
        return [MemoryHit(
            id=item["id"], content=item["content"], category=item["category"],
            importance=item["importance"], first_seen=item["first_seen"],
            last_seen=item["last_seen"], score=item["score"],
            temporal_status=item.get("temporal_status", "current"),
            confidence=item.get("confidence", 1.0), source_file=item.get("source_file"),
            source_date=item.get("source_date"),
        ) for item in ranked]

    def list(self) -> list[dict]:
        with self.store._lock:
            rows = self.store.connection.execute(
                "SELECT id, content, category, importance, first_seen, last_seen, "
                "temporal_status, confidence, source_file, source_date, time_start, time_end "
                "FROM memories ORDER BY last_seen DESC, importance DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete(self, memory_id: int) -> bool:
        with self.store._lock, self.store.connection:
            self.store.connection.execute(
                "DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,)
            )
            cursor = self.store.connection.execute(
                "DELETE FROM memories WHERE id = ?", (memory_id,)
            )
        return bool(cursor.rowcount)

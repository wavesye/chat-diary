from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx


logger = logging.getLogger(__name__)


class LocalONNXEmbeddingProvider:
    """In-process all-MiniLM-L6-v2 embeddings with a verified local cache.

    The inference pipeline follows the public Chroma ONNX implementation, while
    keeping this project independent from Chroma's vector database.  Long inputs
    are encoded as overlapping tokenizer windows and aggregated into one vector,
    so existing SQLite history chunks can stay intact.
    """

    provider = "local"
    model = "all-MiniLM-L6-v2"
    supports_cjk = False

    MODEL_DOWNLOAD_URL = (
        "https://chroma-onnx-models.s3.amazonaws.com/"
        "all-MiniLM-L6-v2/onnx.tar.gz"
    )
    MODEL_SHA256 = (
        "913d7300ceae3b2dbc2c50d1de4baacab4be7b9380491c27fab7418616a16ec3"
    )
    REQUIRED_FILES = (
        "config.json",
        "model.onnx",
        "special_tokens_map.json",
        "tokenizer_config.json",
        "tokenizer.json",
        "vocab.txt",
    )
    MAX_TOKENS = 256
    WINDOW_STRIDE = 32
    DIMENSIONS = 384
    MAX_ARCHIVE_BYTES = 200 * 1024 * 1024

    def __init__(self, *, cache_dir: Path | str | None = None,
                 threads: int = 2, batch_size: int = 32):
        default_cache = (
            Path.home() / ".cache" / "chat-diary" / "onnx_models" / self.model
        )
        self.cache_dir = Path(cache_dir or default_cache).expanduser()
        self.threads = max(1, min(int(threads), 32))
        self.batch_size = max(1, min(int(batch_size), 256))
        self._runtime_lock = threading.RLock()
        self._runtime_state = "not_loaded"
        self._runtime_error: str | None = None
        self._tokenizer = None
        self._session = None

    @property
    def signature(self) -> str:
        algorithm = "max256-stride32-windowmean-l2-v1"
        return f"local-onnx:{self.model}:{self.MODEL_SHA256[:12]}:{algorithm}"

    @property
    def runtime_state(self) -> str:
        return self._runtime_state

    @property
    def runtime_error(self) -> str | None:
        return self._runtime_error

    @property
    def model_cached(self) -> bool:
        return self._model_files_ready()

    @property
    def model_dir(self) -> Path:
        return self.cache_dir / "onnx"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not all(isinstance(text, str) for text in texts):
            raise TypeError("Embedding 输入必须是字符串列表")
        self._ensure_runtime()
        try:
            import numpy as np

            with self._runtime_lock:
                encodings = []
                owners: list[int] = []
                for owner, text in enumerate(texts):
                    first = self._tokenizer.encode(text)
                    windows = [first, *list(first.overflowing)]
                    encodings.extend(windows)
                    owners.extend([owner] * len(windows))

                window_vectors = []
                for start in range(0, len(encodings), self.batch_size):
                    batch = encodings[start:start + self.batch_size]
                    input_ids = np.asarray([item.ids for item in batch], dtype=np.int64)
                    attention_mask = np.asarray(
                        [item.attention_mask for item in batch], dtype=np.int64
                    )
                    token_type_ids = np.zeros_like(input_ids, dtype=np.int64)
                    last_hidden_state = self._session.run(None, {
                        "input_ids": input_ids,
                        "attention_mask": attention_mask,
                        "token_type_ids": token_type_ids,
                    })[0]
                    expanded_mask = np.broadcast_to(
                        np.expand_dims(attention_mask, -1), last_hidden_state.shape
                    )
                    pooled = np.sum(last_hidden_state * expanded_mask, axis=1) / np.clip(
                        expanded_mask.sum(axis=1), a_min=1e-9, a_max=None
                    )
                    window_vectors.append(self._normalize(pooled, np))

                combined = np.concatenate(window_vectors, axis=0)
                if combined.ndim != 2 or combined.shape[1] != self.DIMENSIONS:
                    raise RuntimeError(
                        f"本地 ONNX 模型返回了异常向量维度：{combined.shape}"
                    )
                document_vectors = np.zeros(
                    (len(texts), self.DIMENSIONS), dtype=np.float32
                )
                counts = np.zeros(len(texts), dtype=np.float32)
                for owner, vector in zip(owners, combined):
                    document_vectors[owner] += vector
                    counts[owner] += 1.0
                document_vectors /= np.maximum(counts[:, np.newaxis], 1.0)
                document_vectors = self._normalize(document_vectors, np)
                return document_vectors.astype(np.float32).tolist()
        except Exception as error:
            with self._runtime_lock:
                self._runtime_state = "error"
                self._runtime_error = f"{type(error).__name__}: {error}"
            raise RuntimeError(f"本地 ONNX Embedding 运行失败：{error}") from error

    @staticmethod
    def _normalize(vectors, np):
        norms = np.linalg.norm(vectors, axis=1)
        norms[norms == 0] = 1e-12
        return vectors / norms[:, np.newaxis]

    def _ensure_runtime(self):
        with self._runtime_lock:
            if self._runtime_state == "ready":
                return self._tokenizer, self._session
            if self._runtime_state == "error":
                raise RuntimeError(
                    "本地 ONNX Embedding 本进程初始化失败；修复配置后请重启："
                    f"{self._runtime_error}"
                )
            try:
                model_dir = self._ensure_model_files()
                self._tokenizer, self._session = self._load_runtime(model_dir)
            except Exception as error:
                self._runtime_state = "error"
                self._runtime_error = f"{type(error).__name__}: {error}"
                raise RuntimeError(f"本地 ONNX Embedding 初始化失败：{error}") from error
            self._runtime_state = "ready"
            self._runtime_error = None
            return self._tokenizer, self._session

    def _load_runtime(self, model_dir: Path):
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as error:
            raise RuntimeError(
                "缺少本地向量依赖，请重新运行 pip install -r requirements.txt"
            ) from error

        tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        tokenizer.enable_truncation(
            max_length=self.MAX_TOKENS, stride=self.WINDOW_STRIDE
        )
        tokenizer.enable_padding(
            pad_id=0, pad_token="[PAD]", length=self.MAX_TOKENS
        )
        options = ort.SessionOptions()
        options.log_severity_level = 3
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.intra_op_num_threads = self.threads
        options.inter_op_num_threads = self.threads
        session = ort.InferenceSession(
            str(model_dir / "model.onnx"),
            providers=["CPUExecutionProvider"],
            sess_options=options,
        )
        return tokenizer, session

    def _model_files_ready(self) -> bool:
        return all(
            (self.model_dir / name).is_file()
            and (self.model_dir / name).stat().st_size > 0
            for name in self.REQUIRED_FILES
        )

    @contextmanager
    def _download_lock(self):
        self.cache_dir.parent.mkdir(parents=True, exist_ok=True)
        handle = (self.cache_dir.parent / f".{self.model}.lock").open("a+b")
        try:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - Windows fallback
                pass
            yield
        finally:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover - Windows fallback
                pass
            handle.close()

    def _ensure_model_files(self) -> Path:
        if self._model_files_ready():
            return self.model_dir
        with self._download_lock():
            if self._model_files_ready():
                return self.model_dir
            logger.warning(
                "首次使用本地向量模型，正在下载 %s（约 80 MB）；之后将完全从本地加载。",
                self.model,
            )
            with tempfile.TemporaryDirectory(
                prefix=".model-download-", dir=self.cache_dir.parent
            ) as temporary:
                temporary_dir = Path(temporary)
                archive = temporary_dir / "onnx.tar.gz"
                self._download_archive(archive)
                extracted = temporary_dir / "extracted" / "onnx"
                self._extract_required_files(archive, extracted)
                self.model_dir.mkdir(parents=True, exist_ok=True)
                for name in self.REQUIRED_FILES:
                    os.replace(extracted / name, self.model_dir / name)
            if not self._model_files_ready():
                raise RuntimeError("模型文件下载完成但校验不完整")
        return self.model_dir

    def _download_archive(self, target: Path) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            partial = target.with_suffix(f".part-{attempt}")
            try:
                digest = hashlib.sha256()
                timeout = httpx.Timeout(300.0, connect=20.0)
                with httpx.stream(
                    "GET", self.MODEL_DOWNLOAD_URL, follow_redirects=True,
                    timeout=timeout,
                ) as response:
                    response.raise_for_status()
                    content_length = int(response.headers.get("content-length", "0"))
                    if content_length > self.MAX_ARCHIVE_BYTES:
                        raise RuntimeError("模型下载文件超过预期大小")
                    downloaded = 0
                    with partial.open("wb") as output:
                        for block in response.iter_bytes(chunk_size=1024 * 1024):
                            downloaded += len(block)
                            if downloaded > self.MAX_ARCHIVE_BYTES:
                                raise RuntimeError("模型下载文件超过预期大小")
                            output.write(block)
                            digest.update(block)
                if digest.hexdigest() != self.MODEL_SHA256:
                    raise RuntimeError("下载文件的 SHA256 校验失败")
                os.replace(partial, target)
                return
            except Exception as error:
                last_error = error
                partial.unlink(missing_ok=True)
                if attempt < 3:
                    time.sleep(attempt)
        raise RuntimeError(f"本地向量模型下载失败：{last_error}") from last_error

    def _extract_required_files(self, archive: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:gz") as bundle:
            members = {member.name.lstrip("./"): member for member in bundle.getmembers()}
            for name in self.REQUIRED_FILES:
                member = members.get(f"onnx/{name}")
                if member is None or not member.isfile():
                    raise RuntimeError(f"模型压缩包缺少安全的 onnx/{name}")
                source = bundle.extractfile(member)
                if source is None:
                    raise RuntimeError(f"无法读取模型文件 onnx/{name}")
                with source, (destination / name).open("wb") as output:
                    shutil.copyfileobj(source, output)

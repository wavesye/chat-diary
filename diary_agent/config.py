from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _clock(value: str, name: str) -> str:
    try:
        hour, minute = (int(part) for part in value.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <=
                59):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须使用 HH:MM 格式，例如 08:30") from None
    return f"{hour:02d}:{minute:02d}"


@dataclass(frozen=True)
class Settings:
    vault_path: Path
    journal_folder: str
    database_path: Path
    timezone: ZoneInfo
    provider: str
    model: str
    base_url: str
    api_key: str
    user_name: str
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    memory_top_k: int = 5
    todo_reminders_enabled: bool = True
    todo_morning_time: str = "08:30"
    todo_evening_time: str = "21:30"
    history_path: Path | None = None
    history_extract_model: str = ""
    history_extract_base_url: str = ""
    history_extract_api_key: str = ""
    todo_multilingual_model_fallback: bool = True
    embedding_provider: str = ""
    memory_search_mode: str = "hybrid"
    memory_vector_min_similarity: float = 0.1
    local_embedding_cache_dir: Path | None = None
    local_embedding_threads: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
        presets = {
            "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "OPENAI_MODEL"),
            "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"),
            "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "OPENROUTER_MODEL"),
            "ollama": ("http://127.0.0.1:11434/v1", "OLLAMA_API_KEY", "OLLAMA_MODEL"),
            "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", "QWEN_MODEL"),
        }
        default_url, key_var, model_var = presets.get(
            provider, ("", "LLM_API_KEY", "LLM_MODEL")
        )
        base_url = os.getenv("LLM_BASE_URL", default_url)
        api_key = os.getenv("LLM_API_KEY") or os.getenv(key_var, "")
        model = os.getenv("LLM_MODEL") or os.getenv(model_var, "")
        vault = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
        if not vault:
            raise ValueError("请设置 OBSIDIAN_VAULT_PATH（Obsidian vault 的绝对路径）")
        if not model:
            raise ValueError("请设置 LLM_MODEL，或所选 Provider 对应的模型变量")
        if not api_key and provider != "ollama":
            raise ValueError("请设置 LLM_API_KEY，或所选 Provider 对应的 API Key")
        timezone = ZoneInfo(os.getenv("DIARY_TIMEZONE", "Asia/Shanghai"))
        morning = _clock(os.getenv("TODO_MORNING_REMINDER_TIME", "08:30"),
                         "TODO_MORNING_REMINDER_TIME")
        evening = _clock(os.getenv("TODO_EVENING_REMINDER_TIME", "21:30"),
                         "TODO_EVENING_REMINDER_TIME")
        if morning >= evening:
            raise ValueError("TODO_MORNING_REMINDER_TIME 必须早于晚间提醒时间")
        history_value = os.getenv("OBSIDIAN_HISTORY_PATH", "").strip()
        history_model = os.getenv("HISTORY_LLM_MODEL", "").strip()
        history_base_url = os.getenv("HISTORY_LLM_BASE_URL", "").strip() or (
            base_url if history_model else ""
        )
        history_api_key = os.getenv("HISTORY_LLM_API_KEY", "").strip() or (
            api_key if history_model else ""
        )
        if history_model and not history_base_url:
            raise ValueError("设置 HISTORY_LLM_MODEL 后还需要可用的 HISTORY_LLM_BASE_URL")

        search_mode = os.getenv("MEMORY_SEARCH_MODE", "hybrid").strip().lower()
        if search_mode not in {"hybrid", "vector", "keyword"}:
            raise ValueError("MEMORY_SEARCH_MODE 只能是 hybrid、vector 或 keyword")
        try:
            vector_min_similarity = float(os.getenv(
                "MEMORY_VECTOR_MIN_SIMILARITY", "0.1"
            ))
        except ValueError:
            raise ValueError(
                "MEMORY_VECTOR_MIN_SIMILARITY 必须是 -1 到 1 之间的数字"
            ) from None
        if not -1.0 <= vector_min_similarity <= 1.0:
            raise ValueError(
                "MEMORY_VECTOR_MIN_SIMILARITY 必须是 -1 到 1 之间的数字"
            )

        embedding_provider = os.getenv("EMBEDDING_PROVIDER", "").strip().lower()
        generic_embedding_configured = any(os.getenv(name, "").strip() for name in (
            "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL",
        ))
        if not embedding_provider:
            # Keep existing .env files working, but make a zero-config install use
            # the in-process ONNX model rather than an external embedding service.
            embedding_provider = (
                "openai-compatible" if generic_embedding_configured else "local"
            )
        if embedding_provider in {"openai", "compatible"}:
            embedding_provider = "openai-compatible"
        if embedding_provider in {"local-onnx", "onnx"}:
            embedding_provider = "local"
        if embedding_provider in {"disabled", "off"}:
            embedding_provider = "none"
        if embedding_provider not in {
            "local", "none", "ollama", "openai-compatible",
        }:
            raise ValueError(
                "EMBEDDING_PROVIDER 只能是 local、none、ollama 或 openai-compatible"
            )
        if embedding_provider == "local":
            embedding_base_url = ""
            embedding_api_key = ""
            embedding_model = "all-MiniLM-L6-v2"
        elif embedding_provider == "none":
            embedding_base_url = ""
            embedding_api_key = ""
            embedding_model = ""
        elif embedding_provider == "ollama":
            embedding_base_url = (
                os.getenv("OLLAMA_EMBEDDING_BASE_URL", "").strip()
                or "http://127.0.0.1:11434/v1"
            ).rstrip("/")
            embedding_api_key = (
                os.getenv("OLLAMA_API_KEY", "").strip()
                or "ollama"
            )
            embedding_model = os.getenv("OLLAMA_EMBEDDING_MODEL", "").strip()
        else:
            embedding_base_url = os.getenv("EMBEDDING_BASE_URL", "").strip().rstrip("/")
            embedding_api_key = os.getenv("EMBEDDING_API_KEY", "").strip()
            embedding_model = os.getenv("EMBEDDING_MODEL", "").strip()
        if (
            embedding_provider == "openai-compatible"
            and embedding_model
            and not embedding_base_url
        ):
            raise ValueError("设置 EMBEDDING_MODEL 后还需要 EMBEDDING_BASE_URL")
        if (
            embedding_provider == "openai-compatible"
            and embedding_model
            and not embedding_api_key
        ):
            raise ValueError("设置 EMBEDDING_MODEL 后还需要 EMBEDDING_API_KEY")
        cache_value = os.getenv("LOCAL_EMBEDDING_CACHE_DIR", "").strip()
        try:
            local_embedding_threads = int(os.getenv("LOCAL_EMBEDDING_THREADS", "2"))
        except ValueError:
            raise ValueError("LOCAL_EMBEDDING_THREADS 必须是 1 到 32 之间的整数") from None
        if not 1 <= local_embedding_threads <= 32:
            raise ValueError("LOCAL_EMBEDDING_THREADS 必须是 1 到 32 之间的整数")
        return cls(
            vault_path=Path(vault).expanduser().resolve(),
            journal_folder=os.getenv("OBSIDIAN_JOURNAL_FOLDER", "日记/AI 日记"),
            database_path=Path(os.getenv("DIARY_DB_PATH", "data/diary.sqlite")),
            timezone=timezone,
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key or "ollama",
            user_name=os.getenv("DIARY_USER_NAME", "你"),
            embedding_base_url=embedding_base_url,
            embedding_api_key=embedding_api_key,
            embedding_model=embedding_model,
            embedding_provider=embedding_provider,
            memory_search_mode=search_mode,
            memory_vector_min_similarity=vector_min_similarity,
            local_embedding_cache_dir=(
                Path(cache_value).expanduser() if cache_value else None
            ),
            local_embedding_threads=local_embedding_threads,
            memory_top_k=max(1, min(int(os.getenv("MEMORY_TOP_K", "5")), 10)),
            todo_reminders_enabled=os.getenv("TODO_REMINDERS_ENABLED", "true").lower()
            not in {"0", "false", "no", "off"},
            todo_morning_time=morning,
            todo_evening_time=evening,
            history_path=Path(history_value).expanduser().resolve() if history_value else None,
            history_extract_model=history_model,
            history_extract_base_url=history_base_url,
            history_extract_api_key=history_api_key,
            todo_multilingual_model_fallback=os.getenv(
                "TODO_MULTILINGUAL_MODEL_FALLBACK", "true"
            ).lower() not in {"0", "false", "no", "off"},
        )

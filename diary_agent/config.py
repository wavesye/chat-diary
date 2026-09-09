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
            embedding_base_url=os.getenv("EMBEDDING_BASE_URL", "").rstrip("/"),
            embedding_api_key=os.getenv("EMBEDDING_API_KEY", ""),
            embedding_model=os.getenv("EMBEDDING_MODEL", ""),
            memory_top_k=max(1, min(int(os.getenv("MEMORY_TOP_K", "5")), 10)),
            todo_reminders_enabled=os.getenv("TODO_REMINDERS_ENABLED", "true").lower()
            not in {"0", "false", "no", "off"},
            todo_morning_time=morning,
            todo_evening_time=evening,
            history_path=Path(history_value).expanduser().resolve() if history_value else None,
            history_extract_model=history_model,
            history_extract_base_url=history_base_url,
            history_extract_api_key=history_api_key,
        )

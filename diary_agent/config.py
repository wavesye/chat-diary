from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


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
        return cls(
            vault_path=Path(vault).expanduser().resolve(),
            journal_folder=os.getenv("OBSIDIAN_JOURNAL_FOLDER", "日记/AI 日记"),
            database_path=Path(os.getenv("DIARY_DB_PATH", "data/diary.sqlite")),
            timezone=ZoneInfo(os.getenv("DIARY_TIMEZONE", "Asia/Shanghai")),
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key or "ollama",
            user_name=os.getenv("DIARY_USER_NAME", "你"),
        )


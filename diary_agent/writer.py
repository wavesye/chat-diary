from __future__ import annotations

import os
import re
import tempfile
from datetime import date
from pathlib import Path


FIELDS = ("moments", "emotions", "insights", "gratitude", "tomorrow", "tags")


def _clean(value: object) -> str:
    return str(value).replace("\x00", "").strip()


def normalize_entry(raw: dict) -> dict:
    entry = {
        "title": _clean(raw.get("title") or "平常的一天"),
        "diary": _clean(raw.get("diary") or raw.get("summary") or "今天没有留下更多文字。"),
        "quote": _clean(raw.get("quote") or ""),
    }
    for field in FIELDS:
        value = raw.get(field, [])
        entry[field] = [_clean(item) for item in value if _clean(item)] if isinstance(value, list) else []
    entry["tags"] = [re.sub(r"[\s#]+", "-", tag).strip("-") for tag in entry["tags"]]
    entry["tags"] = [tag for tag in entry["tags"] if tag]
    return entry


def render_markdown(day: str, raw: dict) -> str:
    entry = normalize_entry(raw)
    tags = ", ".join(entry["tags"] or ["日记"])
    lines = [
        "---", f"date: {day}", "type: diary", f"tags: [{tags}]", "---", "",
        f"# {entry['title']}", "", entry["diary"], "",
    ]
    if entry["quote"]:
        lines.extend([f"> {entry['quote']}", ""])
    if entry["tomorrow"]:
        lines.extend(["## 留给明天", "", *[f"- [ ] {x}" for x in entry["tomorrow"]], ""])
    lines.extend(["---", "", "_由当天对话整理；内容仍属于我。_", ""])
    return "\n".join(lines)


def entry_filename(day: str) -> str:
    value = date.fromisoformat(day)
    return f"{value.year}-{value.month}-{value.day}-AIGEN.md"


def write_entry(vault: Path, folder: str, day: str, markdown: str) -> Path:
    vault = vault.resolve()
    if not vault.is_dir():
        raise ValueError(f"Obsidian vault 不存在或不是目录：{vault}")
    target_dir = (vault / folder).resolve()
    if vault != target_dir and vault not in target_dir.parents:
        raise ValueError("OBSIDIAN_JOURNAL_FOLDER 不能指向 vault 之外")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / entry_filename(day)
    fd, temp_name = tempfile.mkstemp(prefix=f".{day}-", suffix=".tmp", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(markdown)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return target

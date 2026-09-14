from __future__ import annotations

import os
import re
import tempfile
from datetime import date
from pathlib import Path


FIELDS = ("moments", "emotions", "insights", "gratitude", "tomorrow", "tags")


def _clean(value: object) -> str:
    return str(value).replace("\x00", "").strip()


def _title(value: object) -> str:
    title = _clean(value or "").splitlines()
    title = title[0] if title else ""
    title = re.sub(r"^#{1,6}\s*", "", title)
    title = re.sub(r"^(?:日记标题|标题|title)\s*[:：]\s*", "", title, flags=re.I)
    # Presentation wrappers occasionally leak out of the model's JSON title.
    wrappers = (("**", "**"), ("__", "__"), ("`", "`"), ('"', '"'),
                ("“", "”"), ("「", "」"), ("《", "》"))
    for opening, closing in wrappers:
        if title.startswith(opening) and title.endswith(closing) and len(title) >= len(opening + closing):
            title = title[len(opening):-len(closing)].strip()
    title = re.sub(
        r"^(?:(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2})|"
        r"(?:(?:\d{4}年)?\d{1,2}月\d{1,2}日))\s*[-—–:：|｜,，·]*\s*", "", title,
    )
    return title.strip() or "日常记录"


def normalize_entry(raw: dict) -> dict:
    entry = {
        "title": _title(raw.get("title")),
        "diary": _clean(raw.get("diary") or raw.get("summary") or "今天没有留下更多文字。"),
        "quote": _clean(raw.get("quote") or ""),
    }
    for field in FIELDS:
        value = raw.get(field, [])
        entry[field] = [_clean(item) for item in value if _clean(item)] if isinstance(value, list) else []
    entry["tags"] = [re.sub(r"[\s#]+", "-", tag).strip("-") for tag in entry["tags"]]
    entry["tags"] = [tag for tag in entry["tags"] if tag]
    # These two fields are replaced with database records by DiaryService.
    # Never infer completion from the prose or from the model's tomorrow list.
    todos = raw.get("todos", [])
    entry["todos"] = []
    for item in todos if isinstance(todos, list) else []:
        if not isinstance(item, dict) or item.get("status") not in {"active", "completed"}:
            continue
        title = re.sub(r"\s+", " ", _clean(item.get("title") or ""))
        if title:
            entry["todos"].append({"title": title, "status": item["status"]})
    quotes = raw.get("verbatim_quotes", [])
    # Do not clean, strip, reflow or add Markdown prefixes to user-owned text.
    entry["verbatim_quotes"] = [
        item for item in quotes if isinstance(item, str) and item.strip()
    ] if isinstance(quotes, list) else []
    return entry


def render_markdown(day: str, raw: dict) -> str:
    entry = normalize_entry(raw)
    tags = ", ".join(entry["tags"] or ["日记"])
    lines = [
        "---", f"date: {day}", "type: diary", f"tags: [{tags}]", "---", "",
        f"# {entry['title']}", "", entry["diary"], "",
    ]
    if entry["verbatim_quotes"]:
        lines.extend(["## 我的原话", ""])
        for quote in entry["verbatim_quotes"]:
            lines.extend([quote, ""])
    elif entry["quote"]:
        lines.extend([f"> {entry['quote']}", ""])
    if entry["todos"]:
        lines.extend(["## Todo", ""])
        for item in entry["todos"]:
            mark = "x" if item["status"] == "completed" else " "
            lines.append(f"- [{mark}] {item['title']}")
        lines.append("")
    todo_titles = {item["title"].casefold() for item in entry["todos"]}
    tomorrow = [item for item in entry["tomorrow"]
                if re.sub(r"\s+", " ", item).casefold() not in todo_titles]
    if tomorrow:
        lines.extend(["## 留给明天", "", *[f"- [ ] {x}" for x in tomorrow], ""])
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

"""Resolve explicit preservation requests without asking a model to copy text."""
from __future__ import annotations

import re


_REQUEST = (
    r"(?:请|麻烦)?(?:帮我)?(?:把|将)?(?:这段话|这句话|这一段|这段|这句|上面这段话|"
    r"上一段|刚才那段话|刚才那段|上条消息|上一条消息|下面这段话|以下内容)?"
    r"(?:要|请|给我)?(?:原封不动|一字不改|逐字|保持原样)"
    r"(?:地)?(?:保留在|放到|放进|放入|写进|写入|加入|收进|保存到|记到|记进)"
    r"(?:我的|今天的|今日的)?日记(?:里|中)?"
)
_PREFIX = re.compile(r"^\s*" + _REQUEST + r"(?:[：:]|[。！!]?\r?\n)")
_ONLY = re.compile(r"^\s*" + _REQUEST + r"[。！!]?\s*$")
_SUFFIX = re.compile(r"(?:\r?\n|[，,。；;])[ \t]*" + _REQUEST + r"[。！!]?\s*$")


def preservation_request(text: str) -> tuple[bool, str | None]:
    """Return (recognized, exact text); None means the previous user message."""
    prefix = _PREFIX.match(text)
    if prefix and text[prefix.end():].strip():
        return True, text[prefix.end():]
    if _ONLY.fullmatch(text):
        return True, None
    suffix = _SUFFIX.search(text)
    if suffix and text[:suffix.start()].strip():
        # A sentence-ending mark still belongs to the user's sentence; a
        # newline separating the preservation instruction is only a delimiter.
        end = suffix.start() + (1 if text[suffix.start()] not in "\r\n" else 0)
        return True, text[:end]
    return False, None


def command_parts(text: str) -> tuple[str, str]:
    """Remove exactly one command delimiter; preserve the payload's whitespace."""
    match = re.match(r"^\s*(/[^\s]+)(?:\r\n|[ \t\n])?", text)
    if not match:
        return "", ""
    return match.group(1).split("@", 1)[0].lower(), text[match.end():]

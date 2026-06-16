"""
wisp-agent long-term personal memory.

Stores user facts/preferences in ~/wisp/memory.md. The file is injected into the
system prompt at startup, so anything the user asks wisp to remember persists
across sessions, gateway restarts, and the Telegram chat — and is recalled
automatically (it's always in context). Written via the `remember` tool, pruned
via `forget`.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

_MEMORY_PATH: Path | None = None

_HEADER = "# 关于用户的长期记忆"
_DEFAULT_CONTENT = (
    _HEADER + "\n"
    "(wisp 通过 remember 工具记录的用户事实与偏好；每次启动注入，自动想起。)\n"
)


def init_memory(workspace: str) -> None:
    """Called once at startup. workspace = ~/wisp/workspace."""
    global _MEMORY_PATH
    wisp_root = Path(workspace).expanduser().resolve().parent  # ~/wisp/
    _MEMORY_PATH = wisp_root / "memory.md"
    if not _MEMORY_PATH.exists():
        _MEMORY_PATH.write_text(_DEFAULT_CONTENT, encoding="utf-8")


def load() -> str:
    """Return memory.md for prompt injection (empty string if no facts yet)."""
    if _MEMORY_PATH is None or not _MEMORY_PATH.exists():
        return ""
    content = _MEMORY_PATH.read_text(encoding="utf-8").strip()
    # Only inject if there's at least one real fact (a `- ` line)
    if not any(l.startswith("- ") for l in content.splitlines()):
        return ""
    return content


def add(fact: str) -> str:
    """Record a user fact/preference. Returns an ok/skip/error string."""
    if _MEMORY_PATH is None:
        return "error: memory not initialized"
    fact = " ".join((fact or "").split()).strip()
    if not fact:
        return "error: empty fact"
    if len(fact) > 300:
        return "error: fact too long (keep it under 300 chars)"
    if _is_similar(fact):
        return "ok: already remembered something similar, skipped"
    content = (_MEMORY_PATH.read_text(encoding="utf-8")
               if _MEMORY_PATH.exists() else _DEFAULT_CONTENT)
    content = content.rstrip() + f"\n- {fact}  ({date.today().isoformat()})\n"
    _MEMORY_PATH.write_text(content, encoding="utf-8")
    return f"ok: remembered — {fact}"


def forget(keyword: str) -> str:
    """Remove remembered facts whose text contains keyword (case-insensitive)."""
    if _MEMORY_PATH is None:
        return "error: memory not initialized"
    keyword = keyword.strip()
    if not keyword:
        return "error: keyword cannot be empty"
    if not _MEMORY_PATH.exists():
        return f"ok: nothing remembered matching '{keyword}'"
    kw = keyword.lower()
    lines = _MEMORY_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    kept, removed = [], 0
    for line in lines:
        if line.startswith("- ") and kw in line.lower():
            removed += 1
        else:
            kept.append(line)
    if removed == 0:
        return f"ok: nothing remembered matching '{keyword}'"
    _MEMORY_PATH.write_text(re.sub(r"\n{3,}", "\n\n", "".join(kept)), encoding="utf-8")
    return f"ok: forgot {removed} item{'s' if removed > 1 else ''} matching '{keyword}'"


def _is_similar(new_fact: str) -> bool:
    """True if an existing fact overlaps >= 75% word-wise (avoid duplicates)."""
    if _MEMORY_PATH is None or not _MEMORY_PATH.exists():
        return False
    new_words = set(re.findall(r"\w+", new_fact.lower()))
    if not new_words:
        return False
    for line in _MEMORY_PATH.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- "):
            continue
        # drop the trailing "  (YYYY-MM-DD)" stamp so it doesn't dilute the match
        text = re.sub(r"\s*\(\d{4}-\d\d-\d\d\)\s*$", "", line)
        ex = set(re.findall(r"\w+", text.lower()))
        if ex and len(new_words & ex) / max(len(new_words), len(ex)) >= 0.75:
            return True
    return False

"""
wisp-agent tool knowledge base.
Stores learned tool usage rules in ~/wisp/knowledge.md.
Rules are injected into the system prompt at startup so the agent
learns from past corrections without relying on session history.
"""

from __future__ import annotations

import re
from pathlib import Path

_KNOWLEDGE_PATH: Path | None = None

_DEFAULT_CONTENT = """\
# Wisp 工具知识库

## browser
- 查机票、订酒店、需要登录的网站 → 用 browser_open，不用 http_get
- 需要 JavaScript 渲染的交互页面 → 必须用 browser 系列工具

## bash
- 多文件操作 → bash glob 模式，不要循环调用 read_file
- 命令输出可能超长 → 先用 head 或 grep 过滤再返回

## files
- 生成报告或文件后 → 用 open 工具打开，不要只打印路径
"""

# Generic terms a rule might legitimately use without naming an exact tool
# (e.g. "改用 bash 里的 curl", "用 grep 过滤"). Real tool names (mail_delete,
# calendar_list, etc.) are added at runtime by _tool_keywords() below, so this
# quality check can never again silently fall behind the actual tool set —
# that staleness previously made EVERY mail/calendar/reminders/notes rule
# unsaveable (no keyword in this set matched), and the model had no way to
# recover from the rejection: it kept resubmitting the same rule verbatim
# until the turn limit was hit.
_GENERIC_KEYWORDS = {
    "browser", "bash", "osascript", "screenshot", "clipboard",
    "glob", "grep", "head", "curl", "find",
}

_TOOL_KEYWORDS: set[str] | None = None   # lazily built, cached — see _tool_keywords()


def _tool_keywords() -> set[str]:
    global _TOOL_KEYWORDS
    if _TOOL_KEYWORDS is None:
        try:
            import tools as _tools
            names = {s["function"]["name"].lower() for s in _tools.SCHEMAS}
        except Exception:
            names = set()   # tools.py not importable yet — fall back to generic-only
        _TOOL_KEYWORDS = _GENERIC_KEYWORDS | names
    return _TOOL_KEYWORDS


def init_knowledge(workspace: str) -> None:
    """Called once at startup. workspace = ~/wisp/workspace."""
    global _KNOWLEDGE_PATH
    wisp_root = Path(workspace).expanduser().resolve().parent  # ~/wisp/
    _KNOWLEDGE_PATH = wisp_root / "knowledge.md"
    if not _KNOWLEDGE_PATH.exists():
        _KNOWLEDGE_PATH.write_text(_DEFAULT_CONTENT, encoding="utf-8")


def load() -> str:
    """Return full knowledge.md content, or empty string if not initialized."""
    if _KNOWLEDGE_PATH is None or not _KNOWLEDGE_PATH.exists():
        return ""
    return _KNOWLEDGE_PATH.read_text(encoding="utf-8").strip()


def append_rule(rule: str, category: str = "general") -> str:
    """Append a rule under the given category section.

    Returns:
        "ok: rule added to [category]"
        "ok: similar rule exists, skipped"
        "error: ..."
    """
    if _KNOWLEDGE_PATH is None:
        return "error: knowledge not initialized"

    rule = rule.strip()
    if not rule:
        return "error: empty rule"

    # Quality check: rule must mention at least one known tool/command
    rule_lower = rule.lower()
    if not any(k in rule_lower for k in _tool_keywords()):
        return "error: rule must mention a specific tool or command"

    # Dedup check
    if _is_similar(rule):
        return "ok: similar rule exists, skipped"

    category = (category or "general").strip().lower()
    content   = _KNOWLEDGE_PATH.read_text(encoding="utf-8") if _KNOWLEDGE_PATH.exists() else _DEFAULT_CONTENT
    new_line  = f"- {rule}"
    header    = f"## {category}"

    if header in content:
        # Insert at the end of the existing section (before the next ## or EOF)
        lines     = content.split("\n")
        insert_at = len(lines)  # default: end of file
        in_sec    = False
        for i, line in enumerate(lines):
            if line.strip() == header:
                in_sec = True
                continue
            if in_sec and line.startswith("## "):
                insert_at = i
                break
        lines.insert(insert_at, new_line)
        content = "\n".join(lines)
    else:
        # Create a new section at the end
        content = content.rstrip() + f"\n\n{header}\n{new_line}\n"

    _KNOWLEDGE_PATH.write_text(content, encoding="utf-8")
    return f"ok: rule added to [{category}]"


def forget(keyword: str) -> str:
    """Remove all rules whose text contains keyword (case-insensitive).

    Returns:
        "ok: removed N rule(s) matching '<keyword>'"
        "ok: no rules matching '<keyword>' found"
        "error: ..."
    """
    if _KNOWLEDGE_PATH is None:
        return "error: knowledge not initialized"
    keyword = keyword.strip()
    if not keyword:
        return "error: keyword cannot be empty"
    if not _KNOWLEDGE_PATH.exists():
        return f"ok: no rules matching '{keyword}' found"

    kw_lower = keyword.lower()
    lines    = _KNOWLEDGE_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    kept     = []
    removed  = 0
    for line in lines:
        if line.startswith("- ") and kw_lower in line.lower():
            removed += 1
        else:
            kept.append(line)

    if removed == 0:
        return f"ok: no rules matching '{keyword}' found"

    # Collapse triple+ blank lines left by removals
    content = re.sub(r"\n{3,}", "\n\n", "".join(kept))
    _KNOWLEDGE_PATH.write_text(content, encoding="utf-8")
    return f"ok: removed {removed} rule{'s' if removed > 1 else ''} matching '{keyword}'"


def _is_similar(new_rule: str) -> bool:
    """Return True if an existing rule overlaps >= 75% word-wise."""
    if _KNOWLEDGE_PATH is None or not _KNOWLEDGE_PATH.exists():
        return False
    new_words = set(re.findall(r"\w+", new_rule.lower()))
    if not new_words:
        return False
    for line in _KNOWLEDGE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- "):
            continue
        existing_words = set(re.findall(r"\w+", line.lower()))
        if not existing_words:
            continue
        overlap = len(new_words & existing_words) / max(len(new_words), len(existing_words))
        if overlap >= 0.75:
            return True
    return False

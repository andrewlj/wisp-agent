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


# ── Pending review queue ──────────────────────────────────────────────────────
# Rules proposed by _post_task_reflect (automatic) or the `learn` tool no
# longer go live immediately — they sit in this one extra section of the same
# knowledge.md file until a human approves them with /rules. This is what
# stopped a real incident: a self-contradictory auto-written rule ("test
# emails should use mail_move, not AppleScript" — mail_move IS implemented
# via AppleScript) sat in the live, injected file for a while before anyone
# noticed. No separate file/format, no id bookkeeping — a candidate is just a
# line under this header, numbered by its position when listed.
_PENDING_HEADER = "## _pending"
_PENDING_LINE_RE = re.compile(r"^-\s*\[([^\]]+)\]\s*(.+?)(?:\s*\((auto|learn)\))?$")


def _strip_section(content: str, header: str) -> str:
    """Remove one '## header' section (through the next '## ' or EOF)."""
    lines = content.split("\n")
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.strip() == header:
            skipping = True
            continue
        if skipping and line.startswith("## "):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out)


def _insert_into_section(content: str, header: str, new_line: str) -> str:
    """Insert new_line as the last line of the '## header' section, creating
    the section at EOF if it doesn't exist. If the section runs to EOF (no
    later '## ' header), trims a trailing blank-line artifact first — without
    this, a second insert into a freshly-created section leaves a blank line
    stuck in the middle instead of appending cleanly."""
    if header not in content:
        return content.rstrip() + f"\n\n{header}\n{new_line}\n"
    lines      = content.split("\n")
    insert_at  = len(lines)
    in_sec     = False
    found_next = False
    for i, line in enumerate(lines):
        if line.strip() == header:
            in_sec = True
            continue
        if in_sec and line.startswith("## "):
            insert_at, found_next = i, True
            break
    if not found_next:
        while insert_at > 0 and lines[insert_at - 1] == "":
            insert_at -= 1
    lines.insert(insert_at, new_line)
    return "\n".join(lines)


def _rule_text_of(line: str) -> str:
    """Strip '- ', an optional leading '[category]', and an optional
    trailing '(source)'/'(date)' annotation — leaves just the rule's own
    words, so dedup compares active and pending lines on equal footing."""
    m = _PENDING_LINE_RE.match(line.strip())
    if m:
        return m.group(2).strip()
    text = line.strip()
    if text.startswith("- "):
        text = text[2:]
    return re.sub(r"\s*\([^()]*\)\s*$", "", text).strip()


def init_knowledge(workspace: str) -> None:
    """Called once at startup. workspace = ~/wisp/workspace."""
    global _KNOWLEDGE_PATH
    wisp_root = Path(workspace).expanduser().resolve().parent  # ~/wisp/
    _KNOWLEDGE_PATH = wisp_root / "knowledge.md"
    if not _KNOWLEDGE_PATH.exists():
        _KNOWLEDGE_PATH.write_text(_DEFAULT_CONTENT, encoding="utf-8")


def load() -> str:
    """Return knowledge.md content for system-prompt injection — approved
    rules only. The `## _pending` section (unreviewed candidates) is
    deliberately excluded: they don't affect behaviour until approved via
    /rules. Returns '' if not initialized."""
    if _KNOWLEDGE_PATH is None or not _KNOWLEDGE_PATH.exists():
        return ""
    content = _KNOWLEDGE_PATH.read_text(encoding="utf-8")
    return _strip_section(content, _PENDING_HEADER).strip()


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
    content  = (_KNOWLEDGE_PATH.read_text(encoding="utf-8")
                if _KNOWLEDGE_PATH.exists() else _DEFAULT_CONTENT)
    content  = _insert_into_section(content, f"## {category}", f"- {rule}")
    _KNOWLEDGE_PATH.write_text(content, encoding="utf-8")
    return f"ok: rule added to [{category}]"


def propose_rule(rule: str, category: str = "general", source: str = "auto") -> str:
    """Submit a rule for human review instead of writing it live. Runs the
    same quality gate as append_rule (must name a real tool) plus dedup
    against every existing rule, active or already-pending — only genuinely
    new, plausible candidates reach the queue.

    Returns:
        "ok: proposed — review with /rules"
        "ok: already covered by an existing/pending rule, skipped"
        "error: ..."
    """
    if _KNOWLEDGE_PATH is None:
        return "error: knowledge not initialized"

    rule = rule.strip()
    if not rule:
        return "error: empty rule"

    rule_lower = rule.lower()
    if not any(k in rule_lower for k in _tool_keywords()):
        return "error: rule must mention a specific tool or command"

    if _is_similar(rule):
        return "ok: already covered by an existing/pending rule, skipped"

    category = (category or "general").strip().lower()
    source   = source if source in ("auto", "learn") else "auto"
    content  = (_KNOWLEDGE_PATH.read_text(encoding="utf-8")
                if _KNOWLEDGE_PATH.exists() else _DEFAULT_CONTENT)
    content  = _insert_into_section(content, _PENDING_HEADER,
                                     f"- [{category}] {rule}  ({source})")
    _KNOWLEDGE_PATH.write_text(content, encoding="utf-8")
    return "ok: proposed — review with /rules"


def format_pending() -> str:
    """Plain-text listing of pending candidates for REPL/Telegram display —
    mirrors schedule.format_jobs()'s convention (one shared formatter, no
    ANSI codes, both surfaces print/send it as-is)."""
    items = list_pending()
    if not items:
        return "no pending rules"
    lines = []
    for it in items:
        lines.append(f"#{it['n']} [{it['category']}] ({it['source']})\n"
                     f"    {it['rule']}")
    return "\n".join(lines)


def list_pending() -> list[dict]:
    """Return pending candidates as [{"n", "category", "rule", "source"}, …],
    numbered by their current position in the file. Not persisted ids —
    fine for a single-user, one-review-pass-at-a-time workflow; re-run this
    (e.g. via /rules) after each approve/reject before acting on a number
    again."""
    if _KNOWLEDGE_PATH is None or not _KNOWLEDGE_PATH.exists():
        return []
    lines  = _KNOWLEDGE_PATH.read_text(encoding="utf-8").split("\n")
    in_sec = False
    items: list[dict] = []
    for line in lines:
        if line.strip() == _PENDING_HEADER:
            in_sec = True
            continue
        if in_sec and line.startswith("## "):
            break
        if in_sec and line.startswith("- "):
            m = _PENDING_LINE_RE.match(line.strip())
            if m:
                items.append({"category": m.group(1), "rule": m.group(2),
                              "source": m.group(3) or "auto"})
    return [{"n": i + 1, **it} for i, it in enumerate(items)]


def _remove_pending_line(n: int) -> None:
    """Delete the nth pending line (1-based, list_pending()'s numbering) and
    drop the '## _pending' header itself if nothing is left under it."""
    lines  = _KNOWLEDGE_PATH.read_text(encoding="utf-8").split("\n")
    in_sec = False
    idx    = 0
    for i, line in enumerate(lines):
        if line.strip() == _PENDING_HEADER:
            in_sec = True
            continue
        if in_sec and line.startswith("## "):
            break
        if in_sec and line.startswith("- "):
            idx += 1
            if idx == n:
                del lines[i]
                break
    content = _drop_empty_pending_header("\n".join(lines))
    _KNOWLEDGE_PATH.write_text(content, encoding="utf-8")


def _drop_empty_pending_header(content: str) -> str:
    if _PENDING_HEADER not in content:
        return content
    lines = content.split("\n")
    start = end = None
    for i, line in enumerate(lines):
        if line.strip() == _PENDING_HEADER:
            start = i
        elif start is not None and end is None and line.startswith("## "):
            end = i
            break
    if start is None:
        return content
    if end is None:
        end = len(lines)
    if any(l.startswith("- ") for l in lines[start + 1:end]):
        return content   # still has pending items — leave it alone
    del lines[start:end]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


def approve(n: int) -> str:
    """Promote pending candidate #n to the active rule set via the normal
    append_rule() path — inherits its keyword gate and dedup for free."""
    match = next((it for it in list_pending() if it["n"] == n), None)
    if not match:
        return f"error: no pending rule #{n}"
    _remove_pending_line(n)
    result = append_rule(match["rule"], match["category"])
    remaining = len(list_pending())
    return f"{result}  ({remaining} pending left)" if remaining else result


def reject(n: int) -> str:
    """Discard pending candidate #n without adding it anywhere."""
    match = next((it for it in list_pending() if it["n"] == n), None)
    if not match:
        return f"error: no pending rule #{n}"
    _remove_pending_line(n)
    remaining = len(list_pending())
    msg = f"ok: rejected — {match['rule']}"
    return f"{msg}  ({remaining} pending left)" if remaining else msg


def reject_all() -> str:
    """Discard every pending candidate at once."""
    n = len(list_pending())
    if n == 0:
        return "ok: no pending rules"
    content = _KNOWLEDGE_PATH.read_text(encoding="utf-8")
    content = _strip_section(content, _PENDING_HEADER)
    content = re.sub(r"\n{3,}", "\n\n", content).rstrip() + "\n"
    _KNOWLEDGE_PATH.write_text(content, encoding="utf-8")
    return f"ok: rejected {n} pending rule{'s' if n != 1 else ''}"


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
    """Return True if an existing rule (active OR pending — this scans every
    '- ' line in the file regardless of section) overlaps >= 75% word-wise."""
    if _KNOWLEDGE_PATH is None or not _KNOWLEDGE_PATH.exists():
        return False
    new_words = set(re.findall(r"\w+", new_rule.lower()))
    if not new_words:
        return False
    for line in _KNOWLEDGE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- "):
            continue
        existing_words = set(re.findall(r"\w+", _rule_text_of(line).lower()))
        if not existing_words:
            continue
        overlap = len(new_words & existing_words) / max(len(new_words), len(existing_words))
        if overlap >= 0.75:
            return True
    return False

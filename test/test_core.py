#!/usr/bin/env python3
"""
Pure-logic unit tests for the regression-prone internals — no LLM, no network,
no Mail. Covers context compression, the scheduler's time matching, token
estimation, and per-job tool gating. These are the bits that have actually
broken before (a compression 400, a scheduling misfire), so they get a net.

Run from the project root:  python test/test_core.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

import agent
import schedule
import tools

_passed = _failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        _failed += 1
        print(f"  \033[31m✗\033[0m {name}   \033[90m{detail}\033[0m")


# ── token estimation (CJK-aware) ─────────────────────────────────────────────
print("\ntoken estimation")
check("ascii ≈ chars/4", abs(agent._count_tokens_est("a" * 400) - 100) <= 1)
check("cjk ≈ 1 token/char", agent._count_tokens_est("中" * 100) >= 100)
check("cjk counts higher than equal-length ascii",
      agent._count_tokens_est("中文标点符号") > agent._count_tokens_est("abcdef"))
check("schema tokens > 0", agent._schema_tokens() > 0)

# ── schedule: _to_calendar ───────────────────────────────────────────────────
print("\nschedule._to_calendar")
check("HH:MM daily", schedule._to_calendar("08:00") == {"Hour": 8, "Minute": 0})
check("cron daily", schedule._to_calendar("0 8 * * *") == {"Minute": 0, "Hour": 8})
check("cron weekday", schedule._to_calendar("30 7 * * 1") == {"Minute": 30, "Hour": 7, "Weekday": 1})
check("reject '*' minute", schedule._to_calendar("* 8 * * *") is None)
check("reject garbage", schedule._to_calendar("nonsense") is None)
check("reject out-of-range", schedule._to_calendar("99:99") is None)

# ── schedule: _most_recent_due ───────────────────────────────────────────────
print("\nschedule._most_recent_due")
now = datetime(2026, 6, 15, 9, 0, 0)            # a fixed reference time
check("daily, already passed today → today",
      schedule._most_recent_due(now, {"Hour": 8, "Minute": 0}) == now.replace(hour=8, minute=0))
check("daily, not yet today → yesterday",
      schedule._most_recent_due(now, {"Hour": 10, "Minute": 0})
      == (now - timedelta(days=1)).replace(hour=10, minute=0))
_lw = 7 if now.weekday() == 6 else now.weekday() + 1   # python→launchd weekday
check("weekday matching today",
      schedule._most_recent_due(now, {"Hour": 8, "Minute": 0, "Weekday": _lw})
      == now.replace(hour=8, minute=0))

# ── schedule: due_jobs (seed / catch-up / no double-fire) ────────────────────
print("\nschedule.due_jobs")
_state: dict = {}
_jobs = [{"name": "j", "schedule": "08:00", "enabled": True, "sink": "stdout", "prompt": "x"}]
schedule._load_state = lambda: dict(_state)
schedule._save_state = lambda s: (_state.clear(), _state.update(s))
schedule.list_jobs = lambda: list(_jobs)
T = datetime(2026, 6, 15, 9, 0)
check("first tick seeds, no fire", [j["name"] for j in schedule.due_jobs(T)] == [])
_state["j"] = datetime(2026, 6, 15, 7, 0).isoformat()    # backdate before today's 08:00
check("overdue fires", [j["name"] for j in schedule.due_jobs(T)] == ["j"])
check("no double-fire", [j["name"] for j in schedule.due_jobs(T)] == [])
_jobs[0]["enabled"] = False
_state.clear()
check("disabled never fires", [j["name"] for j in schedule.due_jobs(T)] == [])

# ── schedule: interval jobs ──────────────────────────────────────────────────
print("\nschedule: interval scheduling")
check("parse 'every 60m 08:00-22:00'",
      schedule._parse_interval("every 60m 08:00-22:00")
      == {"kind": "interval", "minutes": 60, "start": 480, "end": 1320})
check("parse 'every 2h'", schedule._parse_interval("every 2h") == {"kind": "interval", "minutes": 120})
check("reject 'every blah'", schedule._parse_interval("every blah") is None)
_iv = schedule._parse_interval("every 60m 08:00-22:00")
_noon = datetime(2026, 6, 16, 12, 0)
check("interval first-fire in window", schedule._interval_due(_noon, _iv, None))
check("interval too-soon → no", not schedule._interval_due(_noon, _iv, _noon - timedelta(minutes=30)))
check("interval elapsed → fire", schedule._interval_due(_noon, _iv, _noon - timedelta(minutes=61)))
check("interval outside window → no", not schedule._interval_due(datetime(2026, 6, 16, 23, 0), _iv, None))
# via due_jobs (reuses the monkeypatched storage above)
_state.clear()
_jobs[:] = [{"name": "iv", "schedule": "every 60m", "enabled": True, "sink": "stdout", "builtin": "x"}]
check("interval due_jobs: first tick fires",
      [j["name"] for j in schedule.due_jobs(datetime(2026, 6, 16, 12, 0))] == ["iv"])
check("interval due_jobs: too soon → no",
      [j["name"] for j in schedule.due_jobs(datetime(2026, 6, 16, 12, 30))] == [])
check("interval due_jobs: after interval → fires",
      [j["name"] for j in schedule.due_jobs(datetime(2026, 6, 16, 13, 1))] == ["iv"])

# ── compression: _sanitize_tool_pairing ──────────────────────────────────────
print("\nagent._sanitize_tool_pairing")
msgs = [
    {"role": "system", "content": "s"},
    {"role": "user", "content": "u"},
    {"role": "tool", "tool_call_id": "X", "content": "orphan"},                 # parent gone
    {"role": "assistant", "content": "", "tool_calls": [{"id": "Y", "type": "function", "function": {}}]},
    {"role": "tool", "tool_call_id": "Y", "content": "ok"},                      # matched
    {"role": "assistant", "content": "", "tool_calls": [{"id": "Z", "type": "function", "function": {}}]},  # unanswered
]
agent._sanitize_tool_pairing(msgs)
check("orphan tool dropped", not any(m.get("tool_call_id") == "X" for m in msgs))
check("matched tool kept", any(m.get("tool_call_id") == "Y" for m in msgs))
check("unanswered tool_calls stripped", not msgs[-1].get("tool_calls"))

# ── compression: _maybe_compress (stubbed summariser, no LLM) ────────────────
print("\nagent._maybe_compress")
agent.CONTEXT_WINDOW = 1000
agent.COMPRESS_AT_PCT = 50
agent.CTX_KEEP_RECENT = 2
agent._call_api_simple = lambda *a, **k: "SUMMARY"
big = [{"role": "system", "content": "s"}]
for i in range(8):
    big.append({"role": "assistant", "content": "",
                "tool_calls": [{"id": f"c{i}", "type": "function",
                                "function": {"name": "x", "arguments": "{}"}}]})
    big.append({"role": "tool", "tool_call_id": f"c{i}", "content": "R" * 500})
_before = len(big)
agent._maybe_compress(big)
check("compression shrinks message count", len(big) < _before)
# tool-call pairing must remain valid (the bug that caused a 400)
_declared, _ok = set(), True
for m in big:
    if m["role"] == "assistant":
        for tc in m.get("tool_calls", []):
            _declared.add(tc["id"])
    elif m["role"] == "tool" and m.get("tool_call_id") not in _declared:
        _ok = False
check("compressed list keeps valid tool pairing", _ok)
check("summary is a user message (not mid-list system)",
      any(m["role"] == "user" and "compressed" in str(m.get("content", "")) for m in big))

# ── compression: _compress_threshold / CONTEXT_HARD_CAP ──────────────────────
print("\nagent._compress_threshold (hard cap)")
agent.CONTEXT_WINDOW = 65000
agent.COMPRESS_AT_PCT = 60
agent.CONTEXT_HARD_CAP = 20000
check("hard cap wins when tighter than window%",
      agent._compress_threshold() == 20000)
agent.CONTEXT_HARD_CAP = 100000
check("window% wins when tighter than hard cap",
      agent._compress_threshold() == 65000 * 60 // 100)
agent.CONTEXT_WINDOW = 0
agent.CONTEXT_HARD_CAP = 20000
check("hard cap alone still triggers when window is undetected",
      agent._compress_threshold() == 20000)
agent.CONTEXT_HARD_CAP = 0
check("compression off when neither window nor hard cap is set",
      agent._compress_threshold() == 0)
agent.CONTEXT_WINDOW = 1000  # restore for later tests in this file

# ── _extract_final_answer ────────────────────────────────────────────────────
print("\nagent._extract_final_answer")
import json as _json
conv = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "intermediate"},
    {"role": "assistant", "content": "",
     "tool_calls": [{"id": "d", "type": "function",
                     "function": {"name": "done", "arguments": _json.dumps({"result": "FINAL"})}}]},
]
check("final answer from done result", agent._extract_final_answer(conv) == "FINAL")

# ── long-term memory (use a temp file, don't touch ~/wisp) ───────────────────
print("\nmemory (long-term facts)")
import memory as _mem
import tempfile
_mem._MEMORY_PATH = Path(tempfile.mkdtemp()) / "memory.md"
_mem._MEMORY_PATH.write_text(_mem._DEFAULT_CONTENT, encoding="utf-8")
check("empty memory → load() returns ''", _mem.load() == "")
check("add returns ok", _mem.add("我住在都柏林").startswith("ok: remembered"))
check("dedup skips a near-duplicate", "skipped" in _mem.add("我住在都柏林"))
_mem.add("我老板叫张三")
check("load shows stored facts", "都柏林" in _mem.load() and "张三" in _mem.load())
check("forget removes matching", "1" in _mem.forget("张三"))
check("forgotten fact is gone", "张三" not in _mem.load())
check("kept fact remains", "都柏林" in _mem.load())

# ── tool gating: schemas_for_groups ──────────────────────────────────────────
print("\ntools.schemas_for_groups")
_names = [s["function"]["name"] for s in tools.schemas_for_groups(["mail"])]
check("core always included", "bash" in _names and "done" in _names)
check("requested group included", "mail_list" in _names)
check("other groups excluded", "calendar_list" not in _names and "daily_briefing" not in _names)

# ── resilience: friendly error on server-down ───────────────────────────────
print("\nresilience + send_to_phone")
import io
import contextlib
import requests as _rq


def _boom(_messages):
    raise _rq.exceptions.ConnectionError("server down")


_orig = agent.call_api_stream
agent.call_api_stream = _boom
with contextlib.redirect_stdout(io.StringIO()):
    _ans = agent.run_agent("hi", [{"role": "system", "content": ""}], reflect=False, max_turns=2)
agent.call_api_stream = _orig
check("run_agent → friendly msg when server unreachable",
      _ans.startswith("⚠️") and "连不上" in _ans)

tools.init_telegram("", "")
check("send_to_phone: not configured → error",
      tools.tool_send_to_phone("/tmp/x").startswith("error: Telegram not configured"))
tools.init_telegram("tok", "123")
check("send_to_phone: missing file → error",
      tools.tool_send_to_phone("/no/such/file").startswith("error: file not found"))

# ── important-mail watcher (stubbed mail + classifier) ───────────────────────
print("\nimportant_mail watcher")
import pathlib
import tempfile
_t = tools
_t.tool_mail_list = lambda **k: ("[acct] 1 unread message(s)\n"
                                 "● Uber <uber@uber.com> | promo | 2026 | id:abc123@x")
agent._MAIL_SEEN = pathlib.Path(tempfile.mkdtemp()) / ".mail_seen.json"
agent._classify_important = lambda items: []
check("watcher silent when nothing important", agent._builtin_important_mail() == "")
agent._MAIL_SEEN.unlink(missing_ok=True)                 # reset so item is 'new' again
agent._classify_important = lambda items: [dict(items[0], reason="需回复")]
_out = agent._builtin_important_mail()
check("watcher pushes when important", "重要邮件" in _out and "Uber" in _out)
check("watcher silent on second run (id already seen)",
      agent._builtin_important_mail() == "")

print(f"\n{'─' * 40}\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)

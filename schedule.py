#!/usr/bin/env python3
"""
wisp scheduler — recurring tasks, driven by the gateway's in-process tick loop.

A job = a prompt (or builtin) + a schedule + an output sink, stored in
~/wisp/schedule.yaml. The gateway daemon (gateway.py) calls due_jobs() every
tick and fires whatever is due, so there is ONE long-lived process managing both
chat and schedules — no per-job launchd plists. Tick-based matching means a job
missed while the Mac slept is fired on the next wake-up tick (catch-up), and a
small persisted state file prevents double-firing across restarts.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import yaml

_HOME       = Path("~/wisp").expanduser()
_SCHED_FILE = _HOME / "schedule.yaml"
_STATE_FILE = _HOME / ".schedule_state.json"
_LAUNCH_DIR = Path("~/Library/LaunchAgents").expanduser()
_LABEL_PFX  = "com.wisp."

_VALID_SINKS = {"notify", "file", "stdout", "telegram"}


# ── Storage ────────────────────────────────────────────────────────────────

def _load() -> dict:
    if _SCHED_FILE.exists():
        return yaml.safe_load(_SCHED_FILE.read_text(encoding="utf-8")) or {"jobs": []}
    return {"jobs": []}


def _save(data: dict) -> None:
    _HOME.mkdir(parents=True, exist_ok=True)
    _SCHED_FILE.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def list_jobs() -> list[dict]:
    return _load().get("jobs", [])


def get_job(name: str) -> dict | None:
    for j in list_jobs():
        if j.get("name") == name:
            return j
    return None


# ── Schedule parsing ─────────────────────────────────────────────────────────

def _to_calendar(sched: str):
    """'HH:MM' (daily) or cron 'M H D Mon Wday' (with * = any) → a dict with
    Minute/Hour and optional Day/Month/Weekday. None on anything unparseable.
    The minute must be concrete (a '*' minute would mean every minute)."""
    s = (sched or "").strip()
    if ":" in s and len(s.split()) == 1:
        try:
            h, m = s.split(":")
            h, m = int(h), int(m)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return {"Hour": h, "Minute": m}
        except ValueError:
            pass
        return None

    parts = s.split()
    if len(parts) != 5:
        return None
    minute, hour, day, month, weekday = parts
    if minute == "*":
        return None
    cal: dict = {}
    spec = [(minute, "Minute", 0, 59), (hour, "Hour", 0, 23),
            (day, "Day", 1, 31), (month, "Month", 1, 12),
            (weekday, "Weekday", 0, 7)]
    for val, key, lo, hi in spec:
        if val == "*":
            continue
        if not val.lstrip("-").isdigit():
            return None
        n = int(val)
        if not (lo <= n <= hi):
            return None
        cal[key] = n
    return cal or None


def _most_recent_due(now: datetime, cal: dict):
    """Most recent datetime <= now that matches the calendar spec, or None.
    Searches back up to ~1 year (covers daily/weekly/monthly/yearly)."""
    minute = cal.get("Minute", 0)
    hour   = cal.get("Hour", 0)
    for back in range(0, 367):
        d = now - timedelta(days=back)
        if "Month" in cal and d.month != cal["Month"]:
            continue
        if "Day" in cal and d.day != cal["Day"]:
            continue
        if "Weekday" in cal:
            # launchd weekday: 0/7=Sun, 1=Mon..6=Sat → Python weekday() Mon0..Sun6
            py_wd = 6 if cal["Weekday"] in (0, 7) else cal["Weekday"] - 1
            if d.weekday() != py_wd:
                continue
        cand = d.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand <= now:
            return cand
    return None


# ── Tick state (prevents double-fire; enables sleep catch-up) ─────────────────

def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    _HOME.mkdir(parents=True, exist_ok=True)
    try:
        _STATE_FILE.write_text(json.dumps(state))
    except Exception:
        pass


def due_jobs(now: datetime | None = None) -> list[dict]:
    """Return the enabled jobs that are due to fire now, marking them fired.

    A never-seen job is seeded with `now` (so a fresh start doesn't retroactively
    fire a slot that already passed today). Thereafter a job fires when its most
    recent scheduled time is later than its last run — which also catches up a
    slot missed while the Mac was asleep.
    """
    now = now or datetime.now()
    state = _load_state()
    out: list[dict] = []
    changed = False
    for j in list_jobs():
        if not j.get("enabled"):
            continue
        cal = _to_calendar(j.get("schedule", ""))
        if not cal:
            continue
        name = j["name"]
        last = state.get(name)
        if last is None:
            state[name] = now.isoformat()        # seed, no retro-fire
            changed = True
            continue
        try:
            last_dt = datetime.fromisoformat(last)
        except Exception:
            last_dt = None
        due = _most_recent_due(now, cal)
        if due and (last_dt is None or last_dt < due):
            out.append(j)
            state[name] = now.isoformat()
            changed = True
    if changed:
        _save_state(state)
    return out


# ── Legacy launchd cleanup (migration off per-job plists) ────────────────────

def _remove_legacy_plist(name: str) -> None:
    """Unload + delete a per-job LaunchAgent from the old design, if present."""
    path = _LAUNCH_DIR / f"{_LABEL_PFX}{name}.plist"
    if path.exists():
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        path.unlink()


# ── CRUD (used by the schedule_* tools and the CLI) ──────────────────────────

def add_job(name: str, prompt: str, schedule: str,
            sink: str = "notify", enabled: bool = True,
            tool_groups: list | None = None, builtin: str | None = None) -> str:
    if not name or not (prompt or builtin) or not schedule:
        return "error: name, schedule and (prompt or builtin) are required"
    if sink not in _VALID_SINKS:
        return f"error: sink must be one of {', '.join(sorted(_VALID_SINKS))}"
    if _to_calendar(schedule) is None:
        return (f"error: bad schedule '{schedule}' — use 'HH:MM' (daily) or cron "
                f"'M H * * *' (minute must be a number)")
    data = _load()
    jobs = data.setdefault("jobs", [])
    jobs[:] = [j for j in jobs if j.get("name") != name]  # replace if exists
    job = {
        "name": name, "prompt": prompt, "schedule": schedule,
        "sink": sink, "enabled": enabled, "allow_outward": False,
    }
    if tool_groups:
        job["tool_groups"] = list(tool_groups)
    if builtin:
        job["builtin"] = builtin
    jobs.append(job)
    _save(data)
    _remove_legacy_plist(name)   # in case an old per-job plist exists
    # reset state so the new/edited job is seeded fresh on the next tick
    st = _load_state(); st.pop(name, None); _save_state(st)
    return f"ok: job '{name}' saved — {schedule}, sink={sink}, enabled={enabled}"


def remove_job(name: str) -> str:
    data = _load()
    jobs = data.get("jobs", [])
    if not any(j.get("name") == name for j in jobs):
        return f"error: no job '{name}'"
    _remove_legacy_plist(name)
    data["jobs"] = [j for j in jobs if j.get("name") != name]
    _save(data)
    st = _load_state(); st.pop(name, None); _save_state(st)
    return f"ok: removed job '{name}'"


def set_enabled(name: str, enabled: bool) -> str:
    data = _load()
    job = next((j for j in data.get("jobs", []) if j.get("name") == name), None)
    if not job:
        return f"error: no job '{name}'"
    job["enabled"] = enabled
    _save(data)
    st = _load_state(); st.pop(name, None); _save_state(st)  # reseed on re-enable
    return f"ok: {'enabled' if enabled else 'disabled'} '{name}'"


def format_jobs() -> str:
    jobs = list_jobs()
    if not jobs:
        return "no scheduled jobs"
    lines = []
    for j in jobs:
        state = "on " if j.get("enabled") else "off"
        what  = j.get("builtin") or (j.get("prompt", "")[:80])
        lines.append(f"[{state}] {j['name']}  ({j.get('schedule')}, sink={j.get('sink')})\n"
                     f"        {what}")
    return "\n".join(lines)


# ── CLI: `wisp schedule …` ───────────────────────────────────────────────────

def cli(argv: list[str]) -> None:
    cmd = argv[0] if argv else "list"
    rest = argv[1:]
    if cmd == "list":
        print(format_jobs())
    elif cmd == "remove" and rest:
        print(remove_job(rest[0]))
    elif cmd in ("on", "enable") and rest:
        print(set_enabled(rest[0], True))
    elif cmd in ("off", "disable") and rest:
        print(set_enabled(rest[0], False))
    else:
        print("usage: wisp schedule [list | remove <name> | on <name> | off <name>]\n"
              "(scheduled jobs run inside the gateway — start it with: wisp gateway install)")

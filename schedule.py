#!/usr/bin/env python3
"""
wisp scheduler — define recurring agent tasks that run headlessly via launchd.

A job = a natural-language prompt + a schedule + an output sink. Jobs live in
~/wisp/schedule.yaml; each enabled job gets a LaunchAgent plist that invokes
`agent.py run --job <name>` at the scheduled time. launchd (not cron) because it
is macOS-native, catches up missed runs after sleep, and runs in the user's GUI
session so Mail/Calendar/Reminders/notification access works.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import yaml

_HOME       = Path("~/wisp").expanduser()
_SCHED_FILE = _HOME / "schedule.yaml"
_LOG_DIR    = _HOME / "logs"
_LAUNCH_DIR = Path("~/Library/LaunchAgents").expanduser()
_LABEL_PFX  = "com.wisp."
_AGENT_PY   = str(Path(__file__).resolve().parent / "agent.py")
_PYTHON     = sys.executable

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


# ── Schedule parsing → launchd StartCalendarInterval ─────────────────────────

def _to_calendar(sched: str):
    """'HH:MM' (daily) or cron 'M H D Mon Wday' (with * = any) → launchd dict.
    Returns None on anything we can't safely express. The minute must be
    concrete (a '*' minute would fire 60×/hour)."""
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
        return None  # refuse every-minute schedules
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


# ── launchd install / uninstall ──────────────────────────────────────────────

def _plist_path(name: str) -> Path:
    return _LAUNCH_DIR / f"{_LABEL_PFX}{name}.plist"


def _install(name: str) -> str:
    job = get_job(name)
    if not job:
        return f"error: no job '{name}'"
    cal = _to_calendar(job.get("schedule", ""))
    if cal is None:
        return f"error: bad schedule '{job.get('schedule')}'"
    _LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    # run_once owns <name>.log (the run transcript); launchd writes only its own
    # level (crashes/import errors before run_once) to a separate file so the two
    # don't fight over the same handle.
    launchd_log = str(_LOG_DIR / f"{name}.launchd.log")
    plist = {
        "Label": f"{_LABEL_PFX}{name}",
        "ProgramArguments": [_PYTHON, _AGENT_PY, "run", "--job", name],
        "StartCalendarInterval": cal,
        "StandardOutPath": launchd_log,
        "StandardErrorPath": launchd_log,
        "RunAtLoad": False,
        "EnvironmentVariables": {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
        },
    }
    path = _plist_path(name)
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    # reload so changes take effect (unload is harmless if not loaded)
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    r = subprocess.run(["launchctl", "load", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        return f"error: launchctl load failed: {r.stderr.strip()}"
    return "ok"


def _uninstall(name: str) -> None:
    path = _plist_path(name)
    if path.exists():
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        path.unlink()


# ── CRUD (used by the schedule_* tools and the CLI) ──────────────────────────

def add_job(name: str, prompt: str, schedule: str,
            sink: str = "notify", enabled: bool = True) -> str:
    if not name or not prompt or not schedule:
        return "error: name, prompt and schedule are required"
    if sink not in _VALID_SINKS:
        return f"error: sink must be one of {', '.join(sorted(_VALID_SINKS))}"
    if _to_calendar(schedule) is None:
        return (f"error: bad schedule '{schedule}' — use 'HH:MM' (daily) or cron "
                f"'M H * * *' (minute must be a number)")
    data = _load()
    jobs = data.setdefault("jobs", [])
    jobs[:] = [j for j in jobs if j.get("name") != name]  # replace if exists
    jobs.append({
        "name": name, "prompt": prompt, "schedule": schedule,
        "sink": sink, "enabled": enabled, "allow_outward": False,
    })
    _save(data)
    if enabled:
        r = _install(name)
        if r != "ok":
            return r
    return f"ok: job '{name}' saved — {schedule}, sink={sink}, enabled={enabled}"


def remove_job(name: str) -> str:
    data = _load()
    jobs = data.get("jobs", [])
    if not any(j.get("name") == name for j in jobs):
        return f"error: no job '{name}'"
    _uninstall(name)
    data["jobs"] = [j for j in jobs if j.get("name") != name]
    _save(data)
    return f"ok: removed job '{name}'"


def set_enabled(name: str, enabled: bool) -> str:
    data = _load()
    job = next((j for j in data.get("jobs", []) if j.get("name") == name), None)
    if not job:
        return f"error: no job '{name}'"
    job["enabled"] = enabled
    _save(data)
    if enabled:
        return f"ok: enabled '{name}' — {_install(name)}"
    _uninstall(name)
    return f"ok: disabled '{name}'"


def format_jobs() -> str:
    jobs = list_jobs()
    if not jobs:
        return "no scheduled jobs"
    lines = []
    for j in jobs:
        state = "on " if j.get("enabled") else "off"
        lines.append(f"[{state}] {j['name']}  ({j.get('schedule')}, sink={j.get('sink')})\n"
                     f"        {j.get('prompt', '')[:80]}")
    return "\n".join(lines)


# ── CLI: `agent.py schedule …` ───────────────────────────────────────────────

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
    elif cmd == "install":
        # re-sync all enabled jobs' plists (e.g. after moving the repo)
        for j in list_jobs():
            if j.get("enabled"):
                print(f"{j['name']}: {_install(j['name'])}")
    else:
        print("usage: agent.py schedule [list | remove <name> | "
              "on <name> | off <name> | install]")

#!/usr/bin/env python3
"""
wisp Telegram gateway — chat with wisp from your phone.

Long-polls Telegram for messages from the single authorized user, runs each
through wisp headlessly (reusing agent.run_once with the telegram sink), and
replies in the same chat. Runs as a persistent LaunchAgent (KeepAlive) so it is
always listening and restarts if it dies.

This deliberately reuses the same headless core the Scheduler uses — the only
gateway-specific part is the transport (Telegram getUpdates/sendMessage).
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import requests

_HOME       = Path("~/wisp").expanduser()
_LOG_DIR    = _HOME / "logs"
_LAUNCH_DIR = Path("~/Library/LaunchAgents").expanduser()
_LABEL      = "com.wisp.gateway"
_AGENT_PY   = str(Path(__file__).resolve().parent / "agent.py")
_PYTHON     = sys.executable
_API        = "https://api.telegram.org/bot{token}/{method}"


# One run at a time across chat + scheduler, so two tasks never hit Mail.app /
# EventKit concurrently (that wedged Mail in testing).
import threading
_RUN_LOCK = threading.Lock()


def _scheduler_loop() -> None:
    """Tick every 30s: fire any scheduled job that is due. Runs inside the
    gateway process so there is a single daemon managing chat + schedules.
    Catches up a slot missed while the Mac slept (see schedule.due_jobs)."""
    import agent, schedule
    while True:
        try:
            for job in schedule.due_jobs():
                with _RUN_LOCK:
                    try:
                        agent.run_once(
                            job.get("prompt", ""), job.get("sink", "notify"),
                            name=job["name"], tool_groups=job.get("tool_groups"),
                            builtin=job.get("builtin"))
                    except Exception as e:
                        agent._telegram_send(f"⚠️ 定时任务 {job['name']} 失败：{e}")
        except Exception:
            pass
        time.sleep(30)


def run() -> None:
    """Gateway main loop: Telegram long-poll + a scheduler tick thread. Blocks
    forever (launchd KeepAlive keeps it alive)."""
    import agent
    token   = agent.TELEGRAM_BOT_TOKEN
    user_id = agent.TELEGRAM_USER_ID
    if not token or not user_id:
        print("error: telegram.bot_token / user_id not set in config.yaml")
        return

    def api(method, **params):
        return requests.get(_API.format(token=token, method=method),
                            params=params, timeout=60)

    # Start the in-process scheduler.
    threading.Thread(target=_scheduler_loop, daemon=True).start()

    agent._telegram_send("🤖 wisp 网关已上线，随时为你服务。")
    offset = None
    while True:
        try:
            r = api("getUpdates", timeout=50, offset=offset)
            updates = r.json().get("result", [])
        except Exception:
            time.sleep(5)
            continue

        for u in updates:
            offset = u["update_id"] + 1            # ack even unauthorized msgs
            msg  = u.get("message") or {}
            frm  = str((msg.get("from") or {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if frm != str(user_id) or not text:
                continue                            # serve only the owner
            if text.lower() in ("/start", "/help"):
                agent._telegram_send(
                    "我是 wisp。直接发指令即可，例如：\n"
                    "• 看看我未读邮件\n• 今天和明天的日程\n• 搜索都柏林天气\n"
                    "• 给 X 起草一封邮件")
                continue
            agent._telegram_send("⏳ 收到，处理中…")
            try:
                with _RUN_LOCK:   # serialize with scheduled runs
                    agent.run_once(text, sink="telegram", name="telegram")
            except Exception as e:
                agent._telegram_send(f"出错了：{e}")


# ── launchd daemon (KeepAlive) ───────────────────────────────────────────────

def _plist_path() -> Path:
    return _LAUNCH_DIR / f"{_LABEL}.plist"


def install() -> str:
    _LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = str(_LOG_DIR / "gateway.log")
    plist = {
        "Label": _LABEL,
        "ProgramArguments": [_PYTHON, _AGENT_PY, "gateway", "run"],
        "RunAtLoad": True,
        "KeepAlive": True,          # restart if it exits/crashes
        "ThrottleInterval": 10,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
        },
    }
    path = _plist_path()
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    r = subprocess.run(["launchctl", "load", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        return f"error: launchctl load failed: {r.stderr.strip()}"
    return "ok: gateway installed and running (KeepAlive)"


def uninstall() -> str:
    path = _plist_path()
    if path.exists():
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        path.unlink()
        return "ok: gateway stopped and removed"
    return "gateway not installed"


def status() -> str:
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if _LABEL in line:
            pid = line.split()[0]
            return (f"running (pid {pid})" if pid != "-"
                    else "loaded but not running")
    installed = "installed" if _plist_path().exists() else "not installed"
    return f"stopped ({installed})"


def start() -> str:
    p = _plist_path()
    if not p.exists():
        return "not installed — run: agent.py gateway install"
    r = subprocess.run(["launchctl", "load", str(p)], capture_output=True, text=True)
    return "ok: started" if r.returncode == 0 else "already running"


def stop() -> str:
    p = _plist_path()
    if not p.exists():
        return "not installed"
    subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
    return "ok: stopped (auto-starts again at next login; use uninstall to remove)"


def restart() -> str:
    r = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{_LABEL}"],
        capture_output=True, text=True)
    if r.returncode == 0:
        return "ok: restarted"
    p = _plist_path()                      # fallback: reload
    subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
    subprocess.run(["launchctl", "load", str(p)], capture_output=True)
    return "ok: restarted (reloaded)"


def cli(argv: list[str]) -> None:
    cmd = argv[0] if argv else "run"
    actions = {"run": run, "install": install, "uninstall": uninstall,
               "status": status, "start": start, "stop": stop, "restart": restart}
    if cmd == "run":
        run()
    elif cmd in actions:
        print(actions[cmd]())
    else:
        print("usage: agent.py gateway "
              "[run | install | uninstall | status | start | stop | restart]")

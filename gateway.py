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


def _new_session(agent) -> list:
    """Fresh conversation for the Telegram chat (system slot only)."""
    return [{"role": "system", "content": ""}]


def _status_text(agent, session: list, started: float) -> str:
    import schedule
    from datetime import timedelta
    up = timedelta(seconds=int(time.time() - started))
    est = agent._estimate_tokens(session) + agent._schema_tokens()
    win = agent.CONTEXT_WINDOW
    ctx = f"~{est}/{win} ({est * 100 // win}%)" if win else f"~{est} tok"
    jobs = [j for j in schedule.list_jobs() if j.get("enabled")]
    msgs = max(0, len(session) - 1)
    return ("📊 wisp 状态\n"
            f"• 运行时长: {up}\n"
            f"• 模型: {agent.MODEL}\n"
            f"• 当前会话: {msgs} 条消息, 上下文 {ctx}\n"
            f"• 定时任务: {len(jobs)} 个启用"
            + ("\n  - " + "\n  - ".join(f"{j['name']} ({j.get('schedule')})" for j in jobs)
               if jobs else ""))


_HELP = ("我是 wisp，可多轮对话（记得上文）。直接发指令，例如：\n"
         "• 看看我未读邮件 → 然后“删掉第二封”\n"
         "• 今天和明天的日程\n"
         "• 搜索都柏林天气\n"
         "• 每晚10点提醒我睡觉\n\n"
         "命令：\n"
         "/new 开新会话（清空上下文）\n"
         "/status 查看运行状态与上下文占用\n"
         "/jobs 查看定时任务\n"
         "/help 帮助")


def run() -> None:
    """Gateway main loop: Telegram long-poll + a scheduler tick thread. Keeps a
    persistent multi-turn session (auto-compressed). Blocks forever."""
    import agent
    import schedule
    token   = agent.TELEGRAM_BOT_TOKEN
    user_id = agent.TELEGRAM_USER_ID
    if not token or not user_id:
        print("error: telegram.bot_token / user_id not set in config.yaml")
        return

    def api(method, **params):
        return requests.get(_API.format(token=token, method=method),
                            params=params, timeout=60)

    if agent.CONTEXT_WINDOW <= 0:        # so /status shows ctx %
        agent.CONTEXT_WINDOW = agent._detect_context_window()
    threading.Thread(target=_scheduler_loop, daemon=True).start()

    started = time.time()
    session = _new_session(agent)        # persistent conversation across messages
    agent._telegram_send("🤖 wisp 网关已上线。发 /help 看用法。")
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

            low = text.lower()
            if low in ("/start", "/help"):
                agent._telegram_send(_HELP); continue
            if low in ("/new", "/reset"):
                session = _new_session(agent)
                agent._telegram_send("🆕 已开新会话，上下文已清空。"); continue
            if low == "/status":
                agent._telegram_send(_status_text(agent, session, started)); continue
            if low == "/jobs":
                agent._telegram_send("⏰ 定时任务\n" + schedule.format_jobs()); continue

            # Keep a "typing…" indicator alive while the (possibly multi-minute)
            # task runs, so the chat doesn't look frozen.
            stop = threading.Event()

            def _typing():
                while not stop.is_set():
                    try:
                        api("sendChatAction", chat_id=user_id, action="typing")
                    except Exception:
                        pass
                    stop.wait(4)

            threading.Thread(target=_typing, daemon=True).start()
            try:
                with _RUN_LOCK:                      # serialize with scheduled runs
                    answer = agent.run_session(text, session, log_name="telegram")
                agent._telegram_send(answer or "(无输出)")
            except Exception as e:
                agent._telegram_send(f"出错了：{e}")
            finally:
                stop.set()


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
    """Reload the gateway with the current code. Uses unload+load (deterministic)
    rather than `launchctl kickstart -k`, which was observed to report success
    without actually replacing the process — leaving stale code running."""
    p = _plist_path()
    if not p.exists():
        return "not installed — run: wisp gateway install"
    old = _pid()
    subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
    r = subprocess.run(["launchctl", "load", str(p)], capture_output=True, text=True)
    if r.returncode != 0:
        return f"error: launchctl load failed: {r.stderr.strip()}"
    time.sleep(1.5)
    new = _pid()
    if new and new != old:
        return f"ok: restarted (pid {old or '—'} → {new})"
    return "ok: reloaded"


def _pid() -> str | None:
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if _LABEL in line:
            pid = line.split()[0]
            return pid if pid != "-" else None
    return None


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


if __name__ == "__main__":
    cli(sys.argv[1:])

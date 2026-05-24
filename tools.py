"""
wisp-agent tool definitions and implementations.
Each section: schemas (sent to LLM) + implementations (executed locally).
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import requests as _requests
import task as _task

# ── Workspace sandbox ─────────────────────────────────────────────────────────

_WORKSPACE: Path | None = None
_STRICT: bool = True

def init_workspace(workspace: str, strict: bool = True) -> None:
    global _WORKSPACE, _STRICT
    _WORKSPACE = Path(workspace).expanduser().resolve()
    _WORKSPACE.mkdir(parents=True, exist_ok=True)
    (workspace_path("screenshots")).mkdir(exist_ok=True)
    _STRICT = strict

def workspace_path(name: str = "") -> Path:
    """Return a path inside the workspace, creating it if needed."""
    if _WORKSPACE is None:
        raise RuntimeError("workspace not initialised — call init_workspace() first")
    p = _WORKSPACE / name if name else _WORKSPACE
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def _in_workspace(path: Path) -> bool:
    if _WORKSPACE is None:
        return False
    try:
        path.resolve().relative_to(_WORKSPACE)
        return True
    except ValueError:
        return False

def _guard_path(path: str, action: str = "write") -> str | None:
    """
    Returns an error string if the operation is not allowed,
    or None if it's fine to proceed.
    """
    if not _STRICT:
        return None
    p = Path(path).expanduser().resolve()
    if not _in_workspace(p):
        return (
            f"security: {action} outside workspace is not allowed. "
            f"Workspace is {_WORKSPACE}. "
            f"Use a path inside the workspace instead."
        )
    return None

# ── Bash risk scanner ─────────────────────────────────────────────────────────

_RISKY = [
    (r"\brm\s+(-\w*[rRfF]\w*\s+|-\w*[rRfF]\w*$|--recursive|--force)", "destructive rm"),
    (r"\bsudo\s+rm\b",   "sudo rm"),
    (r"\b(mkfs|format)\b", "disk format"),
    (r"\bdd\s+if=",      "dd disk write"),
    (r"\bchmod\s+(777|a\+[wx])", "unsafe chmod"),
    (r"\bsudo\b",        "sudo usage"),
]

# Patterns that write to a file path (shell redirects + tee + common write tools)
_WRITE_OPS = re.compile(
    r"(?:>>?|tee\s+|tee\s+-a\s+|cp\s+\S+\s+|mv\s+\S+\s+)"
    r"\s*([~/\$][^\s;|&>]+)"
)

def _bash_risk(command: str) -> str | None:
    """Return a warning string if the command looks risky, else None."""
    for pattern, label in _RISKY:
        if re.search(pattern, command, re.IGNORECASE):
            return (
                f"security: '{label}' is not allowed.\n"
                f"Use workspace-safe alternatives instead."
            )

    # Detect writes / redirects to paths outside workspace
    if _STRICT and _WORKSPACE:
        for m in _WRITE_OPS.finditer(command):
            raw = m.group(1).strip().rstrip("'\"")
            # /dev/null, /dev/stderr, /dev/stdout — safe kernel sinks, not real files
            if raw.startswith("/dev/"):
                continue
            raw = raw.replace("$HOME", str(Path.home())).replace("~", str(Path.home()))
            try:
                target = Path(raw).expanduser().resolve()
                if not _in_workspace(target):
                    return (
                        f"security: writing to '{raw}' is outside the workspace ({_WORKSPACE}).\n"
                        f"Use write_file with a path inside the workspace instead."
                    )
            except Exception:
                pass
    return None

# ── Browser singleton ─────────────────────────────────────────────────────────
# Playwright browser/page kept alive across tool calls within a session.

_pw   = None
_browser = None
_page    = None
_BROWSER_TIMEOUT_MS: int = 30_000   # default 30 s; overridden by init_browser_timeout()


def init_browser_timeout(seconds: int) -> None:
    """Set the global browser operation timeout (in seconds)."""
    global _BROWSER_TIMEOUT_MS
    _BROWSER_TIMEOUT_MS = max(1, seconds) * 1000

def _get_page():
    global _pw, _browser, _page
    if _page is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError(
                "playwright not installed. Run: pip install playwright && playwright install chromium"
            )
        _pw      = sync_playwright().start()
        _browser = _pw.chromium.launch(headless=False)
        _page    = _browser.new_page()
    return _page


def _close_browser():
    global _pw, _browser, _page
    if _browser:
        _browser.close()
    if _pw:
        _pw.stop()
    _pw = _browser = _page = None


# ═════════════════════════════════════════════════════════════════════════════
# SCHEMAS
# ═════════════════════════════════════════════════════════════════════════════

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    """Helper to build a function-calling tool schema."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


SCHEMAS: list[dict] = [

    # ── Core ──────────────────────────────────────────────────────────────────
    _fn("bash", "Run a shell command on macOS (zsh). Use for system info, running scripts, etc.",
        {"command": {"type": "string"}}, ["command"]),

    _fn("read_file", "Read the full text content of a file.",
        {"path": {"type": "string"}}, ["path"]),

    _fn("write_file", "Write text content to a file, creating parent directories if needed.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),

    _fn("done", "Call this when the task is fully complete.",
        {"result": {"type": "string", "description": "Summary of what was accomplished"}},
        ["result"]),

    # ── Mac system ────────────────────────────────────────────────────────────
    _fn("screenshot",
        "Capture the screen (or a specific app window) and save as PNG. "
        "Returns the file path. Embeds the image if vision mode is enabled.",
        {
            "app":       {"type": "string", "description": "Optional: capture only this app's window, e.g. 'Safari'."},
            "save_path": {"type": "string", "description": "Optional: save path. Defaults to /tmp/wisp-<timestamp>.png"},
        }, []),

    _fn("osascript",
        "Run an AppleScript snippet. Use for: system notifications, controlling apps "
        "(Finder, Safari, Mail…), showing dialogs, getting frontmost app.",
        {"script": {"type": "string", "description": "AppleScript code to execute"}}, ["script"]),

    _fn("clipboard_read", "Read the current macOS clipboard contents.",
        {}, []),

    _fn("clipboard_write", "Write text to the macOS clipboard.",
        {"text": {"type": "string"}}, ["text"]),

    _fn("open",
        "Open a file, directory, URL, or application. Equivalent to double-clicking in Finder.",
        {
            "target": {"type": "string", "description": "File path, URL, or app name"},
            "app":    {"type": "string", "description": "Optional: open with a specific app, e.g. 'TextEdit'"},
        }, ["target"]),

    _fn("list_apps", "List all currently running (visible) applications on macOS.",
        {}, []),

    _fn("focus_app", "Bring an application to the foreground.",
        {"app": {"type": "string", "description": "Application name, e.g. 'Safari'"}}, ["app"]),

    _fn("quit_app", "Quit an application.",
        {
            "app":   {"type": "string", "description": "Application name, e.g. 'Safari'"},
            "force": {"type": "boolean", "description": "If true, force quit. Default false."},
        }, ["app"]),

    # ── File operations ───────────────────────────────────────────────────────
    _fn("find_files",
        "Search for files using Spotlight (mdfind). Fast, supports name, extension, content keywords.",
        {
            "query":   {"type": "string",  "description": "Search query: filename, keyword, or extension like '.py'"},
            "dir":     {"type": "string",  "description": "Optional: limit search to this directory"},
            "limit":   {"type": "integer", "description": "Max results to return. Default 20."},
        }, ["query"]),

    _fn("move_file", "Move or rename a file or directory.",
        {"src": {"type": "string"}, "dst": {"type": "string"}}, ["src", "dst"]),

    _fn("copy_file", "Copy a file or directory.",
        {"src": {"type": "string"}, "dst": {"type": "string"}}, ["src", "dst"]),

    _fn("delete_file",
        "Move a file or directory to the Trash (safe delete). Use bash 'rm' only for truly permanent deletes.",
        {"path": {"type": "string"}}, ["path"]),

    # ── Network ───────────────────────────────────────────────────────────────
    _fn("http_get",
        "Send an HTTP GET request and return the response body (truncated to 4000 chars).",
        {
            "url":     {"type": "string"},
            "headers": {"type": "object", "description": "Optional request headers"},
        }, ["url"]),

    _fn("http_post",
        "Send an HTTP POST request with a JSON body and return the response.",
        {
            "url":     {"type": "string"},
            "body":    {"type": "object", "description": "JSON body to send"},
            "headers": {"type": "object", "description": "Optional request headers"},
        }, ["url", "body"]),

    # ── Browser ───────────────────────────────────────────────────────────────
    _fn("browser_open",
        "Open a URL in a Playwright-controlled Chromium browser. Creates the browser if not running.",
        {"url": {"type": "string"}}, ["url"]),

    _fn("browser_click",
        "Click an element on the current browser page. Prefer CSS selectors or text content.",
        {
            "selector": {"type": "string", "description": "CSS selector, e.g. 'button#submit' or 'text=Login'"},
        }, ["selector"]),

    _fn("browser_type",
        "Type text into a focused input field on the current browser page.",
        {
            "selector": {"type": "string", "description": "CSS selector for the input"},
            "text":     {"type": "string", "description": "Text to type"},
        }, ["selector", "text"]),

    _fn("browser_get_text",
        "Get the visible text content of the current browser page.",
        {
            "selector": {"type": "string", "description": "Optional: CSS selector to extract text from a specific element. Defaults to full page body."},
        }, []),

    _fn("browser_screenshot",
        "Take a screenshot of the current browser page and save as PNG.",
        {
            "save_path": {"type": "string", "description": "Optional save path. Defaults to /tmp/wisp-browser-<timestamp>.png"},
        }, []),

    _fn("browser_close", "Close the Playwright browser.",
        {}, []),

    # ── Reminders ─────────────────────────────────────────────────────────────
    _fn("reminders_list",
        "List incomplete reminders from macOS Reminders app.",
        {
            "list_name": {"type": "string", "description": "Optional: name of the Reminders list. Omit to read the default list. Pass \"*\" to read all lists."},
            "limit":     {"type": "integer", "description": "Max reminders to return. Default 20."},
        }, []),

    _fn("reminders_add",
        "Add a new reminder to macOS Reminders app.",
        {
            "title":     {"type": "string", "description": "Reminder title"},
            "due":       {"type": "string", "description": "Optional due date/time: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'"},
            "notes":     {"type": "string", "description": "Optional notes/body text"},
            "list_name": {"type": "string", "description": "Target list name. Default: 'Reminders'"},
        }, ["title"]),

    # ── Calendar ──────────────────────────────────────────────────────────────
    _fn("calendar_list",
        "List upcoming events from macOS Calendar app.",
        {
            "days":     {"type": "integer", "description": "How many days ahead to look. Default 7."},
            "calendar": {"type": "string",  "description": "Optional: name of a specific calendar."},
        }, []),

    _fn("calendar_add",
        "Add a new event to macOS Calendar app.",
        {
            "title":    {"type": "string", "description": "Event title"},
            "start":    {"type": "string", "description": "Start date/time: 'YYYY-MM-DD HH:MM'"},
            "end":      {"type": "string", "description": "Optional end date/time. Default: start + 1 hour."},
            "calendar": {"type": "string", "description": "Optional: target calendar name. Default: first writable calendar."},
            "notes":    {"type": "string", "description": "Optional event notes."},
        }, ["title", "start"]),

    # ── Daily briefing ────────────────────────────────────────────────────────
    _fn("daily_briefing",
        "Generate today's global news briefing as an HTML report. "
        "Fetches from RSS/sitemap sources, translates to Chinese, saves HTML. "
        "Sections: political/international, ICT research, ICT business, "
        "Irish news, entertainment, China news. Takes ~60–90 s.",
        {
            "open_browser": {
                "type":        "boolean",
                "description": "Open the report in the default browser when done. Default true.",
            },
            "sections": {
                "type":        "array",
                "items":       {"type": "string",
                                "enum": ["political", "ict_research", "ict_business",
                                         "ireland", "entertainment", "china"]},
                "description": "Sections to include. Omit to generate all six.",
            },
        }, []),

    # ── Task checkpoint ───────────────────────────────────────────────────────
    _fn("task_init",
        "Create a task plan with numbered steps. Call this at the start of any multi-step task "
        "(3 or more steps). Returns a task_id you must pass to task_step_done/task_step_fail.",
        {
            "title": {"type": "string", "description": "Short title for the overall task"},
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ordered list of step descriptions",
            },
        }, ["title", "steps"]),

    _fn("task_step_done",
        "Mark a step as completed. Call after each step finishes successfully.",
        {
            "task_id": {"type": "string", "description": "Task ID returned by task_init"},
            "step_id": {"type": "integer", "description": "Step number (1-based)"},
            "note":    {"type": "string",  "description": "Optional: brief note about what was done"},
        }, ["task_id", "step_id"]),

    _fn("task_step_fail",
        "Mark a step as failed. Use when a step cannot be completed. "
        "The task will be saved as 'interrupted' so it can be resumed later.",
        {
            "task_id": {"type": "string",  "description": "Task ID returned by task_init"},
            "step_id": {"type": "integer", "description": "Step number (1-based)"},
            "reason":  {"type": "string",  "description": "Why this step failed"},
        }, ["task_id", "step_id"]),

    _fn("learn",
        "Record a tool usage rule discovered during this task — e.g. when the user "
        "corrects a mistake or a better approach is found. Call this immediately when "
        "you recognise a correction. The rule is saved to knowledge.md and will apply "
        "to all future sessions.",
        {
            "rule": {
                "type": "string",
                "description": (
                    "One concise sentence: '完成[任务类型]应该用[工具/方法]，不用[错误方法]'. "
                    "Must mention a specific tool name."
                ),
            },
            "category": {
                "type": "string",
                "description": "Tool category this rule belongs to.",
                "enum": ["browser", "bash", "files", "general"],
            },
        }, ["rule", "category"]),
]


# ═════════════════════════════════════════════════════════════════════════════
# IMPLEMENTATIONS
# ═════════════════════════════════════════════════════════════════════════════

def _run(cmd: list[str], timeout: int = 30) -> str:
    """Run a subprocess, return combined stdout/stderr + exit code string."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        parts = []
        if p.stdout: parts.append(p.stdout.rstrip())
        if p.stderr: parts.append(f"[stderr]\n{p.stderr.rstrip()}")
        parts.append(f"[exit {p.returncode}]")
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return f"error: timed out after {timeout}s"
    except Exception as e:
        return f"error: {e}"


# ── Core ──────────────────────────────────────────────────────────────────────

def tool_bash(command: str, timeout: int = 30) -> str:
    risk = _bash_risk(command)
    if risk:
        return risk
    try:
        p = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
        parts = []
        if p.stdout: parts.append(p.stdout.rstrip())
        if p.stderr: parts.append(f"[stderr]\n{p.stderr.rstrip()}")
        parts.append(f"[exit {p.returncode}]")
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return f"error: timed out after {timeout}s"
    except Exception as e:
        return f"error: {e}"


def tool_read_file(path: str) -> str:
    try:
        return Path(path).expanduser().read_text()
    except FileNotFoundError:
        return f"error: file not found: {path}"
    except Exception as e:
        return f"error: {e}"


def tool_write_file(path: str, content: str) -> str:
    err = _guard_path(path, "write")
    if err: return err
    try:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"ok: wrote {len(content)} chars to {p}"
    except Exception as e:
        return f"error: {e}"


# ── Mac system ────────────────────────────────────────────────────────────────

def tool_screenshot(app: str = "", save_path: str = "", vision: bool = False) -> str | dict:
    path = save_path or str(workspace_path(f"screenshots/wisp-{int(time.time())}.png"))
    try:
        cmd = ["screencapture", "-x"]
        if app:
            wid = subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to tell process "{app}" to get id of front window'],
                capture_output=True, text=True,
            ).stdout.strip()
            if wid:
                cmd += ["-l", wid]
        cmd.append(path)
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            err = p.stderr.rstrip()
            if "could not create image" in err:
                return ("error: screen recording permission denied. "
                        "Go to System Settings → Privacy & Security → Screen Recording.")
            return f"error: screencapture failed — {err}"
        if not vision:
            return f"ok: screenshot saved to {path}"
        b64 = base64.b64encode(Path(path).read_bytes()).decode()
        return {"text": f"Screenshot saved to {path}", "image_b64": b64}
    except Exception as e:
        return f"error: {e}"


def tool_osascript(script: str) -> str:
    return _run(["osascript", "-e", script], timeout=15)


def tool_clipboard_read() -> str:
    try:
        p = subprocess.run(["pbpaste"], capture_output=True, text=True)
        return p.stdout or "(clipboard is empty)"
    except Exception as e:
        return f"error: {e}"


def tool_clipboard_write(text: str) -> str:
    try:
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
        return f"ok: wrote {len(text)} chars to clipboard"
    except Exception as e:
        return f"error: {e}"


def tool_open(target: str, app: str = "") -> str:
    try:
        cmd = ["open"] + (["-a", app] if app else []) + [target]
        subprocess.run(cmd, check=True)
        return f"ok: opened {target}" + (f" with {app}" if app else "")
    except Exception as e:
        return f"error: {e}"


def tool_list_apps() -> str:
    script = ('tell application "System Events" to get name of every '
              'application process whose background only is false')
    result = _run(["osascript", "-e", script])
    # Format comma-separated list into one-per-line
    if "[exit 0]" in result:
        names = result.split("[exit 0]")[0].strip()
        return "\n".join(n.strip() for n in names.split(","))
    return result


def tool_focus_app(app: str) -> str:
    return _run(["osascript", "-e", f'tell application "{app}" to activate'])


def tool_quit_app(app: str, force: bool = False) -> str:
    if force:
        return tool_bash(f"pkill -x '{app}'")
    return _run(["osascript", "-e", f'tell application "{app}" to quit'])


# ── File operations ───────────────────────────────────────────────────────────

def tool_find_files(query: str, dir: str = "", limit: int = 20) -> str:
    try:
        import os
        cmd = ["mdfind"]
        if dir:
            expanded = os.path.expandvars(os.path.expanduser(dir))
            cmd += ["-onlyin", expanded]
        # Support "*.png" or ".png" → kMDItemFSName query
        if query.startswith("*."):
            cmd += [f"kMDItemFSName == '{query}'"]
        elif query.startswith("."):
            cmd += [f"kMDItemFSName == '*{query}'"]
        else:
            cmd += [query]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        lines = [l for l in p.stdout.splitlines() if l][:limit]
        if not lines:
            return "no files found"
        return "\n".join(lines) + (f"\n(showing {limit} of more)" if len(lines) == limit else "")
    except Exception as e:
        return f"error: {e}"


def tool_move_file(src: str, dst: str) -> str:
    err = _guard_path(dst, "move destination")
    if err: return err
    try:
        s, d = Path(src).expanduser(), Path(dst).expanduser()
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
        return f"ok: moved {s} → {d}"
    except Exception as e:
        return f"error: {e}"


def tool_copy_file(src: str, dst: str) -> str:
    err = _guard_path(dst, "copy destination")
    if err: return err
    try:
        s, d = Path(src).expanduser(), Path(dst).expanduser()
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_dir():
            shutil.copytree(str(s), str(d))
        else:
            shutil.copy2(str(s), str(d))
        return f"ok: copied {s} → {d}"
    except Exception as e:
        return f"error: {e}"


def tool_delete_file(path: str) -> str:
    """Move to Trash — only allowed inside workspace."""
    err = _guard_path(path, "delete")
    if err: return err
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return f"error: path not found: {p}"
    script = f'tell application "Finder" to delete POSIX file "{p}"'
    result = _run(["osascript", "-e", script])
    if "[exit 0]" in result:
        return f"ok: moved to Trash: {p}"
    return result


# ── Network ───────────────────────────────────────────────────────────────────

def tool_http_get(url: str, headers: dict | None = None) -> str:
    try:
        resp = _requests.get(url, headers=headers or {}, timeout=30)
        body = resp.text[:4000] + ("…" if len(resp.text) > 4000 else "")
        return f"status: {resp.status_code}\n{body}"
    except Exception as e:
        return f"error: {e}"


def tool_http_post(url: str, body: dict, headers: dict | None = None) -> str:
    try:
        resp = _requests.post(url, json=body, headers=headers or {}, timeout=30)
        text = resp.text[:4000] + ("…" if len(resp.text) > 4000 else "")
        return f"status: {resp.status_code}\n{text}"
    except Exception as e:
        return f"error: {e}"


# ── Browser ───────────────────────────────────────────────────────────────────

def tool_browser_open(url: str) -> str:
    try:
        page = _get_page()
        page.goto(url, timeout=_BROWSER_TIMEOUT_MS)
        return f"ok: opened {url} — title: {page.title()}"
    except Exception as e:
        return f"error: {e}"


def tool_browser_click(selector: str) -> str:
    try:
        _get_page().click(selector, timeout=_BROWSER_TIMEOUT_MS)
        return f"ok: clicked '{selector}'"
    except Exception as e:
        return f"error: {e}"


def tool_browser_type(selector: str, text: str) -> str:
    try:
        _get_page().fill(selector, text, timeout=_BROWSER_TIMEOUT_MS)
        return f"ok: typed into '{selector}'"
    except Exception as e:
        return f"error: {e}"


def tool_browser_get_text(selector: str = "") -> str:
    try:
        page = _get_page()
        el = page.locator(selector) if selector else page.locator("body")
        text = el.inner_text(timeout=_BROWSER_TIMEOUT_MS)
        return text[:4000] + ("…" if len(text) > 4000 else "")
    except Exception as e:
        return f"error: {e}"


def tool_browser_screenshot(save_path: str = "") -> str:
    path = save_path or str(workspace_path(f"screenshots/wisp-browser-{int(time.time())}.png"))
    try:
        _get_page().screenshot(path=path, timeout=_BROWSER_TIMEOUT_MS)
        return f"ok: browser screenshot saved to {path}"
    except Exception as e:
        return f"error: {e}"


def tool_browser_close() -> str:
    try:
        _close_browser()
        return "ok: browser closed"
    except Exception as e:
        return f"error: {e}"


# ── Reminders & Calendar helpers ─────────────────────────────────────────────

def _osascript_clean(script: str, timeout: int = 15) -> str:
    """Run AppleScript; return stdout only (strips [exit N] suffix)."""
    result = _run(["osascript", "-e", script], timeout=timeout)
    if "[exit 0]" in result:
        return result.split("[exit 0]")[0].strip()
    return result  # error case: caller sees the raw error


def _parse_dt(s: str):
    """Parse 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD'. Returns datetime or None."""
    from datetime import datetime
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _as_date_script(var: str, dt) -> str:
    """Return AppleScript lines that set `var` to the given datetime.
    Uses component assignment — locale-independent."""
    secs = dt.hour * 3600 + dt.minute * 60
    return (
        f"set {var} to current date\n"
        f"set year of {var} to {dt.year}\n"
        f"set month of {var} to {dt.month}\n"
        f"set day of {var} to {dt.day}\n"
        f"set time of {var} to {secs}\n"
    )


# ── Reminders ─────────────────────────────────────────────────────────────────

def tool_reminders_list(list_name: str = "", limit: int = 20) -> str:
    # "*" means all lists; empty string means first/default list (faster, avoids iCloud timeout)
    if list_name == "*":
        list_clause = "every list"
        guard = ""
    elif list_name:
        list_clause = f'{{list "{list_name}"}}'
        guard = (
            f'if not (exists list "{list_name}") then\n'
            f'    return "error: list \\"{list_name}\\" not found"\n'
            f'end if\n'
        )
    else:
        list_clause = "{first list}"
        guard = ""

    # Batch-fetch properties (name / completed / due date) per list — much faster
    # than accessing reminder objects one by one. Scan at most 5 lists.
    script = f"""
tell application "Reminders"
    {guard}
    set theLists to {list_clause}
    set output to ""
    set counter to 0
    set listNum to 0
    repeat with theList in theLists
        set listNum to listNum + 1
        if listNum > 5 then exit repeat
        set listLabel to name of theList
        set rNames to name of every reminder of theList
        set rDones to completed of every reminder of theList
        set rDues  to due date of every reminder of theList
        repeat with i from 1 to count of rNames
            if counter >= {limit} then exit repeat
            if item i of rDones is false then
                set itemLine to listLabel & " | " & item i of rNames
                try
                    set dd to item i of rDues
                    if dd is not missing value then
                        set y to year of dd as string
                        set mo to (month of dd as integer)
                        if mo < 10 then set mo to "0" & mo
                        set dy to day of dd
                        if dy < 10 then set dy to "0" & dy
                        set t to time of dd
                        set h to t div 3600
                        set mi to (t mod 3600) div 60
                        if h < 10 then set h to "0" & h
                        if mi < 10 then set mi to "0" & mi
                        set itemLine to itemLine & " | due: " & y & "-" & mo & "-" & dy & " " & h & ":" & mi
                    end if
                end try
                set output to output & itemLine & "\\n"
                set counter to counter + 1
            end if
        end repeat
    end repeat
    if output is "" then return "no pending reminders"
    return output
end tell
"""
    return _osascript_clean(script, timeout=60)


def tool_reminders_add(title: str, due: str = "", notes: str = "",
                       list_name: str = "Reminders") -> str:
    date_lines = ""
    set_due    = ""
    if due:
        dt = _parse_dt(due)
        if dt is None:
            return f"error: cannot parse due date '{due}' — use YYYY-MM-DD or YYYY-MM-DD HH:MM"
        date_lines = _as_date_script("dueDate", dt)
        set_due    = "set due date of newReminder to dueDate"

    notes_escaped = notes.replace('"', '\\"')
    title_escaped = title.replace('"', '\\"')
    notes_prop    = f', body:"{notes_escaped}"' if notes else ""

    script = f"""
tell application "Reminders"
    if not (exists list "{list_name}") then
        make new list with properties {{name:"{list_name}"}}
    end if
    tell list "{list_name}"
        set newReminder to make new reminder with properties {{name:"{title_escaped}"{notes_prop}}}
        {date_lines}
        {set_due}
    end tell
end tell
return "ok"
"""
    result = _osascript_clean(script, timeout=60)
    if result == "ok" or "[exit 0]" in result:
        due_hint = f" (due {due})" if due else ""
        return f"ok: added reminder '{title}'{due_hint} to '{list_name}'"
    return result


# ── Calendar ──────────────────────────────────────────────────────────────────

def tool_calendar_list(days: int = 7, calendar_name: str = "") -> str:
    if calendar_name:
        cal_clause = f'{{calendar "{calendar_name}"}}'
        guard = (
            f'if not (exists calendar "{calendar_name}") then\n'
            f'    return "error: calendar \\"{calendar_name}\\" not found"\n'
            f'end if\n'
        )
    else:
        cal_clause = "every calendar"
        guard = ""

    script = f"""
tell application "Calendar"
    {guard}
    set startDate to current date
    set time of startDate to 0
    set endDate to startDate + ({days} * days)
    set theCals to {cal_clause}
    set output to ""
    set counter to 0
    repeat with cal in theCals
        try
            set theEvents to (every event of cal whose start date >= startDate and start date < endDate)
            repeat with e in theEvents
                if counter < 50 then
                    set sd to start date of e
                    set y to year of sd as string
                    set mo to (month of sd as integer)
                    if mo < 10 then set mo to "0" & mo
                    set dy to day of sd
                    if dy < 10 then set dy to "0" & dy
                    set t to time of sd
                    set h to t div 3600
                    set mi to (t mod 3600) div 60
                    if h < 10 then set h to "0" & h
                    if mi < 10 then set mi to "0" & mi
                    set dateStr to y & "-" & mo & "-" & dy & " " & h & ":" & mi
                    set output to output & dateStr & "\\t" & (name of cal) & "\\t" & (summary of e) & "\\n"
                    set counter to counter + 1
                end if
            end repeat
        end try
    end repeat
    if output is "" then return "no upcoming events in the next {days} days"
    return output
end tell
"""
    raw = _osascript_clean(script, timeout=60)
    if raw.startswith("error:") or raw.startswith("no upcoming"):
        return raw

    # Sort by date and format for display
    lines = [l for l in raw.splitlines() if l.strip()]
    try:
        lines.sort(key=lambda l: l.split("\t")[0])
    except Exception:
        pass
    result_lines = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) == 3:
            result_lines.append(f"{parts[0]}  {parts[1]}  {parts[2]}")
        else:
            result_lines.append(line)
    return "\n".join(result_lines)


def tool_calendar_add(title: str, start: str, end: str = "",
                      calendar_name: str = "", notes: str = "") -> str:
    start_dt = _parse_dt(start)
    if start_dt is None:
        return f"error: cannot parse start '{start}' — use YYYY-MM-DD HH:MM"

    from datetime import timedelta
    if end:
        end_dt = _parse_dt(end)
        if end_dt is None:
            return f"error: cannot parse end '{end}' — use YYYY-MM-DD HH:MM"
    else:
        end_dt = start_dt + timedelta(hours=1)

    start_lines = _as_date_script("startDate", start_dt)
    end_lines   = _as_date_script("endDate",   end_dt)

    if calendar_name:
        cal_clause = f'calendar "{calendar_name}"'
        guard = (
            f'if not (exists calendar "{calendar_name}") then\n'
            f'    return "error: calendar \\"{calendar_name}\\" not found"\n'
            f'end if\n'
        )
    else:
        cal_clause = "first calendar whose writable is true"
        guard = ""

    title_escaped = title.replace('"', '\\"')
    notes_prop    = f', description:"{notes.replace(chr(34), chr(92)+chr(34))}"' if notes else ""

    script = f"""
tell application "Calendar"
    {guard}
    {start_lines}
    {end_lines}
    tell {cal_clause}
        make new event with properties {{summary:"{title_escaped}", start date:startDate, end date:endDate{notes_prop}}}
    end tell
end tell
return "ok"
"""
    result = _osascript_clean(script, timeout=15)
    if result == "ok":
        end_hint = f" → {end}" if end else f" → {end_dt.strftime('%Y-%m-%d %H:%M')}"
        return f"ok: added event '{title}' on {start}{end_hint}"
    return result


# ── Daily briefing ───────────────────────────────────────────────────────────

def tool_daily_briefing(open_browser: bool = True,
                        sections: list | None = None) -> str:
    try:
        import briefing as _briefing
        return _briefing.generate_report(
            sections=sections or None,
            open_browser=open_browser,
        )
    except Exception as e:
        return f"error: {e}"


# ── Task checkpoint ───────────────────────────────────────────────────────────

def tool_task_init(title: str, steps: list[str]) -> str:
    task = _task.create(title, steps)
    total = len(task["steps"])
    lines = [f"ok: task created — id: {task['id']}", f"Title: {title}", f"Steps ({total}):"]
    for s in task["steps"]:
        lines.append(f"  {s['id']}. {s['title']}")
    return "\n".join(lines)


def tool_task_step_done(task_id: str, step_id: int, note: str = "") -> str:
    return _task.step_done(task_id, step_id, note)


def tool_task_step_fail(task_id: str, step_id: int, reason: str = "") -> str:
    return _task.step_fail(task_id, step_id, reason)


def tool_learn(rule: str, category: str = "general") -> str:
    import knowledge as _knowledge
    return _knowledge.append_rule(rule, category)


# ═════════════════════════════════════════════════════════════════════════════
# DISPATCH
# ═════════════════════════════════════════════════════════════════════════════

def dispatch(name: str, args: dict, vision: bool = False) -> Any:
    match name:
        # Core
        case "bash":             return tool_bash(args["command"])
        case "read_file":        return tool_read_file(args["path"])
        case "write_file":       return tool_write_file(args["path"], args["content"])
        # Mac system
        case "screenshot":       return tool_screenshot(args.get("app", ""), args.get("save_path", ""), vision)
        case "osascript":        return tool_osascript(args["script"])
        case "clipboard_read":   return tool_clipboard_read()
        case "clipboard_write":  return tool_clipboard_write(args["text"])
        case "open":             return tool_open(args["target"], args.get("app", ""))
        case "list_apps":        return tool_list_apps()
        case "focus_app":        return tool_focus_app(args["app"])
        case "quit_app":         return tool_quit_app(args["app"], args.get("force", False))
        # File operations
        case "find_files":       return tool_find_files(args["query"], args.get("dir", ""), args.get("limit", 20))
        case "move_file":        return tool_move_file(args["src"], args["dst"])
        case "copy_file":        return tool_copy_file(args["src"], args["dst"])
        case "delete_file":      return tool_delete_file(args["path"])
        # Network
        case "http_get":         return tool_http_get(args["url"], args.get("headers"))
        case "http_post":        return tool_http_post(args["url"], args["body"], args.get("headers"))
        # Daily briefing
        case "daily_briefing":     return tool_daily_briefing(
                                       args.get("open_browser", True),
                                       args.get("sections"))
        # Browser
        case "browser_open":       return tool_browser_open(args["url"])
        case "browser_click":      return tool_browser_click(args["selector"])
        case "browser_type":       return tool_browser_type(args["selector"], args["text"])
        case "browser_get_text":   return tool_browser_get_text(args.get("selector", ""))
        case "browser_screenshot": return tool_browser_screenshot(args.get("save_path", ""))
        case "browser_close":      return tool_browser_close()
        # Task checkpoint
        case "task_init":        return tool_task_init(args["title"], args["steps"])
        case "task_step_done":   return tool_task_step_done(args["task_id"], args["step_id"], args.get("note", ""))
        case "task_step_fail":   return tool_task_step_fail(args["task_id"], args["step_id"], args.get("reason", ""))
        case "learn":            return tool_learn(args["rule"], args.get("category", "general"))
        # Reminders
        case "reminders_list":   return tool_reminders_list(args.get("list_name", ""), args.get("limit", 20))
        case "reminders_add":    return tool_reminders_add(args["title"], args.get("due", ""), args.get("notes", ""), args.get("list_name", "Reminders"))
        # Calendar
        case "calendar_list":    return tool_calendar_list(args.get("days", 7), args.get("calendar", ""))
        case "calendar_add":     return tool_calendar_add(args["title"], args["start"], args.get("end", ""), args.get("calendar", ""), args.get("notes", ""))
        case _:                  return f"error: unknown tool '{name}'"

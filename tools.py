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
    (r"\bmkfs\b|\bformat\s+/dev/", "disk format"),
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

    _fn("browser_wait",
        "Wait for a CSS selector or text to appear on the current browser page. "
        "Essential after clicking search buttons on dynamic pages. "
        "Use 'text=Some Text' to wait for visible text content.",
        {
            "selector": {"type": "string", "description": "CSS selector or 'text=...' for visible text"},
            "timeout":  {"type": "integer", "description": "Max seconds to wait. Default 10."},
        }, ["selector"]),

    _fn("browser_select",
        "Select an option from a <select> dropdown on the current browser page.",
        {
            "selector": {"type": "string", "description": "CSS selector for the <select> element"},
            "value":    {"type": "string", "description": "Option label text or value to select"},
        }, ["selector", "value"]),

    _fn("browser_scroll",
        "Scroll the current browser page.",
        {
            "direction": {"type": "string",  "description": "Scroll direction: 'down' (default), 'up', 'left', 'right'"},
            "amount":    {"type": "integer", "description": "Pixels to scroll. Default 500."},
        }, []),

    _fn("browser_snapshot",
        "Return a structured snapshot of the current browser page: title, URL, headings, "
        "prices, buttons, input fields, and key links. Use this after waiting for results "
        "to extract flight prices, hotel rates, or other structured data without reading the full page.",
        {}, []),

    # ── Travel search ──────────────────────────────────────────────────────────
    _fn("flight_search",
        "Search for flights on Google Flights via browser automation. "
        "Returns airline names, departure/arrival times, duration, stops, and prices. "
        "Covers all major and budget airlines including European low-cost carriers "
        "(Ryanair, easyJet, Wizz Air, etc.). Use city names or IATA codes.",
        {
            "origin":      {"type": "string",  "description": "Departure city or IATA code (e.g. 'PEK', 'Dublin')"},
            "destination": {"type": "string",  "description": "Arrival city or IATA code (e.g. 'DUB', 'London')"},
            "date":        {"type": "string",  "description": "Departure date YYYY-MM-DD"},
            "return_date": {"type": "string",  "description": "Return date YYYY-MM-DD for round-trip. Omit for one-way."},
            "passengers":  {"type": "integer", "description": "Number of passengers. Default 1."},
            "cabin":       {"type": "string",  "description": "Cabin class: economy (default), business, first"},
        }, ["origin", "destination", "date"]),

    _fn("hotel_search",
        "Search for hotels on Google Hotels via browser automation. "
        "Returns hotel names, prices per night, ratings, and locations.",
        {
            "city":     {"type": "string",  "description": "Destination city or area (e.g. 'Dublin', '上海浦东')"},
            "checkin":  {"type": "string",  "description": "Check-in date YYYY-MM-DD"},
            "checkout": {"type": "string",  "description": "Check-out date YYYY-MM-DD"},
            "guests":   {"type": "integer", "description": "Number of guests. Default 1."},
            "rooms":    {"type": "integer", "description": "Number of rooms. Default 1."},
        }, ["city", "checkin", "checkout"]),

    # ── Reminders ─────────────────────────────────────────────────────────────
    _fn("reminders_list",
        "List incomplete reminders from macOS Reminders app.",
        {
            "list_name":  {"type": "string",  "description": "Optional: name of a specific Reminders list. Omit (or pass \"*\") to read all lists."},
            "due_before": {"type": "string",  "description": "Optional: only return reminders due on or before this date (YYYY-MM-DD). Use today's date to get today's todos. Leave empty for all pending reminders."},
            "limit":      {"type": "integer", "description": "Max reminders to return. Default 20."},
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

    # ── Notes ─────────────────────────────────────────────────────────────────
    _fn("notes_list",
        "List notes from macOS Notes app.",
        {
            "folder": {"type": "string",  "description": "Folder name to list. Omit or pass \"*\" for all folders. Default folder if empty."},
            "limit":  {"type": "integer", "description": "Max notes to return. Default 20."},
        }, []),

    _fn("notes_read",
        "Read the content of a note from macOS Notes app by title.",
        {
            "title":  {"type": "string", "description": "Note title (exact or partial match)."},
            "folder": {"type": "string", "description": "Optional: folder to search in. Omit to search all folders."},
        }, ["title"]),

    _fn("notes_create",
        "Create a new note in macOS Notes app.",
        {
            "title":   {"type": "string", "description": "Note title."},
            "content": {"type": "string", "description": "Note body text (plain text or simple markdown)."},
            "folder":  {"type": "string", "description": "Optional: target folder name. Creates folder if not exists. Default: first folder."},
        }, ["title", "content"]),

    _fn("notes_append",
        "Append text to an existing note in macOS Notes app.",
        {
            "title":   {"type": "string", "description": "Note title (exact or partial match)."},
            "content": {"type": "string", "description": "Text to append."},
            "folder":  {"type": "string", "description": "Optional: folder to search in."},
        }, ["title", "content"]),

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

def _browser_dismiss_consent(page) -> bool:
    """Auto-dismiss cookie/consent banners and bot-check press-and-hold challenges.

    Returns True if a banner was dismissed (caller should re-navigate to restore
    the target URL, which consent handlers may have redirected or reloaded away).
    """
    # 1. Standard click-to-accept banners
    candidates = [
        "全部拒绝", "Reject all", "Reject All",       # Google zh/en
        "Accept all cookies", "Accept all", "Reject",  # Skyscanner
        "全部接受", "Accept",                           # fallback
    ]
    for label in candidates:
        try:
            btn = page.locator(f"button:has-text(\"{label}\")")
            if btn.count() > 0:
                btn.first.click(timeout=3000)
                page.wait_for_load_state("networkidle", timeout=5000)
                return True
        except Exception:
            continue

    # 2. "PRESS & HOLD" bot-check (Skyscanner / DataDome style)
    try:
        hold_btn = page.locator(
            "button:has-text('PRESS & HOLD'), "
            "[class*='press-and-hold'], [class*='press_hold'], "
            "[id*='hold'], [data-testid*='hold']"
        )
        if hold_btn.count() > 0:
            box = hold_btn.first.bounding_box()
            if box:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                page.mouse.move(cx, cy)
                page.mouse.down()
                page.wait_for_timeout(2500)   # hold 2.5 seconds
                page.mouse.up()
                page.wait_for_load_state("networkidle", timeout=8000)
                return True
    except Exception:
        pass

    return False


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


def tool_browser_wait(selector: str, timeout: int = 10) -> str:
    """Wait for a CSS selector or visible text to appear on the page.

    Prefix selector with 'text=' to wait for visible text content.
    Example: 'text=Search Results', '#flight-list', '.price-card'
    """
    try:
        page = _get_page()
        ms = timeout * 1000
        if selector.startswith("text="):
            page.wait_for_selector(f"*:has-text(\"{selector[5:]}\")", timeout=ms)
        else:
            page.wait_for_selector(selector, timeout=ms)
        return f"ok: element '{selector}' appeared"
    except Exception as e:
        return f"error: {e}"


def tool_browser_select(selector: str, value: str) -> str:
    """Select an option from a <select> dropdown by value text or option value."""
    try:
        page = _get_page()
        # Try by label first, fall back to value
        try:
            page.select_option(selector, label=value, timeout=_BROWSER_TIMEOUT_MS)
        except Exception:
            page.select_option(selector, value=value, timeout=_BROWSER_TIMEOUT_MS)
        return f"ok: selected '{value}' in '{selector}'"
    except Exception as e:
        return f"error: {e}"


def tool_browser_scroll(direction: str = "down", amount: int = 500) -> str:
    """Scroll the current page. direction: 'down'|'up'|'left'|'right'."""
    try:
        page = _get_page()
        dx = {"left": -amount, "right": amount}.get(direction, 0)
        dy = {"down": amount, "up": -amount}.get(direction, 0)
        page.evaluate(f"window.scrollBy({dx}, {dy})")
        return f"ok: scrolled {direction} {amount}px"
    except Exception as e:
        return f"error: {e}"


def tool_browser_snapshot() -> str:
    """Return a structured snapshot of the current page.

    Extracts: page title, URL, headings, price-like numbers, links, buttons,
    and visible input fields — useful for parsing travel search results.
    """
    try:
        page = _get_page()
        data = page.evaluate("""() => {
            const prices = [];
            const seen = new Set();
            // Price-like patterns: numbers with currency symbols or decimals
            document.querySelectorAll('*').forEach(el => {
                if (el.children.length === 0) {
                    const t = el.innerText || '';
                    if (/[¥$€£￥]\\s?\\d|\\d+\\.\\d{2}|\\d{1,3}(,\\d{3})+/.test(t) && t.length < 30) {
                        const key = t.trim();
                        if (key && !seen.has(key)) { seen.add(key); prices.push(key); }
                    }
                }
            });
            const headings = Array.from(document.querySelectorAll('h1,h2,h3'))
                .map(h => h.innerText.trim()).filter(t => t).slice(0, 10);
            const links = Array.from(document.querySelectorAll('a[href]'))
                .map(a => ({text: a.innerText.trim().slice(0, 60), href: a.href}))
                .filter(l => l.text).slice(0, 20);
            const buttons = Array.from(document.querySelectorAll('button,[role=button]'))
                .map(b => b.innerText.trim()).filter(t => t).slice(0, 15);
            const inputs = Array.from(document.querySelectorAll('input,select,textarea'))
                .map(i => ({tag: i.tagName, name: i.name||i.id||i.placeholder||'', type: i.type||''}))
                .filter(i => i.name).slice(0, 15);
            return {
                title: document.title,
                url: location.href,
                headings, prices: prices.slice(0, 30), links, buttons, inputs
            };
        }""")
        import json
        out = [
            f"title: {data['title']}",
            f"url:   {data['url']}",
        ]
        if data["headings"]:
            out.append("headings: " + " | ".join(data["headings"]))
        if data["prices"]:
            out.append("prices: " + "  ".join(data["prices"]))
        if data["buttons"]:
            out.append("buttons: " + " | ".join(data["buttons"]))
        if data["inputs"]:
            out.append("inputs: " + "  ".join(f"{i['tag']}[{i['name']}]" for i in data["inputs"]))
        if data["links"]:
            out.append("links:")
            for l in data["links"]:
                out.append(f"  {l['text']}  →  {l['href']}")
        return "\n".join(out)
    except Exception as e:
        return f"error: {e}"


# ── Travel search ─────────────────────────────────────────────────────────────

_google_consent_done = False   # True once Google's consent cookie has been set


def _ensure_google_consent(page) -> None:
    """Handle Google's GDPR consent banner on google.com *before* navigating to
    any Google Travel SPA.  Doing consent on the simple homepage prevents the
    banner from disrupting the Travel SPA's initialisation, which breaks ?q=
    parameter parsing when consent fires mid-load on a complex SPA page.

    The flag is process-scoped; each new agent.py run starts fresh (new browser,
    no persistent profile), so consent is re-handled once per session.
    """
    global _google_consent_done
    if _google_consent_done:
        return
    try:
        page.goto("https://www.google.com/?hl=en", timeout=15000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        _browser_dismiss_consent(page)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        _google_consent_done = True
    except Exception:
        pass


def _flights_fill_airport(page, placeholder_zh: str, placeholder_en: str,
                          value: str) -> bool:
    """Fill an airport input on Google Flights and pick the first autocomplete suggestion.

    Clicking the field opens a dialog whose input gets keyboard focus.
    We use page.keyboard.type() (not locator.type) so keystrokes go to the
    dialog input.  Then ArrowDown+Enter selects the first airport suggestion.
    Returns True on success.
    """
    for ph in [placeholder_zh, placeholder_en]:
        try:
            loc = page.get_by_placeholder(ph)
            if loc.count() == 0:
                continue
            loc.first.click(timeout=5000)    # opens dialog, dialog input gets focus
            page.wait_for_timeout(500)
            page.keyboard.type(value, delay=70)   # keyboard → focused dialog input
            page.wait_for_timeout(2000)            # wait for autocomplete suggestions
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(300)
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)
            return True
        except Exception:
            continue
    return False


def _flights_patch_tfs_dates(tfs_url: str, dep_date: str,
                             ret_date: str = "") -> str:
    """Inject departure (and optional return) dates into a Google Flights tfs URL.

    After form-filling airports, the tfs protobuf contains airport codes but no
    dates.  This function inserts the date bytes into each flight leg.

    Protobuf structure used by Google Flights (simplified):
      field 1: trip type
      field 2: cabin class
      field 3 (repeated): flight leg
        sub-field 2 (optional): date string, YYYY-MM-DD, 10 chars
        sub-field 13: origin airport
        sub-field 14: destination airport

    The leg tag is 0x1a (field 3, wire type 2).  A length byte follows.
    Inserting \\x12\\x0a<YYYY-MM-DD> (12 bytes) at the start of each leg
    content adds the date and updates the length byte.
    """
    import base64, re as _re

    m = _re.search(r'[?&]tfs=([^&]+)', tfs_url)
    if not m:
        return tfs_url
    tfs_b64 = m.group(1)
    padded = tfs_b64 + '=' * (4 - len(tfs_b64) % 4)
    try:
        raw = bytearray(base64.urlsafe_b64decode(padded))
    except Exception:
        return tfs_url

    dep_bytes  = dep_date.encode('ascii')
    ret_bytes  = ret_date.encode('ascii') if ret_date else b''
    DATE_HDR   = 12   # \x12\x0a + 10-byte date

    # Collect positions of all field-3 legs (tag 0x1a)
    legs: list[tuple[int, int]] = []   # (tag_pos, len_pos)
    i = 0
    while i < len(raw) - 1:
        if raw[i] == 0x1a:
            legs.append((i, i + 1))
            leg_len = raw[i + 1]
            i += 2 + leg_len
        else:
            i += 1

    # Insert dates in reverse order so earlier offsets remain valid
    dates = [dep_bytes, ret_bytes]
    for leg_idx, (tag_pos, len_pos) in reversed(list(enumerate(legs))):
        if leg_idx >= len(dates):
            break
        date_b = dates[leg_idx]
        if not date_b:
            continue
        content_start = len_pos + 1
        # Skip if leg already contains a date (starts with \x12\x0a followed by digit)
        if raw[content_start] == 0x12 and raw[content_start + 1] == 0x0a and chr(raw[content_start + 2]).isdigit():
            continue
        insert = b'\x12\x0a' + date_b
        raw[content_start:content_start] = insert
        raw[len_pos] += DATE_HDR    # update leg length byte

    new_tfs = base64.urlsafe_b64encode(bytes(raw)).rstrip(b'=').decode('ascii')
    return _re.sub(r'([?&]tfs=)[^&]+', rf'\g<1>{new_tfs}', tfs_url)


def tool_flight_search(origin: str, destination: str, date: str,
                       return_date: str = "", passengers: int = 1,
                       cabin: str = "economy") -> str:
    """Search flights on Google Flights via browser form automation.

    Strategy:
    1. Navigate to Google Flights and fill origin/destination via the form
       (keyboard.type into the airport dialog that opens on click).
    2. After airports are selected the URL becomes a tfs= protobuf URL.
       We patch the date bytes into that protobuf and navigate to the new URL.
    3. Wait for real flight results (times + prices) and extract them.

    origin / destination: city name or IATA code (e.g. 'Dublin', 'PEK', 'London').
    date / return_date  : YYYY-MM-DD. Omit return_date for one-way.
    cabin               : economy | business | first
    Returns structured text of flight options with airline, times, and price.
    """
    try:
        import re
        page = _get_page()

        # 1. Handle Google consent on the simple homepage first so the Flights SPA
        #    initialises without the consent banner disrupting anything.
        _ensure_google_consent(page)

        # 2. Navigate to Google Flights (plain URL, no ?q= which is not processed).
        page.goto("https://www.google.com/travel/flights", timeout=_BROWSER_TIMEOUT_MS)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(1500)

        # 3. Fill origin airport.
        #    Clicking the field opens a modal dialog; keyboard.type goes to the
        #    focused dialog input (not the placeholder input on the main page).
        _flights_fill_airport(page, "从哪里出发？", "Where from?", origin)

        # 4. Fill destination airport.
        _flights_fill_airport(page, "要去哪儿？", "Where to?", destination)
        page.wait_for_timeout(500)

        # 5. Extract the tfs= URL that now encodes the selected airports,
        #    patch in the requested dates, then navigate to the patched URL.
        #    The tfs URL pre-fills the form but does NOT auto-run the search —
        #    we must click "搜索" after navigation.
        tfs_url = page.url
        if "tfs=" in tfs_url:
            tfs_url = _flights_patch_tfs_dates(tfs_url, date, return_date)
            page.goto(tfs_url, timeout=_BROWSER_TIMEOUT_MS)
        else:
            # Fallback: navigate with a plain search approach
            import urllib.parse
            q = f"flights from {origin} to {destination} on {date}"
            if return_date:
                q += f" returning {return_date}"
            page.goto(
                "https://www.google.com/travel/flights?" + urllib.parse.urlencode({"q": q}),
                timeout=_BROWSER_TIMEOUT_MS,
            )

        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(1000)

        # 6. Click the Search / 搜索 button to actually execute the search.
        try:
            search_btn = page.get_by_role("button", name=re.compile(r"搜索|Search", re.I))
            if search_btn.count() > 0:
                search_btn.first.click(timeout=5000)
                page.wait_for_timeout(1000)
        except Exception:
            pass

        # 8. Wait for actual flight content (times + prices) to appear.
        try:
            page.wait_for_function(
                """() => {
                    const t = document.body.innerText;
                    return /\\d+\\s*h\\s*\\d*\\s*m|\\d{1,2}:\\d{2}/.test(t)
                        && /[€£$¥]\\s*\\d|\\d\\s*[€£$¥]/.test(t);
                }""",
                timeout=25000,
            )
        except Exception:
            pass

        text = page.inner_text("body")
        lines = [l.strip() for l in text.splitlines() if l.strip()]

        flight_pat = re.compile(
            r"\d{1,2}:\d{2}|\$\d|¥\d|€\d|£\d|\d+h\s*\d*m?|direct|nonstop|\d\s*stop",
            re.I,
        )
        hit_idx = {i for i, l in enumerate(lines) if flight_pat.search(l)}
        ctx_idx = set()
        for i in hit_idx:
            for j in range(max(0, i - 1), min(len(lines), i + 2)):
                ctx_idx.add(j)
        useful = [lines[i] for i in sorted(ctx_idx)]
        result_text = "\n".join(useful[:80]) if useful else "\n".join(lines[:80])

        return (
            f"Flight search (Google Flights): {origin} → {destination} on {date}"
            + (f"  return {return_date}" if return_date else "  one-way")
            + f"\nPassengers: {passengers}  Cabin: {cabin}\n"
            + "─" * 50 + "\n"
            + result_text[:3000]
        )
    except Exception as e:
        return f"error: {e}"


def tool_hotel_search(city: str, checkin: str, checkout: str,
                      guests: int = 1, rooms: int = 1) -> str:
    """Search hotels on Google Hotels via browser automation.

    city            : destination city or area (e.g. 'Dublin', '上海').
    checkin/checkout: YYYY-MM-DD.
    Returns structured text of hotel options found.
    """
    try:
        page = _get_page()
        import urllib.parse, re

        q = f"hotels in {city} checkin {checkin} checkout {checkout}"
        if guests > 1:
            q += f" {guests} guests"
        # Ensure Google consent cookie is set before navigating to the Travel SPA.
        _ensure_google_consent(page)

        search_url = "https://www.google.com/travel/hotels?" + urllib.parse.urlencode({"q": q})
        page.goto(search_url, timeout=_BROWSER_TIMEOUT_MS)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        try:
            page.wait_for_selector("[data-ved],[data-hveid]", timeout=15000)
        except Exception:
            pass

        text = page.inner_text("body")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        # Capture lines with prices/ratings AND their neighbours (hotel names)
        price_pat = re.compile(r"\$\d|¥\d|€\d|£\d|★|☆|\d\.\d|per night|/night|评分|晚|stars?", re.I)
        hit_idx = {i for i, l in enumerate(lines) if price_pat.search(l)}
        ctx_idx = set()
        for i in hit_idx:
            for j in range(max(0, i - 2), min(len(lines), i + 3)):
                ctx_idx.add(j)
        useful = [lines[i] for i in sorted(ctx_idx)]
        result_text = "\n".join(useful[:80])
        if not result_text:
            result_text = "\n".join(lines[:80])

        return (
            f"Hotel search: {city}  {checkin} → {checkout}"
            + f"  guests: {guests}  rooms: {rooms}\n"
            + "─" * 40 + "\n"
            + result_text[:3000]
        )
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

def tool_reminders_list(list_name: str = "", limit: int = 20,
                        due_before: str = "") -> str:
    """List incomplete reminders.

    list_name : "" or "*" → all lists; specific name → one list.
    due_before: "YYYY-MM-DD" — only return reminders due on or before this date.
                Leave empty to return all incomplete reminders (no date filter).
    """
    # Build list clause
    if list_name in ("*", ""):
        list_clause = "every list"
        guard = ""
    else:
        list_clause = f'{{list "{list_name}"}}'
        guard = (
            f'if not (exists list "{list_name}") then\n'
            f'    return "error: list \\"{list_name}\\" not found"\n'
            f'end if\n'
        )

    # Build optional cutoff date block (injected into AppleScript)
    cutoff_block = ""
    use_cutoff   = False
    if due_before:
        dt = _parse_dt(due_before)
        if dt is None:
            return f'error: invalid due_before date "{due_before}" — use YYYY-MM-DD'
        # end-of-day of the cutoff date (23:59:59)
        cutoff_block = (
            f"set cutoff to current date\n"
            f"set year of cutoff to {dt.year}\n"
            f"set month of cutoff to {dt.month}\n"
            f"set day of cutoff to {dt.day}\n"
            f"set time of cutoff to 86399\n"
        )
        use_cutoff = True

    date_filter = (
        # skip reminders with no due date when filtering, or due after cutoff
        "if dd is missing value then\n"
        "    -- no due date: skip when date filter is active\n"
        "else if dd > cutoff then\n"
        "    -- due after cutoff: skip\n"
        "else\n"
        "    set showItem to true\n"
        "end if\n"
        if use_cutoff else
        # no filter: show reminders with or without due date
        "set showItem to true\n"
    )

    script = f"""
tell application "Reminders"
    {guard}
    {cutoff_block}
    set theLists to {list_clause}
    set output to ""
    set counter to 0
    set listNum to 0
    repeat with theList in theLists
        set listNum to listNum + 1
        if listNum > 10 then exit repeat
        set listLabel to name of theList
        -- batch-fetch only incomplete reminders (one round-trip per property)
        set rNames to name of (every reminder of theList whose completed is false)
        set rDues  to due date of (every reminder of theList whose completed is false)
        if (count of rNames) = 0 then
        else
            repeat with i from 1 to count of rNames
                if counter >= {limit} then exit repeat
                set showItem to false
                try
                    set dd to item i of rDues
                    {date_filter}
                end try
                if showItem then
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
        end if
    end repeat
    if output is "" then return "no pending reminders"
    return output
end tell
"""
    return _osascript_clean(script, timeout=90)


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


# ── Notes ────────────────────────────────────────────────────────────────────

def _notes_folder_clause(folder: str) -> tuple[str, str]:
    """Return (folder_clause, guard) for AppleScript Notes queries."""
    if folder in ("*", ""):
        return "every folder", ""
    f = folder.replace('"', '\\"')
    guard = (
        f'if not (exists folder "{f}") then\n'
        f'    return "error: folder \\"{f}\\" not found"\n'
        f'end if\n'
    )
    return f'{{folder "{f}"}}', guard


def _notes_fmt_date(var: str) -> str:
    """AppleScript snippet to format a date variable into YYYY-MM-DD."""
    return f"""
set _y to year of {var} as string
set _m to (month of {var} as integer)
if _m < 10 then set _m to "0" & _m
set _d to day of {var}
if _d < 10 then set _d to "0" & _d
set _dateStr to _y & "-" & _m & "-" & _d
"""


def tool_notes_list(folder: str = "", limit: int = 20) -> str:
    folder_clause, guard = _notes_folder_clause(folder)
    fmt = _notes_fmt_date("modDate")
    script = f"""
tell application "Notes"
    {guard}
    set theFolders to {folder_clause}
    set output to ""
    set counter to 0
    repeat with f in theFolders
        set fName to name of f
        set nTitles to name of every note of f
        set nDates  to modification date of every note of f
        repeat with i from 1 to count of nTitles
            if counter >= {limit} then exit repeat
            set modDate to item i of nDates
            {fmt}
            set output to output & fName & " | " & item i of nTitles & " | modified: " & _dateStr & "\\n"
            set counter to counter + 1
        end repeat
        if counter >= {limit} then exit repeat
    end repeat
    if output is "" then return "no notes found"
    return output
end tell
"""
    return _osascript_clean(script, timeout=30)


def tool_notes_read(title: str, folder: str = "") -> str:
    t = title.replace('"', '\\"')
    if folder and folder != "*":
        f = folder.replace('"', '\\"')
        search_clause = f'notes of folder "{f}" whose name contains "{t}"'
        guard = (
            f'if not (exists folder "{f}") then\n'
            f'    return "error: folder \\"{f}\\" not found"\n'
            f'end if\n'
        )
    else:
        search_clause = f'notes whose name contains "{t}"'
        guard = ""

    script = f"""
tell application "Notes"
    {guard}
    set matches to {search_clause}
    if (count of matches) = 0 then
        return "error: no note found matching \\"{t}\\""
    end if
    set theNote to item 1 of matches
    set nTitle to name of theNote
    -- plaintext always starts with the title as first line; strip it
    set rawText to plaintext of theNote
    set tid to AppleScript's text item delimiters
    set AppleScript's text item delimiters to "\\n"
    set allLines to text items of rawText
    set AppleScript's text item delimiters to tid
    if (count of allLines) > 1 then
        set bodyLines to items 2 thru -1 of allLines
        set AppleScript's text item delimiters to "\\n"
        set nBody to bodyLines as string
        set AppleScript's text item delimiters to tid
    else
        set nBody to ""
    end if
    return "title: " & nTitle & "\\n---\\n" & nBody
end tell
"""
    return _osascript_clean(script, timeout=20)


def tool_notes_create(title: str, content: str, folder: str = "") -> str:
    t = title.replace('"', '\\"')
    # Notes body: wrap plain text in basic HTML (preserves line breaks)
    body_html = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body_html = body_html.replace("\n", "<br>")
    body_escaped = body_html.replace('"', '\\"')

    if folder and folder != "*":
        f = folder.replace('"', '\\"')
        make_cmd = (
            f'if not (exists folder "{f}") then\n'
            f'    make new folder with properties {{name:"{f}"}}\n'
            f'end if\n'
            f'tell folder "{f}"\n'
            f'    make new note with properties {{name:"{t}", body:"{body_escaped}"}}\n'
            f'end tell\n'
        )
    else:
        # No folder specified — let Notes choose the default location
        make_cmd = f'make new note with properties {{name:"{t}", body:"{body_escaped}"}}'

    script = f"""
tell application "Notes"
    {make_cmd}
end tell
return "ok"
"""
    result = _osascript_clean(script, timeout=20)
    if result == "ok":
        folder_hint = f" in '{folder}'" if folder and folder != "*" else ""
        return f"ok: created note '{title}'{folder_hint}"
    return result


def tool_notes_append(title: str, content: str, folder: str = "") -> str:
    t = title.replace('"', '\\"')
    # Escape content for AppleScript
    body_html = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body_html = body_html.replace("\n", "<br>")
    body_escaped = body_html.replace('"', '\\"')

    if folder and folder != "*":
        f = folder.replace('"', '\\"')
        search_clause = f'notes of folder "{f}" whose name contains "{t}"'
        guard = (
            f'if not (exists folder "{f}") then\n'
            f'    return "error: folder \\"{f}\\" not found"\n'
            f'end if\n'
        )
    else:
        search_clause = f'notes whose name contains "{t}"'
        guard = ""

    script = f"""
tell application "Notes"
    {guard}
    set matches to {search_clause}
    if (count of matches) = 0 then
        return "error: no note found matching \\"{t}\\""
    end if
    set theNote to item 1 of matches
    set currentBody to body of theNote
    set body of theNote to currentBody & "<br><br>{body_escaped}"
end tell
return "ok"
"""
    result = _osascript_clean(script, timeout=20)
    if result == "ok":
        return f"ok: appended to note '{title}'"
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
        case "browser_wait":       return tool_browser_wait(args["selector"], args.get("timeout", 10))
        case "browser_select":     return tool_browser_select(args["selector"], args["value"])
        case "browser_scroll":     return tool_browser_scroll(args.get("direction", "down"), args.get("amount", 500))
        case "browser_snapshot":   return tool_browser_snapshot()
        # Travel
        case "flight_search":      return tool_flight_search(args["origin"], args["destination"], args["date"], args.get("return_date", ""), args.get("passengers", 1), args.get("cabin", "economy"))
        case "hotel_search":       return tool_hotel_search(args["city"], args["checkin"], args["checkout"], args.get("guests", 1), args.get("rooms", 1))
        # Task checkpoint
        case "task_init":        return tool_task_init(args["title"], args["steps"])
        case "task_step_done":   return tool_task_step_done(args["task_id"], args["step_id"], args.get("note", ""))
        case "task_step_fail":   return tool_task_step_fail(args["task_id"], args["step_id"], args.get("reason", ""))
        case "learn":            return tool_learn(args["rule"], args.get("category", "general"))
        # Reminders
        case "reminders_list":   return tool_reminders_list(args.get("list_name", ""), args.get("limit", 20), args.get("due_before", ""))
        case "reminders_add":    return tool_reminders_add(args["title"], args.get("due", ""), args.get("notes", ""), args.get("list_name", "Reminders"))
        # Calendar
        case "calendar_list":    return tool_calendar_list(args.get("days", 7), args.get("calendar", ""))
        case "calendar_add":     return tool_calendar_add(args["title"], args["start"], args.get("end", ""), args.get("calendar", ""), args.get("notes", ""))
        # Notes
        case "notes_list":       return tool_notes_list(args.get("folder", ""), args.get("limit", 20))
        case "notes_read":       return tool_notes_read(args["title"], args.get("folder", ""))
        case "notes_create":     return tool_notes_create(args["title"], args["content"], args.get("folder", ""))
        case "notes_append":     return tool_notes_append(args["title"], args["content"], args.get("folder", ""))
        case _:                  return f"error: unknown tool '{name}'"

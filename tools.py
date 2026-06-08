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
    _kgmid_cache_init(_WORKSPACE)

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


# ── SerpAPI key (set once from config.yaml) ───────────────────────────────────

_SERPAPI_KEY: str = ""

def init_serpapi(api_key: str) -> None:
    """Set the SerpAPI key used by flight_search and hotel_search."""
    global _SERPAPI_KEY
    _SERPAPI_KEY = (api_key or "").strip()

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
        "Search for flights via SerpAPI Google Flights (structured JSON API). "
        "Returns airline names, departure/arrival times, duration, stops, and prices. "
        "Covers all major and budget airlines including European low-cost carriers "
        "(Ryanair, easyJet, Wizz Air, etc.). City names are resolved automatically "
        "via Google's Knowledge Graph and a single search covers ALL airports for "
        "multi-airport cities (Paris → CDG+ORY+BVA, London → LHR+LGW+STN+LTN, etc).",
        {
            "origin":      {"type": "string",  "description": "Departure: city name in any language ('Paris', '巴黎', '都柏林'), city code ('PAR'), or specific airport code ('CDG'). City names cover all of that city's airports."},
            "destination": {"type": "string",  "description": "Arrival: city name in any language ('London', '伦敦'), city code ('LON'), or specific airport code ('LHR'). City names cover all of that city's airports."},
            "date":        {"type": "string",  "description": "Departure date YYYY-MM-DD"},
            "return_date": {"type": "string",  "description": "Return date YYYY-MM-DD for round-trip. Omit for one-way."},
            "passengers":  {"type": "integer", "description": "Number of passengers. Default 1."},
            "cabin":       {"type": "string",  "description": "Cabin class: economy (default), business, first"},
        }, ["origin", "destination", "date"]),

    _fn("hotel_search",
        "Search for hotels via SerpAPI Google Hotels (structured JSON API). "
        "Returns hotel names, prices per night, ratings, review counts, "
        "hotel class, and amenities.",
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
            "due_before": {"type": "string",  "description": "Optional: only return reminders due on or before this date (YYYY-MM-DD)."},
            "due_after":  {"type": "string",  "description": "Optional: only return reminders due on or after this date (YYYY-MM-DD). Combine with due_before to query a date range, e.g. today through tomorrow in one call."},
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

    _fn("reminders_complete",
        "Mark a reminder as completed (moves it to the Completed list). "
        "Requires the exact list name — use reminders_list first if unsure.",
        {
            "title":     {"type": "string", "description": "Reminder title (exact or partial match)."},
            "list_name": {"type": "string", "description": "Name of the Reminders list containing the reminder."},
        }, ["title", "list_name"]),

    _fn("reminders_delete",
        "Permanently delete a reminder from macOS Reminders app. "
        "Requires the exact list name — use reminders_list first if unsure.",
        {
            "title":     {"type": "string", "description": "Reminder title (exact or partial match)."},
            "list_name": {"type": "string", "description": "Name of the Reminders list containing the reminder."},
        }, ["title", "list_name"]),

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

    _fn("calendar_delete",
        "Delete an event from macOS Calendar app.",
        {
            "title":    {"type": "string", "description": "Event title (exact or partial match)."},
            "start":    {"type": "string", "description": "Optional: event start date 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' to disambiguate when multiple events share the same title."},
            "calendar": {"type": "string", "description": "Optional: calendar name to narrow the search."},
        }, ["title"]),

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

    _fn("notes_delete",
        "Delete a note from macOS Notes app.",
        {
            "title":  {"type": "string", "description": "Note title (exact or partial match)."},
            "folder": {"type": "string", "description": "Optional: folder to search in."},
        }, ["title"]),

    # ── Mail (macOS Mail.app) ─────────────────────────────────────────────────
    _fn("mail_accounts",
        "List all email accounts configured in macOS Mail.app. "
        "Call this first to discover account names and email addresses "
        "before using mail_list or other mail tools.",
        {}, []),

    _fn("mail_list",
        "List emails from macOS Mail.app. Works across all configured accounts "
        "(Gmail, Hotmail, iCloud, etc.) or a specific account. "
        "Returns sender, subject, date, and read/unread status for each message. "
        "Use unread_only=true to fetch only new emails.",
        {
            "account":     {"type": "string",  "description": "Filter by account email address (e.g. 'john@gmail.com'). Omit to list all configured accounts."},
            "mailbox":     {"type": "string",  "description": "Folder name. Default: 'INBOX'. Others: 'Sent', 'Junk', 'Archive', 'Drafts'."},
            "limit":       {"type": "integer", "description": "Max messages to return. Default 20."},
            "unread_only": {"type": "boolean", "description": "If true, only return unread messages. Default false."},
        }, []),

    _fn("mail_read",
        "Read the full content of an email from macOS Mail.app. "
        "Returns sender, date, subject, and body text (truncated to 3000 chars).",
        {
            "subject": {"type": "string", "description": "Email subject (exact or partial match)."},
            "sender":  {"type": "string", "description": "Optional: sender address or name to disambiguate."},
            "account": {"type": "string", "description": "Optional: account email address to narrow the search."},
            "mailbox": {"type": "string", "description": "Folder to search. Default: 'INBOX'."},
        }, ["subject"]),

    _fn("mail_delete",
        "Move an email to Trash in macOS Mail.app. Safe delete — recoverable from Trash. "
        "Always prefer this over permanent deletion.",
        {
            "subject": {"type": "string", "description": "Email subject (exact or partial match)."},
            "sender":  {"type": "string", "description": "Optional: sender to narrow the match."},
            "account": {"type": "string", "description": "Optional: account email address."},
            "mailbox": {"type": "string", "description": "Source folder. Default: 'INBOX'."},
        }, ["subject"]),

    _fn("mail_move",
        "Move an email to another folder/mailbox in macOS Mail.app.",
        {
            "subject":        {"type": "string", "description": "Email subject (exact or partial match)."},
            "sender":         {"type": "string", "description": "Optional: sender to narrow the match."},
            "account":        {"type": "string", "description": "Optional: account email address."},
            "source_mailbox": {"type": "string", "description": "Source folder. Default: 'INBOX'."},
            "target_mailbox": {"type": "string", "description": "Target folder, e.g. 'Junk', 'Archive'."},
        }, ["subject", "target_mailbox"]),

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


# ── Travel search (SerpAPI) ───────────────────────────────────────────────────
#
# flight_search and hotel_search use SerpAPI's structured-JSON access to
# Google Flights / Google Hotels.  Free tier: 100 searches/month shared.
# Set serpapi.api_key in config.yaml; sign up at https://serpapi.com.

_SERPAPI_BASE = "https://serpapi.com/search"


# ── City/airport resolution via Google Knowledge Graph ───────────────────────
#
# SerpAPI's google_flights engine accepts EITHER a 3-letter airport IATA code
# OR a Google Knowledge Graph MID like '/m/05qtj' (Paris).  A KGMID resolves
# to the CITY, so SerpAPI returns flights to ALL of that city's airports in
# one call — Paris KGMID covers CDG + ORY + BVA together.
#
# Strategy:
#   1. 3-letter IATA codes → pass through (airport-level precision)
#   2. Any other input (city name in any language) → look up KGMID via
#      SerpAPI's google search engine, cache result locally, reuse forever
#   3. Bundle a starter cache of ~25 globally-busy multi-airport cities so
#      most queries hit cache on first use
#
# This avoids enumerating airports and scales to every city on Earth.

import json as _json

_KGMID_CACHE_PATH: Path | None = None
_KGMID_CACHE: dict[str, str] = {}

# Bundled starter cache: verified via SerpAPI google search. KGMIDs are
# Google internal IDs that are extremely stable over time.
_KGMID_STARTER: dict[str, str] = {
    # Major multi-airport metropolitan areas (English + Chinese + IATA city code)
    "paris": "/m/05qtj",          "巴黎": "/m/05qtj",          "par": "/m/05qtj",
    "london": "/m/04jpl",         "伦敦": "/m/04jpl",          "lon": "/m/04jpl",
    "new york": "/m/02_286",      "newyork": "/m/02_286",     "纽约": "/m/02_286",     "nyc": "/m/02_286",
    "tokyo": "/m/07dfk",          "东京": "/m/07dfk",          "tyo": "/m/07dfk",
    "beijing": "/m/01914",        "北京": "/m/01914",          "bjs": "/m/01914",
    "shanghai": "/m/06wjf",       "上海": "/m/06wjf",          "sha": "/m/06wjf",
    "moscow": "/m/04swd",         "莫斯科": "/m/04swd",         "mow": "/m/04swd",
    "stockholm": "/m/06mxs",      "斯德哥尔摩": "/m/06mxs",      "sto": "/m/06mxs",
    "milan": "/m/0947l",          "米兰": "/m/0947l",          "mil": "/m/0947l",
    "seoul": "/m/0hsqf",          "首尔": "/m/0hsqf",          "sel": "/m/0hsqf",
    "osaka": "/m/0dqyw",          "大阪": "/m/0dqyw",          "osa": "/m/0dqyw",
    "hong kong": "/m/03h64",      "hongkong": "/m/03h64",     "香港": "/m/03h64",
    "bangkok": "/m/0fn2g",        "曼谷": "/m/0fn2g",
    "dubai": "/m/01f08r",         "迪拜": "/m/01f08r",
    "istanbul": "/m/09949m",      "伊斯坦布尔": "/m/09949m",
    "buenos aires": "/m/01ly5m",  "布宜诺斯艾利斯": "/m/01ly5m",  "bue": "/m/01ly5m",
    "são paulo": "/m/022pfm",     "sao paulo": "/m/022pfm",   "圣保罗": "/m/022pfm",  "sao": "/m/022pfm",
    "rio de janeiro": "/m/06gmr", "里约热内卢": "/m/06gmr",      "rio": "/m/06gmr",
    "washington": "/m/0rh6k",     "washington dc": "/m/0rh6k","华盛顿": "/m/0rh6k",   "was": "/m/0rh6k",
    "chicago": "/m/01_d4",        "芝加哥": "/m/01_d4",         "chi": "/m/01_d4",
    "houston": "/m/03l2n",        "休斯顿": "/m/03l2n",
    "los angeles": "/m/030qb3t",  "losangeles": "/m/030qb3t", "洛杉矶": "/m/030qb3t",
    "san francisco": "/m/0d6lp",  "sanfrancisco": "/m/0d6lp", "旧金山": "/m/0d6lp",
    "toronto": "/m/0h7h6",        "多伦多": "/m/0h7h6",
    "mexico city": "/m/04sqj",    "墨西哥城": "/m/04sqj",
}


def _kgmid_cache_init(workspace: Path) -> None:
    """Initialise the KGMID cache file location and load existing entries."""
    global _KGMID_CACHE_PATH, _KGMID_CACHE
    cache_dir = workspace.parent / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    _KGMID_CACHE_PATH = cache_dir / "city_kgmid.json"
    if _KGMID_CACHE_PATH.exists():
        try:
            _KGMID_CACHE = _json.loads(_KGMID_CACHE_PATH.read_text())
        except Exception:
            _KGMID_CACHE = {}
    # Merge starter entries (don't overwrite user-cached values)
    for k, v in _KGMID_STARTER.items():
        _KGMID_CACHE.setdefault(k, v)


def _kgmid_cache_save() -> None:
    """Persist the KGMID cache to disk."""
    if _KGMID_CACHE_PATH is None:
        return
    try:
        _KGMID_CACHE_PATH.write_text(_json.dumps(_KGMID_CACHE, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _kgmid_lookup_via_search(city: str) -> str:
    """Use SerpAPI google search to find a city's KGMID dynamically.
    Returns "" on any failure.  Costs 1 SerpAPI call per new city."""
    if not _SERPAPI_KEY:
        return ""
    try:
        resp = _requests.get(_SERPAPI_BASE, params={
            "engine":  "google",
            "q":       city,
            "api_key": _SERPAPI_KEY,
        }, timeout=20)
        data = resp.json()
    except Exception:
        return ""
    kg = data.get("knowledge_graph") or {}
    kgmid = kg.get("kgmid") or ""
    # Only accept location-like KGMIDs (starts with /m/ or /g/)
    if kgmid.startswith("/m/") or kgmid.startswith("/g/"):
        return kgmid
    return ""


def _resolve_airport(value: str) -> str:
    """Resolve a city name or airport code to a SerpAPI-acceptable identifier.

    Returns:
      - The original 3-letter IATA airport code (uppercased) for inputs like
        'DUB', 'CDG' — gives airport-level precision.
      - A Google KGMID like '/m/05qtj' for city names in any language — gives
        coverage of ALL airports serving that city (Paris → CDG+ORY+BVA).

    The KGMID cache is populated from a bundled starter list plus dynamic
    Google Search lookups; entries are persisted to ~/wisp/cache/city_kgmid.json
    so each new city is only resolved once.
    """
    if not value:
        return value
    v = value.strip()

    # Cache hit on either lowercase city name or uppercase city/airport code.
    # This way "PAR" (city code) finds the Paris KGMID, but "DUB" (airport)
    # falls through to the 3-letter IATA passthrough below.
    cache_key_lower = v.lower()
    cache_key_upper = v.upper()
    if cache_key_lower in _KGMID_CACHE:
        return _KGMID_CACHE[cache_key_lower]
    if cache_key_upper in _KGMID_CACHE:
        return _KGMID_CACHE[cache_key_upper]

    # 3-letter all-ASCII-alpha and not in cache → assume it's an airport IATA
    # code (NOT 3-character Chinese names — '都柏林' is len=3 and isalpha() in
    # Python, but is not an IATA code).
    if len(v) == 3 and v.isascii() and v.isalpha():
        return cache_key_upper

    # Otherwise: dynamic KGMID lookup via Google Search (one-time per city).
    kgmid = _kgmid_lookup_via_search(v)
    if kgmid:
        _KGMID_CACHE[cache_key_lower] = kgmid
        _kgmid_cache_save()
        return kgmid

    # Last resort: return as-is so SerpAPI reports a meaningful error.
    return v


def _serpapi_call(params: dict, timeout: int = 30) -> dict | str:
    """Call SerpAPI with the given params. Returns parsed JSON dict or
    an error string on failure."""
    if not _SERPAPI_KEY:
        return ("error: SerpAPI key not configured. Set serpapi.api_key in "
                "config.yaml. Sign up at https://serpapi.com/users/sign_up")
    try:
        resp = _requests.get(
            _SERPAPI_BASE,
            params={**params, "api_key": _SERPAPI_KEY},
            timeout=timeout,
        )
    except _requests.exceptions.Timeout:
        return f"error: SerpAPI request timed out after {timeout}s"
    except Exception as e:
        return f"error: SerpAPI request failed — {e}"

    try:
        data = resp.json()
    except Exception:
        return f"error: SerpAPI returned non-JSON (status {resp.status_code}): {resp.text[:200]}"

    if resp.status_code != 200:
        msg = data.get("error") or resp.text[:200]
        return f"error: SerpAPI {resp.status_code} — {msg}"
    if "error" in data:
        return f"error: SerpAPI — {data['error']}"
    return data


def _fmt_duration(minutes: int) -> str:
    """120 → '2h 0m'."""
    if not isinstance(minutes, (int, float)):
        return ""
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m}m" if h else f"{m}m"


def _fmt_flight_leg(legs: list[dict]) -> str:
    """Format a list of flight segments (one itinerary leg) into one line.
    Returns 'HH:MM XXX → HH:MM YYY · Airline FN123 · 2h 30m'."""
    if not legs:
        return ""
    first, last = legs[0], legs[-1]
    dep   = first.get("departure_airport", {}) or {}
    arr   = last.get("arrival_airport", {}) or {}
    dep_t = (dep.get("time", "") or "").split(" ")[-1][:5]
    arr_t = (arr.get("time", "") or "").split(" ")[-1][:5]
    dep_id, arr_id = dep.get("id", "?"), arr.get("id", "?")
    airlines = " / ".join(dict.fromkeys(s.get("airline", "") for s in legs if s.get("airline")))
    stops = len(legs) - 1
    stops_str = "直飞" if stops == 0 else f"经停 {stops} 次"
    return f"{dep_t} {dep_id} → {arr_t} {arr_id} · {airlines} · {stops_str}"


def tool_flight_search(origin: str, destination: str, date: str,
                       return_date: str = "", passengers: int = 1,
                       cabin: str = "economy") -> str:
    """Search flights via SerpAPI Google Flights.

    Returns a clean, formatted list of best flights with airline, times,
    duration, stops, and price — extracted from structured JSON.

    origin / destination: Accepts any of:
                          - IATA airport code   ('DUB', 'CDG')
                          - IATA city code      ('PAR', 'NYC', 'LON')
                          - English city name   ('Paris', 'New York')
                          - Chinese city name   ('巴黎', '纽约')
                          The tool resolves city names to the primary airport
                          internally, so callers do not need to know IATA codes.
    date / return_date  : YYYY-MM-DD. Omit return_date for one-way.
    cabin               : economy | premium_economy | business | first
    """
    cabin_map = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}
    travel_class = cabin_map.get(cabin.lower(), 1)
    trip_type = 1 if return_date else 2   # SerpAPI: 1=round-trip, 2=one-way

    # Normalise origin/destination to a 3-letter airport IATA code.  Without
    # this, SerpAPI rejects city names with HTTP 400 and returns empty results
    # for multi-airport city codes (PAR / NYC / LON / etc).
    origin_iata = _resolve_airport(origin)
    dest_iata   = _resolve_airport(destination)

    params = {
        "engine":         "google_flights",
        "departure_id":   origin_iata,
        "arrival_id":     dest_iata,
        "outbound_date":  date,
        "type":           trip_type,
        "adults":         passengers,
        "travel_class":   travel_class,
        "currency":       "GBP",
        "hl":             "zh-cn",
        "gl":             "uk",
    }
    if return_date:
        params["return_date"] = return_date

    data = _serpapi_call(params, timeout=45)
    if isinstance(data, str):           # error string
        return data

    best  = data.get("best_flights", []) or []
    other = data.get("other_flights", []) or []
    all_flights = best + other

    if not all_flights:
        return (f"no flights found for {origin} → {destination} on {date}"
                + (f" returning {return_date}" if return_date else ""))

    # Extract the actual airports covered by the search from the result data.
    # When origin/destination was a KGMID, this reveals e.g. CDG/ORY/BVA for Paris.
    dep_airports: set[str] = set()
    arr_airports: set[str] = set()
    for opt in all_flights:
        segs = opt.get("flights") or []
        if not segs:
            continue
        d = segs[0].get("departure_airport", {}).get("id")
        a = segs[-1].get("arrival_airport", {}).get("id")
        if d: dep_airports.add(d)
        if a: arr_airports.add(a)

    def _airport_summary(orig_input: str, resolved: str, airports: set[str]) -> str:
        """'Dublin (DUB)' or 'Paris (CDG/ORY/BVA)'."""
        codes = "/".join(sorted(airports)) if airports else resolved
        # Only show the input prefix when it differs from the code
        if orig_input.upper() == codes:
            return codes
        return f"{orig_input} ({codes})"

    lines = [
        f"Flight search: "
        f"{_airport_summary(origin, origin_iata, dep_airports)} → "
        f"{_airport_summary(destination, dest_iata, arr_airports)}  "
        f"{date}"
        + (f"  return {return_date}" if return_date else "  one-way"),
        f"Passengers: {passengers}  Cabin: {cabin}  ({len(all_flights)} options)",
        "─" * 60,
    ]

    # Show up to 8 best + 7 other
    show = best[:8] + other[:7]
    for i, opt in enumerate(show, 1):
        flights = opt.get("flights", []) or []
        price   = opt.get("price")
        total_d = opt.get("total_duration")
        leg     = _fmt_flight_leg(flights)
        tag     = "★" if i <= len(best[:8]) else " "
        price_s = f"£{price}" if price else "—"
        dur_s   = _fmt_duration(total_d) if total_d else ""
        lines.append(f"{tag} {price_s:>7}  {dur_s:>7}  {leg}")

        # Return leg for round-trips
        if return_date and len(opt.get("flights", [])) == 0:
            continue
        # Some SerpAPI responses split outbound + return into one "flights" array
        # — we'll show segments together. If there are >2 segments and a stop
        # between dep and return, we don't try to split them further.

    return "\n".join(lines)


def tool_hotel_search(city: str, checkin: str, checkout: str,
                      guests: int = 1, rooms: int = 1) -> str:
    """Search hotels via SerpAPI Google Hotels.

    Returns a clean, formatted list of properties with name, rating,
    review count, hotel class, amenities, and price per night.

    city            : destination city or area (e.g. 'Barcelona', '上海浦东').
    checkin/checkout: YYYY-MM-DD.
    """
    params = {
        "engine":          "google_hotels",
        "q":               city,
        "check_in_date":   checkin,
        "check_out_date":  checkout,
        "adults":          guests,
        "currency":        "GBP",
        "hl":              "zh-cn",
        "gl":              "uk",
    }
    data = _serpapi_call(params, timeout=45)
    if isinstance(data, str):
        return data

    props = data.get("properties", []) or []
    if not props:
        return (f"no hotels found for '{city}' "
                f"{checkin} → {checkout}")

    nights = 1
    try:
        from datetime import datetime
        d1 = datetime.strptime(checkin, "%Y-%m-%d")
        d2 = datetime.strptime(checkout, "%Y-%m-%d")
        nights = max(1, (d2 - d1).days)
    except Exception:
        pass

    lines = [
        f"Hotel search: {city}  {checkin} → {checkout}  "
        f"({nights} night{'s' if nights > 1 else ''}, "
        f"{guests} guest{'s' if guests > 1 else ''}, "
        f"{rooms} room{'s' if rooms > 1 else ''})",
        f"{len(props)} properties found",
        "─" * 60,
    ]

    for p in props[:15]:
        name   = p.get("name", "—")
        rating = p.get("overall_rating")
        revs   = p.get("reviews")
        klass  = p.get("hotel_class", "")
        rate   = p.get("rate_per_night", {}) or {}
        price  = rate.get("lowest") or rate.get("extracted_lowest")
        total  = (p.get("total_rate") or {}).get("lowest")
        ams    = p.get("amenities", []) or []

        rating_s = f"★{rating:.1f}" if isinstance(rating, (int, float)) else "—"
        revs_s   = f"({revs:,})" if isinstance(revs, int) else (f"({revs})" if revs else "")
        klass_s  = f" · {klass}" if klass else ""
        price_s  = f"{price}/晚" if price else "—"
        total_s  = f" total {total}" if total and total != price else ""
        ams_s    = " · ".join(ams[:3]) if ams else ""

        lines.append(f"{name}")
        lines.append(f"  {rating_s} {revs_s}{klass_s}  {price_s}{total_s}")
        if ams_s:
            lines.append(f"  {ams_s}")

    return "\n".join(lines)



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
                        due_before: str = "", due_after: str = "") -> str:
    """List incomplete reminders.

    list_name : "" or "*" → all lists; specific name → one list.
    due_before: "YYYY-MM-DD" — only return reminders due on or before this date.
    due_after : "YYYY-MM-DD" — only return reminders due on or after this date.
    Combine both for a date range (e.g. today through tomorrow in one call).
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

    # Build cutoff date blocks injected into AppleScript
    cutoff_block = ""
    use_before = False
    use_after  = False

    if due_before:
        dt = _parse_dt(due_before)
        if dt is None:
            return f'error: invalid due_before date "{due_before}" — use YYYY-MM-DD'
        cutoff_block += (
            f"set cutoff_before to current date\n"
            f"set year of cutoff_before to {dt.year}\n"
            f"set month of cutoff_before to {dt.month}\n"
            f"set day of cutoff_before to {dt.day}\n"
            f"set time of cutoff_before to 86399\n"
        )
        use_before = True

    if due_after:
        dt2 = _parse_dt(due_after)
        if dt2 is None:
            return f'error: invalid due_after date "{due_after}" — use YYYY-MM-DD'
        cutoff_block += (
            f"set cutoff_after to current date\n"
            f"set year of cutoff_after to {dt2.year}\n"
            f"set month of cutoff_after to {dt2.month}\n"
            f"set day of cutoff_after to {dt2.day}\n"
            f"set time of cutoff_after to 0\n"
        )
        use_after = True

    if use_before and use_after:
        date_filter = (
            "if dd is missing value then\n"
            "    -- no due date: skip when date filter is active\n"
            "else if dd < cutoff_after then\n"
            "    -- before range start: skip\n"
            "else if dd > cutoff_before then\n"
            "    -- after range end: skip\n"
            "else\n"
            "    set showItem to true\n"
            "end if\n"
        )
    elif use_before:
        date_filter = (
            "if dd is missing value then\n"
            "    -- no due date: skip when date filter is active\n"
            "else if dd > cutoff_before then\n"
            "    -- due after cutoff: skip\n"
            "else\n"
            "    set showItem to true\n"
            "end if\n"
        )
    elif use_after:
        date_filter = (
            "if dd is missing value then\n"
            "    -- no due date: skip when date filter is active\n"
            "else if dd < cutoff_after then\n"
            "    -- before range start: skip\n"
            "else\n"
            "    set showItem to true\n"
            "end if\n"
        )
    else:
        date_filter = "set showItem to true\n"

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
        -- batch-fetch both properties via inline specifiers (one round-trip each)
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
                    set itemLine to "[" & listLabel & "] " & item i of rNames
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
                            set itemLine to itemLine & "  due: " & y & "-" & mo & "-" & dy & " " & h & ":" & mi
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


def tool_reminders_complete(title: str, list_name: str) -> str:
    """Mark the first matching incomplete reminder as completed."""
    t = title.replace('"', '\\"')
    l = list_name.replace('"', '\\"')
    script = f"""
tell application "Reminders"
    if not (exists list "{l}") then
        return "error: list \\"{l}\\" not found"
    end if
    set matches to (reminders of list "{l}" whose name contains "{t}" and completed is false)
    if (count of matches) = 0 then
        return "error: no incomplete reminder matching \\"{t}\\" in \\"{l}\\""
    end if
    set completed of item 1 of matches to true
    return "ok"
end tell
"""
    result = _osascript_clean(script, timeout=30)
    if result == "ok":
        return f"ok: marked '{title}' as completed in '{list_name}'"
    return result


def tool_reminders_delete(title: str, list_name: str) -> str:
    """Permanently delete the first matching reminder."""
    t = title.replace('"', '\\"')
    l = list_name.replace('"', '\\"')
    script = f"""
tell application "Reminders"
    if not (exists list "{l}") then
        return "error: list \\"{l}\\" not found"
    end if
    set matches to (reminders of list "{l}" whose name contains "{t}")
    if (count of matches) = 0 then
        return "error: no reminder matching \\"{t}\\" in \\"{l}\\""
    end if
    delete item 1 of matches
    return "ok"
end tell
"""
    result = _osascript_clean(script, timeout=30)
    if result == "ok":
        return f"ok: deleted '{title}' from '{list_name}'"
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


def tool_calendar_delete(title: str, start: str = "", calendar_name: str = "") -> str:
    """Delete the first calendar event matching title (+ optional start date)."""
    t = title.replace('"', '\\"')

    # Optional start-date filter: compute a narrow time window (±1 min)
    date_filter = ""
    if start:
        dt = _parse_dt(start)
        if dt is None:
            return f"error: cannot parse start '{start}' — use YYYY-MM-DD or YYYY-MM-DD HH:MM"
        start_lines = _as_date_script("filterDate", dt)
        date_filter = (
            f"{start_lines}"
            f"set filterEnd to filterDate + 60\n"
            f"set calMatches to (every event of cal whose summary contains \"{t}\" "
            f"and start date >= filterDate and start date <= filterEnd)\n"
        )
    else:
        date_filter = f'set calMatches to (every event of cal whose summary contains "{t}")\n'

    if calendar_name:
        cal_scope = f'{{calendar "{calendar_name.replace(chr(34), chr(92)+chr(34))}"}}'
        guard = (
            f'if not (exists calendar "{calendar_name.replace(chr(34), chr(92)+chr(34))}") then\n'
            f'    return "error: calendar \\"{calendar_name}\\" not found"\n'
            f'end if\n'
        )
    else:
        cal_scope = "every calendar"
        guard = ""

    script = f"""
tell application "Calendar"
    {guard}
    set found to false
    repeat with cal in ({cal_scope})
        try
            {date_filter}
            if (count of calMatches) > 0 then
                delete item 1 of calMatches
                set found to true
                exit repeat
            end if
        end try
    end repeat
    if found then
        return "ok"
    else
        return "error: no event found matching \\"{t}\\""
    end if
end tell
"""
    result = _osascript_clean(script, timeout=15)
    if result == "ok":
        hint = f" on {start}" if start else ""
        cal_hint = f" in '{calendar_name}'" if calendar_name else ""
        return f"ok: deleted event '{title}'{hint}{cal_hint}"
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


def tool_notes_delete(title: str, folder: str = "") -> str:
    """Delete the first note matching title."""
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
    delete item 1 of matches
    return "ok"
end tell
"""
    result = _osascript_clean(script, timeout=20)
    if result == "ok":
        folder_hint = f" from '{folder}'" if folder and folder != "*" else ""
        return f"ok: deleted note '{title}'{folder_hint}"
    return result


# ── Mail (macOS Mail.app) ─────────────────────────────────────────────────────
#
# All tools operate on Mail.app via AppleScript.
# Mail.app must be running; accounts are whatever is configured in the app.
# mail_delete moves to Trash (recoverable); no permanent-delete tool is provided.

def _mail_acc_script(account: str) -> str:
    """AppleScript lines to set `theAccounts` to all accounts or a specific one."""
    if account:
        a = account.replace('"', '\\"')
        return (
            # Match against both email addresses list and user name (login field)
            f'set theAccounts to (every account whose email addresses contains "{a}" or user name is "{a}")\n'
            f'if (count of theAccounts) = 0 then\n'
            f'    return "error: no Mail account found for \\"{a}\\""\n'
            f'end if\n'
        )
    return "set theAccounts to every account\n"


def _mail_where(subject: str, sender: str) -> str:
    """AppleScript whose-clause filtering by subject and optional sender."""
    s = subject.replace('"', '\\"')
    if sender:
        snd = sender.replace('"', '\\"')
        return f'whose subject contains "{s}" and sender contains "{snd}"'
    return f'whose subject contains "{s}"'


# AppleScript snippet: formats `msgDate` → `dateStr` (YYYY-MM-DD HH:MM)
_MAIL_FMT_DATE = (
    'set _y to year of msgDate as string\n'
    'set _mo to (month of msgDate as integer)\n'
    'if _mo < 10 then set _mo to "0" & _mo\n'
    'set _dy to day of msgDate\n'
    'if _dy < 10 then set _dy to "0" & _dy\n'
    'set _t to time of msgDate\n'
    'set _h to _t div 3600\n'
    'set _mi to (_t mod 3600) div 60\n'
    'if _h < 10 then set _h to "0" & _h\n'
    'if _mi < 10 then set _mi to "0" & _mi\n'
    'set dateStr to _y & "-" & _mo & "-" & _dy & " " & _h & ":" & _mi\n'
)


def tool_mail_accounts() -> str:
    """List all email accounts configured in macOS Mail.app."""
    script = """
tell application "Mail"
    set output to ""
    set i to 0
    repeat with acc in every account
        set i to i + 1
        set accName to full name of acc
        -- Try email addresses list first, then user name (the login/email field)
        set accEmail to ""
        try
            set addrs to email addresses of acc
            if (count of addrs) > 0 then set accEmail to item 1 of addrs
        end try
        if accEmail is "" then
            try
                set accEmail to user name of acc
            on error
                set accEmail to "(unknown)"
            end try
        end if
        set output to output & i & ". " & accName & " <" & accEmail & ">\\n"
    end repeat
    if output is "" then return "no accounts configured in Mail.app"
    return output
end tell
"""
    return _osascript_clean(script, timeout=15)


def tool_mail_list(account: str = "", mailbox: str = "INBOX",
                   limit: int = 20, unread_only: bool = False) -> str:
    """List emails from macOS Mail.app across all (or a specific) account."""
    acc_script = _mail_acc_script(account)
    mbox_name = mailbox.replace('"', '\\"')
    # Use the inbox property for INBOX (reliable across all IMAP providers),
    # fall back to mailbox name for other folders (Sent, Junk, Archive, etc.)
    if mailbox.upper() == "INBOX":
        mbox_stmt = "set theMbox to inbox of acc"
    else:
        mbox_stmt = f'set theMbox to mailbox "{mbox_name}" of acc'
    msgs_stmt = (
        "set msgs to (messages of theMbox whose read status is false)"
        if unread_only else
        "set msgs to messages of theMbox"
    )
    unread_tag = " unread" if unread_only else ""

    script = f"""
tell application "Mail"
    {acc_script}
    set output to ""
    set counter to 0
    repeat with acc in theAccounts
        try
            -- user name holds the login email for IMAP/Exchange accounts
            set accEmail to user name of acc
            {mbox_stmt}
            {msgs_stmt}
            set msgCount to count of msgs
            if msgCount > 0 then
                set output to output & "[" & accEmail & "] " & msgCount & "{unread_tag} message(s)" & "\\n"
                repeat with i from 1 to msgCount
                    if counter >= {limit} then exit repeat
                    set msg to item i of msgs
                    set msgSubject to subject of msg
                    set msgSender to sender of msg
                    set msgRead to read status of msg
                    set msgDate to date received of msg
                    {_MAIL_FMT_DATE}
                    if msgRead then
                        set mark to "  "
                    else
                        set mark to "● "
                    end if
                    set output to output & mark & msgSender & " | " & msgSubject & " | " & dateStr & "\\n"
                    set counter to counter + 1
                end repeat
                set output to output & "\\n"
            end if
        on error errMsg
            set output to output & "[error on account] " & errMsg & "\\n"
        end try
    end repeat
    if output is "" then return "no messages found"
    return output
end tell
"""
    return _osascript_clean(script, timeout=60)


def tool_mail_read(subject: str, sender: str = "", account: str = "",
                   mailbox: str = "INBOX") -> str:
    """Read the full content of an email from macOS Mail.app."""
    acc_script = _mail_acc_script(account)
    where = _mail_where(subject, sender)
    s_escaped = subject.replace('"', '\\"')
    if mailbox.upper() == "INBOX":
        mbox_stmt = "set theMbox to inbox of acc"
    else:
        mbox_stmt = f'set theMbox to mailbox "{mailbox.replace(chr(34), chr(92)+chr(34))}" of acc'

    script = f"""
tell application "Mail"
    {acc_script}
    repeat with acc in theAccounts
        try
            {mbox_stmt}
            set msgs to (messages of theMbox {where})
            if (count of msgs) > 0 then
                set msg to item 1 of msgs
                set msgFrom to sender of msg
                set msgSubject to subject of msg
                set msgDate to date received of msg
                set msgContent to content of msg
                {_MAIL_FMT_DATE}
                if (length of msgContent) > 3000 then
                    set msgContent to (text 1 thru 3000 of msgContent) & "\\n...[truncated]"
                end if
                return "From: " & msgFrom & "\\nDate: " & dateStr & "\\nSubject: " & msgSubject & "\\n---\\n" & msgContent
            end if
        end try
    end repeat
    return "error: no message found matching \\"{s_escaped}\\""
end tell
"""
    return _osascript_clean(script, timeout=30)


def tool_mail_delete(subject: str, sender: str = "", account: str = "",
                     mailbox: str = "INBOX") -> str:
    """Move an email to Trash in macOS Mail.app."""
    acc_script = _mail_acc_script(account)
    where = _mail_where(subject, sender)
    s_escaped = subject.replace('"', '\\"')
    if mailbox.upper() == "INBOX":
        mbox_stmt = "set theMbox to inbox of acc"
    else:
        mbox_stmt = f'set theMbox to mailbox "{mailbox.replace(chr(34), chr(92)+chr(34))}" of acc'

    script = f"""
tell application "Mail"
    {acc_script}
    repeat with acc in theAccounts
        try
            {mbox_stmt}
            set msgs to (messages of theMbox {where})
            if (count of msgs) > 0 then
                delete item 1 of msgs
                return "ok"
            end if
        end try
    end repeat
    return "error: no message found matching \\"{s_escaped}\\""
end tell
"""
    result = _osascript_clean(script, timeout=30)
    if result == "ok":
        return f"ok: moved '{subject}' to Trash"
    return result


def tool_mail_move(subject: str, sender: str = "", account: str = "",
                   source_mailbox: str = "INBOX", target_mailbox: str = "") -> str:
    """Move an email to another folder in macOS Mail.app."""
    acc_script = _mail_acc_script(account)
    tgt = target_mailbox.replace('"', '\\"')
    where = _mail_where(subject, sender)
    s_escaped = subject.replace('"', '\\"')
    if source_mailbox.upper() == "INBOX":
        src_stmt = "set srcMbox to inbox of acc"
    else:
        src_stmt = f'set srcMbox to mailbox "{source_mailbox.replace(chr(34), chr(92)+chr(34))}" of acc'

    script = f"""
tell application "Mail"
    {acc_script}
    repeat with acc in theAccounts
        try
            {src_stmt}
            set msgs to (messages of srcMbox {where})
            if (count of msgs) > 0 then
                set tgtMbox to mailbox "{tgt}" of acc
                move item 1 of msgs to tgtMbox
                return "ok"
            end if
        end try
    end repeat
    return "error: no message found matching \\"{s_escaped}\\" or target mailbox \\"{tgt}\\" not found"
end tell
"""
    result = _osascript_clean(script, timeout=30)
    if result == "ok":
        return f"ok: moved '{subject}' to '{target_mailbox}'"
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
        case "reminders_list":     return tool_reminders_list(args.get("list_name", ""), args.get("limit", 20), args.get("due_before", ""), args.get("due_after", ""))
        case "reminders_add":      return tool_reminders_add(args["title"], args.get("due", ""), args.get("notes", ""), args.get("list_name", "Reminders"))
        case "reminders_complete": return tool_reminders_complete(args["title"], args["list_name"])
        case "reminders_delete":   return tool_reminders_delete(args["title"], args["list_name"])
        # Calendar
        case "calendar_list":    return tool_calendar_list(args.get("days", 7), args.get("calendar", ""))
        case "calendar_add":     return tool_calendar_add(args["title"], args["start"], args.get("end", ""), args.get("calendar", ""), args.get("notes", ""))
        case "calendar_delete":  return tool_calendar_delete(args["title"], args.get("start", ""), args.get("calendar", ""))
        # Notes
        case "notes_list":       return tool_notes_list(args.get("folder", ""), args.get("limit", 20))
        case "notes_read":       return tool_notes_read(args["title"], args.get("folder", ""))
        case "notes_create":     return tool_notes_create(args["title"], args["content"], args.get("folder", ""))
        case "notes_append":     return tool_notes_append(args["title"], args["content"], args.get("folder", ""))
        case "notes_delete":     return tool_notes_delete(args["title"], args.get("folder", ""))
        # Mail
        case "mail_accounts": return tool_mail_accounts()
        case "mail_list":    return tool_mail_list(args.get("account", ""), args.get("mailbox", "INBOX"), args.get("limit", 20), args.get("unread_only", False))
        case "mail_read":    return tool_mail_read(args["subject"], args.get("sender", ""), args.get("account", ""), args.get("mailbox", "INBOX"))
        case "mail_delete":  return tool_mail_delete(args["subject"], args.get("sender", ""), args.get("account", ""), args.get("mailbox", "INBOX"))
        case "mail_move":    return tool_mail_move(args["subject"], args.get("sender", ""), args.get("account", ""), args.get("source_mailbox", "INBOX"), args["target_mailbox"])
        case _:              return f"error: unknown tool '{name}'"

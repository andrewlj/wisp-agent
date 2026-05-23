#!/usr/bin/env python3
"""
wisp-agent
A lightweight local AI agent for macOS automation.
Dependencies: requests, pyyaml, prompt_toolkit, playwright
"""

import json
import sys
from pathlib import Path

import requests
import yaml
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory

from tools import SCHEMAS, dispatch
import session as _session
import task as _task

# ── Config ────────────────────────────────────────────────────────────────────

_CONFIG_PATH = Path(__file__).parent / "config.yaml"

def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        print(f"error: config file not found: {_CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)

_cfg = _load_config()

VERSION    = "0.4"
BASE_URL   = _cfg["server"]["base_url"]
API_KEY    = _cfg["server"]["api_key"]
MODEL      = _cfg["model"]["name"]
MAX_TOKENS = _cfg["model"]["max_tokens"]
MAX_TURNS  = _cfg["agent"]["max_turns"]
BASH_TO    = _cfg["agent"]["bash_timeout"]
VISION          = _cfg.get("model", {}).get("vision", False)
WORKSPACE       = _cfg["agent"].get("workspace", "~/wisp-workspace")
STRICT          = _cfg["agent"].get("strict_workspace", True)
CTX_LIMIT       = _cfg["agent"].get("context_limit", 6000)
CTX_KEEP_RECENT = _cfg["agent"].get("context_keep_recent", 6)
BROWSER_TO      = _cfg["agent"].get("browser_timeout", 30)

# Initialise workspace sandbox and browser timeout
from tools import init_workspace, init_browser_timeout
init_workspace(WORKSPACE, STRICT)
init_browser_timeout(BROWSER_TO)

_workspace_abs = str(Path(WORKSPACE).expanduser().resolve())
SYSTEM_PROMPT = f"""You are a helpful assistant running on macOS (Apple Silicon). \
You have tools to complete tasks on the user's Mac.

## Shell environment
- Shell: zsh. Python: python3.11. Package manager: Homebrew (brew).
- Always use macOS/BSD command syntax — NOT Linux/GNU syntax. Key differences:
  - Memory : `vm_stat`, `sysctl hw.memsize` — NOT `free`
  - Disk   : `df -h`, `diskutil` — NOT `lsblk`
  - Process: `ps aux` or `ps -eo pid,rss,comm` (BSD flags, no `--` prefix)
  - File   : `stat -f "%z %N"` — NOT `stat --format`
  - Date   : `date -r <epoch>` — NOT `date -d`
  - sed    : `sed -i ''` — NOT `sed -i`

## Workspace & security rules
- ALL file outputs (write_file, screenshot, downloads) MUST go inside the workspace: {_workspace_abs}
- NEVER write, move, copy, or delete files outside the workspace.
- NEVER run destructive bash commands (rm -rf, sudo rm, dd, mkfs, chmod 777).
- If a security error is returned, do NOT attempt to bypass it — report to the user instead.

## Tool usage rules
- Prefer a single well-formed command over multiple probing attempts.
- If a command fails, read the error and fix it before retrying.
- Use `read_file` / `write_file` for file content; `bash` for everything else.
- Use `osascript` for notifications, app control, dialogs — not bash.
- Use `clipboard_read` / `clipboard_write` for clipboard.
- Use `screenshot` to capture the screen or a specific app window.
- Use `list_apps` / `focus_app` / `quit_app` to manage running applications.
- Use `find_files` to search by name, extension, or keyword (Spotlight-powered).
- Use `move_file` / `copy_file` / `delete_file` for file operations.
- Use `http_get` / `http_post` for HTTP requests.
- Use `browser_*` tools for interactive web automation (form fill, click, scrape).
- Call `done` as soon as the task is fully complete.

## Task tracking rules
- For any task with 3 or more steps, ALWAYS call `task_init` first to declare the plan.
- Call `task_step_done` immediately after each step completes successfully.
- Call `task_step_fail` if a step cannot be completed, with a clear reason.
- Never skip task tracking for multi-step tasks — it enables recovery if interrupted.
"""

# ── Colors ────────────────────────────────────────────────────────────────────

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    CYAN   = "\033[36m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    RED    = "\033[31m"
    GRAY   = "\033[90m"

def c(color: str, text: str) -> str:
    return f"{color}{text}{C.RESET}"

def _rgb(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m"

# ── Welcome banner ────────────────────────────────────────────────────────────

_W = ["██╗    ██╗", "██║    ██║", "██║ █╗ ██║", "██║███╗██║", "╚███╔███╔╝", " ╚══╝╚══╝ "]
_I = ["██╗",        "██║",        "██║",        "██║",        "██║",        "╚═╝"       ]
_S = ["███████╗",   "██╔════╝",   "███████╗",   "╚════██║",   "███████║",   "╚══════╝"  ]
_P = ["██████╗ ",   "██╔══██╗",   "██████╔╝",   "██╔═══╝ ",   "██║     ",   "╚═╝     "  ]
_DA = ["      ",    "      ",     "──────",     "──────",     "      ",     "      "    ]
_A = [" █████╗ ",   "██╔══██╗",   "███████║",   "██╔══██║",   "██║  ██║",   "╚═╝  ╚═╝"  ]
_G = [" ██████╗ ",  "██╔════╝ ",  "██║  ███╗",  "██║   ██║",  "╚██████╔╝",  " ╚═════╝ " ]
_E = ["███████╗",   "██╔════╝",   "█████╗  ",   "██╔══╝  ",   "███████╗",   "╚══════╝"  ]
_N = ["███╗  ██╗",  "████╗ ██║",  "██╔██╗██║",  "██║╚████║",  "██║ ╚███║",  "╚═╝  ╚══╝" ]
_T = ["████████╗",  "╚══██╔══╝",  "   ██║   ",  "   ██║   ",  "   ██║   ",  "   ╚═╝   " ]

_LOGO_COLORS = [
    (  0, 255, 255), (  0, 238, 248), (  0, 218, 235),
    (  0, 195, 218), (  0, 168, 198), (  0, 140, 175),
]
_DASH_C = _rgb(0, 150, 175)

# Panel width derived from logo — keeps box aligned with WISP-AGENT letters.
# Logo visible width = indent(2) + letters + gaps, minus the "  │ " + " │" = 4
# So inner panel width = logo_chars_width - 4
_LOGO_LINE_W = (
    len(_W[0]) + 2 + len(_I[0]) + 2 + len(_S[0]) + 2 + len(_P[0])   # WISP + gaps
    + 2 + len(_DA[0]) + 2                                              # dash section
    + len(_A[0]) + 2 + len(_G[0]) + 2 + len(_E[0]) + 2               # AGE + gaps
    + len(_N[0]) + 2 + len(_T[0])                                      # NT
)
_PANEL_W = _LOGO_LINE_W - 4   # subtract "│ " prefix + " │" suffix

_TOOLS_MAP = [
    ("core",    ["bash", "read_file", "write_file", "done"]),
    ("mac",     ["screenshot", "osascript", "clipboard_*", "open",
                 "list_apps", "focus_app", "quit_app"]),
    ("files",   ["find_files", "move_file", "copy_file", "delete_file"]),
    ("network", ["http_get", "http_post"]),
    ("browser", ["browser_open", "browser_click", "browser_type",
                 "browser_get_text", "browser_screenshot", "browser_close"]),
    ("task",    ["task_init", "task_step_done", "task_step_fail"]),
]

def _rpad(s: str, width: int) -> str:
    import re
    visible = len(re.sub(r'\033\[[^m]*m', '', s))
    return s + " " * max(0, width - visible)

def print_banner() -> None:
    import session as _sess
    # ── Logo: WISP-AGENT ──────────────────────────────────────────────────────
    print()
    sp = "  "
    for row in range(6):
        r, g, b = _LOGO_COLORS[row]
        lc = f"\033[1m{_rgb(r, g, b)}"
        dc = _DASH_C
        print("  "
              + lc + _W[row]  + C.RESET + sp
              + lc + _I[row]  + C.RESET + sp
              + lc + _S[row]  + C.RESET + sp
              + lc + _P[row]  + C.RESET
              + "  " + dc + _DA[row] + C.RESET + "  "
              + lc + _A[row]  + C.RESET + sp
              + lc + _G[row]  + C.RESET + sp
              + lc + _E[row]  + C.RESET + sp
              + lc + _N[row]  + C.RESET + sp
              + lc + _T[row]  + C.RESET)

    # ── Subtitle ──────────────────────────────────────────────────────────────
    print()
    dot_sep = f"{_rgb(55, 55, 55)}  ·  {C.RESET}"
    print(f"  {_rgb(0,210,210)}幽灵轻使{C.RESET}"
          f"{dot_sep}{C.DIM}{_rgb(150,150,150)}A ghost of light, at your command{C.RESET}")

    # ── Info panel ────────────────────────────────────────────────────────────
    print()
    bc       = _rgb(0, 75, 90)
    teal     = _rgb(0, 215, 215)
    cat_c    = _rgb(0, 165, 175)
    val_c    = _rgb(185, 185, 185)
    dim_gray = C.DIM + _rgb(115, 115, 115)
    meta_c   = _rgb(0, 165, 178)
    W        = _PANEL_W

    n_tools = len(SCHEMAS)
    from datetime import datetime
    today = datetime.now().strftime("%Y.%-m.%-d")

    def row(content: str) -> None:
        print(f"  {bc}│{C.RESET} {_rpad(content, W)} {bc}│{C.RESET}")

    print(f"  {bc}┌{'─'*(W+2)}┐{C.RESET}")

    # version + date
    row(f"{C.BOLD}{teal}wisp v{VERSION}{C.RESET}  {dim_gray}({today}){C.RESET}")
    row("")

    # tools by category
    row(f"{C.BOLD}{teal}▶ Tools  {dim_gray}({n_tools} total){C.RESET}")
    for cat, tools in _TOOLS_MAP:
        joined = ", ".join(tools)
        # visible prefix = "  " + cat + ":" + "  " = len(cat) + 5
        max_w  = W - len(cat) - 5
        if len(joined) > max_w:
            joined = joined[:max_w - 3] + "..."
        row(f"  {cat_c}{cat}:{C.RESET}  {val_c}{joined}{C.RESET}")

    row("")
    row(f"{dim_gray}/help for commands{C.RESET}")

    # meta: model · workspace · session
    n_sess  = _sess.count()
    sess_hint = f"  ·  {n_sess} saved session{'s' if n_sess != 1 else ''}" if n_sess else ""
    print(f"  {bc}├{'─'*(W+2)}┤{C.RESET}")
    meta = (f"{meta_c}{MODEL}{C.RESET}  "
            f"{dim_gray}·  {WORKSPACE}{sess_hint}{C.RESET}")
    row(meta)
    print(f"  {bc}└{'─'*(W+2)}┘{C.RESET}")
    print()

# ── Streaming API ─────────────────────────────────────────────────────────────

def call_api_stream(messages: list) -> tuple[str, list[dict]]:
    resp = requests.post(
        BASE_URL,
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model":       MODEL,
            "messages":    messages,
            "tools":       SCHEMAS,
            "tool_choice": "auto",
            "max_tokens":  MAX_TOKENS,
            "stream":      True,
        },
        stream=True,
        timeout=120,
    )
    resp.raise_for_status()

    full_content = ""
    acc: dict[int, dict] = {}

    for raw in resp.iter_lines():
        if not raw or not raw.startswith(b"data: "):
            continue
        data = raw[6:]
        if data == b"[DONE]":
            break
        chunk  = json.loads(data)
        choice = chunk["choices"][0]
        delta  = choice.get("delta", {})

        if delta.get("content"):
            print(delta["content"], end="", flush=True)
            full_content += delta["content"]

        for tc in delta.get("tool_calls", []):
            idx = tc["index"]
            if idx not in acc:
                acc[idx] = {"id": "", "name": "", "arguments": ""}
            if tc.get("id"):
                acc[idx]["id"] = tc["id"]
            fn = tc.get("function", {})
            if fn.get("name"):      acc[idx]["name"]      += fn["name"]
            if fn.get("arguments"): acc[idx]["arguments"] += fn["arguments"]

    if full_content:
        print()

    return full_content, [acc[i] for i in sorted(acc)]

# ── Output helpers ────────────────────────────────────────────────────────────

_TURN_WIDTH = _LOGO_LINE_W  # align turn separators with the banner

def _print_turn_header(turn: int, max_turns: int) -> None:
    label     = f" turn {turn}/{max_turns} "
    bar_total = _TURN_WIDTH - len(label) - 2   # 2 for leading "  "
    left_bar  = 4
    right_bar = bar_total - left_bar
    line = (f"{_rgb(0,80,95)}{'─'*left_bar}{C.RESET}"
            f"{C.DIM}{_rgb(120,120,120)}{label}{C.RESET}"
            f"{_rgb(0,80,95)}{'─'*right_bar}{C.RESET}")
    print(f"\n  {line}")

def _print_tool_call(name: str, args: dict) -> None:
    name_col  = f"{C.BOLD}{_rgb(0,210,210)}{name}{C.RESET}"
    args_str  = json.dumps(args, ensure_ascii=False)
    max_args  = _TURN_WIDTH - len(name) - 10
    if len(args_str) > max_args:
        args_str = args_str[:max_args - 3] + "..."
    print(f"  {_rgb(0,80,95)}→{C.RESET} {name_col}  {C.DIM}{_rgb(140,140,140)}{args_str}{C.RESET}")

def _print_tool_result(result: str) -> None:
    r = result.strip()
    preview = r[:300] + ("..." if len(r) > 300 else "")
    # Color by result type
    if r.startswith("security:") or r.startswith("error:"):
        color = _rgb(220, 80, 80)     # red — error
    elif "[exit 0]" in r:
        color = _rgb(80, 185, 120)    # green — success
    elif r.startswith("ok:"):
        color = _rgb(80, 185, 120)    # green — ok
    elif any(f"[exit {i}]" in r for i in range(1, 256)):
        color = _rgb(210, 170, 50)    # yellow — non-zero exit
    else:
        color = _rgb(130, 130, 130)   # gray — neutral
    print(f"  {_rgb(0,80,95)}←{C.RESET} {color}{preview}{C.RESET}")

def _print_done(result_text: str) -> None:
    bar   = f"{_rgb(0,170,100)}{'─' * _TURN_WIDTH}{C.RESET}"
    check = f"{C.BOLD}{_rgb(0,230,130)}✓{C.RESET}"
    text  = f"{C.BOLD}{_rgb(200,200,200)}{result_text}{C.RESET}"
    print(f"\n  {bar}")
    print(f"  {check}  {text}")
    print(f"  {bar}")

# ── Agent Turn ────────────────────────────────────────────────────────────────

def run_turn(messages: list, turn: int = 0, max_turns: int = 0) -> bool:
    content, tool_calls = call_api_stream(messages)

    assistant_msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        assistant_msg["tool_calls"] = [
            {"id": tc["id"], "type": "function",
             "function": {"name": tc["name"], "arguments": tc["arguments"]}}
            for tc in tool_calls
        ]
    messages.append(assistant_msg)

    if not tool_calls:
        return True

    for tc in tool_calls:
        name = tc["name"]
        args = json.loads(tc["arguments"])

        _print_tool_call(name, args)

        if name == "done":
            _print_done(args.get("result", ""))
            return True

        result = dispatch(name, args, vision=VISION)

        if isinstance(result, dict) and "image_b64" in result:
            _print_tool_result(result["text"] + " [image attached]")
            tool_content = [
                {"type": "text", "text": result["text"]},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{result['image_b64']}"
                }},
            ]
        else:
            _print_tool_result(str(result))
            tool_content = result

        messages.append({
            "role": "tool", "tool_call_id": tc["id"], "content": tool_content,
        })

    return False

# ── Context Compression ───────────────────────────────────────────────────────

def _estimate_tokens(messages: list) -> int:
    """Rough token estimate: total chars / 4."""
    total = 0
    for m in messages:
        c = m.get("content") or ""
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        total += len(str(c))
        # also count tool_calls arguments
        for tc in m.get("tool_calls", []):
            total += len(tc.get("function", {}).get("arguments", ""))
    return total // 4


def _call_api_simple(messages: list) -> str:
    """Single non-streaming LLM call; returns content string."""
    resp = requests.post(
        BASE_URL,
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model":      MODEL,
            "messages":   messages,
            "max_tokens": 1024,
            "stream":     False,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _maybe_compress(messages: list) -> None:
    """Compress context in-place if token estimate exceeds CTX_LIMIT.

    Keeps messages[0] (system prompt) + the last CTX_KEEP_RECENT messages
    verbatim. Summarises everything in between with a single LLM call and
    replaces those messages with one system-role summary message.
    """
    if CTX_LIMIT <= 0:
        return
    est = _estimate_tokens(messages)
    if est <= CTX_LIMIT:
        return

    # Need at least system + 2 middle + recent to bother compressing
    min_len = 1 + 2 + CTX_KEEP_RECENT
    if len(messages) < min_len:
        return

    sys_msg  = messages[0]
    recent   = messages[-CTX_KEEP_RECENT:]
    middle   = messages[1: len(messages) - CTX_KEEP_RECENT]

    # Build a plain-text transcript of the middle section for summarisation
    lines = []
    for m in middle:
        role = m.get("role", "?")
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                lines.append(f"[tool_call] {fn.get('name','')}({fn.get('arguments','')})")
        elif content:
            lines.append(f"[{role}] {content}")

    transcript = "\n".join(lines)
    summary_prompt = [
        {"role": "system", "content":
            "You are a memory compressor. Summarise the following conversation excerpt "
            "into a compact but complete recap. Preserve: task goals, key facts, "
            "decisions made, commands run and their outcomes, and any open questions. "
            "Respond with plain text only — no preamble, no commentary."},
        {"role": "user", "content": transcript},
    ]

    # Print visual notice
    before = len(messages)
    label  = f" compressing context ({est} est. tokens → summarising {len(middle)} messages) "
    bar_w  = _TURN_WIDTH - len(label) - 2
    left_b = 4
    right_b = max(0, bar_w - left_b)
    notice = (f"{_rgb(90,60,180)}{'─'*left_b}{C.RESET}"
              f"{C.DIM}{_rgb(150,120,220)}{label}{C.RESET}"
              f"{_rgb(90,60,180)}{'─'*right_b}{C.RESET}")
    print(f"\n  {notice}")

    try:
        summary_text = _call_api_simple(summary_prompt)
    except Exception as e:
        print(f"  {_rgb(200,80,80)}context compression failed: {e}{C.RESET}")
        return

    summary_msg = {
        "role":    "system",
        "content": f"[Conversation Summary — earlier context compressed]\n{summary_text}",
    }

    # Replace messages in-place: system + summary + recent
    messages[1:len(messages) - CTX_KEEP_RECENT] = [summary_msg]
    after = len(messages)
    print(f"  {_rgb(90,60,180)}→{C.RESET} {C.DIM}{_rgb(150,120,220)}"
          f"context compressed: {before} → {after} messages{C.RESET}")


# ── Agent Loop ────────────────────────────────────────────────────────────────

def run_agent(user_input: str, messages: list, session_id: str = "") -> None:
    messages.append({"role": "user", "content": user_input})
    _maybe_compress(messages)
    for turn in range(1, MAX_TURNS + 1):
        _print_turn_header(turn, MAX_TURNS)
        if run_turn(messages, turn, MAX_TURNS):
            break
    else:
        bar = f"{_rgb(200,80,80)}{'─' * _TURN_WIDTH}{C.RESET}"
        print(f"\n  {bar}")
        print(f"  {_rgb(200,80,80)}✗  stopped: {MAX_TURNS}-turn limit reached{C.RESET}")
        print(f"  {bar}")
    # Auto-save session after every agent loop
    if session_id:
        _session.save(session_id, messages)

# ── REPL helpers ──────────────────────────────────────────────────────────────

def _print_sessions() -> None:
    sessions = _session.list_all()
    if not sessions:
        print(c(C.GRAY, "  no saved sessions"))
        return
    for s in sessions:
        name  = s["name"] or c(C.GRAY, "(unnamed)")
        ts    = s["updated"][:16].replace("T", " ")
        msgs  = s["message_count"]
        print(f"  {c(C.CYAN, s['id'])}  {name}  "
              f"{c(C.GRAY, f'{msgs} messages · {ts}')}")

def _print_tasks() -> None:
    text = _task.format_list()
    if text == "no saved tasks":
        print(c(C.GRAY, "  no saved tasks"))
    else:
        print(text)

# ── Interactive REPL ──────────────────────────────────────────────────────────

def interactive() -> None:
    # Start a new session
    sid = _session.new_id()
    _task.set_session_id(sid)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    print_banner()

    # Hint if there are saved sessions
    n_sessions = _session.count()
    if n_sessions:
        print(c(C.GRAY, f"  {n_sessions} saved session{'s' if n_sessions > 1 else ''}  ·  "
                        f"/sessions to browse\n"))

    repl = PromptSession(history=InMemoryHistory())

    while True:
        try:
            user_input = repl.prompt("▶ ", in_thread=True).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue

        # ── /commands (slash-prefixed, never sent to LLM) ─────────────────
        if user_input.startswith("/"):
            parts = user_input[1:].split(maxsplit=1)
            cmd   = parts[0].lower()
            arg   = parts[1].strip() if len(parts) > 1 else ""

            if cmd in ("exit", "quit"):
                break

            elif cmd == "sessions":
                _print_sessions()

            elif cmd == "save":
                _session.save(sid, messages, name=arg)
                print(c(C.GREEN, f"  saved as '{arg or sid}'"))

            elif cmd == "load":
                if not arg:
                    print(c(C.RED, "  usage: /load <session-id or name>"))
                else:
                    data = _session.load(arg)
                    if data is None:
                        print(c(C.RED, f"  session not found: {arg}"))
                    else:
                        messages = data["messages"]
                        sid      = data["id"]
                        _task.set_session_id(sid)
                        print(c(C.GREEN,
                                f"  loaded '{data.get('name') or sid}'  "
                                f"({data['message_count']} messages)"))

            elif cmd == "new":
                _session.save(sid, messages)
                sid      = _session.new_id()
                messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                _task.set_session_id(sid)
                _task.set_current_id(None)
                print(c(C.GRAY, "  new session started"))

            elif cmd == "reset":
                messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                _task.set_current_id(None)
                print(c(C.GRAY, "  history cleared"))

            elif cmd == "tasks":
                _print_tasks()

            elif cmd == "resume":
                if not arg:
                    print(c(C.RED, "  usage: /resume <task-id>"))
                else:
                    tdata = _task.load(arg)
                    if tdata is None:
                        print(c(C.RED, f"  task not found: {arg}"))
                    else:
                        if tdata.get("session_id"):
                            sdata = _session.load(tdata["session_id"])
                            if sdata:
                                messages = sdata["messages"]
                                sid      = sdata["id"]
                                _task.set_session_id(sid)
                                print(c(C.GREEN, f"  restored session: {sid}"))
                        context = _task.format_resume_context(tdata)
                        messages.append({"role": "system", "content": context})
                        _task.set_current_id(arg)
                        print(c(C.CYAN, context))

            elif cmd == "help":
                print(c(C.GRAY,
                    "  /sessions          list saved sessions\n"
                    "  /save [name]       save current session\n"
                    "  /load <name|id>    restore a session\n"
                    "  /new               start a new session\n"
                    "  /reset             clear current history\n"
                    "  /tasks             list saved tasks\n"
                    "  /resume <task-id>  resume an interrupted task\n"
                    "  /exit              quit wisp"
                ))

            else:
                print(c(C.RED, f"  unknown command: /{cmd}  (try /help)"))

        # ── regular input → LLM ───────────────────────────────────────────
        else:
            run_agent(user_input, messages, session_id=sid)

# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1:
        oneshot_task = " ".join(sys.argv[1:]).strip()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        run_agent(oneshot_task, messages)
    else:
        interactive()

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

# ── Config ────────────────────────────────────────────────────────────────────

_CONFIG_PATH = Path(__file__).parent / "config.yaml"

def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        print(f"error: config file not found: {_CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)

_cfg = _load_config()

VERSION    = "0.3"
BASE_URL   = _cfg["server"]["base_url"]
API_KEY    = _cfg["server"]["api_key"]
MODEL      = _cfg["model"]["name"]
MAX_TOKENS = _cfg["model"]["max_tokens"]
MAX_TURNS  = _cfg["agent"]["max_turns"]
BASH_TO    = _cfg["agent"]["bash_timeout"]
VISION     = _cfg.get("model", {}).get("vision", False)
WORKSPACE  = _cfg["agent"].get("workspace", "~/wisp-workspace")
STRICT     = _cfg["agent"].get("strict_workspace", True)

# Initialise workspace sandbox
from tools import init_workspace
init_workspace(WORKSPACE, STRICT)

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

_LOGO_COLORS = [
    (  0, 255, 255), (  0, 235, 245), (  0, 215, 230),
    (  0, 190, 210), (  0, 165, 190), (  0, 140, 170),
]
_SEP_COLOR = _rgb(0, 120, 140)
_SEP       = f"{_SEP_COLOR}  ·  {C.RESET}"

def print_banner() -> None:
    print()
    for row in range(6):
        r, g, b = _LOGO_COLORS[row]
        lc = f"\033[1m{_rgb(r, g, b)}"
        print("  " + lc + _W[row] + C.RESET
              + _SEP
              + lc + _I[row] + C.RESET
              + _SEP
              + lc + _S[row] + C.RESET
              + _SEP
              + lc + _P[row] + C.RESET)

    sep  = f"{_rgb(60, 60, 60)}  ·  {C.RESET}"
    dim  = C.DIM
    teal = _rgb(0, 200, 200)
    gray = _rgb(160, 160, 160)
    print()
    print(f"  {dim}{gray}A lightweight local AI agent for macOS{C.RESET}"
          f"{sep}{dim}{gray}v{VERSION}{C.RESET}")
    print(f"  {dim}model: {teal}{MODEL}{C.RESET}"
          f"{sep}{dim}tools: {teal}{len(__import__('tools').SCHEMAS)}{C.RESET}")
    print()
    print(f"  {_rgb(90,90,90)}reset{C.RESET}  clear history"
          f"       {_rgb(90,90,90)}exit{C.RESET}  quit")
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

# ── Agent Turn ────────────────────────────────────────────────────────────────

def run_turn(messages: list) -> bool:
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

        print(c(C.YELLOW, f"  → {name}") + c(C.GRAY, f"({json.dumps(args, ensure_ascii=False)})"))

        if name == "done":
            print(c(C.GREEN, C.BOLD + f"\n✓ {args['result']}") + C.RESET)
            return True

        result = dispatch(name, args, vision=VISION)

        if isinstance(result, dict) and "image_b64" in result:
            print(c(C.GRAY, f"  ← {result['text']}") + c(C.CYAN, " [image]"))
            tool_content = [
                {"type": "text", "text": result["text"]},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{result['image_b64']}"
                }},
            ]
        else:
            preview = str(result)[:300] + ("…" if len(str(result)) > 300 else "")
            print(c(C.GRAY, f"  ← {preview}"))
            tool_content = result

        messages.append({
            "role": "tool", "tool_call_id": tc["id"], "content": tool_content,
        })

    return False

# ── Agent Loop ────────────────────────────────────────────────────────────────

def run_agent(user_input: str, messages: list) -> None:
    messages.append({"role": "user", "content": user_input})
    for turn in range(1, MAX_TURNS + 1):
        print(c(C.DIM, f"\n[turn {turn}]"))
        if run_turn(messages):
            break
    else:
        print(c(C.RED, f"\n[stopped: {MAX_TURNS}-turn limit reached]"))

# ── Interactive REPL ──────────────────────────────────────────────────────────

def interactive() -> None:
    print_banner()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    session  = PromptSession(history=InMemoryHistory())

    while True:
        try:
            user_input = session.prompt("▶ ", in_thread=True).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        match user_input.lower():
            case "exit" | "quit": break
            case "reset":
                messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                print(c(C.GRAY, "History cleared."))
            case _:
                run_agent(user_input, messages)

# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1:
        task = " ".join(sys.argv[1:]).strip()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        run_agent(task, messages)
    else:
        interactive()

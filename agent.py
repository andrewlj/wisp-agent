#!/usr/bin/env python3
"""
Local Agent - Layer 2A
Adds: streaming output, multi-turn interactive mode, colored terminal.
Dependencies: requests, pyyaml
"""

import json
import subprocess
import sys
from pathlib import Path

import requests
import yaml
from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory

# ── Config ────────────────────────────────────────────────────────────────────

_CONFIG_PATH = Path(__file__).parent / "config.yaml"

def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        print(f"error: config file not found: {_CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)

_cfg = _load_config()

BASE_URL     = _cfg["server"]["base_url"]
API_KEY      = _cfg["server"]["api_key"]
MODEL        = _cfg["model"]["name"]
MAX_TOKENS   = _cfg["model"]["max_tokens"]
MAX_TURNS    = _cfg["agent"]["max_turns"]
BASH_TIMEOUT = _cfg["agent"]["bash_timeout"]

SYSTEM_PROMPT = """You are a helpful assistant running on macOS (Apple Silicon). \
You have tools to complete tasks on the user's Mac.

## Shell environment
- Shell: zsh. Python: python3.11. Package manager: Homebrew (brew).
- Always use macOS/BSD command syntax — NOT Linux/GNU syntax. Key differences:
  - Memory: `vm_stat`, `top -l 1`, `sysctl hw.memsize` — NOT `free`
  - Disk: `df -h`, `diskutil` — NOT `lsblk`
  - Process: `ps aux` or `ps -eo pid,rss,comm` (BSD flags, no `--` prefix)
  - File info: `stat -f "%z %N"` — NOT `stat --format`
  - Date: `date -r <epoch>` — NOT `date -d`
  - sed: `sed -i ''` — NOT `sed -i`
  - Open files/URLs/apps: `open <path|url>` or `open -a <AppName>`
  - Clipboard: `pbcopy` / `pbpaste`
  - Notifications: `osascript -e 'display notification "msg" with title "title"'`
  - System info: `system_profiler`, `sysctl`, `sw_vers`

## Tool usage rules
- Prefer a single well-formed command over multiple probing attempts.
- If a command fails, read the error carefully and fix the syntax before retrying.
- Use `read_file` / `write_file` for file content; use `bash` for everything else.
- Call `done` as soon as the task is complete — do not keep running extra checks.
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

# ── Tool Schemas ──────────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command on macOS. Use for file ops, system info, running scripts, etc.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full text content of a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text content to a file, creating parent directories if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Call this when the task is fully complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {"type": "string", "description": "Summary of what was accomplished"}
                },
                "required": ["result"],
            },
        },
    },
]

# ── Tool Implementations ──────────────────────────────────────────────────────

def tool_bash(command: str) -> str:
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=BASH_TIMEOUT,
        )
        parts = []
        if proc.stdout:
            parts.append(proc.stdout.rstrip())
        if proc.stderr:
            parts.append(f"[stderr]\n{proc.stderr.rstrip()}")
        parts.append(f"[exit {proc.returncode}]")
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return f"error: timed out after {BASH_TIMEOUT}s"
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
    try:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"ok: wrote {len(content)} chars to {p}"
    except Exception as e:
        return f"error: {e}"


def dispatch(name: str, args: dict) -> str:
    match name:
        case "bash":       return tool_bash(args["command"])
        case "read_file":  return tool_read_file(args["path"])
        case "write_file": return tool_write_file(args["path"], args["content"])
        case _:            return f"error: unknown tool '{name}'"

# ── Streaming API ─────────────────────────────────────────────────────────────

def call_api_stream(messages: list) -> tuple[str, list[dict]]:
    """
    Stream a response from the API.
    Returns (full_content, tool_calls) where each tool_call is:
      {"id": str, "name": str, "arguments": str}
    Text content is printed to stdout as it arrives.
    """
    resp = requests.post(
        BASE_URL,
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model":       MODEL,
            "messages":    messages,
            "tools":       TOOLS,
            "tool_choice": "auto",
            "max_tokens":  MAX_TOKENS,
            "stream":      True,
        },
        stream=True,
        timeout=120,
    )
    resp.raise_for_status()

    full_content = ""
    acc: dict[int, dict] = {}  # index → {id, name, arguments}

    for raw in resp.iter_lines():
        if not raw or not raw.startswith(b"data: "):
            continue
        data = raw[6:]
        if data == b"[DONE]":
            break

        chunk  = json.loads(data)
        choice = chunk["choices"][0]
        delta  = choice.get("delta", {})

        # Stream text
        if delta.get("content"):
            print(delta["content"], end="", flush=True)
            full_content += delta["content"]

        # Accumulate tool call fragments
        for tc in delta.get("tool_calls", []):
            idx = tc["index"]
            if idx not in acc:
                acc[idx] = {"id": "", "name": "", "arguments": ""}
            if tc.get("id"):
                acc[idx]["id"] = tc["id"]
            fn = tc.get("function", {})
            if fn.get("name"):
                acc[idx]["name"] += fn["name"]
            if fn.get("arguments"):
                acc[idx]["arguments"] += fn["arguments"]

    if full_content:
        print()  # trailing newline after streamed text

    return full_content, [acc[i] for i in sorted(acc)]

# ── Agent Turn ────────────────────────────────────────────────────────────────

def run_turn(messages: list) -> bool:
    """
    Execute one LLM turn, appending results to messages in place.
    Returns True when the conversation should stop (done tool or plain reply).
    """
    content, tool_calls = call_api_stream(messages)

    # Reconstruct the assistant message with tool_calls if present
    assistant_msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        assistant_msg["tool_calls"] = [
            {
                "id":   tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]},
            }
            for tc in tool_calls
        ]
    messages.append(assistant_msg)

    if not tool_calls:
        return True  # plain text reply, stop

    for tc in tool_calls:
        name = tc["name"]
        args = json.loads(tc["arguments"])

        print(c(C.YELLOW, f"  → {name}") + c(C.GRAY, f"({json.dumps(args, ensure_ascii=False)})"))

        if name == "done":
            print(c(C.GREEN, C.BOLD + f"\n✓ {args['result']}") + C.RESET)
            return True

        result = dispatch(name, args)
        preview = result[:300] + ("…" if len(result) > 300 else "")
        print(c(C.GRAY, f"  ← {preview}"))

        messages.append({
            "role":         "tool",
            "tool_call_id": tc["id"],
            "content":      result,
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
    print(c(C.BOLD, "Local Agent"))
    print(c(C.GRAY, "Commands: 'reset' to clear history, 'exit' to quit\n"))
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    history = InMemoryHistory()

    while True:
        try:
            user_input = prompt("▶ ", history=history).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue

        match user_input.lower():
            case "exit" | "quit":
                break
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

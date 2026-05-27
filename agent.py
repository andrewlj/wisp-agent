#!/usr/bin/env python3
"""
wisp-agent
A lightweight local AI agent for macOS automation.
Dependencies: requests, pyyaml, prompt_toolkit, playwright
"""

import json
import sys
import time
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

VERSION    = "0.6"
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

# Initialise workspace sandbox, browser timeout, and briefing LLM config
from tools import init_workspace, init_browser_timeout
from briefing import init_briefing
import knowledge as _knowledge
init_workspace(WORKSPACE, STRICT)
init_browser_timeout(BROWSER_TO)
init_briefing(BASE_URL, API_KEY, MODEL, WORKSPACE)
_knowledge.init_knowledge(WORKSPACE)

_workspace_abs = str(Path(WORKSPACE).expanduser().resolve())

# ── Soul & Profile ────────────────────────────────────────────────────────────

_SOUL_PATH    = Path(__file__).parent / "soul.md"
_PROFILE_PATH = Path(__file__).parent / "profile.yaml"
_profile = {}   # module-level; set at startup, used by run_agent

def _load_soul() -> str:
    if _SOUL_PATH.exists():
        return _SOUL_PATH.read_text().strip()
    return ""

def _load_profile() -> dict:
    if not _PROFILE_PATH.exists():
        return {}
    with open(_PROFILE_PATH) as f:
        return yaml.safe_load(f) or {}

def _build_system_prompt(profile: dict) -> str:
    """Assemble system prompt: soul + profile + static environment rules."""
    from datetime import datetime
    _weekdays = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    _now      = datetime.now()
    _date_str = _now.strftime(f"%Y-%m-%d %H:%M ({_weekdays[_now.weekday()]})")

    parts = [f"Current date and time: {_date_str}"]

    # 1. Soul — core identity and constraints
    soul = _load_soul()
    if soul:
        parts.append(soul)

    # 2. Profile — agent persona + user context + behaviors
    agent    = profile.get("agent", {})
    user     = profile.get("user", {})
    persona  = (agent.get("persona") or "").strip()
    if persona:
        parts.append(f"## 性格与风格\n{persona}")

    user_lines = []
    if user.get("name"):     user_lines.append(f"用户称呼：{user['name']}")
    if user.get("language"): user_lines.append(f"首选语言：{user['language']}")
    if user.get("location"): user_lines.append(f"所在地：{user['location']}")
    if user.get("bio"):      user_lines.append(f"背景：{user['bio']}")
    if user_lines:
        parts.append("## 用户信息\n" + "\n".join(f"- {l}" for l in user_lines))

    behaviors = profile.get("behaviors", [])
    if behaviors:
        parts.append("## 工具使用规则（用户配置）\n"
                     + "\n".join(f"- {b}" for b in behaviors))

    # 3. Tool knowledge base (learned rules from past sessions)
    kb = _knowledge.load()
    if kb:
        parts.append(kb)

    # 4. Static environment rules
    parts.append(f"""\
## 运行环境
You are running on macOS (Apple Silicon). Shell: zsh. Python: python3.11. Package manager: Homebrew.
Always use macOS/BSD command syntax — NOT Linux/GNU:
- Memory : `vm_stat`, `sysctl hw.memsize`
- Disk   : `df -h`, `diskutil`
- Process: `ps aux` (BSD flags, no `--` prefix)
- File   : `stat -f "%z %N"`
- Date   : `date -r <epoch>`
- sed    : `sed -i ''`

## Workspace & security
- ALL file outputs MUST go inside: {_workspace_abs}
- NEVER write, move, copy, or delete files outside the workspace.
- NEVER run destructive commands (rm -rf, sudo rm, dd, mkfs, chmod 777).
- NEVER send user data to external services autonomously.
- If a security error is returned, report it — do NOT bypass.

## Tool usage
- For any actionable task, call the first tool immediately — output NO text before the first tool call.
- Prefer a single well-formed command over multiple probing attempts.
- If a command fails, read the error and fix it before retrying.
- Use `read_file` / `write_file` for file content; `bash` for everything else.
- Use `osascript` for notifications, app control, dialogs.
- Use `browser_*` for interactive web tasks (flights, forms, logins) — not `http_get`.
- Call `done` as soon as the task is fully complete.
- When the task involves dates or times (today, tomorrow, next week, etc.), ALWAYS run `bash date '+%Y-%m-%d %H:%M %A'` first to get the current time before calculating any date.

## Task tracking
- For any task with 3 or more steps, ALWAYS call `task_init` first.
- Call `task_step_done` after each step; `task_step_fail` if a step cannot complete.

## Learning
- When the user corrects your approach or points out a mistake, IMMEDIATELY call `learn()`
  before continuing. Do not just apologise — record the correct rule.
- Rule format: one sentence — "完成[任务类型]应该用[工具]，不用[错误工具]"
""")

    return "\n\n".join(parts)

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

# ── Onboarding ────────────────────────────────────────────────────────────────

def _detect_timezone() -> str:
    """Auto-detect macOS timezone from /etc/localtime symlink."""
    import subprocess
    try:
        out = subprocess.check_output(["readlink", "/etc/localtime"],
                                      text=True).strip()
        if "zoneinfo/" in out:
            return out.split("zoneinfo/")[-1]
    except Exception:
        pass
    return "UTC"


def _run_onboarding() -> dict:
    """Interactive first-launch setup. Returns the saved profile dict."""
    repl = PromptSession(history=InMemoryHistory())
    teal = _rgb(0, 210, 210)
    dim  = C.DIM + _rgb(150, 150, 150)
    num  = _rgb(0, 180, 180)
    ok   = _rgb(0, 210, 80)

    print()
    print(f"  {C.BOLD}{teal}✦ 你好，我是 Wisp{C.RESET}")
    print(f"  {dim}在开始之前，让我先了解一下你。{C.RESET}")
    print()

    # 1/3 — name
    print(f"  {num}1/3{C.RESET}  我该怎么称呼你？")
    name = repl.prompt("  ▶ ", in_thread=True).strip() or "朋友"
    print()

    # 2/3 — verbosity
    print(f"  {num}2/3{C.RESET}  你希望我回复时的风格是？")
    print(f"  {dim}[1] 简洁直接（默认）  [2] 详细解释{C.RESET}")
    v = repl.prompt("  ▶ ", in_thread=True).strip()
    verbosity = "detailed" if v == "2" else "concise"
    print()

    # 3/3 — language
    print(f"  {num}3/3{C.RESET}  你主要使用什么语言？")
    print(f"  {dim}[1] 中文（默认）  [2] English  [3] 中英混合{C.RESET}")
    lang = repl.prompt("  ▶ ", in_thread=True).strip()
    language = {"2": "en", "3": "zh-en"}.get(lang, "zh-CN")
    print()

    profile = {
        "user": {
            "name":     name,
            "language": language,
            "location": "",
            "timezone": _detect_timezone(),
            "bio":      "",
        },
        "agent": {
            "name": "Wisp",
            "persona": (
                "简洁直接，不说废话，不重复用户已知的信息。\n"
                "除非用户用英文提问，否则一律用中文回答。\n"
                "遇到不确定的事情，先说不确定，再给建议。"
            ),
            "style": {"verbosity": verbosity},
        },
        "behaviors": [
            "交互式网页任务（查票、填表、登录）用 browser 工具，不用 http_get",
            "多文件操作用 bash glob，不要多次调 read_file",
            "生成的文件/报告保存后用 open 工具打开",
            "bash 输出过长时先用 head/grep 过滤再返回",
        ],
    }

    with open(_PROFILE_PATH, "w") as f:
        yaml.dump(profile, f, allow_unicode=True, default_flow_style=False,
                  sort_keys=False)

    print(f"  {ok}✓{C.RESET}  {C.BOLD}{name}{C.RESET}，配置已保存。"
          f"  {dim}之后可以编辑 profile.yaml 修改。{C.RESET}")
    print()
    return profile

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
    ("news",    ["daily_briefing"]),
    ("learn",   ["learn"]),
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

# ── LLM HTTP layer (retry + timeout) ─────────────────────────────────────────

_MAX_RETRIES  = 3
_RETRY_DELAYS = (1, 2, 4)   # seconds between attempts


def _llm_post(payload: dict, stream: bool = False) -> requests.Response:
    """POST to the LLM endpoint with exponential-backoff retry on transient errors.

    Retries on:  connection error, read timeout, HTTP 5xx
    No retry on: HTTP 4xx (bad request / auth failure — retrying won't help)

    Timeout tuple: (connect_timeout=10s, read_timeout)
      stream=True  → read_timeout=30s per chunk  (catches stalled streams)
      stream=False → read_timeout=120s            (non-streaming calls can be slow)
    """
    timeout   = (10, 120)
    last_err: Exception = RuntimeError("LLM request failed")

    for attempt in range(_MAX_RETRIES + 1):
        if attempt > 0:
            delay = _RETRY_DELAYS[attempt - 1]
            print(f"\n  {_rgb(210,170,50)}⟳  LLM unreachable — retry {attempt}/{_MAX_RETRIES}"
                  f" in {delay}s…{C.RESET}", flush=True)
            time.sleep(delay)
        try:
            resp = requests.post(
                BASE_URL,
                headers={"Authorization": f"Bearer {API_KEY}"},
                json=payload,
                stream=stream,
                timeout=timeout,
            )
            if resp.status_code >= 500:
                last_err = requests.exceptions.HTTPError(
                    f"server error {resp.status_code}", response=resp)
                continue          # retry on 5xx
            resp.raise_for_status()   # raises immediately on 4xx
            return resp
        except requests.exceptions.HTTPError:
            raise                     # 4xx → propagate, no retry
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            last_err = e
            continue

    raise last_err   # exhausted all retries


# ── Streaming API ─────────────────────────────────────────────────────────────

def call_api_stream(messages: list) -> tuple[str, list[dict], dict]:
    """Stream one LLM turn.  Returns (content, tool_calls, usage).

    usage = {"prompt_tokens": N, "completion_tokens": M} when the server
    reports it; empty dict when not available.
    """
    resp = _llm_post({
        "model":       MODEL,
        "messages":    messages,
        "tools":       SCHEMAS,
        "tool_choice": "auto",
        "max_tokens":  MAX_TOKENS,
        "stream":      True,
    }, stream=True)

    full_content = ""
    acc: dict[int, dict] = {}
    usage: dict = {}

    for raw in resp.iter_lines():
        if not raw or not raw.startswith(b"data: "):
            continue
        data = raw[6:]
        if data == b"[DONE]":
            break
        chunk = json.loads(data)

        # Capture token usage from any chunk that carries it
        if chunk.get("usage"):
            usage = chunk["usage"]

        choices = chunk.get("choices", [])
        if not choices:
            continue
        choice = choices[0]
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

    return full_content, [acc[i] for i in sorted(acc)], usage

# ── Output helpers ────────────────────────────────────────────────────────────

_TURN_WIDTH = _LOGO_LINE_W  # align turn separators with the banner
_DEBUG      = False          # toggled by /debug command


def _print_turn_header(turn: int, max_turns: int, messages: list | None = None) -> None:
    token_hint = ""
    if _DEBUG and messages is not None:
        est = _estimate_tokens(messages)
        token_hint = f"  {C.DIM}{_rgb(120,90,200)}~{est} tok{C.RESET}"
    label     = f" turn {turn}/{max_turns} "
    # account for token_hint's visible length (no ANSI) when computing bars
    hint_vis  = f"  ~{_estimate_tokens(messages or [])} tok" if (_DEBUG and messages) else ""
    bar_total = _TURN_WIDTH - len(label) - len(hint_vis) - 2
    left_bar  = 4
    right_bar = max(0, bar_total - left_bar)
    line = (f"{_rgb(0,80,95)}{'─'*left_bar}{C.RESET}"
            f"{C.DIM}{_rgb(120,120,120)}{label}{C.RESET}"
            f"{_rgb(0,80,95)}{'─'*right_bar}{C.RESET}"
            + token_hint)
    print(f"\n  {line}")


def _print_tool_call(name: str, args: dict) -> None:
    name_col = f"{C.BOLD}{_rgb(0,210,210)}{name}{C.RESET}"
    if _DEBUG:
        pretty = json.dumps(args, ensure_ascii=False, indent=2)
        print(f"  {_rgb(0,80,95)}→{C.RESET} {name_col}")
        for ln in pretty.splitlines():
            print(f"    {C.DIM}{_rgb(140,140,140)}{ln}{C.RESET}")
    else:
        args_str = json.dumps(args, ensure_ascii=False)
        max_args = _TURN_WIDTH - len(name) - 10
        if len(args_str) > max_args:
            args_str = args_str[:max_args - 3] + "..."
        print(f"  {_rgb(0,80,95)}→{C.RESET} {name_col}  {C.DIM}{_rgb(140,140,140)}{args_str}{C.RESET}")


def _print_tool_result(result: str, elapsed: float | None = None) -> None:
    r = result.strip()
    # Color by result type
    if r.startswith("security:") or r.startswith("error:"):
        color = _rgb(220, 80, 80)
    elif "[exit 0]" in r:
        color = _rgb(80, 185, 120)
    elif r.startswith("ok:"):
        color = _rgb(80, 185, 120)
    elif any(f"[exit {i}]" in r for i in range(1, 256)):
        color = _rgb(210, 170, 50)
    else:
        color = _rgb(130, 130, 130)
    # A — timing suffix (debug only)
    timing = (f"  {C.DIM}{_rgb(100,100,100)}[{elapsed:.2f}s]{C.RESET}"
              if _DEBUG and elapsed is not None else "")
    if _DEBUG:
        print(f"  {_rgb(0,80,95)}←{C.RESET} {color}{r}{C.RESET}{timing}")
    else:
        preview = r[:300] + ("..." if len(r) > 300 else "")
        print(f"  {_rgb(0,80,95)}←{C.RESET} {color}{preview}{C.RESET}")


def _print_debug_usage(usage: dict, llm_s: float) -> None:
    """C — print real token counts + LLM latency after streaming completes."""
    if not _DEBUG:
        return
    pt = usage.get("prompt_tokens",     usage.get("input_tokens",  "?"))
    ct = usage.get("completion_tokens", usage.get("output_tokens", "?"))
    print(f"  {C.DIM}{_rgb(120,90,200)}"
          f"prompt {pt} · completion {ct} · llm {llm_s:.1f}s"
          f"{C.RESET}")

def _print_done(result_text: str) -> None:
    bar   = f"{_rgb(0,170,100)}{'─' * _TURN_WIDTH}{C.RESET}"
    check = f"{C.BOLD}{_rgb(0,230,130)}✓{C.RESET}"
    text  = f"{C.BOLD}{_rgb(200,200,200)}{result_text}{C.RESET}"
    print(f"\n  {bar}")
    print(f"  {check}  {text}")
    print(f"  {bar}")

# ── Agent Turn ────────────────────────────────────────────────────────────────

def run_turn(messages: list, turn: int = 0, max_turns: int = 0,
             tool_log: list | None = None) -> bool:
    """Run one agent turn.

    tool_log — if provided, each tool execution appends (name, elapsed_s).
    """
    t_llm = time.time()
    content, tool_calls, usage = call_api_stream(messages)
    llm_s = time.time() - t_llm

    # C — real token counts + LLM latency
    if usage:
        _print_debug_usage(usage, llm_s)

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

        # A — time each tool dispatch
        t0     = time.time()
        result = dispatch(name, args, vision=VISION)
        elapsed = time.time() - t0

        if tool_log is not None:
            tool_log.append((name, elapsed))

        if isinstance(result, dict) and "image_b64" in result:
            _print_tool_result(result["text"] + " [image attached]", elapsed)
            tool_content = [
                {"type": "text", "text": result["text"]},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{result['image_b64']}"
                }},
            ]
        else:
            _print_tool_result(str(result), elapsed)
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


def _call_api_simple(messages: list, max_tokens: int = 1024) -> str:
    """Single non-streaming LLM call; returns content string."""
    resp = _llm_post({
        "model":      MODEL,
        "messages":   messages,
        "max_tokens": max_tokens,
        "stream":     False,
    })
    return resp.json()["choices"][0]["message"]["content"]


def _build_compress_transcript(middle: list[dict]) -> str:
    """Build a structured transcript of the middle messages for compression.

    Strategy:
    - user / assistant text  → kept, truncated at 300 chars
    - tool call              → kept as  [call] name(args)
    - tool result ok / exit0 → short preview (first 120 chars)
    - tool result error/security/non-zero exit → kept verbatim up to 400 chars
      (errors are critical for understanding what went wrong)
    """
    lines: list[str] = []
    for m in middle:
        role    = m.get("role", "")
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = str(content).strip()

        if role == "assistant":
            if m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    fn   = tc.get("function", {})
                    name = fn.get("name", "")
                    args = fn.get("arguments", "")
                    if len(args) > 160:
                        args = args[:160] + "…"
                    lines.append(f"[call] {name}({args})")
            if content:
                lines.append(f"[assistant] {content[:300]}")

        elif role == "tool":
            is_error = (content.startswith(("error:", "security:"))
                        or any(f"[exit {i}]" in content for i in range(1, 256)))
            if is_error:
                lines.append(f"[tool:FAIL] {content[:400]}")
            else:
                preview = content[:120] + ("…" if len(content) > 120 else "")
                lines.append(f"[tool:ok] {preview}")

        elif role == "user" and content:
            lines.append(f"[user] {content[:300]}")

    return "\n".join(lines)


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

    # Build a structured transcript — errors kept verbatim, ok results trimmed
    transcript = _build_compress_transcript(middle)
    summary_prompt = [
        {"role": "system", "content": (
            "You are a context compressor for an AI agent session. "
            "Summarise the following agent interaction into a compact recap.\n\n"
            "MUST preserve:\n"
            "- The user's original task / goal\n"
            "- Every error, failure, or rejected tool call and its cause\n"
            "- Key findings: file paths, data values, command outputs\n"
            "- Decisions made and steps completed\n"
            "- Any open / remaining steps\n\n"
            "Drop: successful tool results whose content was already used, "
            "verbose ok-outputs, repeated attempts with the same outcome.\n"
            "Plain text only — no preamble, no bullet symbols unless helpful."
        )},
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


# ── Post-task Reflection ──────────────────────────────────────────────────────

_CORRECTION_SIGNALS = ["不对", "不是", "应该", "错了", "不要", "换成", "改用", "别用",
                       "wrong", "incorrect", "should use", "don't use", "use instead"]

def _post_task_reflect(messages: list) -> None:
    """Scan the completed task for failures/corrections; write learned rules.

    Runs a lightweight LLM call (json_mode, max 256 tokens) to extract
    up to 3 generalised tool-usage rules and appends them to knowledge.md.
    Silent on any error — never interrupts the user.
    """
    failures:    list[str] = []
    corrections: list[str] = []
    prev_role = ""

    for msg in messages[1:]:   # skip system prompt
        role    = msg.get("role", "")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        text = str(content).strip()

        if role == "tool":
            if (text.startswith("error:") or text.startswith("security:")
                    or any(f"[exit {i}]" in text for i in range(1, 10))):
                failures.append(text[:200])
        elif role == "user" and prev_role == "tool":
            if any(s in text for s in _CORRECTION_SIGNALS):
                corrections.append(text[:200])
        prev_role = role

    if not failures and not corrections:
        return

    # Build a compact reflection prompt
    excerpts: list[str] = []
    if failures:
        excerpts.append("工具执行失败：\n" + "\n".join(f"- {f}" for f in failures[:3]))
    if corrections:
        excerpts.append("用户纠正：\n" + "\n".join(f"- {c}" for c in corrections[:3]))

    prompt = (
        "以下是刚完成的任务中出现的失败或用户纠正：\n\n"
        + "\n\n".join(excerpts)
        + "\n\n请总结出可以避免类似问题的工具使用规则（最多3条）。"
        "\n规则必须一句话，格式：'完成[任务类型]应该用[工具/方法]，不用[错误方法]'"
        "\n只提取可泛化的规则，跳过过于具体的单次事件。"
        '\n返回 JSON：{"rules":[{"rule":"...","category":"browser|bash|files|general"}]}'
    )

    try:
        resp = requests.post(
            BASE_URL,
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model":           MODEL,
                "messages":        [{"role": "user", "content": prompt}],
                "max_tokens":      256,
                "stream":          False,
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        resp.raise_for_status()
        import json as _json
        data  = _json.loads(resp.json()["choices"][0]["message"]["content"])
        rules = data.get("rules", [])
    except Exception:
        return

    written = 0
    for r in rules[:3]:
        rule_text = (r.get("rule") or "").strip()
        category  = (r.get("category") or "general").strip()
        if rule_text:
            result = _knowledge.append_rule(rule_text, category)
            if "rule added" in result:
                written += 1

    if written:
        print(f"\n  {_rgb(90,60,180)}◆{C.RESET} {C.DIM}{_rgb(150,120,220)}"
              f"learned {written} rule{'s' if written > 1 else ''} → knowledge.md{C.RESET}")


# ── Agent Loop ────────────────────────────────────────────────────────────────

def _print_task_summary(tool_log: list[tuple[str, float]], total_s: float, turns: int) -> None:
    """D — debug-only task summary after done."""
    if not _DEBUG or not tool_log:
        return
    counts: dict[str, int] = {}
    for name, _ in tool_log:
        counts[name] = counts.get(name, 0) + 1
    tool_str = " · ".join(f"{n}×{c}" for n, c in counts.items())
    label = (f" {turns} turn{'s' if turns > 1 else ''}"
             f" · {total_s:.1f}s · {tool_str} ")
    bar_total = _TURN_WIDTH - len(label) - 2
    left_b    = 4
    right_b   = max(0, bar_total - left_b)
    line = (f"{_rgb(0,80,95)}{'─'*left_b}{C.RESET}"
            f"{C.DIM}{_rgb(120,90,200)}{label}{C.RESET}"
            f"{_rgb(0,80,95)}{'─'*right_b}{C.RESET}")
    print(f"  {line}")


def run_agent(user_input: str, messages: list, session_id: str = "") -> None:
    # Refresh system prompt so current date/time is always accurate
    messages[0] = {"role": "system", "content": _build_system_prompt(_profile)}
    messages.append({"role": "user", "content": user_input})
    _maybe_compress(messages)

    tool_log: list[tuple[str, float]] = []
    t_start  = time.time()
    turns_used = 0

    for turn in range(1, MAX_TURNS + 1):
        turns_used = turn
        _print_turn_header(turn, MAX_TURNS, messages)
        if run_turn(messages, turn, MAX_TURNS, tool_log=tool_log):
            break
    else:
        bar = f"{_rgb(200,80,80)}{'─' * _TURN_WIDTH}{C.RESET}"
        print(f"\n  {bar}")
        print(f"  {_rgb(200,80,80)}✗  stopped: {MAX_TURNS}-turn limit reached{C.RESET}")
        print(f"  {bar}")

    # D — task summary (debug only)
    _print_task_summary(tool_log, time.time() - t_start, turns_used)

    # Post-task reflection: learn from failures and corrections
    _post_task_reflect(messages)
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
    global _profile
    # Onboarding: first launch if no profile exists
    if not _PROFILE_PATH.exists():
        print_banner()
        _profile = _run_onboarding()
    else:
        _profile = _load_profile()
        print_banner()

    _sys_prompt = _build_system_prompt(_profile)

    # Start a new session
    sid = _session.new_id()
    _task.set_session_id(sid)
    messages = [{"role": "system", "content": _sys_prompt}]

    # Hint if there are saved sessions
    n_sessions = _session.count()
    if n_sessions:
        print(c(C.GRAY, f"  {n_sessions} saved session{'s' if n_sessions > 1 else ''}  ·  "
                        f"/sessions to browse\n"))

    repl = PromptSession(history=InMemoryHistory())

    # Save exact terminal state now so we can restore it precisely on exit.
    # prompt_toolkit puts the terminal into raw mode; stty sane is a rough
    # approximation — tcsetattr with the saved attrs is the exact fix.
    import termios as _termios
    _saved_termios = None
    try:
        _saved_termios = _termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    try:
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
                    messages = [{"role": "system", "content": _sys_prompt}]
                    _task.set_session_id(sid)
                    _task.set_current_id(None)
                    print(c(C.GRAY, "  new session started"))
    
                elif cmd == "reset":
                    messages = [{"role": "system", "content": _sys_prompt}]
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
    
                elif cmd == "knowledge":
                    if arg.startswith("forget ") or arg == "forget":
                        kw = arg[len("forget"):].strip()
                        if not kw:
                            print(c(C.RED, "  usage: /knowledge forget <keyword>"))
                        else:
                            result = _knowledge.forget(kw)
                            color  = C.GREEN if result.startswith("ok:") else C.RED
                            print(c(color, f"  {result}"))
                    else:
                        kb = _knowledge.load()
                        if kb:
                            print(c(C.GRAY, kb))
                        else:
                            print(c(C.GRAY, "  knowledge base is empty"))
    
                elif cmd == "debug":
                    global _DEBUG
                    _DEBUG = not _DEBUG
                    state = f"{_rgb(0,210,210)}on{C.RESET}" if _DEBUG else f"{_rgb(130,130,130)}off{C.RESET}"
                    print(f"  debug mode {state}"
                          + (f"  {C.DIM}{_rgb(120,90,200)}"
                             f"(full args · full results · real tokens · tool timing · task summary)"
                             f"{C.RESET}"
                             if _DEBUG else ""))
    
                elif cmd == "help":
                    print(c(C.GRAY,
                        "  /sessions          list saved sessions\n"
                        "  /save [name]       save current session\n"
                        "  /load <name|id>    restore a session\n"
                        "  /new               start a new session\n"
                        "  /reset             clear current history\n"
                        "  /tasks             list saved tasks\n"
                        "  /resume <task-id>  resume an interrupted task\n"
                        "  /knowledge                    show learned tool usage rules\n"
                        "  /knowledge forget <keyword>   remove rules containing keyword\n"
                        "  /debug             toggle verbose debug output\n"
                        "  /exit              quit wisp"
                    ))
    
                else:
                    print(c(C.RED, f"  unknown command: /{cmd}  (try /help)"))
    
            # ── regular input → LLM ───────────────────────────────────────────
            else:
                run_agent(user_input, messages, session_id=sid)

    finally:
        # Restore the exact terminal state saved before the REPL started.
        # This is more precise than `stty sane` and handles all the attrs
        # that prompt_toolkit modifies (echo, icanon, alternate-screen, etc.).
        if _saved_termios is not None:
            try:
                _termios.tcsetattr(sys.stdin.fileno(), _termios.TCSANOW,
                                   _saved_termios)
            except Exception:
                pass
        # Belt-and-suspenders: also reset via stty in case tcsetattr misses
        # anything (e.g. lingering alternate-screen escape).
        import subprocess as _sp
        _sp.run(["stty", "sane"], stderr=_sp.DEVNULL)

# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1:
        oneshot_task = " ".join(sys.argv[1:]).strip()
        _profile  = _load_profile()
        messages  = [{"role": "system", "content": _build_system_prompt(_profile)}]
        run_agent(oneshot_task, messages)
    else:
        interactive()

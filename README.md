# wisp-agent

A lightweight local macOS intelligent assistant powered by any OpenAI-compatible local LLM. No cloud dependencies — everything runs on your machine.

## Features

- **ReAct loop** — LLM thinks, calls tools, observes results, repeats until done
- **Streaming output** — responses print token by token in real time
- **Soul + Profile** — immutable core constraints + per-user identity, injected at startup
- **First-launch onboarding** — interactive setup on first run; generates `profile.yaml`
- **Tool knowledge base** — learned rules from past corrections, always in context
- **Self-learning** — agent records mistakes automatically; post-task reflection writes rules to `knowledge.md`
- **Session persistence** — save, restore, and browse conversation history
- **Task checkpoint system** — multi-step task tracking with recovery support
- **Context compression** — auto-summarises old messages when context grows large; errors preserved verbatim
- **Daily briefing** — config-driven HTML news report from global sources (`briefing.yaml`)
- **Workspace sandbox** — all file outputs confined to `~/wisp/workspace`; writes outside are blocked
- **LLM retry** — exponential-backoff retry on connection errors and 5xx; stalled streams time out cleanly
- **Debug mode** — per-tool timing, real token counts, task summary

## Requirements

- macOS (Apple Silicon recommended)
- Python 3.11+
- A local LLM server with OpenAI-compatible API (e.g. `mlx_lm.server`)

## Setup

**1. Clone and install dependencies**

```bash
git clone https://github.com/your-username/wisp-agent.git
cd wisp-agent
pip install -r requirements.txt
playwright install chromium
```

**2. Configure**

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml`:

```yaml
server:
  base_url: "http://127.0.0.1:8000/v1/chat/completions"
  api_key: "your-api-key"

model:
  name: "Qwen3.5-9B-MLX-8bit"
  max_tokens: 4096
  vision: false   # set true for VL models (e.g. Qwen2.5-VL)

agent:
  max_turns: 20
  bash_timeout: 30
  browser_timeout: 30       # seconds before browser ops time out
  workspace: "~/wisp/workspace"
  strict_workspace: true
  context_limit: 6000       # estimated tokens before compression kicks in
  context_keep_recent: 6    # recent messages kept verbatim during compression

# Optional — only needed for flight_search / hotel_search
serpapi:
  api_key: ""               # https://serpapi.com (100 free searches/month)
```

**3. Start your local LLM server**

```bash
mlx_lm.server --model <your-model-path> --port 8000
```

**4. Run**

```bash
# Interactive mode (multi-turn REPL)
python agent.py

# One-shot mode
python agent.py "帮我统计桌面上有多少个文件"
```

## REPL commands

| Command | Action |
|---------|--------|
| `/sessions` | List all saved sessions |
| `/save [name]` | Save the current session |
| `/load <name\|id>` | Restore a saved session |
| `/new` | Start a fresh session (auto-saves current) |
| `/reset` | Clear conversation history |
| `/tasks` | List saved task checkpoints |
| `/resume <task-id>` | Resume an interrupted multi-step task |
| `/knowledge` | Show learned tool usage rules |
| `/knowledge forget <keyword>` | Remove rules containing keyword |
| `/debug` | Toggle verbose output (tool timing, real tokens, task summary) |
| `/help` | Show all commands |
| `/exit` | Quit wisp |

## Tools (45 total)

| Category | Tools |
|----------|-------|
| Core | `bash`, `read_file`, `write_file`, `done` |
| Mac system | `screenshot`, `osascript`, `clipboard_read/write`, `open`, `list_apps`, `focus_app`, `quit_app` |
| File ops | `find_files`, `move_file`, `copy_file`, `delete_file` |
| Network | `http_get`, `http_post` |
| Browser | `browser_open`, `browser_click`, `browser_type`, `browser_get_text`, `browser_screenshot`, `browser_close` |
| Reminders | `reminders_list`, `reminders_add` |
| Calendar | `calendar_list`, `calendar_add` |
| Notes | `notes_list`, `notes_read`, `notes_create`, `notes_append` |
| Browser+ | `browser_wait`, `browser_select`, `browser_scroll`, `browser_snapshot` |
| Travel | `flight_search`, `hotel_search` |
| Task | `task_init`, `task_step_done`, `task_step_fail` |
| News | `daily_briefing` |
| Learn | `learn` |

## Data directory

All persistent data lives under `~/wisp/`:

```
~/wisp/
├── workspace/          # agent file outputs (reports, screenshots, downloads)
│   └── daily-report/   # generated HTML briefings
├── sessions/           # saved conversation histories
├── tasks/              # multi-step task checkpoints
└── knowledge.md        # learned tool usage rules (grows over time)
```

## Project structure

```
wisp-agent/
├── agent.py              # Main loop, REPL, context compression, API
├── tools.py              # All tool schemas and implementations
├── briefing.py           # Daily news briefing generator (processing only)
├── briefing.yaml         # Briefing sources, domains, and filters (edit freely)
├── session.py            # Session save/load/list
├── task.py               # Task checkpoint system
├── knowledge.py          # Tool knowledge base (read/write knowledge.md)
├── soul.md               # Core identity and immutable constraints
├── profile.yaml          # User + agent settings (git-ignored, generated by onboarding)
├── profile.example.yaml  # Profile template
├── config.yaml           # Server/model config (git-ignored)
├── config.example.yaml   # Config template
├── requirements.txt
└── LICENSE
```

## Daily Briefing

The `daily_briefing` tool fetches, filters, and summarises global news into a single HTML report, opened automatically in your browser.

```
▶ 生成今日简报
```

**Sections covered** (defined in `briefing.yaml`, fully customisable):
- 🌍 政治国际 — Reuters, AP, BBC
- 💡 ICT 前沿研究 — Nature, IEEE, MIT Technology Review, DeepMind, HuggingFace, Ars Technica
- 🏢 高科技企业 — The Verge, Wired, Ars Technica, MIT TR
- 🇮🇪 爱尔兰新闻 — RTÉ, Irish Times, Irish Independent, The Journal
- 🇨🇳 中国要闻 — BBC, AP

**How it works:** RSS/page sources → per-source cap + keyword filtering → LLM translate + summarise → HTML report saved to `~/wisp/workspace/daily-report/`

**Adding a new section:** add a block to `briefing.yaml` — no Python changes needed.

---

## Development Log

### v0.7 — Reminders, Calendar, Notes & Test Suite

**New tools**
- `reminders_list(due_before, list_name, limit)` — list incomplete reminders; `due_before` date filter for fast today-only queries across all iCloud lists
- `reminders_add(title, due, notes, list_name)` — add reminder with optional due date
- `calendar_list(days, calendar)` — list upcoming events from macOS Calendar
- `calendar_add(title, start, end, calendar, notes)` — create calendar event
- `notes_list(folder, limit)` — list notes with title + modification date
- `notes_read(title, folder)` — read note content by partial title match
- `notes_create(title, content, folder)` — create note; auto-creates folder
- `notes_append(title, content)` — append text to existing note

**Date accuracy**
- Current date injected as first line of system prompt on every `run_agent` call — LLM always sees today's date regardless of session duration

**Automated test suite** (`test/`)
- `test/test_tools.py` — 47 unit tests, no LLM required, sub-second for most
- `test/run_tests.py` — 27 integration tests against live agent; `expect_any` field for OR-matching; `post_check` for side-effect verification; per-test `setup`/`teardown`

**Bug fixes**
- Bash scanner regex: `\s*` between redirect operator and path (real security fix — `> /tmp/file` was not blocked)
- `reminders_list` default: batch-fetch with `whose completed is false` pre-filter; avoids loading all 400+ completed items from iCloud
- AppleScript reserved word `line` → renamed to `itemLine`
- `delete_file` return value normalised to `ok: moved to Trash: {path}`
- `_profile` type annotation removed (annotated names can't be declared `global` inside functions)

### v0.6 — Stability, Debug & Config-driven Briefing

**Stability**
- `_llm_post()`: central HTTP layer with exponential-backoff retry (up to 3×, 1/2/4s delays)
  - Retries: `ConnectionError`, `ReadTimeout`, HTTP 5xx
  - No retry: HTTP 4xx (auth / bad request)
  - Unified timeout: connect 10s, read 120s
- Context compression: errors and non-zero exits preserved verbatim; ok-results trimmed to 120 chars; prompt explicitly retains failed steps and open tasks

**Debug mode enhancements** (`/debug`)
- Per-tool execution timing on every result line (`[1.24s]`)
- Real token counts from API response (`prompt N · completion M · llm Xs`)
- Task summary line after `done`: `── 3 turns · 12.4s · bash×3 · read_file×2 ──`

**Config-driven briefing**
- All section/source/filter config moved to `briefing.yaml`; `briefing.py` is processing-only
- Generic `_apply_filter()` replaces all section-specific filter functions
- HTML section order driven by YAML; new sections require zero Python changes
- Translation retry: items with English title or empty summary get a dedicated single-item LLM call

**Knowledge base**
- `/knowledge forget <keyword>` — removes all rules containing keyword (case-insensitive)

**Bug fixes**
- Bash scanner false-positive on `2>/dev/null`
- Briefing filter fixes: Ireland 0→3+, China 0→5, ICT Business 1→3+

### v0.5 — Identity, Soul & Self-Learning

**Soul (`soul.md`)**
- Immutable core identity and constraints, always injected first in the system prompt
- Defines what Wisp is, how it behaves, and what it will never do

**Profile (`profile.yaml`) + First-launch onboarding**
- 3-question interactive setup on first launch: name, style, language
- Timezone auto-detected from system; generates `profile.yaml` (git-ignored)

**Tool knowledge base (`knowledge.md`)**
- Learned tool usage rules stored in `~/wisp/knowledge.md`
- Always injected into system prompt so rules apply from the first turn

**Self-learning (two layers)**
- Layer 1 — in-task: agent calls `learn()` immediately when user corrects a mistake
- Layer 2 — post-task: `_post_task_reflect()` scans completed tasks for tool errors and corrections; fires a lightweight LLM call to extract up to 3 generalised rules

### v0.4 — Sessions, Tasks, Briefing & Robustness

- Session persistence (`/save`, `/load`, `/sessions`, `/new`)
- Task checkpoint system (`task_init` / `task_step_done` / `task_step_fail`, `/tasks`, `/resume`)
- Context compression (auto-triggered at `context_limit` tokens)
- Daily briefing tool with JSON-mode LLM, history dedup, per-source cap
- `/debug` toggle; browser timeout control

### v0.3 — Security Controls & Stability
- Workspace sandbox: file writes outside `~/wisp/workspace` blocked at tool level
- Bash risk scanner: regex detection of destructive commands and out-of-workspace shell redirects

### v0.2 — Mac Automation & Tool Expansion
- 14 new tools across mac system, file ops, network, browser categories
- Browser automation via Playwright (Chromium)
- File search powered by macOS Spotlight (`mdfind`)

### v0.1 — Core Agent + Interactive Experience
- ReAct loop with streaming output (SSE)
- Multi-turn conversation with in-memory history
- Colored terminal output + ASCII banner
- Chinese input via `prompt_toolkit`; macOS-aware system prompt

---

## Roadmap

- [ ] Gateway architecture — Unix socket IPC + Telegram bot adapter for remote Mac control
- [ ] Tool set expansion — more reliable automation coverage

## License

MIT

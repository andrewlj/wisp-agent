# wisp-agent

A lightweight local AI agent that connects to any OpenAI-compatible local LLM server. No cloud API dependencies — everything runs on your machine.

## Features

- **ReAct loop** — LLM thinks, calls tools, observes results, repeats until done
- **Streaming output** — responses print token by token in real time
- **Session persistence** — save, restore, and browse conversation history
- **Task checkpoint system** — multi-step task tracking with recovery support
- **Context compression** — auto-summarises old messages when context grows large
- **Daily briefing** — generates a translated, summarised HTML news report from global sources
- **macOS-aware** — system prompt tuned for BSD/macOS command syntax
- **Workspace sandbox** — all file outputs confined to `~/wisp/workspace`; writes outside are blocked

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
| `/debug` | Toggle verbose output (full args, full results, token count) |
| `/help` | Show all commands |
| `/exit` | Quit wisp |

## Tools (28 total)

| Category | Tools |
|----------|-------|
| Core | `bash`, `read_file`, `write_file`, `done` |
| Mac system | `screenshot`, `osascript`, `clipboard_read/write`, `open`, `list_apps`, `focus_app`, `quit_app` |
| File ops | `find_files`, `move_file`, `copy_file`, `delete_file` |
| Network | `http_get`, `http_post` |
| Browser | `browser_open`, `browser_click`, `browser_type`, `browser_get_text`, `browser_screenshot`, `browser_close` |
| Task | `task_init`, `task_step_done`, `task_step_fail` |
| News | `daily_briefing` |

## Data directory

All persistent data lives under `~/wisp/`:

```
~/wisp/
├── workspace/          # agent file outputs (reports, screenshots, downloads)
│   ├── daily-report/   # generated HTML briefings
│   └── screenshots/
├── sessions/           # saved conversation histories
├── tasks/              # multi-step task checkpoints
└── memory/             # persistent memory (coming soon)
```

## Project structure

```
wisp-agent/
├── agent.py              # Main loop, REPL, context compression, API
├── tools.py              # All tool schemas and implementations
├── briefing.py           # Daily news briefing generator
├── session.py            # Session save/load/list
├── task.py               # Task checkpoint system
├── config.yaml           # Your local config (git-ignored)
├── config.example.yaml   # Config template
├── requirements.txt
└── LICENSE
```

## Daily Briefing

The `daily_briefing` tool fetches, filters, and summarises global news into a single HTML report, opened automatically in your browser.

```
▶ 生成今日简报
```

**Sections covered:**
- 🌍 政治国际 — Reuters, AP, BBC
- 💡 ICT 前沿研究与技术突破 — Nature, IEEE, MIT Technology Review, DeepMind, HuggingFace, Ars Technica
- 🏢 高科技企业重大新闻 — The Verge, Wired, Ars Technica, MIT TR
- 🇮🇪 爱尔兰新闻 — RTÉ, Irish Times, Irish Independent, The Journal
- 🎬 娱乐 — Variety, Deadline, Hollywood Reporter, Billboard
- 🇨🇳 中国要闻 — BBC, AP

**How it works:** RSS/page sources → per-source cap + keyword filtering → 1 LLM call per section (translate + summarise, JSON mode) → HTML report saved to `~/wisp/workspace/daily-report/`

---

## Development Log

### v0.4 — Sessions, Tasks, Briefing & Robustness

**Session persistence**
- `/save`, `/load`, `/sessions`, `/new` commands to manage conversation history
- Sessions stored in `~/wisp/sessions/`; auto-saved after every agent loop

**Task checkpoint system**
- `task_init` / `task_step_done` / `task_step_fail` tools for multi-step task tracking
- `/tasks` and `/resume <task-id>` REPL commands for recovery after interruptions
- System prompt instructs the agent to use task tracking for any 3+ step task

**Context compression**
- Auto-triggers when estimated token count exceeds `context_limit`
- Summarises the middle portion of the conversation with a single LLM call
- Keeps the system prompt and the most recent `context_keep_recent` messages verbatim

**Debug toggle**
- `/debug` REPL command: enables full tool argument printing, untruncated results, and per-turn token count

**Browser timeout control**
- `browser_timeout` config option; all Playwright operations share a single configurable timeout

**Daily briefing tool**
- `daily_briefing(sections, open_browser)` — full HTML news report in one agent call
- LLM called with `response_format=json_object` for reliable structured output
- `<think>…</think>` stripping for Qwen reasoning models
- Per-source feed cap ensures all sources get representation (prevents one prolific RSS from dominating)
- Summary validation rejects degenerate LLM output (pure-numeric strings, too-short text)
- History-based dedup: articles seen in the past 7 days are skipped
- Section-level error isolation: one fetch failure never blocks the rest

**Data directory restructure**
- All wisp data unified under `~/wisp/` (sessions, tasks, workspace, future memory)

### v0.3 — Security Controls & Stability
- **Workspace sandbox**: all agent file outputs confined to `~/wisp/workspace`; writes/deletes outside blocked at tool level
- **Bash risk scanner**: regex detection of destructive commands (`rm -rf`, `sudo rm`, `dd`, `mkfs`, `chmod 777`) and out-of-workspace shell redirects
- **asyncio fix**: lazy-loaded Playwright + `PromptSession(in_thread=True)`
- **`find_files` path fix**: handles `~`, `$VAR`, and glob patterns correctly
- **Config additions**: `workspace`, `strict_workspace`

### v0.2 — Mac Automation & Tool Expansion
- Added 14 new tools across 4 categories (mac system, file ops, network, browser)
- Refactored tool definitions into `tools.py`
- Browser automation via Playwright (Chromium)
- File search powered by macOS Spotlight (`mdfind`)
- Safe file deletion (moves to Trash via Finder)

### v0.1 — Core Agent + Interactive Experience
- ReAct loop with streaming output (SSE)
- Multi-turn conversation with in-memory history
- Colored terminal output (ANSI) + ASCII banner
- Chinese input fix via `prompt_toolkit`
- macOS-aware system prompt (BSD command syntax)
- YAML config file
- Initial tools: `bash`, `read_file`, `write_file`, `done`, `osascript`, `clipboard_read/write`, `screenshot`, `open`

---

## Roadmap

- [ ] Persistent memory (`remember` / `recall` across sessions)
- [ ] Parallel tool execution (concurrent tool calls in a single turn)
- [ ] macOS system notifications on task completion

## License

MIT

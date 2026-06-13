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
python agent.py "Count how many files are on my Desktop"
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

## Tools (55 total)

| Category | Tools |
|----------|-------|
| Core | `bash`, `read_file`, `write_file`, `done` |
| Mac system | `screenshot`, `osascript`, `clipboard_read/write`, `open`, `list_apps`, `focus_app`, `quit_app` |
| File ops | `find_files`, `move_file`, `copy_file`, `delete_file` |
| Network | `http_get`, `http_post` |
| Browser | `browser_open`, `browser_click`, `browser_type`, `browser_get_text`, `browser_screenshot`, `browser_close`, `browser_wait`, `browser_select`, `browser_scroll`, `browser_snapshot` |
| Travel | `flight_search`, `hotel_search` |
| Reminders | `reminders_list`, `reminders_add`, `reminders_complete`, `reminders_delete` |
| Calendar | `calendar_list`, `calendar_add`, `calendar_delete` |
| Notes | `notes_list`, `notes_read`, `notes_create`, `notes_append`, `notes_delete` |
| Mail | `mail_accounts`, `mail_list`, `mail_read`, `mail_send`, `mail_delete`, `mail_junk`, `mail_move`, `mail_move_all` |
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
▶ Generate today's briefing
```

**Sections covered** (defined in `briefing.yaml`, fully customisable):
- 🌍 World & Politics — Reuters, AP, BBC
- 💡 ICT Research — Nature, IEEE, MIT Technology Review, DeepMind, HuggingFace, Ars Technica
- 🏢 Tech Industry — The Verge, Wired, Ars Technica, MIT TR
- 🇮🇪 Ireland — RTÉ, Irish Times, Irish Independent, The Journal
- 🇨🇳 China — BBC, AP

**How it works:** RSS/page sources → per-source cap + keyword filtering → LLM translate + summarise → HTML report saved to `~/wisp/workspace/daily-report/`

**Adding a new section:** add a block to `briefing.yaml` — no Python changes needed.

---

## Development Log

### v1.0 — Small-model reliability & EventKit performance

Focused on running stably on a small (~9B) local model and removing the
multi-second / timeout-prone macOS queries.

**Crash-safe tool loop & data-driven dispatch**
- The turn loop no longer dies on bad model output. Malformed/truncated JSON
  arguments and unknown tools are caught and fed back to the model as a tool
  result so it can self-correct, instead of raising and killing the REPL.
- `dispatch` is data-driven: it resolves `tool_<name>`, validates required
  args from the schema (missing arg → friendly error, not `KeyError`), and
  passes args by keyword filtered to the function signature. One registry
  instead of a 60-branch manual match.

**Trim the tool menu for small models**
- Config-driven tool groups: `disabled_tool_groups` in `config.yaml` drops
  whole categories (e.g. `browser`, `travel`) from the schema sent to the
  model — fewer tools = better selection accuracy and far less prompt token
  overhead, with no extra routing call.
- Leaner, group-aware system prompt; over-long tool descriptions trimmed.

**EventKit fast paths for Calendar & Reminders**
- Calendar.app / Reminders.app AppleScript queries are iCloud-bound and
  catastrophically slow (calendar_list took 92s; reminders_add ~12s). Rewrote
  the hot paths on EventKit's indexed predicates via JXA, each with an
  automatic AppleScript fallback when EventKit can't see the account stores.
- `calendar_list` 92s → ~0.1s, `reminders_list` → ~0.2s,
  `reminders_add/complete/delete` ~12s → ~0.2s, `calendar_delete` → ~0.08s.
- Same semantics preserved (locale list/calendar names, date-range filters,
  not-found / no-match errors); AppleScript fallback keeps the slow-calendar
  skip list for the rare un-indexed case.

### v0.9 — Mail Management & Context Auto-Compression

**Mail tools (new)** — full macOS Mail.app integration over AppleScript, working
across multiple accounts (Gmail, Hotmail/Outlook, iCloud) in mixed languages:
- `mail_accounts()` — list configured accounts
- `mail_list(account, mailbox, limit, unread_only)` — list messages; each line
  ends with `id:<message-id>` for reliable follow-up actions
- `mail_read(message_id | subject, …)` — read full body
- `mail_delete(message_id | subject, …)` — move to Trash (recoverable)
- `mail_junk(message_id | subject, …)` — mark as spam (move to Junk)
- `mail_move(message_id | subject, target_mailbox, …)` — move with semantic
  target aliases (`junk`/`trash`/`archive`); refuses misfiling into Drafts/Sent
- `mail_move_all(source_mailbox, target_mailbox, account)` — bulk-move a whole
  folder; clears an un-emptyable IMAP Trash by moving it into Junk

**AppleScript robustness (macOS 26)**
- `inbox of account` is broken even for `imap account` subtypes, and app-level
  `every mailbox` only returns system folders. All tools now traverse
  `every account → mailboxes of acc` instead.
- **Locale folder resolution** — `INBOX`/`Sent`/`Junk`/`Trash`/`Archive`/`Drafts`
  resolve to each account's actual folder whether named in English or Chinese
  (收件箱/已发送邮件/垃圾邮件/已删除邮件/存档/草稿); custom folders match literally.
- **Locate by message-id** — operations target `whose message id is …` instead
  of brittle subject-substring matching (a stray space or emoji used to break it).
- Never holds a mailbox specifier across a move (it invalidates after the
  mutation → error -1728); source/target are re-resolved inline.

**Context auto-compression (fix)**
- Compression ran only once per task, before the turn loop — so long tool-heavy
  loops (e.g. processing many emails) grew context unbounded until the model
  limit was hit. Now checked every turn.
- Compressed output could break the API: a mid-list `system` summary violates
  strict chat templates (→ 400), and the summary boundary could orphan tool
  results. Summary is now a `user`-role message, and `_sanitize_tool_pairing`
  guarantees every `tool` message has a declaring assistant and every
  `tool_calls` entry has a reply.

**Other tool improvements**
- `reminders_list` — `due_before`/range filter, clearer output; new
  `reminders_complete` / `reminders_delete`
- `calendar_delete`, `notes_delete`
- `flight_search` — resolves city names / city codes to airport codes, with
  dynamic KGMID lookup so multi-airport cities are covered

### v0.8 — Travel via SerpAPI, Terminal Fixes

**Travel search rewritten on SerpAPI**
- `flight_search` and `hotel_search` now use SerpAPI's structured-JSON access
  to Google Flights / Google Hotels — no more browser scraping
- Previous Playwright-based approach was fragile: depended on DOM placeholders,
  modal-dialog focus behaviour, tfs protobuf encoding, and the search button
  having to be clicked after URL navigation. Any UI change broke it.
- New approach: one HTTPS call returns clean JSON; output is a formatted
  table with airline, times, duration, stops, price (and for hotels: rating,
  reviews, hotel class, amenities, per-night & total price)
- Net code change: -74 lines in `tools.py`; +1 config field (`serpapi.api_key`)
- Free tier: 100 searches/month shared across flights + hotels
  ([signup](https://serpapi.com/users/sign_up))
- Removed ~250 lines of dead helpers: `_ensure_google_consent`,
  `_flights_fill_airport`, `_flights_fill_date`, `_flights_patch_tfs_dates`,
  `_browser_dismiss_consent`

**Terminal restore on REPL exit**
- `/exit`, Ctrl-C, and unhandled exceptions all now restore the terminal
  exactly to its pre-REPL state via `termios.tcgetattr` + `tcsetattr`
- Replaces the previous `stty sane` approximation which sometimes left
  the terminal in raw mode (no echo, ^[[A on arrows)
- Wrapped REPL loop in `try/finally` so cleanup runs unconditionally

**Bug fixes**
- Bash risk scanner no longer false-positives on `format=` in URL query strings

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

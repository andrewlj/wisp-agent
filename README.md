# wisp-agent

A lightweight local AI agent that connects to any OpenAI-compatible local LLM server. No cloud API dependencies — everything runs on your machine.

## Features

- **ReAct loop** — LLM thinks, calls tools, observes results, repeats until done
- **Streaming output** — responses print token by token in real time
- **Multi-turn conversation** — full session history kept in memory
- **Colored terminal** — tool calls, results, and final output visually distinct
- **macOS-aware** — system prompt tuned for BSD/macOS command syntax

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
```

**2. Configure**

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml`:

```yaml
server:
  base_url: "http://127.0.0.1:8000/v1/chat/completions"
  api_key: "your-api-key"   # set in your mlx-lm server config

model:
  name: "Qwen3.5-9B-MLX-8bit"   # match the model loaded by your server
  max_tokens: 4096

agent:
  max_turns: 20
  bash_timeout: 30
```

**3. Start your local LLM server**

```bash
mlx_lm.server --model <your-model-path> --port 8000
```

**4. Run**

```bash
# Interactive mode (multi-turn)
python agent.py

# One-shot mode
python agent.py "帮我统计桌面上有多少个文件"
```

## Interactive commands

| Command | Action |
|---------|--------|
| `reset` | Clear conversation history |
| `exit` / `quit` | Exit the agent |
| ↑ / ↓ | Browse input history |

## Tools (23 total)

| Category | Tools |
|----------|-------|
| Core | `bash`, `read_file`, `write_file`, `done` |
| Mac system | `screenshot`, `osascript`, `clipboard_read/write`, `open`, `list_apps`, `focus_app`, `quit_app` |
| File ops | `find_files`, `move_file`, `copy_file`, `delete_file` |
| Network | `http_get`, `http_post` |
| Browser | `browser_open`, `browser_click`, `browser_type`, `browser_get_text`, `browser_screenshot`, `browser_close` |

## Project structure

```
wisp-agent/
├── agent.py              # Main loop, API, REPL
├── tools.py              # All tool schemas and implementations
├── config.yaml           # Your local config (git-ignored)
├── config.example.yaml   # Config template
├── requirements.txt
└── LICENSE
```

## Development Log

### v0.3 — Security Controls & Stability
- **Workspace sandbox**: all agent file outputs (write, screenshot, download) are confined to `~/wisp-workspace`; writes and deletes outside are blocked at the tool level
- **Bash risk scanner**: regex-based detection of destructive commands (`rm -rf`, `sudo rm`, `dd`, `mkfs`, `chmod 777`) and shell redirects (`>`, `>>`, `tee`, `cp`, `mv`) targeting paths outside the workspace
- **asyncio fix**: lazy-loaded Playwright inside `_get_page()` + `PromptSession(in_thread=True)` to prevent event loop conflicts
- **`find_files` path fix**: now expands `~` and `$VAR` in directory arguments, and handles glob patterns like `*.png` correctly
- **Config additions**: `workspace` and `strict_workspace` options in `config.yaml`

### v0.2 — Mac Automation & Tool Expansion
- Added 14 new tools across 4 categories (mac system, file ops, network, browser)
- Refactored tool definitions into `tools.py`, keeping `agent.py` focused on the main loop
- Browser automation via Playwright (Chromium)
- File search powered by macOS Spotlight (`mdfind`)
- Safe file deletion (moves to Trash via Finder)

### v0.1 — Core Agent + Interactive Experience
- ReAct loop with streaming output (SSE)
- Multi-turn conversation with in-memory history
- Colored terminal output (ANSI)
- Chinese input fix via `prompt_toolkit`
- macOS-aware system prompt (BSD command syntax)
- YAML config file (API key, model, params externalized)
- Initial tool set: `bash`, `read_file`, `write_file`, `done`
- Mac tools: `osascript`, `clipboard_read/write`, `screenshot`, `open`

## Roadmap

- [ ] Toolset dynamic loading (on-demand, reduce token overhead at scale)
- [ ] Session persistence (save/restore conversation history)
- [ ] Context compression for long sessions
- [ ] Vision support (screenshot analysis with VL models)
- [ ] Multi-agent support

## License

MIT

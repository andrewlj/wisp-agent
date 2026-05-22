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

## Tools

| Tool | Description |
|------|-------------|
| `bash` | Run any shell command |
| `read_file` | Read file contents |
| `write_file` | Write file contents |
| `done` | Signal task completion |

## Project structure

```
wisp-agent/
├── agent.py              # Main agent code
├── config.yaml           # Your local config (git-ignored)
├── config.example.yaml   # Config template
├── requirements.txt
└── LICENSE
```

## Roadmap

- [ ] macOS automation tools (osascript, clipboard, notifications)
- [ ] Session persistence (save/restore conversation history)
- [ ] Context compression for long sessions
- [ ] Multi-agent support

## License

MIT

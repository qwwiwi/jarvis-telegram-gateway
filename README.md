# Jarvis Telegram Gateway

Universal Telegram gateway for autonomous Claude Code agents. Connect your AI agent to Telegram with voice transcription, session management, real-time progress display, and semantic memory.

## Features

- **Voice messages** -- [Groq](https://groq.com) Whisper transcription (Russian + English)
- **Media handling** -- photos, videos, documents, forwarded messages
- **Session persistence** -- multi-turn conversations via `claude --resume`
- **Real-time progress** -- live status updates in Telegram (tool calls, subagents, plan)
- **Hot memory** -- rolling 72h journal, auto-trim
- **[OpenViking](https://github.com/volcengine/OpenViking) integration** -- semantic memory extraction (optional)
- **Producer-consumer architecture** -- non-blocking `/stop`, `/status` commands
- **Markdown to HTML** -- tables, code blocks, bold, links
- **Multi-agent support** -- run multiple agents from one gateway
- **Group chat support** -- @mention routing

## Quick Start

### 1. Prerequisites

```bash
# Python 3.11+
python3 --version

# Claude Code CLI installed and authenticated
claude --version

# Groq API key (for voice transcription)
# Get one at https://console.groq.com
```

### 2. Install

```bash
git clone https://github.com/qwwiwi/jarvis-telegram-gateway.git
cd jarvis-telegram-gateway
pip install -r requirements.txt
```

### 3. Configure

```bash
cp config.example.json config.json
```

Edit `config.json`:

```json
{
  "poll_interval_sec": 2,
  "allowlist_user_ids": [YOUR_TELEGRAM_USER_ID],
  "agents": {
    "jarvis": {
      "enabled": true,
      "telegram_bot_token": "YOUR_BOT_TOKEN",
      "workspace": "/path/to/your/.claude",
      "model": "sonnet",
      "timeout_sec": 120,
      "system_reminder": "You are an autonomous AI agent."
    }
  }
}
```

### 4. Create Telegram Bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram
2. Send `/newbot`, follow instructions
3. Copy the bot token to `config.json`

### 5. Get your Telegram User ID

Send any message to [@userinfobot](https://t.me/userinfobot) -- it will reply with your ID.

### 6. Run

```bash
python3 gateway.py
```

### 7. (Optional) Run as systemd service

```bash
cp jarvis-gateway.service /etc/systemd/system/
# Edit the service file: set User, WorkingDirectory, paths
sudo systemctl daemon-reload
sudo systemctl enable --now jarvis-gateway
```

## Configuration

### config.json

| Field | Type | Description |
|-------|------|-------------|
| `poll_interval_sec` | int | Telegram polling interval (default: 2) |
| `allowlist_user_ids` | int[] | Telegram user IDs allowed to use the bot |
| `agents` | object | Agent configurations (see below) |

### Agent config

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | true | Enable/disable agent |
| `telegram_bot_token` | string | required | Telegram Bot API token |
| `telegram_bot_token_file` | string | -- | Alternative: read token from file |
| `workspace` | string | required | Path to agent's `.claude` directory |
| `model` | string | "sonnet" | Claude model alias |
| `timeout_sec` | int | 120 | Idle timeout before killing subprocess |
| `system_reminder` | string | "" | System prompt injected into each turn |
| `groq_api_key` | string | -- | Groq API key for voice transcription |
| `groq_api_key_file` | string | -- | Alternative: read key from file |
| `openviking_url` | string | -- | OpenViking API URL (optional) |
| `openviking_key_file` | string | -- | OpenViking API key file (optional) |

### Environment variables (alternative to config)

```bash
export GATEWAY_CONFIG=/path/to/config.json
export GROQ_API_KEY=YOUR_GROQ_API_KEY
export TELEGRAM_BOT_TOKEN=123456:ABC...
```

## Commands

| Command | Description |
|---------|-------------|
| `/status` | Show session info, memory sizes |
| `/reset` | Save memory and start new session |
| `/reset force` | Force-reset without saving |
| `/stop` | Kill running Claude process |
| `/compact` | Manually compact hot memory |
| `/help` | Show command list |

## Architecture

```
Telegram
    |
    v
[Polling Producer]  ───>  /stop, /status (instant)
    |
    v
[Message Queue]  (per agent, serial)
    |
    v
[Message Consumer]
    |
    ├── classify source (text/voice/photo/forwarded)
    ├── download & transcribe media (Groq Whisper)
    ├── invoke claude -p --resume <session_id>
    ├── stream progress (edit status message)
    ├── send response (markdown -> HTML)
    ├── append to hot memory (rolling 72h)
    └── push to OpenViking (semantic, optional)
```

### Thread model

```
main (blocks)
├── producer-agent1 (polls Telegram, handles OOB commands)
├── consumer-agent1 (processes messages serially)
├── producer-agent2
└── consumer-agent2
```

## Agent Workspace Structure

The gateway expects this workspace layout:

```
~/.claude-lab/agent-name/.claude/
├── CLAUDE.md              # Agent identity (SOUL)
├── core/
│   ├── hot/recent.md      # Rolling 72h journal (gateway writes here)
│   ├── warm/decisions.md  # 14-day decisions
│   └── MEMORY.md          # Cold archive
├── tools/TOOLS.md         # Available tools/servers
└── skills/                # Agent skills
```

## Multi-agent Setup

Run multiple agents from one gateway:

```json
{
  "agents": {
    "jarvis": {
      "enabled": true,
      "telegram_bot_token": "TOKEN_1",
      "workspace": "/home/user/.claude-lab/jarvis/.claude"
    },
    "assistant": {
      "enabled": true,
      "telegram_bot_token": "TOKEN_2",
      "workspace": "/home/user/.claude-lab/assistant/.claude"
    }
  }
}
```

Each agent gets its own Telegram bot, session storage, and memory.

## Voice Transcription

Voice messages are transcribed via [Groq](https://groq.com) Whisper API ([console](https://console.groq.com)):

- Model: `whisper-large-v3-turbo` (fast, accurate)
- Languages: Russian, English (auto-detect)
- Cost: ~$0.01 per minute of audio

## License

MIT

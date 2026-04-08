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

## Complete Data Flow

End-to-end path from operator message to memory persistence:

```
OPERATOR sends message (Telegram)
    |
    v
1. GATEWAY receives (long-polling, poll_interval_sec)
    |
    v
2. CLASSIFY source tag:
    |  - own_text:       operator typed directly
    |  - own_voice:      operator sent voice (.ogg)
    |  - forwarded:      operator forwarded from another chat
    |  - external_media: photo/video/document (not voice)
    |
    v
3. DOWNLOAD media (if present)
    |  - Size limit: 20 MB
    |  - Supported: .ogg (voice), .jpg/.png (photo), .mp4 (video), .pdf/.txt (documents)
    |
    v
4. TRANSCRIBE voice (if own_voice)
    |  - API: Groq Whisper (whisper-large-v3-turbo)
    |  - If Groq fails: message still processed, marked "[transcription failed]"
    |  - Languages: Russian, English (auto-detect)
    |
    v
5. LAUNCH Claude Code
    |  - Command: claude -p --resume <session_id>
    |  - Session ID stored in: state/sid-{agent}-{chat}.txt
    |  - If no session file: new session (claude -p --session-id <uuid>)
    |  - Context loaded: CLAUDE.md + @includes (AGENTS, USER, rules, TOOLS, WARM, HOT)
    |  - Model: configured per agent (default: sonnet)
    |
    v
6. STREAM progress to Telegram
    |  - Real-time status message: planning, running tools, spawning subagents
    |  - Updated via Telegram editMessageText
    |
    v
7. GET response from Claude Code
    |
    v
8. MEMORY WRITE (parallel, non-blocking)
    |
    |  A. HOT memory: append to core/hot/recent.md
    |     - ALWAYS (all source tags)
    |     - fcntl.LOCK_EX for concurrent safety
    |     - Format: ### YYYY-MM-DD HH:MM [source_tag]
    |     - Snippet: 200 chars user + 200 chars agent
    |     - Emergency trim: if >20KB, keep last 600 lines
    |
    |  B. OpenViking: push to semantic memory (FILTERED)
    |     - own_text:           YES (extract preferences, decisions)
    |     - own_voice:          YES (extract preferences, decisions)
    |     - forwarded:          YES (with anti-pollution guard)
    |     - external_media:     NO  (avoids preference pollution)
    |     - transcription_failed: NO (avoids garbage)
    |     - Fire-and-forget: ThreadPoolExecutor(max_workers=2)
    |
    v
9. REPLY to operator
    - markdown -> HTML conversion (bold, code, tables, links)
    - Chunked at 4000 chars (Telegram limit)
    - Reply-to original message
```

Memory writes (step 8) never block the response -- they happen in parallel after Claude Code returns.

## Memory Compression

### Why compression matters

Without compression, HOT memory grows to **80KB+ per day**. At 150+ messages with ~500 bytes each, this is expected. The problem: 80KB of raw conversation logs = ~36,000 tokens, consuming 70% of startup context.

**Quality degrades** when context is bloated with raw logs. The agent spends attention on unstructured conversation history instead of identity, rules, and tools. An agent with 80KB of raw HOT performs noticeably worse at following instructions than one with 20KB of structured facts.

| Metric | Without compression | With compression |
|--------|--------------------|--------------------|
| HOT size (end of day) | 80 KB+ | 10-20 KB |
| Tokens consumed by HOT | ~36,000 | ~4,500-9,000 |
| Startup context used | ~70% | ~10-15% |
| Cost (Max subscription) | n/a | $0 |
| Agent instruction-following | Degraded | Optimal |

### Compression scripts (cron-based)

The gateway writes HOT memory continuously. Four cron scripts manage compression:

```
Gateway (every message) -> HOT (recent.md)
  |
  +-- Emergency trim (auto, >20KB, bash)
  |     Keeps last 600 lines, trims from top
  |
  +-- trim-hot.sh (cron 05:00 UTC, Sonnet)
  |     Entries >24h -> Sonnet summary -> WARM (decisions.md)
  |     >40 entries remaining -> oldest also compressed
  |     Fallback: bash (first 120 chars if Sonnet unavailable)
  |     Runs from /tmp to avoid loading CLAUDE.md (~35K tokens saved)
  |
  +-- compress-warm.sh (cron 06:00 UTC, Sonnet)
  |     WARM >10KB -> Sonnet re-compression by topic
  |     110 raw entries -> 15-20 key facts
  |     Skip if <10KB or <50 lines
  |
  +-- rotate-warm.sh (cron 04:30 UTC, bash)
  |     WARM entries >14 days -> COLD (MEMORY.md)
  |
  +-- memory-rotate.sh (cron 21:00 UTC, bash)
        COLD >5KB -> archive/YYYY-MM.md
```

### Recommended crontab

```crontab
# 1. Rotate WARM: move >14d entries to COLD (bash, no model)
30 4 * * * /path/to/scripts/rotate-warm.sh

# 2. Trim HOT: entries >24h -> Sonnet summary -> WARM
0 5 * * * /path/to/scripts/trim-hot.sh

# 3. Compress WARM: Sonnet re-compression by topic (>10KB only)
0 6 * * * /path/to/scripts/compress-warm.sh

# 4. Archive COLD: MEMORY.md >5KB -> archive/ (bash)
0 21 * * * /path/to/scripts/memory-rotate.sh
```

Order matters: rotate-warm first (clear old), then trim-hot (add new to WARM), then compress-warm (re-compress if needed).

Ready-to-use scripts: [public-architecture-claude-code/scripts/](https://github.com/qwwiwi/public-architecture-claude-code/tree/main/scripts)

### How Sonnet compression works

trim-hot.sh sends old HOT entries to Sonnet via `claude --model sonnet --print`:

```
INPUT (raw HOT entry, ~500 bytes):
  ### 2026-04-08 14:16 [own_voice]
  **Operator:** (voice message transcription)
  **Agent:** Fixed the data collection. Cloudflare was blocking
  requests. Added User-Agent header and increased timeout to 30s...

OUTPUT (Sonnet summary, 1 line):
  - 2026-04-08 14:16: Fixed data collection -- Cloudflare blocking, added User-Agent header
```

compress-warm.sh groups related entries by topic:

```
INPUT (110 entries):
  - 2026-04-08 12:38: Fixed critical bugs in code review
  - 2026-04-08 12:41: Content validation needed for Tone of Voice
  - 2026-04-08 12:44: All 4 stages pass, ToV validator works
  ...

OUTPUT (16 key facts):
  - CONTENT VALIDATOR: Tone of Voice validator configured, 4 stages pass
  - BIOME: Linter/formatter deployed, 0 errors, 3 non-critical warnings
  - BACKUPS: All backups fixed and tested, DO Spaces storage with tags
  ...
```

## OpenViking Integration

[OpenViking](https://github.com/volcengine/OpenViking) provides semantic memory -- an LLM that **extracts structured facts** from conversations and stores them as searchable embeddings.

### How data flows to OpenViking

```
Gateway processes message
    |
    +-- Source tag filter:
    |   own_text / own_voice / forwarded -> PUSH to OV
    |   external_media / transcription_failed -> SKIP
    |
    v
1. Create session:      POST /api/v1/sessions
2. Send user message:   POST /api/v1/sessions/{sid}/messages (role: user, max 3000 chars)
3. Send agent response:  POST /api/v1/sessions/{sid}/messages (role: assistant, max 3000 chars)
4. Extract memories:    POST /api/v1/sessions/{sid}/extract  (OV's LLM extracts facts)
5. Cleanup session:     DELETE /api/v1/sessions/{sid}
```

Each message includes metadata: `[chat:{telegram_chat_id} agent:{name} at {timestamp}]`

### Anti-pollution guards

For forwarded and external content, OV receives extraction hints that prevent false attribution:

- **Forwarded:** `"This content was FORWARDED from someone else. Do NOT extract as operator's own preferences. Only extract as events/entities about the third-party source."`
- **External media:** Not pushed to OV at all (HOT memory only)

### What OpenViking extracts

OV runs its own LLM to extract structured memories:
- Operator preferences and decisions
- Named entities (tools, projects, people)
- Action items and commitments
- Patterns and recurring topics

### Searching semantic memory

```bash
curl -X POST "http://localhost:1933/api/v1/search/find" \
  -H "X-API-Key: $KEY" \
  -H "X-OpenViking-Account: $ACCOUNT" \
  -H "X-OpenViking-User: $AGENT" \
  -d '{"query": "backup strategy", "limit": 10}'
```

### Setup

```bash
pip install openviking --upgrade
# Start OpenViking locally (default port 1933)
```

In `config.json`:

```json
{
  "agents": {
    "jarvis": {
      "openviking_url": "http://localhost:1933",
      "openviking_key_file": "/path/to/openviking.key"
    }
  }
}
```

## How It All Connects

Three components form the complete system:

```
┌─────────────────────────────────────────────────────────────┐
│                    OPERATOR (you)                           │
│                                                             │
│  Telegram app / Desktop / Phone                            │
└─────┬──────────────────────────────────┬────────────────────┘
      │ voice / text / photo             │ SSH terminal
      v                                  v
┌─────────────────────────┐  ┌──────────────────────────────┐
│  Jarvis Telegram Gateway │  │  Claude Code (interactive)   │
│  (this repo)             │  │  claude-code-telegram plugin │
│                          │  │  (RichardAtCT)               │
│  - Autonomous agent      │  │  - Interactive coding        │
│  - Voice + media         │  │  - Standard Claude Code CLI  │
│  - Session management    │  │  - Telegram as terminal      │
│  - HOT memory writes     │  │  - No memory writes          │
│  - OpenViking push       │  │                              │
│  - Real-time progress    │  │                              │
└─────────┬───────────────┘  └───────────────────────────────┘
          │
          v
┌──────────────────────────────────────────────────────────────┐
│  Claude Code CLI  (claude -p --resume <session>)            │
│  - Loads CLAUDE.md + @includes (IDENTITY + WARM + HOT)      │
│  - Runs in agent workspace directory                         │
│  - Uses configured model (opus/sonnet)                       │
└──────────────────────────────────────────────────────────────┘
          │                              │
          v                              v
┌───────────────────┐    ┌──────────────────────────────────┐
│  File Memory       │    │  OpenViking (semantic memory)    │
│                    │    │  localhost:1933                   │
│  HOT  (24h)       │    │                                   │
│  WARM (14d)       │    │  - LLM extraction per message     │
│  COLD (archive)   │    │  - Embeddings + search            │
│  rules (permanent) │    │  - Per-agent namespace            │
│                    │    │  - Anti-pollution guards           │
│  Cron compression: │    │                                   │
│  trim-hot.sh      │    │  Setup: pip install openviking    │
│  compress-warm.sh │    │                                   │
│  rotate-warm.sh   │    │                                   │
└───────────────────┘    └──────────────────────────────────┘
```

| Component | Purpose | Repo |
|-----------|---------|------|
| **Jarvis Telegram Gateway** | Autonomous agent via Telegram (voice, media, sessions, memory) | [this repo](https://github.com/qwwiwi/jarvis-telegram-gateway) |
| **claude-code-telegram** | Interactive Claude Code via Telegram (standard CLI over chat) | [RichardAtCT/claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram) |
| **Architecture docs** | Memory system, compression, hooks, skills, subagents | [public-architecture-claude-code](https://github.com/qwwiwi/public-architecture-claude-code) |
| **OpenViking** | Semantic memory extraction and search | [volcengine/OpenViking](https://github.com/volcengine/OpenViking) |

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

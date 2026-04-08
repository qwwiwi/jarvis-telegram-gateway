#!/usr/bin/env python3
"""
claude-telegram-gateway -- Universal Telegram <-> Claude Code router.

Routes Telegram messages to Claude Code CLI sessions per agent.
Each agent gets its own Telegram bot, Claude workspace, and session state.

Features:
- Producer-consumer architecture (OOB commands like /stop handled instantly)
- Markdown -> Telegram HTML conversion
- Voice/audio transcription via Groq Whisper
- Hot memory (rolling journal per agent)
- OpenViking semantic memory push (optional)
- Session management with --resume
- Media handling (photo, video, voice, document, sticker)
- Group chat support with @mention / name detection
- Real-time progress tracking (tool calls, subagent dispatches, TodoWrite plans)

Usage:
    1. Copy config.example.json -> config.json
    2. Fill in agent config (telegram token, workspace, etc.)
    3. python3 gateway.py

Config structure (config.json):
    {
        "allowlist_user_ids": [123456789],
        "agents": {
            "myagent": {
                "enabled": true,
                "workspace": "/path/to/agent/.claude",
                "model": "sonnet",
                "timeout_sec": 120,
                "telegram_bot_token": "123456:AABBccdd...",
                "telegram_bot_token_file": "/path/to/token.txt",
                "groq_api_key": "YOUR_GROQ_API_KEY",
                "groq_api_key_file": "/path/to/groq.key",
                "openviking_url": "http://127.0.0.1:1933",
                "openviking_key_file": "/path/to/ov.key",
                "openviking_account": "default",
                "agent_names": ["myagent", "agent"],
                "env": {"KEY": "value"}
            }
        }
    }

Token resolution order:
- "telegram_bot_token" (direct value in config) takes priority
- "telegram_bot_token_file" (path to file) used as fallback
Same pattern for groq_api_key / groq_api_key_file.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import fcntl as _fcntl
import random as _random
import re as _re
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor

import requests

import atexit as _atexit

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

# Bounded thread pool for OV pushes (prevents unbounded thread spawning on message bursts)
_OV_POOL = _ThreadPoolExecutor(max_workers=2, thread_name_prefix="ov-push")

# Active claude subprocesses per (agent, chat_id) for /stop command
_ACTIVE_PROCS: dict[tuple[str, int], Any] = {}

# Per-agent message queues for producer-consumer architecture
_MSG_QUEUES: dict[str, queue.Queue] = {}

# Shutdown event to gracefully stop all threads
_SHUTDOWN_EVENT = threading.Event()

# Out-of-band commands handled instantly by producer thread
_OOB_COMMANDS = frozenset({"/stop", "/cancel", "/status", "/reset"})


@_atexit.register
def _shutdown_ov_pool() -> None:
    """Graceful drain of pending OV pushes on gateway shutdown."""
    try:
        _OV_POOL.shutdown(wait=True, cancel_futures=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Paths -- relative to working directory (cwd where gateway.py is launched)
# ---------------------------------------------------------------------------

_BASE_DIR = Path.cwd()
LOG_PATH = _BASE_DIR / "gateway.log"
CONFIG_PATH = _BASE_DIR / "config.json"
STATE_DIR = _BASE_DIR / "state"
MEDIA_DIR = _BASE_DIR / "media-inbound"
STATE_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

# Legacy fallback for Groq key (checked after agent config)
GROQ_KEY_FILE = Path.home() / ".secrets" / "groq-api-key"

TG_MAX_FILE_MB = 20  # Telegram Bot API hard limit
MEDIA_EXTENSIONS = {
    "voice": ".ogg",
    "audio": "",  # keep original
    "video": ".mp4",
    "video_note": ".mp4",
    "photo": ".jpg",
    "document": "",  # keep original
    "sticker": ".webp",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("gateway")


# ---------------------------------------------------------------------------
# Placeholder -- generic, no per-agent pools
# ---------------------------------------------------------------------------

def _media_to_input_type(msg: dict, source_tag: str) -> str:
    """Map Telegram message to input_type for placeholder selection."""
    if source_tag == "forwarded":
        return "forwarded"
    if "voice" in msg or "video_note" in msg or "audio" in msg:
        return "voice"
    if "video" in msg:
        return "video"
    if "photo" in msg:
        return "photo"
    if "document" in msg:
        return "document"
    if "sticker" in msg:
        return "sticker"
    return "text"


def get_placeholder(agent: str, input_type: str = "text") -> str:
    """Pick a generic placeholder for any agent/input_type."""
    _GENERIC: dict[str, list[str]] = {
        "text": ["думаю", "обрабатываю", "анализирую", "работаю", "принял"],
        "voice": ["слушаю голосовое", "транскрибирую", "разбираю речь"],
        "video": ["смотрю видео", "анализирую видео"],
        "photo": ["рассматриваю изображение", "изучаю картинку"],
        "document": ["читаю документ", "изучаю файл"],
        "forwarded": ["изучаю пересланное", "разбираю материал"],
        "sticker": ["принял стикер"],
    }
    pool = _GENERIC.get(input_type) or _GENERIC["text"]
    return _random.choice(pool) + "..."


# ---------------------------------------------------------------------------
# HTML / Markdown utilities
# ---------------------------------------------------------------------------

# HTML parse error detection
_PARSE_ERR_RE = _re.compile(
    r"can't parse entities|parse entities|find end of the entity|unsupported start tag|unexpected end tag",
    _re.IGNORECASE,
)


def escape_html(text: str) -> str:
    """Escape 3 chars for Telegram HTML body. Order matters: & first."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_html_attr(text: str) -> str:
    return escape_html(text).replace('"', "&quot;")


def is_html_parse_error(resp_body: Any) -> bool:
    if not isinstance(resp_body, dict):
        return False
    desc = resp_body.get("description", "") or ""
    return bool(_PARSE_ERR_RE.search(desc))


# Markdown -> Telegram HTML converter
_MD_CODEBLOCK_RE = _re.compile(r"```([a-zA-Z0-9_+\-]*)\n?(.*?)```", _re.DOTALL)
_MD_INLINECODE_RE = _re.compile(r"`([^`\n]+?)`")
_MD_BOLD_RE = _re.compile(r"\*\*([^\*\n]+?)\*\*")
_MD_ITALIC_STAR_RE = _re.compile(r"(?<![\w\*])\*([^\*\n]+?)\*(?!\w)")
# Underscore italic: require whitespace/punctuation boundary to avoid breaking
# identifiers like config_file.py, snake_case, etc.
_MD_ITALIC_UND_RE = _re.compile(r"(?:(?<=\s)|(?<=^)|(?<=[.,;:!?\(\[]))_([^_\n]+?)_(?=\s|$|[.,;:!?\)\]])")
_MD_LINK_RE = _re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_HEADING_RE = _re.compile(r"^(#{1,6})\s+(.+)$", _re.MULTILINE)
_MD_STRIKE_RE = _re.compile(r"~~([^~\n]+?)~~")
_MD_TABLE_RE = _re.compile(
    r"(?:^[ \t]*\|.+\|[ \t]*\n)+"
    r"(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)?"
    r"(?:^[ \t]*\|.+\|[ \t]*\n)*",
    _re.MULTILINE,
)


def _md_table_to_pre(table_text: str) -> str:
    """Convert a markdown table block into a <pre> block with aligned columns."""
    lines = [ln.strip() for ln in table_text.strip().splitlines()]
    # Filter out separator rows (|---|---|)
    data_lines = [ln for ln in lines if not _re.match(r"^\|[\s\-:|]+\|$", ln)]
    if not data_lines:
        return table_text

    # Parse cells
    rows: list[list[str]] = []
    for ln in data_lines:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        rows.append(cells)

    if not rows:
        return table_text

    # Calculate max width per column
    num_cols = max(len(r) for r in rows)
    col_widths = [0] * num_cols
    for row in rows:
        for i, cell in enumerate(row):
            if i < num_cols:
                col_widths[i] = max(col_widths[i], len(cell))

    # Build aligned output
    out_lines: list[str] = []
    for row in rows:
        parts: list[str] = []
        for i in range(num_cols):
            cell = row[i] if i < len(row) else ""
            parts.append(cell.ljust(col_widths[i]))
        out_lines.append("  ".join(parts).rstrip())

    return "<pre>" + escape_html("\n".join(out_lines)) + "</pre>"


def markdown_to_telegram_html(text: str) -> str:
    """Convert common markdown to Telegram-supported HTML. Safe for already-HTML content."""
    if not text:
        return text

    # Step 1: extract fenced code blocks first (preserve content)
    placeholders: dict[str, str] = {}

    def _save_codeblock(m: _re.Match) -> str:
        lang = m.group(1) or ""
        code = m.group(2).rstrip("\n")
        escaped = escape_html(code)
        if lang:
            html = f'<pre><code class="language-{escape_html_attr(lang)}">{escaped}</code></pre>'
        else:
            html = f"<pre><code>{escaped}</code></pre>"
        key = f"\x00CB{len(placeholders)}\x00"
        placeholders[key] = html
        return key

    text = _MD_CODEBLOCK_RE.sub(_save_codeblock, text)

    # Step 1.5: convert markdown tables to <pre> blocks (before HTML escaping)
    def _save_table(m: _re.Match) -> str:
        html = _md_table_to_pre(m.group(0))
        key = f"\x00TB{len(placeholders)}\x00"
        placeholders[key] = html
        return key

    text = _MD_TABLE_RE.sub(_save_table, text)

    # Step 2: inline code
    def _save_inlinecode(m: _re.Match) -> str:
        code = m.group(1)
        html = f"<code>{escape_html(code)}</code>"
        key = f"\x00IC{len(placeholders)}\x00"
        placeholders[key] = html
        return key

    text = _MD_INLINECODE_RE.sub(_save_inlinecode, text)

    # Step 3: escape HTML special chars in remaining text
    # (but preserve already-present HTML tags the agent might have used)
    # We accept agent's <b>, <i>, <code>, <pre>, <a>, <s>, <tg-spoiler>, <blockquote>
    _SAFE_TAGS = ("b", "i", "s", "code", "pre", "a", "tg-spoiler", "blockquote", "u")
    _TAG_RE = _re.compile(
        r"</?(" + "|".join(_SAFE_TAGS) + r")(?:\s[^>]*)?>", _re.IGNORECASE
    )

    tag_placeholders: dict[str, str] = {}

    def _save_tag(m: _re.Match) -> str:
        key = f"\x00TG{len(tag_placeholders)}\x00"
        tag_placeholders[key] = m.group(0)
        return key

    text = _TAG_RE.sub(_save_tag, text)
    # Now escape
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Restore known tags
    for key, val in tag_placeholders.items():
        text = text.replace(key, val)

    # Step 4: markdown transformations
    text = _MD_HEADING_RE.sub(lambda m: f"<b>{m.group(2)}</b>", text)
    text = _MD_BOLD_RE.sub(r"<b>\1</b>", text)
    text = _MD_STRIKE_RE.sub(r"<s>\1</s>", text)
    text = _MD_ITALIC_STAR_RE.sub(r"<i>\1</i>", text)
    text = _MD_ITALIC_UND_RE.sub(r"<i>\1</i>", text)
    text = _MD_LINK_RE.sub(
        lambda m: f'<a href="{escape_html_attr(m.group(2))}">{m.group(1)}</a>', text
    )

    # Step 5: restore code placeholders
    for key, val in placeholders.items():
        text = text.replace(key, val)

    return text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def expand(p: str) -> str:
    return os.path.expanduser(p)


def _resolve_token(cfg: dict, direct_key: str, file_key: str) -> str | None:
    """Resolve a secret from config: direct value first, then file fallback."""
    val = cfg.get(direct_key)
    if val:
        return val.strip()
    fpath = cfg.get(file_key)
    if fpath:
        p = Path(expand(fpath))
        if p.exists():
            return p.read_text().strip()
    return None


def _resolve_telegram_token(cfg: dict) -> str:
    """Resolve Telegram bot token from agent config. Raises if not found."""
    token = _resolve_token(cfg, "telegram_bot_token", "telegram_bot_token_file")
    if not token:
        raise ValueError(
            "No telegram token: set 'telegram_bot_token' or 'telegram_bot_token_file' in agent config"
        )
    return token


def _resolve_groq_key(cfg: dict) -> str | None:
    """Resolve Groq API key: agent config first, then global fallback file."""
    key = _resolve_token(cfg, "groq_api_key", "groq_api_key_file")
    if key:
        return key
    # Global fallback
    if GROQ_KEY_FILE.exists():
        return GROQ_KEY_FILE.read_text().strip()
    return None


# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------

def tg_api(token: str, method: str, retry: int = 2, **params: Any) -> dict:
    """Telegram API call with retry on 429/5xx/network errors."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    last_exc: Any = None
    for attempt in range(retry + 1):
        try:
            r = requests.post(url, json=params, timeout=30)
            if r.status_code == 429:
                # Rate limit -- honor retry_after if present
                wait = r.json().get("parameters", {}).get("retry_after", 1)
                time.sleep(min(wait, 30))
                continue
            if r.status_code >= 500:
                last_exc = Exception(f"telegram {r.status_code}: {r.text[:200]}")
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_exc = e
            if attempt < retry:
                time.sleep(1 + attempt)
                continue
            raise
    if last_exc:
        raise last_exc
    return {}


def _send_one(token: str, chat_id: int, text: str, reply_to: int | None, parse_mode: str | None) -> None:
    """Send single message with HTML parse_mode, fallback to plain on parse error."""
    params: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    if parse_mode:
        params["parse_mode"] = parse_mode
    try:
        tg_api(token, "sendMessage", **params)
    except requests.HTTPError as e:
        body = None
        try:
            body = e.response.json()
        except Exception:
            pass
        if parse_mode and is_html_parse_error(body):
            # Retry without parse_mode (plain text)
            params.pop("parse_mode", None)
            tg_api(token, "sendMessage", **params)
            return
        raise


def send_message(
    token: str, chat_id: int, text: str, reply_to: int | None = None, html: bool = True
) -> None:
    """Send text to Telegram, chunking by 4000 chars at paragraph boundaries when possible."""
    # Convert markdown -> HTML (if HTML mode)
    if html:
        text = markdown_to_telegram_html(text)

    # Chunk by paragraphs (double newline), fallback to 4000-char slices
    limit = 4000
    if len(text) <= limit:
        chunks = [text]
    else:
        chunks = []
        paragraphs = text.split("\n\n")
        current = ""
        for p in paragraphs:
            if len(current) + len(p) + 2 <= limit:
                current = current + "\n\n" + p if current else p
            else:
                if current:
                    chunks.append(current)
                if len(p) > limit:
                    # Hard split long paragraph
                    for i in range(0, len(p), limit):
                        chunks.append(p[i : i + limit])
                    current = ""
                else:
                    current = p
        if current:
            chunks.append(current)
    parse_mode = "HTML" if html else None
    for chunk in chunks:
        _send_one(token, chat_id, chunk, reply_to, parse_mode)
        reply_to = None  # only first chunk is a reply


def send_chat_action(token: str, chat_id: int, action: str = "typing") -> None:
    try:
        tg_api(token, "sendChatAction", chat_id=chat_id, action=action)
    except Exception as e:
        log.warning(f"sendChatAction failed: {e}")


# ---------------------------------------------------------------------------
# Group chat: agent name detection
# ---------------------------------------------------------------------------

def _get_agent_names(agent: str, cfg: dict) -> list[str]:
    """Get agent name aliases from config, fallback to [agent]."""
    names = cfg.get("agent_names")
    if names and isinstance(names, list):
        return [n.lower() for n in names]
    return [agent.lower()]


def is_addressed_to_agent(agent: str, msg: dict, bot_username: str | None, cfg: dict | None = None) -> bool:
    """For group chats: check if message explicitly addresses this agent.

    Returns True if:
    - DM (private chat) -- always addressed
    - Group chat AND (@bot_username mentioned OR agent name appears in text)
    """
    chat_type = (msg.get("chat") or {}).get("type", "private")
    if chat_type == "private":
        return True  # DM -- always address

    # Group/supergroup -- need explicit mention or name
    text = (msg.get("text") or msg.get("caption") or "").lower()
    if not text:
        return False

    # Check @bot_username mention
    if bot_username:
        if f"@{bot_username.lower()}" in text:
            return True

    # Check agent name aliases
    names = _get_agent_names(agent, cfg or {})
    for name in names:
        if name in text:
            return True

    # Check reply-to-bot: if user replied to bot's message
    reply_to = msg.get("reply_to_message") or {}
    reply_from = (reply_to.get("from") or {})
    if reply_from.get("is_bot") and reply_from.get("username", "").lower() == (bot_username or "").lower():
        return True

    return False


# ---------------------------------------------------------------------------
# Message classification
# ---------------------------------------------------------------------------

def classify_source(msg: dict) -> tuple[str, str]:
    """Classify message source. Returns (source_tag, human_label).

    Tags:
    - own_text: user's own typed message (includes caption under photo/video/doc)
    - own_voice: user's voice/video_note (direct speech)
    - forwarded: forwarded from someone else
    - external_media: bare media (photo/video/document) without caption text
    """
    # Forward detection first -- forwarded content is always external source
    if (msg.get("forward_from") or msg.get("forward_from_chat")
            or msg.get("forward_sender_name") or msg.get("forward_origin")):
        origin = msg.get("forward_origin") or {}
        name = (
            (msg.get("forward_from") or {}).get("first_name")
            or (msg.get("forward_from_chat") or {}).get("title")
            or msg.get("forward_sender_name")
            or origin.get("sender_user_name")
            or "unknown"
        )
        return ("forwarded", f"forwarded from: {name}")
    # Voice/video_note = user speaking directly
    if "voice" in msg or "video_note" in msg:
        return ("own_voice", "user voice message")
    # Media with caption = user's own text with attachment
    has_media = any(k in msg for k in ("audio", "video", "photo", "document", "sticker"))
    has_caption = bool(msg.get("caption"))
    if has_media and has_caption:
        return ("own_text", "user text with attachment")
    if has_media:
        return ("external_media", "media attachment from user")
    return ("own_text", "user text")


# ---------------------------------------------------------------------------
# Media handling
# ---------------------------------------------------------------------------

def resolve_media_ref(msg: dict) -> dict | None:
    """Detect media in Telegram message, return {type, file_id, file_name, mime_type, file_size}."""
    if "photo" in msg and msg["photo"]:
        # photo is array of sizes; take largest
        largest = msg["photo"][-1]
        return {
            "type": "photo",
            "file_id": largest.get("file_id"),
            "file_name": None,
            "mime_type": "image/jpeg",
            "file_size": largest.get("file_size", 0),
        }
    for t in ("voice", "audio", "video", "video_note", "document", "sticker"):
        if t in msg:
            obj = msg[t]
            return {
                "type": t,
                "file_id": obj.get("file_id"),
                "file_name": obj.get("file_name"),
                "mime_type": obj.get("mime_type"),
                "file_size": obj.get("file_size", 0),
            }
    return None


def download_telegram_file(token: str, file_id: str, media_type: str, file_name: str | None) -> Path | None:
    """Download file from Telegram Bot API, save to MEDIA_DIR. Returns path or None."""
    try:
        r = tg_api(token, "getFile", file_id=file_id)
        file_info = r.get("result") or {}
        file_path = file_info.get("file_path")
        file_size = file_info.get("file_size", 0)
        if not file_path:
            log.warning(f"getFile: no file_path for {file_id}")
            return None
        if file_size > TG_MAX_FILE_MB * 1024 * 1024:
            log.warning(f"file too big: {file_size} bytes > {TG_MAX_FILE_MB}MB")
            return None
        # Derive local filename
        # Force known extensions for types where Groq/processors are strict
        if media_type == "voice":
            ext = ".ogg"  # Telegram gives .oga but Groq only accepts .ogg/.opus
        elif media_type == "video_note":
            ext = ".mp4"
        else:
            ext = Path(file_path).suffix or MEDIA_EXTENSIONS.get(media_type, "")
        if file_name:
            sanitized = _re.sub(r"[^\w\-. ]", "_", Path(file_name).stem)[:40]
            local_name = f"{sanitized}---{uuid.uuid4()}{ext}"
        else:
            local_name = f"{uuid.uuid4()}{ext}"
        local_path = MEDIA_DIR / local_name
        # Download
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        resp = requests.get(url, timeout=60, stream=True)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        log.info(f"downloaded {media_type} -> {local_path.name} ({file_size} bytes)")
        return local_path
    except Exception as e:
        log.warning(f"download_telegram_file failed: {e}")
        return None


def transcribe_audio(path: Path, agent_cfg: dict | None = None, language: str = "ru") -> str | None:
    """Transcribe audio via Groq Whisper. Returns transcript text or None."""
    key = _resolve_groq_key(agent_cfg or {})
    if not key:
        log.warning("GROQ key missing, skip transcription")
        return None
    try:
        with open(path, "rb") as f:
            files = {"file": (path.name, f, "audio/ogg")}
            data = {
                "model": "whisper-large-v3-turbo",
                "response_format": "text",
                "language": language,
            }
            r = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files=files,
                data=data,
                timeout=60,
            )
        if r.status_code == 200:
            return r.text.strip()
        log.warning(f"groq transcribe HTTP {r.status_code}: {r.text[:200]}")
        return None
    except Exception as e:
        log.warning(f"groq transcribe failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Telegram message editing / deletion
# ---------------------------------------------------------------------------

def edit_message(token: str, chat_id: int, message_id: int, text: str, html: bool = True) -> None:
    """Edit Telegram message. Silent on 'message not modified'. HTML fallback to plain.
    Retry once on transient network errors (backoff 500ms).
    """
    text = text[:4000]
    params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if html:
        params["parse_mode"] = "HTML"

    def _try(attempt: int = 0) -> bool:
        try:
            tg_api(token, "editMessageText", **params)
            return True
        except requests.HTTPError as e:
            body = None
            try:
                body = e.response.json()
            except Exception:
                pass
            desc = (body.get("description", "") if isinstance(body, dict) else "").lower()
            if "message is not modified" in desc:
                return True
            if html and is_html_parse_error(body):
                params.pop("parse_mode", None)
                try:
                    tg_api(token, "editMessageText", **params)
                    return True
                except Exception as e2:
                    log.warning(f"editMessageText plain fallback failed: {e2}")
                    return False
            log.warning(f"editMessageText HTTP {e}")
            return False
        except requests.RequestException as e:
            # Network/timeout error -- retry once
            if attempt < 1:
                time.sleep(0.5)
                return _try(attempt + 1)
            log.warning(f"editMessageText network error after retry: {e}")
            return False
        except Exception as e:
            log.warning(f"editMessageText failed: {e}")
            return False

    _try()


def delete_message(token: str, chat_id: int, message_id: int) -> None:
    try:
        tg_api(token, "deleteMessage", chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Commands (/status, /reset, /new, /compact, /stop, /help)
# ---------------------------------------------------------------------------

def _get_workspace(agent: str, cfg: dict) -> str:
    """Get workspace path from agent config."""
    return expand(cfg["workspace"])


def handle_command(token: str, chat_id: int, agent: str, cmd: str, args: str, cfg: dict | None = None) -> bool:
    """Handle /status /reset /new /help commands. Returns True if handled."""
    cfg = cfg or {}
    sid_file = STATE_DIR / f"sid-{agent}-{chat_id}.txt"
    first_file = STATE_DIR / f"sid-{agent}-{chat_id}.first"

    if cmd == "/status":
        workspace = _get_workspace(agent, cfg) if cfg.get("workspace") else None
        core_dir = Path(workspace) / "core" if workspace else None

        hot_kb = decisions_kb = memory_kb = rules_kb = 0.0
        if core_dir:
            hot_file = core_dir / "hot" / "recent.md"
            decisions_file = core_dir / "warm" / "decisions.md"
            memory_file = core_dir / "MEMORY.md"
            rules_file = core_dir / "rules.md"
            hot_kb = hot_file.stat().st_size / 1024 if hot_file.exists() else 0
            decisions_kb = decisions_file.stat().st_size / 1024 if decisions_file.exists() else 0
            memory_kb = memory_file.stat().st_size / 1024 if memory_file.exists() else 0
            rules_kb = rules_file.stat().st_size / 1024 if rules_file.exists() else 0

        if sid_file.exists():
            sid = sid_file.read_text().strip()
            age = time.time() - sid_file.stat().st_mtime
            age_str = f"{int(age/3600)}h {int((age%3600)/60)}m" if age > 3600 else f"{int(age/60)}m"
            text = (
                f"<b>session active</b>\n"
                f"id: <code>{sid[:8]}...</code>\n"
                f"age: {age_str}\n\n"
                f"<b>memory</b>\n"
                f"rules (permanent): {rules_kb:.1f} KB\n"
                f"warm (decisions 14d): {decisions_kb:.1f} KB\n"
                f"hot (recent 72h): {hot_kb:.1f} KB\n"
                f"cold (MEMORY.md): {memory_kb:.1f} KB"
            )
        else:
            text = (
                f"<b>no active session</b>\n\n"
                f"next message creates a new one\n\n"
                f"<b>memory</b>\n"
                f"rules: {rules_kb:.1f} KB\n"
                f"warm: {decisions_kb:.1f} KB\n"
                f"hot: {hot_kb:.1f} KB\n"
                f"cold: {memory_kb:.1f} KB"
            )
        try:
            tg_api(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception:
            pass
        return True

    if cmd in ("/reset", "/new"):
        force = args.strip().lower() == "force"
        if not sid_file.exists():
            try:
                tg_api(token, "sendMessage", chat_id=chat_id, text="<b>session already empty</b>", parse_mode="HTML")
            except Exception:
                pass
            return True

        old_sid = sid_file.read_text().strip()

        if force:
            sid_file.unlink(missing_ok=True)
            first_file.unlink(missing_ok=True)
            text = (
                f"<b>session reset (force)</b>\n\n"
                f"old: <code>{old_sid[:8]}...</code>\n"
                f"next message = new session"
            )
            try:
                tg_api(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")
            except Exception:
                pass
            return True

        # Async reset: notify, then run save in background, then complete reset
        try:
            ack = tg_api(token, "sendMessage", chat_id=chat_id,
                         text="<i>saving context to MEMORY.md...</i>", parse_mode="HTML")
            ack_msg_id = ack.get("result", {}).get("message_id")
        except Exception:
            ack_msg_id = None

        def _do_reset_save():
            save_prompt = (
                "SYSTEM: user requested reset. Before reset update core/MEMORY.md -- "
                "add the most important from current context (current focus, decisions, "
                "pending actions, user preferences). Use Edit tool. Reply briefly: what was saved."
            )
            import subprocess as _sp
            workspace = _get_workspace(agent, cfg)
            env = os.environ.copy()
            env["PATH"] = f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
            for k, v in (cfg.get("env") or {}).items():
                env[k] = v
            summary = "(not received)"
            try:
                r = _sp.run(
                    ["claude", "-p", save_prompt, "--model", "sonnet",
                     "--output-format", "text", "--permission-mode", "bypassPermissions",
                     "--resume", old_sid],
                    cwd=workspace, env=env, capture_output=True, text=True, timeout=90,
                )
                summary = (r.stdout or r.stderr or "").strip()[:300]
            except Exception as e:
                summary = f"(save error: {e})"
            # Delete session files AFTER save
            sid_file.unlink(missing_ok=True)
            first_file.unlink(missing_ok=True)
            # Notify user
            final_text = (
                f"<b>session reset</b>\n\n"
                f"old: <code>{old_sid[:8]}...</code>\n"
                f"saved: <i>{escape_html(summary)}</i>\n\n"
                f"next message = new session"
            )
            if ack_msg_id:
                edit_message(token, chat_id, ack_msg_id, final_text)
            else:
                try:
                    tg_api(token, "sendMessage", chat_id=chat_id, text=final_text, parse_mode="HTML")
                except Exception:
                    pass

        import threading as _th
        _th.Thread(target=_do_reset_save, daemon=True).start()
        return True

    if cmd == "/compact":
        if not sid_file.exists():
            try:
                tg_api(token, "sendMessage", chat_id=chat_id, text="<b>no session</b> -- nothing to compact", parse_mode="HTML")
            except Exception:
                pass
            return True

        sid = sid_file.read_text().strip()
        try:
            ack = tg_api(token, "sendMessage", chat_id=chat_id, text="<i>compacting hot -> warm/decisions.md...</i>", parse_mode="HTML")
            ack_msg_id = ack.get("result", {}).get("message_id")
        except Exception:
            ack_msg_id = None

        def _do_compact():
            today = time.strftime("%Y-%m-%d")
            compact_prompt = (
                f"SYSTEM: manual hot->warm compact.\n"
                f"1. Read core/hot/recent.md (Read tool).\n"
                f"2. Extract key facts from last 24h and ADD to beginning of core/warm/decisions.md:\n"
                f"## {today}\n- fact 1\n- fact 2\n\n"
                f"3. Trim hot/recent.md: keep last 48h.\n"
                f"Extract: new preferences, decisions, pending actions, patterns. "
                f"Skip duplicates.\n"
                f"Reply: 'compact: N added, hot trimmed'."
            )
            import subprocess as _sp
            workspace = _get_workspace(agent, cfg)
            env = os.environ.copy()
            env["PATH"] = f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
            for k, v in (cfg.get("env") or {}).items():
                env[k] = v
            reply = "(timeout)"
            try:
                r = _sp.run(
                    ["claude", "-p", compact_prompt, "--model", "sonnet",
                     "--output-format", "text", "--permission-mode", "bypassPermissions",
                     "--resume", sid],
                    cwd=workspace, env=env, capture_output=True, text=True, timeout=180,
                )
                reply = (r.stdout or r.stderr or "").strip()[:400]
            except Exception as e:
                reply = f"error: {str(e)[:200]}"
            final_text = f"<b>compact done</b>\n\n<i>{escape_html(reply)}</i>"
            if ack_msg_id:
                edit_message(token, chat_id, ack_msg_id, final_text)
            else:
                try:
                    tg_api(token, "sendMessage", chat_id=chat_id, text=final_text, parse_mode="HTML")
                except Exception:
                    pass

        import threading as _th
        _th.Thread(target=_do_compact, daemon=True).start()
        return True

    if cmd in ("/stop", "/cancel"):
        proc = _ACTIVE_PROCS.get((agent, chat_id))
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                time.sleep(0.5)
                if proc.poll() is None:
                    proc.kill()
                text = "<b>stopped</b>\n\n<i>agent interrupted current task</i>"
            except Exception as e:
                text = f"<b>stop error</b>: {escape_html(str(e)[:100])}"
        else:
            text = "<b>nothing to stop</b> -- agent is idle"
        try:
            tg_api(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception:
            pass
        return True

    if cmd == "/help":
        text = (
            "<b>gateway commands</b>\n\n"
            "<code>/stop</code> or <code>/cancel</code> -- stop current agent task\n"
            "<code>/status</code> -- session and memory status\n"
            "<code>/reset</code> -- reset session (saves important to MEMORY)\n"
            "<code>/reset force</code> -- reset without saving\n"
            "<code>/compact</code> -- manual memory compaction\n"
            "<code>/help</code> -- this help\n\n"
            "<i>auto-compact: daily 05:00 UTC</i>"
        )
        try:
            tg_api(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception:
            pass
        return True

    return False


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def session_id_for(agent: str, chat_id: int) -> str:
    """Stable UUID per (agent, chat). Persisted on disk for --resume."""
    f = STATE_DIR / f"sid-{agent}-{chat_id}.txt"
    if f.exists():
        return f.read_text().strip()
    sid = str(uuid.uuid4())
    f.write_text(sid)
    return sid


def read_latest_memory_section(agent: str, cfg: dict) -> str:
    """Read latest section from core/MEMORY.md for post-reset injection."""
    workspace = _get_workspace(agent, cfg)
    mem_file = Path(workspace) / "core" / "MEMORY.md"
    if not mem_file.exists():
        return ""
    try:
        content = mem_file.read_text()
        # Find all ## sections and return the last one
        import re as _r
        matches = list(_r.finditer(r'^## .+$', content, _r.MULTILINE))
        if not matches:
            return content[:2000]
        last_start = matches[-1].start()
        # Read until next section or EOF
        return content[last_start:][:2000]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------

def invoke_claude(
    agent: str, cfg: dict, chat_id: int, user_text: str
) -> tuple[str, int, int]:
    """Run claude -p --resume <sid> and return (response_text, duration_ms, success)."""
    sid = session_id_for(agent, chat_id)
    workspace = _get_workspace(agent, cfg)
    model = cfg.get("model", "sonnet")

    env = os.environ.copy()
    env["PATH"] = f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    for k, v in cfg.get("env", {}).items():
        env[k] = v

    # Use --resume if session exists, --session-id for first turn
    sid_file = STATE_DIR / f"sid-{agent}-{chat_id}.first"
    is_first = not sid_file.exists()

    # Post-reset: inject latest MEMORY.md section so agent knows what was saved
    if is_first:
        latest_memory = read_latest_memory_section(agent, cfg)
        if latest_memory:
            user_text = (
                f"[after reset, latest entry in MEMORY.md]:\n{latest_memory}\n\n"
                f"[current user message]:\n{user_text}"
            )

    cmd = [
        "claude",
        "-p",
        user_text,
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--input-format",
        "text",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
    ]
    if is_first:
        cmd.extend(["--session-id", sid])
    else:
        cmd.extend(["--resume", sid])

    timeout_sec = cfg.get("timeout_sec", 120)
    typing_cb = cfg.get("_typing_refresh_cb")
    status_cb = cfg.get("_status_update_cb")  # (status_text: str) -> None
    tracker = _TaskBoundaryTracker(status_cb) if status_cb else None
    t0 = time.time()
    last_activity = t0  # heartbeat: reset on every event from Claude
    last_typing = 0.0
    final_text = ""

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )
        # Register for /stop command
        _ACTIVE_PROCS[(agent, chat_id)] = proc

        # Use select on proc.stdout for line-by-line reading with timeout
        import select as _select
        stdout_fd = proc.stdout.fileno() if proc.stdout else None
        buffer = ""

        while True:
            # Heartbeat timeout: kill only if no events for timeout_sec
            now = time.time()
            idle = now - last_activity
            if idle > timeout_sec:
                proc.kill()
                proc.communicate()
                dur_ms = int((now - t0) * 1000)
                log.error(
                    f"claude idle timeout after {idle:.0f}s silence "
                    f"(total {dur_ms}ms, limit={timeout_sec}s)"
                )
                return (
                    f"[gateway error: claude idle {int(idle)}s -- no activity]",
                    dur_ms,
                    0,
                )

            # Refresh typing every 2s
            if time.time() - last_typing > 2:
                if typing_cb:
                    try:
                        typing_cb()
                    except Exception:
                        pass
                last_typing = time.time()

            # Read with timeout
            if stdout_fd is not None:
                ready, _, _ = _select.select([stdout_fd], [], [], 1.0)
                if ready:
                    line = proc.stdout.readline()
                    if not line:
                        # EOF -- process done
                        break
                    buffer += line
                    # Parse completed lines
                    while "\n" in buffer:
                        jline, buffer = buffer.split("\n", 1)
                        jline = jline.strip()
                        if not jline:
                            continue
                        try:
                            event = json.loads(jline)
                        except json.JSONDecodeError:
                            continue
                        last_activity = time.time()  # heartbeat: Claude is alive
                        _handle_stream_event(event, tracker)
                        # Capture final result text
                        if event.get("type") == "result":
                            final_text = event.get("result") or final_text
                else:
                    # No output yet -- check if process done
                    if proc.poll() is not None:
                        # Drain remaining
                        remaining = proc.stdout.read() if proc.stdout else ""
                        buffer += remaining
                        for jline in (buffer.strip().split("\n") if buffer.strip() else []):
                            jline = jline.strip()
                            if not jline:
                                continue
                            try:
                                event = json.loads(jline)
                                _handle_stream_event(event, tracker)
                                if event.get("type") == "result":
                                    final_text = event.get("result") or final_text
                            except json.JSONDecodeError:
                                pass
                        break

        proc.wait()
        dur_ms = int((time.time() - t0) * 1000)
        if proc.returncode != 0:
            stderr = proc.stderr.read() if proc.stderr else ""
            log.error(f"claude exit {proc.returncode}: {stderr[:500]}")
            return (f"[gateway error: claude exit {proc.returncode}]", dur_ms, 0)
        if is_first:
            sid_file.touch()
        return (final_text.strip(), dur_ms, 1)
    except Exception as e:
        dur_ms = int((time.time() - t0) * 1000)
        log.exception(f"claude invoke failed: {e}")
        return (f"[gateway error: {e}]", dur_ms, 0)
    finally:
        _ACTIVE_PROCS.pop((agent, chat_id), None)


# ---------------------------------------------------------------------------
# Progress tracking (subagent labels, tool tags, status tracker)
# ---------------------------------------------------------------------------

SUBAGENT_LABELS = {
    "researcher": "searching and verifying sources",
    "content-writer": "writing draft",
    "content-orchestrator": "preparing content",
    "firebase-auditor": "auditing data",
    "code-reviewer": "code review",
    "general-purpose": "running research",
    "Explore": "exploring structure",
    "Plan": "building plan",
    "statusline-setup": "setting up status",
    "claude-code-guide": "checking docs",
}

# ASCII tool tags for real-time tool call display (NO emoji)
TOOL_TAGS: dict[str, str] = {
    "Read": "[R]",
    "Write": "[W]",
    "Edit": "[W]",
    "MultiEdit": "[W]",
    "Bash": "[B]",
    "Grep": "[G]",
    "Glob": "[G]",
    "WebFetch": "[F]",
    "WebSearch": "[S]",
    "Agent": "[A]",
    "TodoWrite": "[T]",
}
_TOOL_TAG_DEFAULT = "[.]"


def _summarize_tool_input(name: str, tinput: dict) -> str:
    """Extract a short human-readable summary of tool invocation."""
    s = ""
    if name in ("Read", "Write", "Edit", "MultiEdit"):
        fp = tinput.get("file_path") or tinput.get("path") or ""
        parts = fp.replace("\\", "/").rsplit("/", 2)
        s = "/".join(parts[-2:]) if len(parts) >= 2 else fp
    elif name == "Bash":
        s = (tinput.get("command") or "")[:40]
    elif name == "Grep":
        s = tinput.get("pattern") or ""
        if s:
            s = f'"{s}"'
    elif name == "Glob":
        s = tinput.get("pattern") or ""
        if s:
            s = f'"{s}"'
    elif name == "Agent":
        s = tinput.get("subagent_type") or tinput.get("description") or ""
    elif name == "WebFetch":
        url = tinput.get("url") or ""
        try:
            from urllib.parse import urlparse
            s = urlparse(url).netloc or url[:40]
        except Exception:
            s = url[:40]
    elif name == "WebSearch":
        s = (tinput.get("query") or "")[:40]
    else:
        s = str(tinput)[:30] if tinput else ""
    return escape_html(s[:40])


def _mask_secrets(s: str) -> str:
    """Mask IPs, URLs, tokens, secret paths in status messages for Telegram."""
    import re
    # Mask IPv4 addresses: 1.2.3.4 -> 1.***.***4
    s = re.sub(
        r'\b(\d{1,3})\.\d{1,3}\.\d{1,3}\.(\d{1,3})\b',
        r'\1.***.***.\2', s,
    )
    # Mask secret paths: ~/.secrets/... or /secrets/...
    s = re.sub(r'(~/?\.\w+/)secrets/\S+', r'\1secrets/***', s)
    # Mask API tokens/keys (long alphanumeric strings 20+ chars)
    s = re.sub(r'\b([A-Za-z0-9_-]{4})[A-Za-z0-9_-]{16,}([A-Za-z0-9_-]{4})\b', r'\1***\2', s)
    # Mask bot tokens: 1234567890:AAxx... -> 123***:AA***
    s = re.sub(r'\b(\d{3})\d{7,}:(AA\w{2})\w+', r'\1***:\2***', s)
    # Mask Supabase project URLs
    def _mask_host(m: re.Match) -> str:
        host = m.group(0)
        parts = host.split('.')
        if len(parts[0]) > 8:
            parts[0] = parts[0][:4] + '*****' + parts[0][-4:]
        if len(parts) > 1 and len(parts[-2]) > 5:
            parts[-2] = parts[-2][:4] + '***'
        return '.'.join(parts)
    s = re.sub(r'[a-z0-9]{10,}\.supabase\.co', _mask_host, s)
    return s


def _code(s: str) -> str:
    """Wrap text in <code> tags, escaping content."""
    return f"<code>{escape_html(s)}</code>"


def _humanize_tool(tname: str, tinput: dict) -> str | None:
    """Convert Claude tool invocation into human-readable status line (HTML)."""
    if tname == "Agent":
        sub = tinput.get("subagent_type", "?")
        label = SUBAGENT_LABELS.get(sub, f"running {escape_html(sub)}")
        return f"<b>{escape_html(label)}</b>"
    if tname == "Bash":
        cmd = (tinput.get("command") or "").strip()
        if cmd.startswith("curl"):
            return "calling API"
        if cmd.startswith("git"):
            return "git command"
        if cmd.startswith(("cat ", "tail ", "head ", "ls ", "grep ")):
            return "reading files"
        return f"running: {_code(_mask_secrets(cmd[:60]))}"
    if tname == "Read":
        path = tinput.get("file_path") or ""
        name = path.split("/")[-1] if path else ""
        return f"reading {_code(name)}" if name else "reading file"
    if tname == "Write":
        path = tinput.get("file_path") or ""
        name = path.split("/")[-1] if path else ""
        return f"creating {_code(name)}" if name else "creating file"
    if tname == "Edit":
        path = tinput.get("file_path") or ""
        name = path.split("/")[-1] if path else ""
        return f"editing {_code(name)}" if name else "editing file"
    if tname == "Glob":
        p = tinput.get("pattern") or ""
        return f"searching files: {_code(p[:40])}" if p else "searching files"
    if tname == "Grep":
        p = tinput.get("pattern") or ""
        return f"searching code: {_code(p[:40])}" if p else "searching code"
    if tname == "WebFetch":
        url = tinput.get("url") or ""
        return f"fetching web: {_code(url[:60])}"
    if tname == "WebSearch":
        q = tinput.get("query") or ""
        return f"web search: <i>{escape_html(q[:60])}</i>"
    if tname == "TodoWrite":
        return None  # handled specially
    return None


def _format_todos(todos: list) -> str | None:
    """Format TodoWrite list as HTML checklist."""
    if not todos:
        return None
    lines = []
    for t in todos[:8]:
        status = t.get("status", "pending")
        content = escape_html((t.get("content") or t.get("subject") or "")[:60])
        if status == "completed":
            lines.append(f"  [x] <s>{content}</s>")
        elif status == "in_progress":
            lines.append(f"  [>] <b>{content}</b>")
        else:
            lines.append(f"  [ ] {content}")
    if len(todos) > 8:
        lines.append(f"  ... +{len(todos) - 8} more")
    return "<b>plan:</b>\n" + "\n".join(lines)


class _StatusTracker:
    """Tracks assistant stream events and emits structured progress."""
    def __init__(self, status_cb):
        self.status_cb = status_cb
        self.step_num = 0
        self.current_subagent = None  # set when inside Agent tool turn
        self.lines: list[str] = []

    def _emit(self, line: str) -> None:
        self.lines.append(line)
        if self.status_cb:
            self.status_cb("\n".join(self.lines))

    def handle_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "assistant":
            content = (event.get("message") or {}).get("content") or []
            for block in content:
                btype = block.get("type")
                if btype == "tool_use":
                    tname = block.get("name", "")
                    tinput = block.get("input") or {}
                    if tname == "TodoWrite":
                        todos = tinput.get("todos") or []
                        formatted = _format_todos(todos)
                        if formatted:
                            self._emit(formatted)
                        continue
                    line = _humanize_tool(tname, tinput)
                    if not line:
                        continue
                    if tname == "Agent":
                        sub = tinput.get("subagent_type", "?")
                        self.current_subagent = sub
                        self.step_num += 1
                        self._emit(f"<b>{self.step_num}.</b> {line}")
                    else:
                        if self.current_subagent:
                            self._emit(f"   <i>. {line}</i>")
                        else:
                            self.step_num += 1
                            self._emit(f"<b>{self.step_num}.</b> {line}")
        elif etype == "user":
            # tool_result event -- subagent may have finished
            content = (event.get("message") or {}).get("content") or []
            for block in content:
                if block.get("type") == "tool_result":
                    # Heuristic: if previous step was Agent, close the subagent context
                    if self.current_subagent:
                        self.current_subagent = None


def _handle_stream_event(event: dict, tracker: Any) -> None:
    """Pass event to the status tracker."""
    if tracker:
        tracker.handle_event(event)


def _progress_bar(done: int, total: int, width: int = 10) -> str:
    """Render ASCII progress bar: [------->>>] 70%"""
    if total <= 0:
        return ""
    pct = min(100, int(done * 100 / total))
    filled = int(width * done / total)
    bar = "#" * filled + "." * (width - filled)
    return f"[{bar}] {pct}%"


class _TaskBoundaryTracker:
    """Progress tracker: task boundaries + subagent dispatches.
    NO emoji -- uses ASCII brackets + box-drawing only.

    Format:
      plan:
      [ ] pending task
      [>] in-progress task
      [x] completed task

      steps:
       1 | researcher
       2 | content-writer  <- now
       3 | delivery

      progress [######....] 70%
    """
    def __init__(self, status_cb):
        self.status_cb = status_cb
        self.todos: list[dict] = []
        self.dispatches: list[dict] = []  # [{label, desc, status, summary}]
        self.pending_agents: dict[str, int] = {}  # tool_use_id -> dispatch index
        self.tool_calls: list[dict] = []  # [{tag, name, detail}] last N tool calls
        self._start_time: float = time.time()

    def _render(self) -> None:
        if not self.status_cb:
            return
        parts = []
        # Elapsed time header
        elapsed = int(time.time() - self._start_time)
        parts.append(f"<code>working -- {elapsed}s</code>")
        # Recent tool calls
        if self.tool_calls:
            parts.append(self._render_tools())
        if self.todos:
            parts.append(self._render_todos())
        if self.dispatches:
            parts.append(self._render_dispatches())
        text = "\n\n".join(parts).strip()
        if text:
            self.status_cb(text)

    def _render_tools(self) -> str:
        """Render last 10 tool calls in ASCII format -- no dedup, full stream."""
        lines = []
        for tc in self.tool_calls[-10:]:
            tag = escape_html(tc["tag"])
            detail = tc.get("detail", "")
            if detail:
                lines.append(f"<code>{tag}</code> {_mask_secrets(escape_html(detail))}")
            else:
                lines.append(f"<code>{tag}</code> {escape_html(tc['name'].lower())}")
        return "\n".join(lines)

    def _render_todos(self) -> str:
        lines = ["<b>plan:</b>"]
        done = sum(1 for t in self.todos if t.get("status") == "completed")
        total = len(self.todos)
        for t in self.todos[:10]:
            status = t.get("status", "pending")
            content = escape_html((t.get("content") or "")[:80])
            mark = {"completed": "[x]", "in_progress": "[&gt;]", "pending": "[ ]"}.get(status, "[ ]")
            if status == "completed":
                lines.append(f"<code>{mark}</code> <s>{content}</s>")
            elif status == "in_progress":
                lines.append(f"<code>{mark}</code> <b>{content}</b>")
            else:
                lines.append(f"<code>{mark}</code> {content}")
        if len(self.todos) > 10:
            lines.append(f"  ... +{len(self.todos) - 10} more")
        if total > 0:
            lines.append(f"\n<code>{_progress_bar(done, total)}</code>")
        return "\n".join(lines)

    def _render_dispatches(self) -> str:
        total = len(self.dispatches)
        single = total == 1
        lines = [] if single else ["<b>steps:</b>"]
        for i, d in enumerate(self.dispatches[-8:], start=max(1, total - 7)):
            label = escape_html(d["label"])
            status = d["status"]
            if single:
                # Single dispatch -- no number, just label with status
                if status == "done":
                    summary = escape_html(d.get("summary", "")[:60])
                    marker = f" -- {summary}" if summary else ""
                    lines.append(f"<s>{label}</s>{marker}")
                elif status == "running":
                    desc = escape_html(d.get("desc", "")[:40])
                    desc_part = f" -- {desc}" if desc else ""
                    lines.append(f"<b>{label}</b>{desc_part}")
            else:
                if status == "done":
                    summary = escape_html(d.get("summary", "")[:60])
                    marker = "" if not summary else f" -- <i>{summary}</i>"
                    lines.append(f" {i:>2} | <s>{label}</s>{marker}")
                elif status == "running":
                    desc = escape_html(d.get("desc", "")[:40])
                    desc_part = f" -- {desc}" if desc else ""
                    lines.append(f" {i:>2} | <b>{label}</b>{desc_part}  <- now")
                else:
                    lines.append(f" {i:>2} | {label}")
        done = sum(1 for d in self.dispatches if d["status"] == "done")
        if total > 1:
            lines.append(f"\n<code>{_progress_bar(done, total)}</code>")
        return "\n".join(lines)

    def handle_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "assistant":
            content = (event.get("message") or {}).get("content") or []
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                tname = block.get("name", "")
                tinput = block.get("input") or {}
                tuid = block.get("id", "")
                # Track every tool call for real-time display
                tag = TOOL_TAGS.get(tname, _TOOL_TAG_DEFAULT)
                detail = _summarize_tool_input(tname, tinput)
                display_detail = f"{tname.lower()} {detail}" if detail else ""
                self.tool_calls.append({"tag": tag, "name": tname, "detail": display_detail})
                # Keep last 10 tool calls
                if len(self.tool_calls) > 10:
                    self.tool_calls = self.tool_calls[-10:]
                if tname == "TodoWrite":
                    self.todos = tinput.get("todos") or []
                    self._render()
                elif tname == "Agent":
                    sub = tinput.get("subagent_type", "?")
                    desc = (tinput.get("description") or "")[:60]
                    label = SUBAGENT_LABELS.get(sub, sub)
                    idx = len(self.dispatches)
                    self.dispatches.append({
                        "label": label, "desc": desc, "status": "running", "summary": "",
                    })
                    self.pending_agents[tuid] = idx
                    self._render()
                else:
                    # Re-render for any tool call (not just TodoWrite/Agent)
                    self._render()
        elif etype == "user":
            # tool_result for Agent = subagent finished
            content = (event.get("message") or {}).get("content") or []
            for block in content:
                if block.get("type") != "tool_result":
                    continue
                tuid = block.get("tool_use_id", "")
                if tuid in self.pending_agents:
                    idx = self.pending_agents.pop(tuid)
                    if idx < len(self.dispatches):
                        # Extract short summary
                        raw = block.get("content")
                        summary = ""
                        if isinstance(raw, str):
                            summary = raw
                        elif isinstance(raw, list):
                            for c in raw:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    summary = c.get("text", "")
                                    break
                        first_line = next((ln for ln in summary.split("\n") if ln.strip()), "")
                        self.dispatches[idx]["status"] = "done"
                        self.dispatches[idx]["summary"] = first_line
                    self._render()


# ---------------------------------------------------------------------------
# Hot memory
# ---------------------------------------------------------------------------

HOT_SIZE_THRESHOLD = 15000  # chars, triggers compaction signal


def append_to_hot_memory(agent: str, cfg: dict, user_text: str, agent_response: str, source_tag: str) -> None:
    """Append lightweight turn summary to agent's hot/recent.md rolling journal.
    Uses fcntl.LOCK_EX to prevent interleaved writes from concurrent chat handlers.
    Emergency trim if file exceeds 20KB.
    """
    workspace = _get_workspace(agent, cfg)
    hot_file = Path(workspace) / "core" / "hot" / "recent.md"
    if not hot_file.parent.exists():
        return
    try:
        ts = time.strftime("%Y-%m-%d %H:%M")
        u_snippet = (user_text or "").replace("\n", " ")[:200]
        a_snippet = (agent_response or "(inline)").replace("\n", " ")[:200]
        entry = (
            f"\n### {ts} [{source_tag}]\n"
            f"**User:** {u_snippet}\n"
            f"**{agent.capitalize()}:** {a_snippet}\n"
        )
        # Atomic append with file lock
        with open(hot_file, "a") as f:
            _fcntl.flock(f.fileno(), _fcntl.LOCK_EX)
            try:
                f.write(entry)
                f.flush()
            finally:
                _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)

        # Emergency trim if >20KB (compact failed to run or burst of messages)
        size = hot_file.stat().st_size
        if size > 20480:
            log.warning(f"[hot] {agent} recent.md {size}B -- emergency trim")
            # Keep last ~100 entries (roughly last 2-3 days)
            lines = hot_file.read_text().split("\n")
            # Find cutoff -- keep last 600 lines (entries are 4 lines each, 600/4=150 entries)
            if len(lines) > 600:
                header = "# Hot memory -- last 72h rolling journal\n"
                kept = lines[-600:]
                # Find first entry header to avoid truncated entry at top
                for i, ln in enumerate(kept):
                    if ln.startswith("### "):
                        kept = kept[i:]
                        break
                with open(hot_file, "w") as f:
                    _fcntl.flock(f.fileno(), _fcntl.LOCK_EX)
                    try:
                        f.write(header + "\n" + "\n".join(kept))
                    finally:
                        _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
    except Exception as e:
        log.warning(f"[hot] append failed: {e}")


# ---------------------------------------------------------------------------
# OpenViking semantic memory (optional)
# ---------------------------------------------------------------------------

def push_to_openviking(agent: str, cfg: dict, user_text: str, agent_response: str, chat_id: int) -> None:
    """Push conversation turn to OpenViking for semantic memory extraction.
    Fire-and-forget in background thread."""
    ov_url = cfg.get("openviking_url")
    if not ov_url:
        return
    ov_key_file = cfg.get("openviking_key_file")
    if not ov_key_file:
        return
    key_path = Path(expand(ov_key_file))
    if not key_path.exists():
        return
    key = key_path.read_text().strip()
    ov_account = cfg.get("openviking_account", "default")
    headers = {
        "X-API-Key": key,
        "X-OpenViking-Account": ov_account,
        "X-OpenViking-User": agent,
        "Content-Type": "application/json",
    }
    base = f"{ov_url.rstrip('/')}/api/v1"
    sid = None
    try:
        # Create session
        r = requests.post(f"{base}/sessions", headers=headers, json={}, timeout=10)
        if r.status_code != 200:
            log.warning(f"[ov] create session failed: {r.status_code}")
            return
        sid = r.json().get("result", {}).get("session_id")
        if not sid:
            return
        # user_text already has [source:...] prefix from process_update
        ts = time.strftime("%Y-%m-%d %H:%M")
        # Anti-pollution guard: strong instruction to OV LLM about forwarded content
        if "[source:forwarded" in user_text:
            guard = (
                "\n[extraction hint: this content was FORWARDED to the user from someone else. "
                "Do NOT extract as user's own preferences. Only extract as events/cases/entities about the third-party source.]\n"
            )
        elif "[source:external_media" in user_text:
            guard = (
                "\n[extraction hint: this is external media the user is sharing, not their own words. "
                "Do NOT extract as user's preferences.]\n"
            )
        else:
            guard = ""
        meta_prefix = f"[chat:{chat_id} agent:{agent} at {ts}]{guard}\n"
        requests.post(
            f"{base}/sessions/{sid}/messages",
            headers=headers,
            json={"role": "user", "content": meta_prefix + user_text[:3000]},
            timeout=10,
        )
        if agent_response:
            requests.post(
                f"{base}/sessions/{sid}/messages",
                headers=headers,
                json={"role": "assistant", "content": agent_response[:3000]},
                timeout=10,
            )
        # Extract memories (runs LLM for structured extraction)
        ext = requests.post(f"{base}/sessions/{sid}/extract", headers=headers, json={}, timeout=60)
        extracted = ext.json().get("result", []) if ext.status_code == 200 else []
        log.info(f"[ov] extracted {len(extracted)} memories for {agent}/{chat_id}")
    except Exception as e:
        log.warning(f"[ov] push failed: {e}")
    finally:
        # Always clean up session to prevent leaks
        if sid:
            try:
                requests.delete(f"{base}/sessions/{sid}", headers=headers, timeout=5)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Heartbeat (no-op / log-only, no external dependencies)
# ---------------------------------------------------------------------------

def record_heartbeat(
    agent: str, started_at_ms: int, dur_ms: int, status: str, chat_id: int
) -> None:
    """Log heartbeat info. Override this function to push to your analytics backend."""
    log.info(
        f"[heartbeat] agent={agent} chat={chat_id} status={status} "
        f"duration={dur_ms}ms started={started_at_ms}"
    )


# ---------------------------------------------------------------------------
# Main message processing
# ---------------------------------------------------------------------------

def process_update(agent: str, cfg: dict, token: str, update: dict, allowlist: list[int]) -> None:
    """Handle one Telegram update. Ignore non-message / non-allowlisted / non-addressed."""
    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return
    user_id = (msg.get("from") or {}).get("id")
    if user_id not in allowlist:
        log.info(f"denied user_id={user_id} agent={agent}")
        return

    # Group-chat gating: only respond if addressed via @mention, name, or reply
    bot_username = cfg.get("_bot_username")
    if not is_addressed_to_agent(agent, msg, bot_username, cfg):
        chat_type = (msg.get("chat") or {}).get("type", "?")
        log.info(f"[{agent}] group chat {chat_type}, not addressed, skip")
        return
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()
    message_id = msg.get("message_id")

    # Handle gateway commands (/status, /reset, /help, /new) -- don't go to claude
    if text.startswith("/"):
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        if handle_command(token, chat_id, agent, cmd, args, cfg):
            log.info(f"[{agent}] command: {cmd} {args}".strip())
            return

    # Classify source for memory extraction provenance
    source_tag, source_label = classify_source(msg)

    # Handle media attachments
    media_note = ""
    media_ref = resolve_media_ref(msg)
    if media_ref:
        send_chat_action(token, chat_id, "typing")
        local = download_telegram_file(token, media_ref["file_id"], media_ref["type"], media_ref["file_name"])
        if local:
            mtype = media_ref["type"]
            if mtype in ("voice", "audio", "video_note"):
                # Transcribe via Groq (video_note has audio track)
                transcript = transcribe_audio(local, agent_cfg=cfg)
                if transcript:
                    media_note = f"\n\n[Voice/audio transcript]: {transcript}"
                else:
                    media_note = f"\n\n[Audio transcription failed: {local}]"
            elif mtype == "video":
                # Video: attach path, claude Read tool can't play video but can reference
                media_note = f"\n\n[Video: {local} ({media_ref.get('mime_type') or 'video'})]"
            elif mtype == "photo":
                media_note = f"\n\n[Image: {local}] -- read via Read tool"
            elif mtype == "document":
                fname = media_ref.get("file_name") or local.name
                media_note = f"\n\n[File {fname}: {local}] -- read via Read tool (for PDF use pypdf or shell pdftotext)"
            elif mtype == "sticker":
                media_note = f"\n\n[Sticker: {local}]"
        else:
            media_note = f"\n\n[Failed to download {media_ref['type']} (possibly >20MB)]"

    if not text and not media_note:
        return  # nothing to process
    if not text:
        text = "(user sent attachment)"
    text = text + media_note

    # Prepend source tag for OV memory extraction (invisible to agent context, used by gateway OV push)
    text_for_agent = text  # what agent sees
    text_for_ov = f"[source:{source_tag} | {source_label}]\n{text}"

    log.info(f"[{agent}] chat={chat_id} user={user_id} src={source_tag}: {text[:100]}")

    started_ms = int(time.time() * 1000)
    send_chat_action(token, chat_id, "typing")

    # No placeholder; status message created lazily on first real task-boundary event.
    status_msg_id = [None]
    last_edit_t = [0.0]
    last_sent = [""]

    def status_update(text_html: str) -> None:
        text_html = text_html[:3900]
        now = time.time()
        if text_html == last_sent[0] or now - last_edit_t[0] < 2.0:
            return
        last_edit_t[0] = now
        last_sent[0] = text_html
        if status_msg_id[0] is None:
            # Lazy create status message on first real event (TodoWrite or Agent dispatch)
            try:
                r = tg_api(token, "sendMessage", chat_id=chat_id, text=text_html, parse_mode="HTML")
                status_msg_id[0] = r.get("result", {}).get("message_id")
            except Exception:
                pass
        else:
            edit_message(token, chat_id, status_msg_id[0], text_html, html=True)

    invoke_cfg = {
        **cfg,
        "_typing_refresh_cb": lambda: send_chat_action(token, chat_id, "typing"),
        "_status_update_cb": status_update,
    }
    response, dur_ms, status_int = invoke_claude(agent, invoke_cfg, chat_id, text_for_agent)

    # Delete status message before sending final reply (keep it clean)
    if status_msg_id[0]:
        delete_message(token, chat_id, status_msg_id[0])

    if not response:
        response = "agent did not respond"
    status = "completed" if status_int else "error"

    record_heartbeat(agent, started_ms, dur_ms, status, chat_id)

    # Append to hot memory (always, regardless of source)
    append_to_hot_memory(agent, cfg, text_for_agent, response or "(inline)", source_tag)

    # Skip OV push if voice transcription failed (would pollute with error text)
    transcribe_failed = media_note and "transcription failed" in media_note

    # OV push via bounded thread pool -- only for own content or forwarded (marked)
    if source_tag in ("own_text", "own_voice", "forwarded") and not transcribe_failed:
        _OV_POOL.submit(
            push_to_openviking, agent, cfg, text_for_ov,
            response or "(answered inline)", chat_id
        )
    # external_media -> hot only, not OV memory extraction (to avoid polluting preferences)

    if response:  # not already delivered via edit-in-place
        try:
            send_message(token, chat_id, response, reply_to=message_id)
            log.info(f"[{agent}] replied chat={chat_id} dur={dur_ms}ms")
        except Exception as e:
            log.exception(f"reply failed: {e}")


# ---------------------------------------------------------------------------
# Bot commands menu
# ---------------------------------------------------------------------------

_BOT_COMMANDS = [
    {"command": "stop", "description": "Stop current agent task"},
    {"command": "status", "description": "Session and memory status"},
    {"command": "reset", "description": "Reset session (saves important to MEMORY)"},
    {"command": "compact", "description": "Manual memory compaction"},
    {"command": "help", "description": "Help"},
]


# ---------------------------------------------------------------------------
# Polling: producer-consumer architecture
# ---------------------------------------------------------------------------

def _is_oob_command(text: str) -> bool:
    """Check if message text is an out-of-band command for instant handling."""
    if not text or not text.startswith("/"):
        return False
    parts = text.split(None, 1)
    cmd = parts[0].lower()
    # Strip @botname suffix (e.g. /stop@mybotname)
    if "@" in cmd:
        cmd = cmd.split("@")[0]
    return cmd in _OOB_COMMANDS


def _handle_oob_command(
    agent: str, token: str, chat_id: int, text: str
) -> None:
    """Handle out-of-band command immediately from producer thread.

    Handles: /stop, /status, /reset force
    Non-force /reset and other commands are queued for consumer.
    """
    parts = text.split(None, 2)
    cmd = parts[0].lower()
    # Strip @botname suffix
    if "@" in cmd:
        cmd = cmd.split("@")[0]
    args = parts[1] if len(parts) > 1 else ""

    if cmd in ("/stop", "/cancel"):
        proc = _ACTIVE_PROCS.get((agent, chat_id))
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                time.sleep(0.5)
                if proc.poll() is None:
                    proc.kill()
                reply = (
                    "<b>stopped</b>\n\n"
                    "<i>agent interrupted current task</i>"
                )
            except Exception as e:
                reply = (
                    f"<b>stop error</b>: "
                    f"{escape_html(str(e)[:100])}"
                )
        else:
            reply = "<b>nothing to stop</b> -- agent is idle"
        try:
            tg_api(
                token, "sendMessage",
                chat_id=chat_id, text=reply, parse_mode="HTML",
            )
        except Exception:
            pass
        log.info(f"[{agent}] OOB /stop chat={chat_id}")
        return

    if cmd == "/status":
        # Reuse existing handle_command for /status (it's fast, no blocking)
        handle_command(token, chat_id, agent, "/status", "")
        log.info(f"[{agent}] OOB /status chat={chat_id}")
        return

    if cmd == "/reset" and args.strip().lower() == "force":
        # Kill active subprocess if any
        proc = _ACTIVE_PROCS.pop((agent, chat_id), None)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                time.sleep(0.5)
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        # Delete session files
        sid_file = STATE_DIR / f"sid-{agent}-{chat_id}.txt"
        first_file = STATE_DIR / f"sid-{agent}-{chat_id}.first"
        old_sid = ""
        if sid_file.exists():
            old_sid = sid_file.read_text().strip()
        sid_file.unlink(missing_ok=True)
        first_file.unlink(missing_ok=True)
        reply = (
            "<b>session reset (force)</b>\n\n"
            f"old: <code>{escape_html(old_sid[:8])}...</code>\n"
            "next message = new session"
        )
        try:
            tg_api(
                token, "sendMessage",
                chat_id=chat_id, text=reply, parse_mode="HTML",
            )
        except Exception:
            pass
        log.info(f"[{agent}] OOB /reset force chat={chat_id}")
        return


def _init_bot_metadata(agent: str, cfg: dict, token: str) -> None:
    """Cache bot_username and register commands menu (once per agent)."""
    if cfg.get("_bot_username"):
        return
    try:
        info = tg_api(token, "getMe")
        bot_username = (info.get("result") or {}).get("username")
        cfg["_bot_username"] = bot_username
        try:
            tg_api(token, "setMyCommands", commands=_BOT_COMMANDS)
        except Exception as e:
            log.warning(f"[{agent}] setMyCommands failed: {e}")
    except Exception:
        pass


def polling_producer(
    agent: str, cfg: dict, allowlist: list[int], offset_file: Path
) -> None:
    """Daemon thread: polls Telegram, routes OOB commands immediately,
    queues regular messages for consumer.

    OOB commands (/stop, /status, /reset force) are handled instantly
    even when the consumer thread is blocked on invoke_claude().
    """
    token = _resolve_telegram_token(cfg)
    _init_bot_metadata(agent, cfg, token)
    msg_queue = _MSG_QUEUES[agent]
    poll_interval_sec = 1  # producer polls every 1s for responsiveness

    log.info(f"[{agent}] producer thread started")

    while not _SHUTDOWN_EVENT.is_set():
        # Read offset
        offset = 0
        if offset_file.exists():
            try:
                offset = int(offset_file.read_text().strip() or "0")
            except Exception:
                offset = 0

        try:
            r = tg_api(
                token, "getUpdates",
                offset=offset, timeout=0, limit=10,
            )
        except Exception as e:
            log.warning(f"[{agent}] producer getUpdates failed: {e}")
            time.sleep(poll_interval_sec)
            continue

        updates = r.get("result", [])
        for upd in updates:
            new_offset = upd["update_id"] + 1
            offset_file.write_text(str(new_offset))

            msg = upd.get("message") or upd.get("channel_post")
            if not msg:
                continue

            user_id = (msg.get("from") or {}).get("id")
            if user_id not in allowlist:
                log.info(
                    f"[{agent}] producer denied user_id={user_id}"
                )
                continue

            # Group-chat gating
            bot_username = cfg.get("_bot_username")
            if not is_addressed_to_agent(agent, msg, bot_username, cfg):
                continue

            chat_id = msg["chat"]["id"]
            text = (
                msg.get("text") or msg.get("caption") or ""
            ).strip()

            # Out-of-band commands: handle immediately in producer
            if _is_oob_command(text):
                # /reset without 'force' goes to consumer (it needs
                # blocking Claude save). Only /reset force is OOB.
                parts = text.split(None, 2)
                cmd = parts[0].lower()
                if "@" in cmd:
                    cmd = cmd.split("@")[0]
                args = parts[1].lower() if len(parts) > 1 else ""

                if cmd == "/reset" and args != "force":
                    # Non-force reset -> queue for consumer
                    msg_queue.put(upd)
                    continue

                try:
                    _handle_oob_command(agent, token, chat_id, text)
                except Exception:
                    log.exception(
                        f"[{agent}] OOB command failed: {text}"
                    )
                continue

            # Regular message -> queue for consumer
            msg_queue.put(upd)

        time.sleep(poll_interval_sec)

    log.info(f"[{agent}] producer thread stopped")


def message_consumer(
    agent: str, cfg: dict, token: str, allowlist: list[int]
) -> None:
    """Daemon thread: takes messages from queue, invokes Claude.

    Blocks on queue.get() when no messages. Only one message processed
    at a time per agent (serial execution).
    """
    msg_queue = _MSG_QUEUES[agent]
    log.info(f"[{agent}] consumer thread started")

    while not _SHUTDOWN_EVENT.is_set():
        try:
            # Block with timeout so we can check shutdown event
            upd = msg_queue.get(timeout=2)
        except queue.Empty:
            continue

        try:
            process_update(agent, cfg, token, upd, allowlist)
        except Exception:
            log.exception(f"[{agent}] consumer update processing failed")
        finally:
            msg_queue.task_done()

    log.info(f"[{agent}] consumer thread stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not CONFIG_PATH.exists():
        log.error(f"config not found: {CONFIG_PATH}")
        log.error("create config.json in the working directory (see docstring for format)")
        sys.exit(1)

    cfg = json.loads(CONFIG_PATH.read_text())
    allowlist = cfg.get("allowlist_user_ids", [])
    agents = {
        k: v for k, v in cfg["agents"].items() if v.get("enabled")
    }

    if not agents:
        log.error("no enabled agents in config")
        sys.exit(1)

    log.info(
        f"gateway started (producer-consumer), "
        f"agents={list(agents.keys())}, allowlist={allowlist}"
    )

    offsets = {a: STATE_DIR / f"offset-{a}.txt" for a in agents}
    threads: list[threading.Thread] = []

    for agent, acfg in agents.items():
        # Initialize per-agent message queue
        _MSG_QUEUES[agent] = queue.Queue()

        # Read token for consumer thread
        token = _resolve_telegram_token(acfg)

        # Producer thread: polls Telegram, handles OOB commands
        t_prod = threading.Thread(
            target=polling_producer,
            args=(agent, acfg, allowlist, offsets[agent]),
            name=f"producer-{agent}",
            daemon=True,
        )
        t_prod.start()
        threads.append(t_prod)

        # Consumer thread: processes queued messages
        t_cons = threading.Thread(
            target=message_consumer,
            args=(agent, acfg, token, allowlist),
            name=f"consumer-{agent}",
            daemon=True,
        )
        t_cons.start()
        threads.append(t_cons)

    # Main thread waits for shutdown signal
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("gateway shutting down...")
        _SHUTDOWN_EVENT.set()
        for t in threads:
            t.join(timeout=5)
        log.info("gateway stopped")


if __name__ == "__main__":
    main()

"""Telegram bot command and message handlers.

All handlers check the allow-list before processing. Handlers call
into Work Buddy's existing capabilities rather than implementing
business logic directly.

Slash commands (must be first character, one per message):
    /start   — identity verification
    /help    — list all commands
    /capture — append text to vault location (configurable)
    /remote  — launch Claude Code remote session
    /status  — system status
    /obs     — Obsidian command search/execute
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

if TYPE_CHECKING:
    from work_buddy.telegram.bot import BotState

logger = logging.getLogger(__name__)

_TRUNC = 40  # Max chars for log truncation


def _cmd_display(name: str) -> str:
    """Convert a command name to Telegram-friendly display form.

    Replaces hyphens with underscores so Telegram highlights them
    as clickable commands. E.g., ``/wb-task-new`` → ``/wb_task_new``.
    """
    return name.replace("-", "_")


def _cmd_normalize(text: str) -> str:
    """Normalize a command name for matching.

    Replaces both hyphens and underscores with a common form
    so ``wb_task_new`` and ``wb-task-new`` match the same thing.
    """
    return re.sub(r"[-_]", "-", text.lower())


def _log_inbound(update: Update) -> None:
    """Log an incoming Telegram message (truncated)."""
    text = (update.message.text or "")[:_TRUNC]
    chat = update.effective_chat.id
    logger.info("TG IN  [%s]: %s", chat, text)


async def _reply(update: Update, text: str, **kwargs) -> None:
    """Reply and log outbound (truncated)."""
    logger.info("TG OUT [%s]: %s", update.effective_chat.id, text[:_TRUNC])
    await update.message.reply_text(text, **kwargs)


# ---------------------------------------------------------------------------
# Hybrid command ranking (BM25 + dense via search_against)
# ---------------------------------------------------------------------------

def _rank_commands(
    query: str,
    commands: list[dict],
    top_k: int = 3,
) -> list[dict]:
    """Rank Obsidian commands by hybrid IR relevance.

    Uses ir.search_against for inline BM25 + dense scoring.
    Each command is represented as "id_tokens name" for matching.
    """
    try:
        from work_buddy.ir.engine import search_against
    except ImportError:
        return []

    if not commands or not query.strip():
        return []

    # Build candidate strings: tokenized ID + human name
    candidates = []
    for cmd in commands:
        cmd_id = cmd.get("id", "").replace(":", " ").replace("-", " ")
        cmd_name = cmd.get("name", "")
        candidates.append(f"{cmd_id} {cmd_name}")

    results = search_against(query, candidates, top_k=top_k)
    return [commands[r["index"]] for r in results]


def _rank_slash_commands(
    query: str,
    commands: list[dict],
    threshold: float = 0.020,
) -> list[dict]:
    """Rank slash commands by hybrid IR relevance, filtered by score threshold.

    Args:
        commands: List of {"name": "wb-task-new", "desc": "Create task"}.
        threshold: Minimum fused score to include (default 0.020 sits in
            the clean gap between signal ~0.03 and noise ~0.016).

    Returns empty list if semantic saturation is detected (all scores
    above 0.5 — means the query has no discriminating signal).
    """
    try:
        from work_buddy.ir.engine import search_against
    except ImportError:
        return []

    if not commands or not query.strip():
        return []

    candidates = [
        f"{cmd['name'].replace('-', ' ')} {cmd.get('desc', '')}"
        for cmd in commands
    ]

    results = search_against(query, candidates, threshold=threshold)
    if not results:
        return []

    # Saturation guard: if the lowest returned score is still > 0.5,
    # the query has no discriminating signal — treat as no match.
    if results[-1]["score"] > 0.5:
        return []

    return [commands[r["index"]] for r in results]


# ---------------------------------------------------------------------------
# Slash command description extractor
# ---------------------------------------------------------------------------

def _extract_short_description(raw: str) -> str:
    """Extract a short description from a slash command file.

    Checks for YAML frontmatter with a ``short`` field first,
    then falls back to the first non-empty line of the body.
    """
    lines = raw.split("\n")

    # Check for YAML frontmatter
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                # Parse frontmatter lines for "short:"
                for fm_line in lines[1:i]:
                    if fm_line.startswith("short:"):
                        return fm_line.split(":", 1)[1].strip().strip('"').strip("'")
                # No short field — use first non-empty body line
                for body_line in lines[i + 1:]:
                    if body_line.strip():
                        return body_line.strip()
                break

    # No frontmatter — first non-empty line
    for line in lines:
        if line.strip():
            return line.strip()
    return "(no description)"


# ---------------------------------------------------------------------------
# Auth check
# ---------------------------------------------------------------------------

def _is_authorized(update: Update, state: "BotState") -> bool:
    """Check if the chat is in the allow-list.

    If the allow-list is empty, the first chat to message the bot
    gets auto-added and persisted to config.yaml so restarts remember it.
    """
    chat_id = update.effective_chat.id
    if not state.allowed_chat_ids:
        # Auto-accept first chat and persist
        state.allowed_chat_ids.add(chat_id)
        logger.info("Auto-accepted first chat: %s", chat_id)
        _persist_chat_id(chat_id)
        return True
    return chat_id in state.allowed_chat_ids


def _get_chat_id_file() -> "Path":
    """Return path to the local chat ID persistence file."""
    from work_buddy.paths import resolve
    return resolve("runtime/telegram-chat-id")


def _persist_chat_id(chat_id: int) -> None:
    """Write the chat ID to a local file so it survives bot restarts.

    Uses a dedicated file rather than config.yaml so the registration
    is never lost when config.yaml is edited or regenerated.
    """
    try:
        path = _get_chat_id_file()
        existing = load_persisted_chat_ids()
        if chat_id not in existing:
            existing.add(chat_id)
            path.write_text(
                "\n".join(str(cid) for cid in sorted(existing)) + "\n",
                encoding="utf-8",
            )
            logger.info("Persisted chat_id %s to %s", chat_id, path.name)
    except Exception as exc:
        logger.warning("Failed to persist chat_id: %s", exc)


def load_persisted_chat_ids() -> set[int]:
    """Load chat IDs from the local persistence file."""
    path = _get_chat_id_file()
    if not path.exists():
        return set()
    try:
        ids = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                ids.add(int(line))
        return ids
    except Exception as exc:
        logger.warning("Failed to read persisted chat IDs: %s", exc)
        return set()


async def _deny(update: Update) -> None:
    """Send a denial message for unauthorized chats."""
    await _reply(update, "Not authorized. Contact the bot admin.")


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def cmd_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /start — identity verification."""
    _log_inbound(update)
    state: BotState = context.bot_data["state"]
    if not _is_authorized(update, state):
        await _deny(update)
        return
    await _reply(update,
        "Work Buddy Telegram is online.\n"
        "Use /help to see available commands."
    )


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "Work Buddy Telegram Commands\n"
    "\n"
    "/capture <text> - Append text to your journal Running Notes\n"
    "/reply <id> <answer> - Respond to a pending request by short ID\n"
    "/remote <prompt> - Launch a new remote session\n"
    "/resume <id|name|query> - Resume by ID, name, or search\n"
    "/status - System status summary\n"
    "/dashboard - Open the dashboard in your browser\n"
    "/obs <query> - Search and execute an Obsidian command\n"
    "/slash - List or search wb slash commands\n"
    "/help - Show this help message\n"
    "\n"
    "You can also send plain text without a command - "
    "it will be captured to your journal Running Notes."
)


async def cmd_help(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /help — list all commands."""
    _log_inbound(update)
    state: BotState = context.bot_data["state"]
    if not _is_authorized(update, state):
        await _deny(update)
        return
    await _reply(update, HELP_TEXT)


# ---------------------------------------------------------------------------
# /slash
# ---------------------------------------------------------------------------

async def cmd_slash(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /slash — list or search wb slash commands.

    /slash        — list all commands
    /slash task   — BM25-filtered results matching "task"
    """
    _log_inbound(update)
    state: BotState = context.bot_data["state"]
    if not _is_authorized(update, state):
        await _deny(update)
        return

    query = " ".join(context.args) if context.args else ""

    try:
        from pathlib import Path
        commands_dir = Path(__file__).parent.parent.parent / ".claude" / "commands"
        if not commands_dir.is_dir():
            await _reply(update, "No slash commands found.")
            return

        # Load all commands with metadata
        all_cmds = []
        for f in sorted(commands_dir.glob("wb-*.md")):
            name = f.stem  # e.g., wb-morning
            try:
                raw = f.read_text(encoding="utf-8")
                desc = _extract_short_description(raw)
            except OSError:
                desc = "(no description)"
            all_cmds.append({"name": name, "desc": desc})

        if not all_cmds:
            await _reply(update, "No slash commands found.")
            return

        if query:
            # BM25-filtered search
            filtered = _rank_slash_commands(query, all_cmds)
            if filtered:
                lines = [f"Slash commands matching '{query}':", ""]
                for cmd in filtered:
                    lines.append(f"{_cmd_display('/' + cmd['name'])} - {cmd['desc']}")
                await _reply(update, "\n".join(lines))
            else:
                # No match or saturated — show all as fallback
                lines = [f"No strong match for '{query}'. All commands:", ""]
                for cmd in all_cmds:
                    lines.append(f"{_cmd_display('/' + cmd['name'])} - {cmd['desc']}")
                await _reply(update, "\n".join(lines))
        else:
            # List all
            lines = ["Work Buddy slash commands:", ""]
            for cmd in all_cmds:
                lines.append(f"{_cmd_display('/' + cmd['name'])} - {cmd['desc']}")
            await _reply(update, "\n".join(lines))
    except Exception as exc:
        logger.error("Slash listing failed: %s", exc)
        await _reply(update, f"Error listing commands: {exc}")


# ---------------------------------------------------------------------------
# /capture <text>
# ---------------------------------------------------------------------------

async def cmd_capture(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /capture — append text to vault location."""
    _log_inbound(update)
    state: BotState = context.bot_data["state"]
    if not _is_authorized(update, state):
        await _deny(update)
        return

    # Get raw text after the /capture command, preserving newlines
    raw = update.message.text or ""
    text = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""
    if not text:
        await _reply(update, "Usage: /capture <text to capture>")
        return

    await _do_capture(update, text, state)


async def _do_capture(update: Update, text: str, state: "BotState") -> None:
    """Perform the vault capture operation.

    Telegram messages are direct user intent — auto-grant write consent
    so the user doesn't hit a consent wall from their phone.

    Formats as a fenced capture block:
        ---
        > #wb/capture/mobile from Name (@handle) at YYYY-MM-DD HH:MM

        <content>

        ---
    """
    try:
        from work_buddy.consent import grant_consent
        grant_consent("obsidian.write_file", mode="always")

        from work_buddy.obsidian.vault_writer import write_at_location
        from work_buddy.journal import user_now

        # Build sender info from Telegram user
        user = update.effective_user
        display_name = user.full_name if user else "Unknown"
        handle = f"@{user.username}" if user and user.username else ""
        sender = f"{display_name} ({handle})" if handle else display_name

        now = user_now()
        timestamp = now.strftime("%Y-%m-%d %H:%M")

        # Format the capture block
        block = (
            f"---\n"
            f"> #wb/capture/mobile from {sender} at {timestamp}\n"
            f"\n"
            f"{text}\n"
            f"\n"
            f"---"
        )

        cfg = state.capture_config
        note = cfg.get("note", "latest_journal")
        section = cfg.get("section", "Running Notes")
        position = cfg.get("position", "top")
        logger.info("Capture: note=%s section=%s position=%s text=%s",
                     note, section, position, text[:40])

        result = write_at_location(
            content=block,
            note=note,
            section=section,
            position=position,
            source=None,  # tag is already in the block header
        )

        logger.info("Capture result: status=%s note=%s",
                     result.get("status"), result.get("note"))

        if result["status"] == "ok":
            await _reply(update, f"Captured to {result['note']}")
        elif result["status"] == "consent_required":
            op = result.get("operation", "obsidian.write_file")
            await _reply(update,
                f"Capture requires consent for '{op}'.\n"
                "Grant consent via Obsidian or the MCP gateway, then retry."
            )
        else:
            await _reply(update, f"Capture failed: {result.get('error', 'unknown')}")
    except Exception as exc:
        logger.error("Capture failed: %s", exc, exc_info=True)
        await _reply(update, f"Capture error: {exc}")


# ---------------------------------------------------------------------------
# /remote <prompt>
# ---------------------------------------------------------------------------

async def cmd_remote(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /remote — launch a NEW Claude Code remote session.

    /remote          — new session with default standby prompt
    /remote <prompt> — new session with custom prompt
    """
    _log_inbound(update)
    state: BotState = context.bot_data["state"]
    if not _is_authorized(update, state):
        await _deny(update)
        return

    prompt = " ".join(context.args).strip() if context.args else None

    try:
        from work_buddy.consent import grant_consent
        grant_consent("sidecar:remote_session_launch", mode="always")

        from work_buddy.session_launcher import begin_session
        result = begin_session(prompt=prompt)

        if result.get("status") == "ok":
            await _reply(update, result.get("message", "Session launched."))
        else:
            await _reply(update, f"Remote session failed: {result.get('error', 'Unknown')}")
    except Exception as exc:
        logger.error("Remote session failed: %s", exc)
        await _reply(update, f"Remote session error: {exc}")


# ---------------------------------------------------------------------------
# /resume <session_id | session_name | search query>
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
# Partial UUID prefix: 4+ hex chars, optionally with dashes (e.g. "c740a629"
# or "c740a629-ea22").  Intentionally requires 4+ chars to avoid false
# positives against short session names.
_UUID_PREFIX_RE = re.compile(r"^[0-9a-f]{4,}(?:-[0-9a-f]*)*$", re.IGNORECASE)


async def cmd_resume(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /resume — resume a session by ID, name, or conversation search.

    /resume <uuid>   — resume by exact session ID
    /resume <name>   — resume by exact session name
    /resume <query>  — search conversations, show top matches as buttons
    """
    _log_inbound(update)
    state: BotState = context.bot_data["state"]
    if not _is_authorized(update, state):
        await _deny(update)
        return

    arg = " ".join(context.args).strip() if context.args else ""
    if not arg:
        await _reply(update, "Usage: /resume <session_id | name | search query>")
        return

    try:
        from work_buddy.consent import grant_consent
        grant_consent("sidecar:remote_session_launch", mode="always")

        from work_buddy.session_launcher import begin_session, list_resumable_sessions

        # 1. Exact or partial UUID match — route through begin_session
        #    which calls _find_session_id → resolve_session_id for prefix
        #    resolution with ambiguity detection.
        if _UUID_RE.match(arg) or _UUID_PREFIX_RE.match(arg):
            result = begin_session(session_id=arg)
            if result.get("status") == "ok":
                await _reply(update, result.get("message", "Session resumed."))
            else:
                await _reply(update, f"Resume failed: {result.get('error', 'Unknown')}")
            return

        # 2. Exact session name match
        sessions = list_resumable_sessions()
        name_match = [
            s for s in sessions
            if _cmd_normalize(s.get("name", "")) == _cmd_normalize(arg)
        ]
        if name_match:
            result = begin_session(session_name=arg)
            if result.get("status") == "ok":
                await _reply(update, result.get("message", "Session resumed."))
            else:
                await _reply(update, f"Resume failed: {result.get('error', 'Unknown')}")
            return

        # 3. Fall through to conversation search
        await _search_and_show_sessions(update, arg, state)

    except Exception as exc:
        logger.error("Resume failed: %s", exc)
        await _reply(update, f"Resume error: {exc}")


async def _search_and_show_sessions(
    update: Update,
    query: str,
    state: "BotState",
) -> None:
    """Search conversation index and show matching sessions as buttons."""
    from work_buddy.ir import search as ir_search
    from work_buddy.ir.engine import top_k_weighted_score
    from collections import defaultdict

    results = ir_search(query, source="conversation", top_k=20)

    if not results:
        await _reply(update, f"No conversations matching: {query}")
        return

    # Group by session, aggregate with weighted top-k scoring
    session_chunks: dict[str, list] = defaultdict(list)
    session_meta: dict[str, dict] = {}

    for r in results:
        sid = r["metadata"].get("session_id", "")
        if not sid:
            continue
        session_chunks[sid].append(r)
        if sid not in session_meta or r["score"] > session_meta[sid]["score"]:
            session_meta[sid] = r

    session_scores = {}
    for sid, chunks in session_chunks.items():
        chunk_scores = [c["score"] for c in chunks]
        session_scores[sid] = top_k_weighted_score(chunk_scores)

    ranked = sorted(
        session_scores.items(), key=lambda x: x[1], reverse=True,
    )[:5]

    if not ranked:
        await _reply(update, f"No conversations matching: {query}")
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from datetime import datetime

    buttons = []
    lines = [f"Conversations matching '{query}':", ""]

    for sid, score in ranked:
        meta = session_meta[sid]
        start_time = meta["metadata"].get("start_time", "")
        try:
            dt = datetime.fromisoformat(start_time)
            ts_display = dt.strftime("%b %d, %H:%M")
        except (ValueError, TypeError):
            ts_display = "unknown"

        project = meta["metadata"].get("project_name", "")
        preview = (meta.get("display_text", "") or "")[:80]

        label = f"[{ts_display}] {project}"
        lines.append(label)
        lines.append(f"  {preview}")
        lines.append("")

        # Store full session_id for button lookup
        state.pending_responses.add(sid)
        cb_data = f"rsm:{sid[:8]}"
        if len(cb_data.encode("utf-8")) > 64:
            cb_data = cb_data[:64]
        buttons.append([InlineKeyboardButton(label, callback_data=cb_data)])

    buttons.append([InlineKeyboardButton("Cancel", callback_data="rsm:_cancel")])
    markup = InlineKeyboardMarkup(buttons)

    text = "\n".join(lines)
    logger.info("TG OUT [%s]: %s", update.effective_chat.id, text[:_TRUNC])
    await update.message.reply_text(text, reply_markup=markup)


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

async def cmd_status(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /status — system status summary."""
    _log_inbound(update)
    state: BotState = context.bot_data["state"]
    if not _is_authorized(update, state):
        await _deny(update)
        return

    lines = ["Work Buddy Status", ""]

    # Check Obsidian bridge
    try:
        from work_buddy.obsidian.bridge import is_available
        obs_ok = is_available()
        lines.append(f"Obsidian bridge: {'online' if obs_ok else 'offline'}")
    except Exception:
        lines.append("Obsidian bridge: unknown")

    # Check messaging service
    try:
        import urllib.request
        resp = urllib.request.urlopen("http://127.0.0.1:5123/health", timeout=3)
        lines.append("Messaging service: online")
    except Exception:
        lines.append("Messaging service: offline")

    lines.append("Telegram bot: online")

    await _reply(update, "\n".join(lines))


# ---------------------------------------------------------------------------
# /dashboard
# ---------------------------------------------------------------------------

async def cmd_dashboard(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /dashboard — send the dashboard URL."""
    _log_inbound(update)
    state: BotState = context.bot_data["state"]
    if not _is_authorized(update, state):
        await _deny(update)
        return

    from work_buddy.config import load_config
    cfg = load_config()
    url = cfg.get("dashboard", {}).get("external_url", "")
    if url:
        await _reply(update, url)
    else:
        port = (
            cfg.get("sidecar", {})
            .get("services", {})
            .get("dashboard", {})
            .get("port", 5127)
        )
        await _reply(
            update,
            f"http://127.0.0.1:{port}\n\n"
            "No Tailscale URL configured. Set dashboard.external_url "
            "in config.local.yaml for a mobile-friendly link.",
        )


# ---------------------------------------------------------------------------
# /obs <query>
# ---------------------------------------------------------------------------

async def cmd_obs(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /obs — Obsidian command search and execute."""
    _log_inbound(update)
    state: BotState = context.bot_data["state"]
    if not _is_authorized(update, state):
        await _deny(update)
        return

    query = " ".join(context.args) if context.args else ""
    if not query:
        await _reply(update, "Usage: /obs <command name or search query>")
        return

    try:
        from work_buddy.obsidian.commands import ObsidianCommands
        from pathlib import Path
        from work_buddy.config import load_config

        cfg = load_config()
        vault = Path(cfg["vault_root"])
        client = ObsidianCommands(vault)

        commands = client.list_commands()
        query_lower = query.lower()

        # Exact name match (case-insensitive)
        exact_name = [
            c for c in commands
            if c.get("name", "").lower() == query_lower
        ]
        if len(exact_name) == 1:
            client.execute(exact_name[0]["id"])
            await _reply(update, f"Executed: {exact_name[0]['name']}")
            return

        # Exact ID match
        exact_id = [c for c in commands if c["id"].lower() == query_lower]
        if exact_id:
            client.execute(exact_id[0]["id"])
            await _reply(update, f"Executed: {exact_id[0]['name']}")
            return

        # BM25 ranking over all commands — show top 3 as tappable buttons
        suggestions = _rank_commands(query, commands, top_k=3)
        if suggestions:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            buttons = []
            for s in suggestions:
                name = s.get("name", s["id"])
                # obs: prefix distinguishes from notification callbacks
                cb_data = f"obs:{s['id']}"
                if len(cb_data.encode("utf-8")) > 64:
                    cb_data = cb_data[:64]
                buttons.append([InlineKeyboardButton(name, callback_data=cb_data)])
            buttons.append([InlineKeyboardButton("Cancel", callback_data="obs:_cancel")])
            markup = InlineKeyboardMarkup(buttons)
            text = "Did you mean:"
            logger.info("TG OUT [%s]: %s", update.effective_chat.id, text[:_TRUNC])
            await update.message.reply_text(text, reply_markup=markup)
        else:
            await _reply(update, f"No Obsidian commands matching: {query}")
    except Exception as exc:
        logger.error("Obsidian command failed: %s", exc)
        await _reply(update, f"Command error: {exc}")


# ---------------------------------------------------------------------------
# Cross-surface dismiss helper
# ---------------------------------------------------------------------------

def _dismiss_others(notification_id: str, delivered_surfaces: list[str]) -> None:
    """Dismiss the notification on all other surfaces (first-response-wins).

    Best-effort — failures are logged but don't block the response flow.
    """
    try:
        from work_buddy.notifications.dispatcher import SurfaceDispatcher
        dispatcher = SurfaceDispatcher.from_config()
        dispatcher.dismiss_others(
            notification_id,
            responding_surface="telegram",
            delivered_surfaces=delivered_surfaces,
        )
    except Exception as exc:
        logger.debug("Cross-surface dismiss failed: %s", exc)


# ---------------------------------------------------------------------------
# /reply <short_id> <answer>
# ---------------------------------------------------------------------------

async def cmd_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /reply — respond to a pending request by its 4-digit short ID.

    /reply 4920 yes
    /reply 4920 Allow always
    /reply 4920 I think we should proceed with option B
    """
    _log_inbound(update)
    state: BotState = context.bot_data["state"]
    if not _is_authorized(update, state):
        await _deny(update)
        return

    if not context.args or len(context.args) < 2:
        await _reply(update, "Usage: /reply <short_id> <answer>")
        return

    short_id = context.args[0].lstrip("#")  # accept both "3303" and "#3303"
    answer = " ".join(context.args[1:])

    # Validate short_id format
    if not short_id.isdigit() or len(short_id) != 4:
        await _reply(update, f"Invalid short ID: {short_id}. Expected a 4-digit number.")
        return

    # Resolve short_id → notification_id
    notification_id = state.pending_responses.get_by_short_id(short_id)

    if not notification_id:
        # Disk fallback: scan pending notifications for matching short_id
        try:
            from work_buddy.notifications.store import list_pending
            for notif in list_pending():
                if notif.short_id == short_id:
                    notification_id = notif.notification_id
                    break
        except Exception:
            pass

    if not notification_id:
        await _reply(update, f"No pending request with ID #{short_id}.")
        return

    # Read the notification to determine response type and validate
    try:
        from work_buddy.notifications.store import (
            get_notification, respond_to_notification, dispatch_callback,
        )
        from work_buddy.notifications.models import (
            StandardResponse, ResponseType, NotificationStatus,
        )

        notification = get_notification(notification_id)
        if notification is None:
            await _reply(update, f"Request #{short_id} not found.")
            return

        if notification.status not in (
            NotificationStatus.PENDING.value,
            NotificationStatus.DELIVERED.value,
        ):
            await _reply(update, f"Request #{short_id} has already been answered.")
            return

        response_type = ResponseType(notification.response_type)

        # Build response based on type
        if response_type == ResponseType.BOOLEAN:
            lower = answer.lower()
            if lower in ("yes", "true", "y", "1"):
                value = "yes"
            elif lower in ("no", "false", "n", "0"):
                value = "no"
            else:
                await _reply(update, "Expected yes or no.")
                return
            resp = StandardResponse(
                response_type=ResponseType.BOOLEAN.value,
                value=value,
                raw={"via": "reply_command", "short_id": short_id},
                surface="telegram",
            )

        elif response_type == ResponseType.CHOICE:
            # Match against choice keys (case-insensitive)
            choices = notification.choices or []
            choice_keys = {c.get("key", "").lower(): c.get("key", "") for c in choices}
            choice_labels = {c.get("label", "").lower(): c.get("key", "") for c in choices}

            matched_key = choice_keys.get(answer.lower()) or choice_labels.get(answer.lower())
            if not matched_key:
                options = ", ".join(
                    f"`{c.get('key', '')}` ({c.get('label', '')})"
                    for c in choices
                )
                await _reply(update, f"Invalid choice. Options: {options}")
                return
            resp = StandardResponse(
                response_type=ResponseType.CHOICE.value,
                value=matched_key,
                raw={"via": "reply_command", "short_id": short_id},
                surface="telegram",
            )

        elif response_type == ResponseType.FREEFORM:
            resp = StandardResponse(
                response_type=ResponseType.FREEFORM.value,
                value=answer,
                raw={"via": "reply_command", "short_id": short_id},
                surface="telegram",
            )

        elif response_type == ResponseType.RANGE:
            # Validate numeric input within bounds
            nr = notification.number_range or {}
            range_min = nr.get("min", 1)
            range_max = nr.get("max", 10)
            try:
                num_value = float(answer)
            except ValueError:
                await _reply(update, f"Expected a number between {range_min} and {range_max}.")
                return
            if num_value < range_min or num_value > range_max:
                await _reply(update, f"Out of range. Must be between {range_min} and {range_max}.")
                return
            # Use int if it's a whole number
            value = str(int(num_value)) if num_value == int(num_value) else str(num_value)
            resp = StandardResponse(
                response_type=ResponseType.RANGE.value,
                value=value,
                raw={"via": "reply_command", "short_id": short_id},
                surface="telegram",
            )
        else:
            await _reply(update, f"Cannot reply to this request type ({response_type.value}).")
            return

        # Record the response
        notification = respond_to_notification(notification_id, resp)
        dispatch_callback(notification)

        # Dismiss on other surfaces
        _dismiss_others(notification_id, notification.delivered_surfaces)

        # Clean up pending store
        prefix = notification_id[:8]
        state.pending_responses.remove(prefix)

        await _reply(update, f"Recorded: {resp.value} for [{short_id}] {notification.title}")

    except Exception as exc:
        logger.error("Reply command failed: %s", exc)
        await _reply(update, f"Error: {exc}")


# ---------------------------------------------------------------------------
# Callback query handler (notification responses)
# ---------------------------------------------------------------------------

async def on_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle inline keyboard button presses.

    Routes by callback_data prefix:
        obs:{command_id}  — execute an Obsidian command from /obs suggestions
        {notif_prefix}:{choice_key} — respond to a notification request
    """
    query = update.callback_query
    await query.answer()  # Dismiss the loading indicator

    state: BotState = context.bot_data["state"]
    data = query.data or ""
    logger.info("TG BTN [%s]: %s", update.effective_chat.id, data[:_TRUNC])

    # --- Obsidian command buttons ---
    if data.startswith("obs:"):
        cmd_id = data[4:]
        if cmd_id == "_cancel":
            await query.edit_message_text("Cancelled.")
            return
        try:
            from work_buddy.obsidian.commands import ObsidianCommands
            from pathlib import Path
            from work_buddy.config import load_config
            cfg = load_config()
            vault = Path(cfg["vault_root"])
            client = ObsidianCommands(vault)
            commands = client.list_commands()
            match = [c for c in commands if c["id"] == cmd_id]
            if match:
                client.execute(cmd_id)
                name = match[0].get("name", cmd_id)
                logger.info("TG OUT [%s]: Executed: %s", update.effective_chat.id, name[:_TRUNC])
                await query.edit_message_text(f"Executed: {name}")
            else:
                await query.edit_message_text(f"Command not found: {cmd_id}")
        except Exception as exc:
            logger.error("Obs button execute failed: %s", exc)
            await query.edit_message_text(f"Command error: {exc}")
        return

    # --- Resume session buttons ---
    if data.startswith("rsm:"):
        sid_prefix = data[4:]
        if sid_prefix == "_cancel":
            await query.edit_message_text("Cancelled.")
            return
        # Look up full session ID from pending store
        full_sid = state.pending_responses.get_notification_id(sid_prefix)
        if not full_sid:
            await query.edit_message_text("Session not found. Try /remote_search again.")
            return
        try:
            from work_buddy.consent import grant_consent
            grant_consent("sidecar:remote_session_launch", mode="always")
            from work_buddy.session_launcher import begin_session
            result = begin_session(session_id=full_sid)
            if result.get("status") == "ok":
                msg = result.get("message", "Session resumed.")
                logger.info("TG OUT [%s]: %s", update.effective_chat.id, msg[:_TRUNC])
                await query.edit_message_text(msg)
            else:
                error = result.get("error", "Unknown error")
                await query.edit_message_text(f"Resume failed: {error}")
        except Exception as exc:
            logger.error("Resume button failed: %s", exc)
            await query.edit_message_text(f"Resume error: {exc}")
        return

    # --- Notification response buttons ---
    # Parse callback_data: "{notif_prefix}:{choice_key}"
    if ":" not in data:
        await query.edit_message_text("Invalid button data.")
        return

    prefix, choice_key = data.split(":", 1)

    # Look up the full notification_id from our pending store
    notification_id = state.pending_responses.get_notification_id(prefix)
    if notification_id is None:
        await query.edit_message_text("This notification has expired or was already answered.")
        return

    # Record the response
    try:
        from work_buddy.notifications.store import respond_to_notification, dispatch_callback
        from work_buddy.notifications.models import StandardResponse, ResponseType

        response = StandardResponse(
            response_type=ResponseType.CHOICE.value,
            value=choice_key,
            raw={"callback_data": data, "telegram_message_id": query.message.message_id},
            surface="telegram",
        )
        notification = respond_to_notification(notification_id, response)

        # Dispatch callback if configured
        dispatch_callback(notification)

        # Dismiss on other surfaces (first-response-wins)
        _dismiss_others(notification_id, notification.delivered_surfaces)

        # Update the message to show the selection
        short_id = notification.short_id or ""
        id_tag = f" [#{short_id}]" if short_id else ""
        reply_text = f"Answered: {choice_key}{id_tag}\n(Notification: {notification_id})"
        logger.info("TG OUT [%s]: %s", update.effective_chat.id, reply_text[:_TRUNC])
        await query.edit_message_text(reply_text)

        # Clean up pending entry
        state.pending_responses.remove(prefix)

    except Exception as exc:
        logger.error("Button response failed: %s", exc)
        await query.edit_message_text(f"Error recording response: {exc}")


# ---------------------------------------------------------------------------
# Free text handler (mobile capture)
# ---------------------------------------------------------------------------

async def on_free_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle plain text messages — treat as mobile capture."""
    _log_inbound(update)
    state: BotState = context.bot_data["state"]
    if not _is_authorized(update, state):
        return  # Silently ignore unauthorized free text

    text = update.message.text
    if not text or not text.strip():
        return

    # Check if this is a reply to a freeform request
    if update.message.reply_to_message:
        msg_id = update.message.reply_to_message.message_id
        notif_id = state.pending_responses.get_by_message_id(msg_id)
        if notif_id:
            await _handle_freeform_reply(update, notif_id, text, state)
            return

    # Default: treat as capture
    await _do_capture(update, text, state)


async def _handle_freeform_reply(
    update: Update,
    notification_id: str,
    text: str,
    state: "BotState",
) -> None:
    """Handle a reply to a freeform or range request notification."""
    try:
        from work_buddy.notifications.store import (
            get_notification, respond_to_notification, dispatch_callback,
        )
        from work_buddy.notifications.models import StandardResponse, ResponseType

        # Determine actual response type for validation
        notif = get_notification(notification_id)
        resp_type = ResponseType(notif.response_type) if notif else ResponseType.FREEFORM

        if resp_type == ResponseType.RANGE:
            # Validate numeric input within bounds
            nr = (notif.number_range if notif else None) or {}
            range_min = nr.get("min", 1)
            range_max = nr.get("max", 10)
            try:
                num_value = float(text.strip())
            except ValueError:
                await _reply(update, f"Expected a number between {range_min} and {range_max}.")
                return
            if num_value < range_min or num_value > range_max:
                await _reply(update, f"Out of range. Must be between {range_min} and {range_max}.")
                return
            value = str(int(num_value)) if num_value == int(num_value) else str(num_value)
            response = StandardResponse(
                response_type=ResponseType.RANGE.value,
                value=value,
                raw={"telegram_message_id": update.message.message_id},
                surface="telegram",
            )
        else:
            response = StandardResponse(
                response_type=ResponseType.FREEFORM.value,
                value=text,
                raw={"telegram_message_id": update.message.message_id},
                surface="telegram",
            )

        notification = respond_to_notification(notification_id, response)
        dispatch_callback(notification)

        # Dismiss on other surfaces (first-response-wins)
        _dismiss_others(notification_id, notification.delivered_surfaces)

        short_id = notification.short_id or ""
        id_tag = f" [#{short_id}]" if short_id else ""
        await _reply(update, f"Response recorded for:{id_tag} {notification.title}")
    except Exception as exc:
        logger.error("Freeform response failed: %s", exc)
        await _reply(update, f"Error recording response: {exc}")


# ---------------------------------------------------------------------------
# Unknown command handler
# ---------------------------------------------------------------------------

async def on_unknown_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle unrecognized /commands."""
    _log_inbound(update)
    cmd = (update.message.text or "").split()[0] if update.message.text else "?"
    await _reply(update, f"Unknown command: {cmd}\n\n{HELP_TEXT}")


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def on_error(
    update: object, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Log errors from PTB handlers."""
    logger.error("Telegram handler error: %s", context.error, exc_info=context.error)

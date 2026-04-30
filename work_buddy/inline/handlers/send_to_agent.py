"""``send-to-agent`` inline command — user handoff to the triage agent.

Replaces the old ``task/new`` handler with a generic "user sent this
selection, figure out what it wants" handoff. Rather than opening a
modal thread and asking the user to confirm a single pre-baked action,
we drop the selection into the same pipeline the background journal
producer uses: a :class:`BackgroundTriageProducer` pass creates exactly
one :class:`TriageItem` with ``source="inline"``, runs it through the
``triage_agent`` local-LLM preset, and lands the agent's verdict in the
pending-review pool for the user to resolve from the dashboard.

The handler returns immediately; the LLM call happens in a daemon
thread so the Obsidian plugin's POST doesn't block on model latency.
"""

from __future__ import annotations

import logging
import threading

from work_buddy.inline.models import InlineContext
from work_buddy.inline.registry import inline_command

logger = logging.getLogger(__name__)


def _run_producer(
    *,
    file_path: str,
    selection: str,
    paragraph: str,
    cursor_line: int,
    hint: str,
) -> None:
    """Thread target — invoke the triage-scan capability and log outcome."""
    try:
        from work_buddy.clarify.capabilities.inline_triage_scan import (
            inline_triage_scan,
        )

        result = inline_triage_scan(
            file_path=file_path,
            selection=selection,
            paragraph=paragraph,
            cursor_line=cursor_line,
            hint=hint,
            force=True,
        )
        logger.info(
            "send-to-agent: triage pass status=%s run_id=%s submitted=%s",
            result.get("status"),
            result.get("run_id"),
            result.get("submitted"),
        )
    except Exception:
        logger.exception("send-to-agent: inline_triage_scan raised")


@inline_command(
    name="send-to-agent",
    surfaces=["menu", "tag"],
    consume_mode="leave",
    persistent=False,
    menu_label="Send to agent",
    interactive=False,
    context_scope="paragraph",
    description="Hand the selection off to the agent via the Review queue.",
)
async def send_to_agent(ctx: InlineContext) -> dict:
    selection = (ctx.selection or "").strip()
    paragraph = (ctx.paragraph or "").strip()
    if not selection and not paragraph:
        return {"status": "empty", "error": "no_text"}

    hint = (getattr(ctx, "hint", "") or "").strip()
    file_path = ctx.file_path or ""
    cursor_line = int(ctx.cursor_line or 0)

    thread = threading.Thread(
        target=_run_producer,
        kwargs={
            "file_path": file_path,
            "selection": selection,
            "paragraph": paragraph,
            "cursor_line": cursor_line,
            "hint": hint,
        },
        name=f"send-to-agent:{file_path[-40:]}",
        daemon=True,
    )
    thread.start()

    return {
        "status": "queued",
        "surface": ctx.surface,
        "file_path": file_path,
    }

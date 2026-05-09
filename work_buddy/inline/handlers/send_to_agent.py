"""``send-to-agent`` inline command — user handoff to the agent.

When the user right-clicks in Obsidian and picks "Send to agent" (or
fires a persistent ``#wb/cmd/...`` capture tag), this handler drops
the selection into the inline pipeline. That pipeline runs a
local-first deadline pre-pass + multi-record verdict, then spawns
1+ Threads carrying the inferred actions for the user to resolve via
the dashboard's Threads tab.

The handler returns immediately; the LLM call happens in a daemon
thread so the Obsidian plugin's POST doesn't block on model latency.

Phase 2 of the clarify -> Threads migration replaced the legacy
``inline_triage_scan`` capability path (which dropped pool entries
for the dead Review tab) with the native ``pipelines.inline_capture``
path that writes directly to the Threads table.
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
    """Thread target — invoke the inline pipeline and log outcome."""
    try:
        from work_buddy.pipelines.inline import inline_capture

        result = inline_capture(
            file_path=file_path,
            selection=selection,
            paragraph=paragraph,
            cursor_line=cursor_line,
            hint=hint,
        )
        logger.info(
            "send-to-agent: inline_capture status=%s umbrella=%s "
            "single=%s children=%d dropped=%d",
            result.get("status"),
            result.get("umbrella_id"),
            result.get("single_thread_id"),
            len(result.get("child_thread_ids") or []),
            result.get("dropped_count", 0),
        )
    except Exception:
        logger.exception("send-to-agent: inline_capture raised")


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

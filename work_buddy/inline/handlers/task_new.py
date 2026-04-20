"""First concrete inline command — propose a task from a selection / tag line.

v1 defers LLM drafting and async confirmation: the handler opens a thread
with the proposed text and returns an ``awaiting_confirmation`` status.
The user answers via the existing thread UI. A follow-up pass will wire
``task_create`` into the confirmation answer.

TODO: LLM-backed drafting, post-confirm ``task_create`` call.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from work_buddy.inline.models import InlineContext
from work_buddy.inline.registry import inline_command
from work_buddy.threads.store import add_message, create_thread

logger = logging.getLogger(__name__)


@inline_command(
    name="task/new",
    surfaces=["menu", "tag"],
    consume_mode="annotate",
    persistent=False,
    menu_label="New task from selection",
    interactive=True,
    context_scope="paragraph",
    description="Create a task from the selected/current text.",
)
async def task_new(ctx: InlineContext) -> dict:
    proposed = ctx.text_for_llm().strip()
    if not proposed:
        return {"status": "empty", "error": "no_text"}

    file_name = Path(ctx.file_path).name if ctx.file_path else "Obsidian"
    source = f"inline:task/new:{os.environ.get('WORK_BUDDY_SESSION_ID', 'unknown')}"
    thread = create_thread(title=f"New task from {file_name}", source=source)

    add_message(
        thread.thread_id,
        "agent",
        f"Proposed task:\n\n> {proposed}",
    )
    add_message(
        thread.thread_id,
        "agent",
        "Create this task?",
        message_type="question",
        response_type="choice",
        choices=[
            {"key": "yes", "label": "Yes, create it"},
            {"key": "no", "label": "No, cancel"},
        ],
    )
    return {
        "status": "awaiting_confirmation",
        "thread_id": thread.thread_id,
        "proposed": proposed,
    }

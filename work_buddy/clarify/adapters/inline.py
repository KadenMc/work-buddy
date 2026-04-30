"""Inline-selection adapter — turns a single user selection (from the
Obsidian right-click "Send to agent" command) into exactly one
:class:`TriageItem` for :class:`BackgroundTriageProducer`.

Unlike the journal adapter, there is nothing to segment: the user has
already isolated the piece they want the agent to reason about. Our
only jobs are:

  1. Pick the richest available text (selection → paragraph).
  2. Derive a short human-friendly label.
  3. Stuff the file path, cursor line, optional user hint, and a
     truncated paragraph snippet into metadata so the agent can decide
     what to do.
  4. Return a content hash so accidental duplicate clicks are caught
     by the producer's idempotence gate (``force=True`` on the
     capability side bypasses this on purpose).
"""

from __future__ import annotations

from work_buddy.logging_config import get_logger
from work_buddy.clarify.background import content_hash
from work_buddy.clarify.items import TriageItem

logger = get_logger(__name__)


def _derive_label(text: str, *, max_chars: int = 72) -> str:
    """First non-empty stripped line, truncated — mirrors journal adapter."""
    for line in (text or "").splitlines():
        stripped = line.strip().lstrip("-*+# ").strip()
        if stripped:
            if len(stripped) > max_chars:
                return stripped[: max_chars - 1] + "…"
            return stripped
    return "(empty selection)"


def collect_inline_selection(
    *,
    file_path: str,
    selection: str,
    paragraph: str,
    cursor_line: int,
    hint: str,
) -> tuple[list[TriageItem], str | None]:
    """Return ``([item], content_hash)`` for the producer.

    Falls back to the surrounding paragraph if the user had nothing
    selected (tag-surface invocations in particular). Returns
    ``([], None)`` when there is genuinely nothing to reason about.
    """
    body = (selection or "").strip() or (paragraph or "").strip()
    if not body:
        return [], None

    label_seed = hint or body
    label = _derive_label(label_seed, max_chars=72)

    ch = content_hash([body, file_path or ""])
    item_id = f"inline_{ch[:12]}"

    item = TriageItem(
        id=item_id,
        text=body,
        label=label,
        source="inline",
        metadata={
            "file_path": file_path or "",
            "cursor_line": int(cursor_line or 0),
            "hint": hint or "",
            "paragraph": (paragraph or "")[:500],
        },
    )
    return [item], ch

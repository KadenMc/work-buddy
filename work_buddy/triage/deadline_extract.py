"""Cheap Haiku-tier deadline / dependency extraction for Clarify (Slice 3).

Runs BEFORE the main Sonnet verdict pass. Detects deadline mentions
("by Friday", "before May 15", "next Tuesday") and dependency mentions
("waiting on Bob", "after the meeting") in a single captured item's
text, returning structured hints. The main verdict pass receives these
as inputs so the resulting ``task_proposal`` can populate Slice 2's
``has_deadline`` / ``deadline_date`` / ``has_dependency`` /
``dependency_hint`` fields without burning frontier-balanced tokens
on a structured-text-classification task.

Failure-tolerant by design: if Haiku errors, returns a sentinel
"no hints found" result rather than raising. The main verdict pass
still runs (just without the hints). Sparse captures get sparse
results — the function does NOT hallucinate dates the text doesn't
mention. The output schema requires explicit booleans + nullable
strings so the model can't sneak through partial guesses.

The function is module-level + caches the LLMRunner instance so
producers can call it once per item without per-call overhead.
"""

from __future__ import annotations

from datetime import date as _date_cls
from typing import Any

from work_buddy.llm import LLMRunner, ModelTier
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


_DEADLINE_SYSTEM_PROMPT = """\
You are extracting deadline + dependency hints from a single captured
note or thought. The output is a structured JSON object — you do NOT
make routing decisions, you only flag what the text mentions.

## Rules

- ``has_deadline=true`` ONLY when the text mentions a specific time
  pressure (a date, a day-of-week, "by EOD", "next week",
  "before X", "needs to be done by Y"). Vague time references that
  don't constrain when the work happens ("eventually", "someday",
  "at some point") do NOT count.
- ``deadline_date`` is your best ISO-8601 (YYYY-MM-DD) interpretation
  when ``has_deadline=true``. For relative phrases ("Friday", "next
  Tuesday"), assume the speaker's perspective is the message_date
  passed in the user prompt; pick the closest future date matching
  the phrase. If the text says only "soon" or "next week" without a
  specific day, leave ``deadline_date`` as null even with
  ``has_deadline=true``.
- ``has_dependency=true`` ONLY when the text mentions something that
  must happen first ("waiting on Bob's review", "after the meeting",
  "when X arrives"). Aspirational dependencies ("ideally after Y but
  not blocking") do NOT count.
- ``dependency_hint`` is a short noun phrase naming the dependency
  ("Bob's code review", "team meeting Thursday"). Null when
  ``has_dependency=false``.
- DO NOT infer deadlines that aren't textually present. The downstream
  resurfacing system relies on this signal being honest — false
  positives cause bad prioritization.

## Examples (illustrative, not exhaustive)

"Need to send the recommendation letter by Friday."
  → has_deadline=true, deadline_date="<this Friday>", has_dependency=false

"Buy gift for Sarah's birthday on May 12"
  → has_deadline=true, deadline_date="2026-05-12", has_dependency=false

"Random thought — should we tighten the threshold on Figure 3?"
  → has_deadline=false, deadline_date=null, has_dependency=false

"After the team meeting Thursday, follow up on the API redesign."
  → has_deadline=false (the work itself isn't deadline-bound),
    has_dependency=true, dependency_hint="team meeting Thursday"
"""


_DEADLINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "has_deadline": {"type": "boolean"},
        "deadline_date": {"type": ["string", "null"]},
        "has_dependency": {"type": "boolean"},
        "dependency_hint": {"type": ["string", "null"]},
    },
    "required": ["has_deadline", "has_dependency"],
    "additionalProperties": False,
}


# Module-level singleton. Producers create N items; we only want
# one runner instance.
_runner: LLMRunner | None = None


def _get_runner() -> LLMRunner:
    global _runner
    if _runner is None:
        _runner = LLMRunner()
    return _runner


def extract_deadline_hints(
    text: str,
    *,
    message_date: _date_cls | str | None = None,
    tier: ModelTier = ModelTier.FRONTIER_FAST,
    item_id: str = "",
) -> dict[str, Any]:
    """Cheap Haiku call extracting deadline / dependency hints.

    Args:
        text: The captured-item text to scan.
        message_date: The date the capture was made. Used so relative
            phrases like "Friday" can be resolved to absolute dates.
            Pass ``None`` to use today's date. Accepts ``date`` or ISO
            string.
        tier: LLM tier. Default ``FRONTIER_FAST`` (Haiku-class).
        item_id: Used in escalation log trace IDs; optional.

    Returns:
        ``{"has_deadline": bool, "deadline_date": str | None,
           "has_dependency": bool, "dependency_hint": str | None}``

        On any LLM failure (timeout, schema violation, empty content,
        rate limit), returns the all-false sentinel — the main verdict
        pass still runs, just without hints. The error is logged.

        ``hint_extraction_failed: True`` is added to the result on
        failure so the main verdict pass can show graceful degradation.
    """
    if not text or not text.strip():
        return {
            "has_deadline": False,
            "deadline_date": None,
            "has_dependency": False,
            "dependency_hint": None,
        }

    if message_date is None:
        message_date = _date_cls.today()
    if isinstance(message_date, _date_cls):
        date_iso = message_date.isoformat()
    else:
        date_iso = str(message_date)

    user_prompt = (
        f"message_date: {date_iso}\n"
        f"\n--- captured text ---\n"
        f"{text.strip()}\n"
        f"--- end ---\n"
        f"\nReturn the JSON object."
    )

    runner = _get_runner()
    try:
        resp = runner.call(
            tier=tier,
            system=_DEADLINE_SYSTEM_PROMPT,
            user=user_prompt,
            output_schema=_DEADLINE_SCHEMA,
            cache_ttl_minutes=0,  # No caching: text-keyed but the
                                  # message_date in the prompt makes
                                  # the cache key unstable across days.
            trace_id=f"deadline_extract:{item_id}" if item_id else "deadline_extract",
        )
    except Exception as exc:
        logger.warning("deadline_extract: runner.call threw: %s", exc)
        return _failure_sentinel()

    if resp.is_error():
        logger.info(
            "deadline_extract: %s tier=%s — %s; falling back to no-hints",
            resp.error_kind, resp.tier_used, resp.error,
        )
        return _failure_sentinel()

    out = resp.structured_output or {}
    # Defensive normalization — booleans + nullable strings.
    return {
        "has_deadline": bool(out.get("has_deadline")),
        "deadline_date": out.get("deadline_date") or None,
        "has_dependency": bool(out.get("has_dependency")),
        "dependency_hint": out.get("dependency_hint") or None,
    }


def _failure_sentinel() -> dict[str, Any]:
    return {
        "has_deadline": False,
        "deadline_date": None,
        "has_dependency": False,
        "dependency_hint": None,
        "hint_extraction_failed": True,
    }


def merge_hints_into_records(
    records: list[dict[str, Any]] | None,
    hints: dict[str, Any],
) -> list[dict[str, Any]]:
    """Stamp deadline hints onto each task_proposal in records[].

    Idempotent: if a record's task_proposal already declares
    ``has_deadline`` / ``has_dependency`` (e.g., the Sonnet verdict
    ALSO independently extracted them), the existing values WIN. The
    Haiku pass is a hint, not a constraint.

    Returns the records list (mutating in place is fine but the
    return makes the call site read cleaner).
    """
    if not records:
        return records or []
    if not hints:
        return records

    fields_to_merge = (
        "has_deadline", "deadline_date",
        "has_dependency", "dependency_hint",
    )
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("destination") != "task":
            continue
        proposal = rec.get("task_proposal")
        if not isinstance(proposal, dict):
            continue
        for f in fields_to_merge:
            if f in proposal and proposal[f] not in (None, ""):
                # Sonnet already filled it; honor the verdict.
                continue
            proposal[f] = hints.get(f)
    return records

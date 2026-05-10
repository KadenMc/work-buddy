"""Generic text-segmentation SubCall: split prose into distinct *matters*.

A "matter" is a coherent subject the user might think of as one thing
(one task, one question, one observation). Source pipelines that need
to detect "did the user select N unrelated things in one capture?"
call into this segmenter and route each segment to its own per-matter
spawn primitive (see :mod:`work_buddy.pipelines.singular`).

Used today by :mod:`work_buddy.pipelines.inline` (right-click "Send to
agent") to split a selection into matters before running the verdict.
Reusable by any future singular-input pipeline (per-message email
triage, etc.).

Design:

- Built on the :class:`work_buddy.llm.SubCall` framework so it inherits
  tier-chain walking, schema-validated structured output, soft-fail
  policy, observability, and config-driven dials.
- Output schema: a list of segments, each carrying ``start_char``,
  ``end_char``, and a short ``label``. Offsets index into the input
  text.
- Soft-fail default: empty list. Caller treats as a single-matter
  passthrough (don't fragment the user's input on segmenter failure).
- Short-text bypass: skip the LLM entirely for selections that are
  BOTH under ``short_text_bypass_chars`` (default 120) AND visually
  one logical block (≤1 significant newline, where bullet-prefixed
  newlines like ``\\n- foo`` don't count as significant). The
  multi-line clause matters because a short multi-matter capture —
  e.g. ``"Email Bob about the report.\\n\\nRenew car insurance Friday."``
  — would be 56 chars but contains two distinct matters; bypassing on
  char count alone conflates them. A short bullet list — e.g.
  ``"Read research paper\\n- Paper A\\n- Paper B"`` — IS one matter
  even with multiple newlines, so the bullet-aware count keeps it
  bypassed.
- Bias-toward-cohesion in the prompt: only split when there's clear
  textual evidence of distinct subjects (different topics, conjunction
  shifts, paragraph breaks with no semantic continuity). Mirrors
  :mod:`project_picker`'s "lean toward null when uncertain" stance.

What this segmenter does NOT subsume:

- Journal-line range segmentation. The journal pipeline has its own
  segmenter (``clarify/adapters/journal.py:_segment_with_escalation``)
  with line-range output and post-parse semantic-validation-driven
  escalation. That escalation pattern doesn't fit SubCall today.
  Migrating journal to this generic segmenter is future work after
  SubCall grows a ``validate_post_parse`` hook.
"""

from __future__ import annotations

import re
from typing import Any

from work_buddy.llm import LLMRunner, SubCall, run_subcall
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# Newlines immediately followed by a bullet marker (``-``, ``*``,
# ``+``, or ``1.``-style numbered list) are list-item continuations
# of the line above them, not matter boundaries. Subtracting them
# from the raw newline count gives a "significant newlines" score
# that the short-text bypass uses to distinguish "short single block"
# from "short but visually multi-matter."
_BULLET_NEWLINE_RE = re.compile(r"\n[ \t]*(?:[-*+]|\d+\.)\s")


def _significant_newline_count(text: str) -> int:
    """Count newlines that likely separate distinct matters.

    Newlines followed by a bullet marker (``- foo``, ``* foo``,
    ``+ foo``, ``1. foo``, with optional leading whitespace) are
    treated as list-item separators within one matter, not as
    inter-matter boundaries — and thus excluded from the count.

    Examples::

        "Email Bob"                              → 0
        "Email Bob\\nFollow up Friday"            → 1
        "Email Bob.\\n\\nRenew car insurance"     → 2  (multi-matter)
        "Read paper\\n- Paper A\\n- Paper B"      → 0  (one matter, bulleted)
        "A\\n\\nB\\n- bullet under B"             → 2  (the \\n\\n pair counts;
                                                       the \\n- doesn't)
    """
    if not text:
        return 0
    return text.count("\n") - len(_BULLET_NEWLINE_RE.findall(text))


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


_TEXT_SEGMENTER_SYSTEM_PROMPT = """\
You are splitting a captured text selection into discrete *matters*.

A matter is a coherent subject the user would think of as ONE thing —
one task, one question, one observation, one event. Multiple proposed
actions on the SAME matter (e.g. "buy gift for Sarah's birthday May 12"
yields a task + a calendar event) is still ONE matter; they share a
single underlying subject.

## Your output

A JSON object with one field, ``segments``: an array of entries, each
with:

- ``start_char``: integer offset (inclusive) into the input text
  where this segment begins.
- ``end_char``: integer offset (exclusive) where this segment ends.
- ``label``: a short noun-phrase (≤8 words) summarising what THIS
  matter is about. The label is for human review and routing — keep
  it terse and specific.

Segments are non-overlapping and ordered by ``start_char`` ascending.
Whitespace/blank-line gaps between segments are fine.

## Hard rules

1. **Bias strongly toward "one matter"**. If you're not confident the
   selection contains distinct subjects, return a SINGLE segment
   covering the whole input. False-splits create user-visible thread
   fragmentation; false-merges absorb cleanly into the multi-action
   pattern downstream.
2. **Only split when there's clear textual evidence of distinct
   subjects.** Strong signals: different topics with no semantic
   bridge, separating conjunctions ("Also,", "Separately,"),
   paragraph breaks that introduce a new subject, distinct
   imperatives addressing unrelated work.
3. **Multiple ACTIONS on the same matter is NOT a split signal.**
   "Buy gift for Sarah's birthday on May 12" is ONE matter (the
   birthday) — it'll yield two records (task + calendar event)
   downstream, but those records are about the same subject. Don't
   split it.
4. **Never split inside a sentence.** Segment boundaries align with
   sentence-end or paragraph-end positions.
5. **Cover the entire input** — every non-whitespace character should
   appear in some segment. Whitespace gaps between segments are OK.
6. **Soft cap: at most 6 segments.** If you'd produce more, you're
   probably over-splitting; merge related ones.

## Examples

INPUT (one matter, one segment):
"Buy gift for Sarah's birthday on May 12"
→ ``[{start: 0, end: 41, label: "Sarah's birthday gift"}]``

INPUT (one matter despite two actions implied):
"Email Bob about the report by Friday — needs his sign-off"
→ ``[{start: 0, end: 56, label: "Email Bob re report"}]``

INPUT (TWO distinct matters):
"Email Bob about the report. Also, renew car insurance by Friday."
→ ``[{start: 0, end: 27, label: "Email Bob re report"},
       {start: 28, end: 64, label: "Renew car insurance"}]``

INPUT (one matter — a multi-line GTD framework reference):
"GTD: Six horizons of focus
1. Purpose
2. Vision
3. Goals
..."
→ ``[{start: 0, end: <full length>, label: "GTD horizons of focus"}]``

INPUT (THREE matters):
"Need to draft the TKA paper intro by Monday.
Reminder: dentist appointment Tuesday.
Should I follow up with the JAMA reviewer?"
→ ``[{start: 0, end: 44, label: "TKA paper intro draft"},
       {start: 45, end: 84, label: "Dentist appointment"},
       {start: 85, end: 124, label: "JAMA reviewer follow-up"}]``

## Output the JSON object exactly. No prose around it.
"""


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


_TEXT_SEGMENTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "start_char": {"type": "integer"},
                    "end_char": {"type": "integer"},
                    "label": {"type": "string"},
                },
                "required": ["start_char", "end_char", "label"],
            },
        },
    },
    "required": ["segments"],
}


# ---------------------------------------------------------------------------
# User-prompt builder
# ---------------------------------------------------------------------------


def _build_text_segmenter_user_prompt(inputs: dict[str, Any]) -> str:
    """SubCall user-prompt builder.

    Reads:
        ``text``: the captured-item body to segment.
        ``hint``: optional user-typed intent hint (passed via the
            inline modal, etc.). Empty string when absent.
    """
    text = inputs.get("text") or ""
    hint = (inputs.get("hint") or "").strip()
    hint_block = f"\n## User hint\n\n{hint}\n" if hint else ""
    return (
        f"## Captured text\n\n"
        f"{text}\n"
        f"{hint_block}"
        f"\n## Length\n\n"
        f"The captured text is {len(text)} characters long.\n"
        f"\nReturn the JSON object."
    )


# ---------------------------------------------------------------------------
# Soft-fail default
# ---------------------------------------------------------------------------


# Empty segments list. Caller treats this as "no segmentation produced;
# pass through as a single matter." Defensive: never fragment the user's
# input on segmenter failure.
_FAILURE_DEFAULT: dict[str, Any] = {"segments": []}


TEXT_SEGMENTER_SUBCALL = SubCall(
    name="text_segmenter",
    system_prompt=_TEXT_SEGMENTER_SYSTEM_PROMPT,
    user_prompt=_build_text_segmenter_user_prompt,
    output_schema=_TEXT_SEGMENTER_SCHEMA,
    config_key="triage.text_segmenter",
    fail_policy="soft",
    soft_fail_default=_FAILURE_DEFAULT,
)


# ---------------------------------------------------------------------------
# Validation / post-processing
# ---------------------------------------------------------------------------


def _validate_and_normalize_segments(
    raw_output: dict[str, Any],
    *,
    text: str,
    max_segments: int = 6,
    coverage_floor: float = 0.85,
) -> list[dict[str, Any]]:
    """Validate and clean the SubCall output into final segments.

    Operations performed (in order):

    1. Coerce ``start_char`` / ``end_char`` to ints; drop entries where
       coercion fails or where ``start_char >= end_char`` or where
       offsets fall outside the input text.
    2. Sort by ``start_char`` ascending.
    3. Drop segments that overlap with an earlier segment.
    4. Cap at ``max_segments``.
    5. Coverage check: if the union of all segments covers less than
       ``coverage_floor`` fraction of the non-whitespace input, distrust
       the segmentation entirely and return [] (caller treats as
       passthrough). The model probably hallucinated boundaries.
    6. Attach the ``text`` slice (``text[start:end]``) to each kept
       segment as a ``text`` field for caller convenience.

    Returns a list of dicts ``[{start_char, end_char, label, text}, ...]``,
    or ``[]`` on validation failure / soft-fail.
    """
    raw = raw_output.get("segments") or []
    text_len = len(text)

    cleaned: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            start = int(entry.get("start_char"))
            end = int(entry.get("end_char"))
        except (TypeError, ValueError):
            continue
        if start < 0 or end > text_len or start >= end:
            continue
        label = entry.get("label") or ""
        if not isinstance(label, str):
            label = str(label)
        cleaned.append({
            "start_char": start,
            "end_char": end,
            "label": label.strip(),
        })

    cleaned.sort(key=lambda s: s["start_char"])

    # Drop overlapping segments — keep first occurrence only.
    nonoverlapping: list[dict[str, Any]] = []
    last_end = 0
    for s in cleaned:
        if s["start_char"] < last_end:
            logger.info(
                "text_segmenter: dropping overlapping segment "
                "start=%d end=%d (last end=%d)",
                s["start_char"], s["end_char"], last_end,
            )
            continue
        nonoverlapping.append(s)
        last_end = s["end_char"]

    if max_segments and len(nonoverlapping) > max_segments:
        logger.info(
            "text_segmenter: %d segments exceeds cap %d; truncating",
            len(nonoverlapping), max_segments,
        )
        nonoverlapping = nonoverlapping[:max_segments]

    # Coverage check: drop the whole result if too many non-whitespace
    # chars are uncovered. The model probably hallucinated.
    total_chars = sum(1 for c in text if not c.isspace())
    if total_chars > 0:
        covered_chars = 0
        for s in nonoverlapping:
            slice_text = text[s["start_char"]:s["end_char"]]
            covered_chars += sum(1 for c in slice_text if not c.isspace())
        coverage = covered_chars / total_chars
        if coverage < coverage_floor:
            logger.warning(
                "text_segmenter: coverage %.2f below floor %.2f; "
                "discarding segmentation (caller passthrough)",
                coverage, coverage_floor,
            )
            return []

    # Attach text slices.
    for s in nonoverlapping:
        s["text"] = text[s["start_char"]:s["end_char"]]

    return nonoverlapping


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def segment_into_matters(
    text: str,
    *,
    hint: str = "",
    item_id: str = "",
    short_text_bypass_chars: int = 120,
    runner: LLMRunner | None = None,
) -> list[dict[str, Any]]:
    """Split ``text`` into a list of distinct-matter segments.

    Returns a list of dicts ``[{start_char, end_char, label, text}, ...]``.
    ALWAYS returns at least one segment when the input has any
    non-whitespace content (the whole input as a single matter when
    bypassed or when the SubCall soft-fails).

    Bypass rules:

    - Empty / whitespace-only ``text`` → ``[]`` (caller treats as no work).
    - **Short single-block bypass**: when ``len(text) <
      short_text_bypass_chars`` AND the text contains ≤1 significant
      newline (bullet-prefixed newlines don't count), skip the LLM
      and return a single-segment passthrough. Both clauses matter
      independently:
      - The char-count clause alone misses short multi-matter
        captures: ``"Email Bob.\\n\\nRenew car insurance Friday."``
        is 56 chars but is two distinct matters that the user expects
        to land as two separate threads.
      - The newline clause alone would over-segment short bullet lists:
        ``"Read paper\\n- Paper A\\n- Paper B"`` has multiple newlines
        but is one matter; the bullet-aware count of significant
        newlines is 0, so it stays bypassed.
    - SubCall soft-fail (every tier exhausts) → single-segment with
      the whole input. Worst case: behaves like always-singular.

    Args:
        text: The captured-item body to segment.
        hint: Optional user-typed intent hint. Empty string when absent.
        item_id: Used in the trace_id for ``escalation_log`` correlation.
        short_text_bypass_chars: Lower bound below which segmentation
            is skipped (when paired with the single-block check).
            Defaults to 120 chars (roughly two short sentences).
        runner: Optional :class:`LLMRunner` override for tests.
    """
    text_clean = text or ""
    if not text_clean.strip():
        return []

    # Short single-block bypass — see docstring for the both-clauses
    # rationale. ≤1 significant newline approximates "one paragraph"
    # without falsely tripping on bullet-list formatting.
    is_short = len(text_clean) < short_text_bypass_chars
    is_single_block = _significant_newline_count(text_clean) <= 1
    if is_short and is_single_block:
        return [{
            "start_char": 0,
            "end_char": len(text_clean),
            "label": "(short capture)",
            "text": text_clean,
        }]

    inputs = {"text": text_clean, "hint": hint or ""}
    trace_id = (
        f"text_segmenter:{item_id}" if item_id else "text_segmenter"
    )

    # Resolve max_segments from config (with hard fallback).
    max_segments = _resolve_max_segments()

    result = run_subcall(
        TEXT_SEGMENTER_SUBCALL,
        inputs,
        trace_id=trace_id,
        runner=runner,
    )

    cleaned = _validate_and_normalize_segments(
        result.output or {},
        text=text_clean,
        max_segments=max_segments,
    )

    if not cleaned:
        # Soft-fail or model output rejected → passthrough.
        return [{
            "start_char": 0,
            "end_char": len(text_clean),
            "label": "(unsegmented)",
            "text": text_clean,
        }]

    return cleaned


def _resolve_max_segments() -> int:
    """Read ``triage.text_segmenter.max_segments`` from config.

    Falls back to 6 when the config block is missing or unreadable.
    """
    try:
        from work_buddy.clarify.config import load_triage_config

        cfg = load_triage_config() or {}
    except Exception as exc:
        logger.warning(
            "text_segmenter: load_triage_config failed (%s); using "
            "max_segments=6",
            exc,
        )
        return 6
    block = cfg.get("text_segmenter") or {}
    cap = block.get("max_segments")
    if isinstance(cap, int) and cap > 0:
        return cap
    return 6


__all__ = [
    "TEXT_SEGMENTER_SUBCALL",
    "segment_into_matters",
]

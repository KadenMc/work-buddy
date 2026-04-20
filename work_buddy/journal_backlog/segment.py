"""Thread segmentation utilities for journal backlog processing.

Provides ID generation, banner stripping, segmentation validation,
thread extraction, and manifest handling for LLM-tagged Running Notes.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Banner pattern: ***'Running Notes / Considerations' carried over from YYYY-MM-DD***
_BANNER_RE = re.compile(
    r"^\*{3}'Running Notes\s*/\s*Considerations'\s*carried over from\s+"
    r"(\d{4}-\d{2}-\d{2})\*{3}\s*$",
    re.MULTILINE,
)

# Thread tag patterns
_OPEN_TAG_RE = re.compile(r"^<!--\s*\[(t_[a-f0-9]{6})\]\s*-->\s*$")
_CLOSE_TAG_RE = re.compile(r"^<!--\s*\[/(t_[a-f0-9]{6})\]\s*-->\s*$")
_MULTI_TAG_RE = re.compile(r"^<!--\s*\[multi\]\s*-->\s*$")
_THREAD_ID_RE = re.compile(r"^t_[a-f0-9]{6}$")

# Structural separators (banner boundaries, not user content)
_SEPARATOR_RE = re.compile(r"^-{3,}\s*$")


def generate_thread_ids(count: int = 50) -> list[str]:
    """Generate a pool of unique thread IDs.

    Args:
        count: Number of IDs to generate.

    Returns:
        Sorted list of unique IDs in format ``t_`` + 6 hex chars.
    """
    ids: set[str] = set()
    while len(ids) < count:
        ids.add(f"t_{uuid.uuid4().hex[:6]}")
    return sorted(ids)


def strip_banners(text: str) -> tuple[str, list[str], list[tuple[int, str]]]:
    """Remove carried-over banners from Running Notes text.

    Args:
        text: Raw Running Notes section content.

    Returns:
        Tuple of:
        - Cleaned text with banners and structural separators removed.
        - List of source date strings extracted from banners.
        - Banner date map: list of (line_number, date_str) tuples for
          attributing threads to source dates.
    """
    source_dates: list[str] = []
    banner_date_map: list[tuple[int, str]] = []
    lines = text.split("\n")
    cleaned_lines: list[str] = []
    current_date: str | None = None

    # Track whether we're in a "banner zone" (banner + surrounding separators)
    prev_was_banner_or_sep = False

    for i, line in enumerate(lines):
        banner_match = _BANNER_RE.match(line)
        if banner_match:
            date_str = banner_match.group(1)
            source_dates.append(date_str)
            current_date = date_str
            banner_date_map.append((len(cleaned_lines), date_str))
            prev_was_banner_or_sep = True
            continue

        if _SEPARATOR_RE.match(line):
            # Only strip separators adjacent to banners
            if prev_was_banner_or_sep:
                continue
            # Check if next non-empty line is a banner
            next_content = _peek_next_content(lines, i + 1)
            if next_content is not None and _BANNER_RE.match(next_content):
                prev_was_banner_or_sep = True
                continue

        prev_was_banner_or_sep = False
        cleaned_lines.append(line)

    # Collapse multiple consecutive blank lines to at most one
    result_lines: list[str] = []
    blank_count = 0
    for line in cleaned_lines:
        if line.strip() == "":
            blank_count += 1
            if blank_count <= 1:
                result_lines.append(line)
        else:
            blank_count = 0
            result_lines.append(line)

    # Strip trailing whitespace
    while result_lines and result_lines[-1].strip() == "":
        result_lines.pop()

    cleaned = "\n".join(result_lines)
    return cleaned, source_dates, banner_date_map


def _peek_next_content(lines: list[str], start: int) -> str | None:
    """Find the next non-empty line starting from index ``start``."""
    for i in range(start, len(lines)):
        if lines[i].strip():
            return lines[i]
    return None


def validate_segmentation(
    tagged_text: str, original_text: str
) -> dict[str, Any]:
    """Validate that LLM-produced segmentation is complete and consistent.

    Checks:
    1. Balanced open/close tags
    2. No nested threads
    3. Content preservation (no lines added, removed, or modified)
    4. Complete coverage (no orphaned content)
    5. ID format compliance

    Args:
        tagged_text: The LLM-annotated text with thread tags.
        original_text: The banner-stripped original text (from strip_banners).

    Returns:
        Dict with ``valid``, ``thread_count``, ``thread_ids``, ``errors``,
        ``warnings`` keys.
    """
    errors: list[str] = []
    warnings: list[str] = []

    tagged_lines = tagged_text.split("\n")

    # Collect open and close tag IDs
    open_ids: list[str] = []
    close_ids: list[str] = []

    for line in tagged_lines:
        om = _OPEN_TAG_RE.match(line.strip())
        if om:
            open_ids.append(om.group(1))
        cm = _CLOSE_TAG_RE.match(line.strip())
        if cm:
            close_ids.append(cm.group(1))

    open_set = set(open_ids)
    close_set = set(close_ids)

    # --- Check 1: balanced tags ---
    unmatched_open = open_set - close_set
    unmatched_close = close_set - open_set
    if unmatched_open:
        errors.append(f"Open tags without close: {sorted(unmatched_open)}")
    if unmatched_close:
        errors.append(f"Close tags without open: {sorted(unmatched_close)}")

    # Duplicate open tags
    if len(open_ids) != len(open_set):
        seen: set[str] = set()
        dupes: set[str] = set()
        for tid in open_ids:
            if tid in seen:
                dupes.add(tid)
            seen.add(tid)
        errors.append(f"Duplicate open tags: {sorted(dupes)}")

    # --- Check 2: no nesting ---
    current_thread: str | None = None
    for line_num, line in enumerate(tagged_lines, 1):
        stripped = line.strip()
        om = _OPEN_TAG_RE.match(stripped)
        cm = _CLOSE_TAG_RE.match(stripped)

        if om:
            if current_thread is not None:
                errors.append(
                    f"Nested thread: {om.group(1)} opened inside "
                    f"{current_thread} at line {line_num}"
                )
            current_thread = om.group(1)
        elif cm:
            if current_thread is None:
                errors.append(
                    f"Close tag {cm.group(1)} without open at line {line_num}"
                )
            elif cm.group(1) != current_thread:
                errors.append(
                    f"Mismatched close: expected {current_thread}, "
                    f"got {cm.group(1)} at line {line_num}"
                )
            current_thread = None

    if current_thread is not None:
        errors.append(f"Thread {current_thread} still open at end of text")

    # --- Check 3: content preservation ---
    original_content = _content_lines(original_text)
    tagged_content = _content_lines(
        _strip_all_tags(tagged_text)
    )

    if len(original_content) != len(tagged_content):
        errors.append(
            f"Content line count mismatch: "
            f"{len(original_content)} original vs {len(tagged_content)} tagged"
        )
    else:
        for i, (orig, tagged) in enumerate(
            zip(original_content, tagged_content)
        ):
            if orig != tagged:
                errors.append(
                    f"Content modified at line {i + 1}: "
                    f"{orig[:60]!r} -> {tagged[:60]!r}"
                )
                if len(errors) > 10:
                    errors.append("(further content differences truncated)")
                    break

    # --- Check 4: complete coverage ---
    current_thread = None
    for line_num, line in enumerate(tagged_lines, 1):
        stripped = line.strip()
        om = _OPEN_TAG_RE.match(stripped)
        cm = _CLOSE_TAG_RE.match(stripped)
        is_multi = _MULTI_TAG_RE.match(stripped)
        is_tag = om or cm or is_multi

        if om:
            current_thread = om.group(1)
            continue
        if cm:
            current_thread = None
            continue
        if is_multi:
            continue

        # Non-tag, non-empty line outside a thread
        if not is_tag and stripped and current_thread is None:
            # Allow blank lines and separators outside threads
            if not _SEPARATOR_RE.match(stripped):
                errors.append(
                    f"Orphaned content at line {line_num}: {stripped[:60]!r}"
                )

    # --- Check 5: ID format ---
    all_ids = open_set | close_set
    for tid in all_ids:
        if not _THREAD_ID_RE.match(tid):
            errors.append(f"Invalid thread ID format: {tid!r}")

    thread_ids = sorted(open_set & close_set)
    return {
        "valid": len(errors) == 0,
        "thread_count": len(thread_ids),
        "thread_ids": thread_ids,
        "errors": errors,
        "warnings": warnings,
    }


def extract_threads(
    tagged_text: str,
    banner_date_map: list[tuple[int, str]] | None = None,
) -> list[dict[str, Any]]:
    """Extract thread objects from validated tagged text.

    Args:
        tagged_text: LLM-annotated text that passed validation.
        banner_date_map: Optional list of (line_number, date_str) tuples
            from ``strip_banners()`` for source date attribution.

    Returns:
        List of thread dicts, each with ``id``, ``raw_text``,
        ``line_count``, ``source_dates``, ``has_multi_flag``.
    """
    threads: dict[str, dict[str, Any]] = {}
    current_thread: str | None = None
    current_lines: list[str] = []
    current_start_line: int = 0
    has_multi = False

    lines = tagged_text.split("\n")
    content_line_num = 0  # tracks position in banner-stripped space

    for line in lines:
        stripped = line.strip()
        om = _OPEN_TAG_RE.match(stripped)
        cm = _CLOSE_TAG_RE.match(stripped)
        is_multi = _MULTI_TAG_RE.match(stripped)

        if om:
            current_thread = om.group(1)
            current_lines = []
            current_start_line = content_line_num
            has_multi = False
            continue

        if cm and current_thread:
            raw = "\n".join(current_lines)
            non_empty = [l for l in current_lines if l.strip()]
            source_dates = _attribute_dates(
                current_start_line, content_line_num, banner_date_map
            )
            threads[current_thread] = {
                "id": current_thread,
                "raw_text": raw,
                "line_count": len(non_empty),
                "source_dates": source_dates,
                "has_multi_flag": has_multi,
            }
            current_thread = None
            current_lines = []
            has_multi = False
            continue

        if is_multi:
            has_multi = True
            continue

        if current_thread is not None:
            current_lines.append(line)

        # Count content lines for banner date mapping
        if stripped and not om and not cm and not is_multi:
            content_line_num += 1

    result = list(threads.values())
    logger.info(f"Extracted {len(result)} threads from tagged text")
    return result


# ---------------------------------------------------------------------------
# Line-range segmentation (local-model friendly path)
# ---------------------------------------------------------------------------
#
# The tagged-text segmentation above requires the model to re-emit the
# entire input with <!-- [t_...] --> tags wrapping each thread, then we
# validate byte-for-byte content preservation. That's a good fit for an
# interactive Claude-driven workflow but wasteful for background local-
# model runs: output size is O(input), reasoning budget is often blown
# before the model finishes reciting the input, and any whitespace drift
# invalidates the whole attempt.
#
# The line-range path below flips the contract: we number the input
# lines, the model only emits a mapping of ``thread_id → line numbers``,
# and we reconstruct the same thread dicts locally. Output size is
# O(threads), not O(input), and no content drift is possible because
# the model never emits content.


def number_lines(text: str) -> tuple[str, list[str]]:
    """Prefix each line of ``text`` with a 1-based line number.

    Returns ``(numbered_text, original_lines)``. Blank lines are
    numbered too — they are real positions in the source and may
    legitimately fall inside a thread. Callers feed the numbered
    text to the LLM and use ``original_lines`` to reconstruct
    thread ``raw_text`` from the returned line numbers.

    The format is stable and chosen to be easy for small models:
    ``"<N>| <line content>"`` — pipe and space, no padding.
    """
    lines = text.split("\n")
    numbered = "\n".join(f"{i + 1}| {line}" for i, line in enumerate(lines))
    return numbered, lines


# JSON schema for constrained output. Keeps the model honest about
# the exact shape without requiring prose-level instruction
# discipline.
LINE_RANGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["threads"],
    "properties": {
        "threads": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "lines"],
                "properties": {
                    "id": {"type": "string"},
                    "lines": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1},
                    },
                    "multi": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def validate_line_range_segmentation(
    segmentation: dict[str, Any],
    original_lines: list[str],
    id_pool: list[str] | None = None,
) -> dict[str, Any]:
    """Validate a line-range segmentation output.

    Checks:

    1. Structure — ``threads`` is a list of ``{id, lines, multi?}`` dicts.
    2. ID format — each id matches ``t_`` + 6 hex chars.
    3. ID pool — if ``id_pool`` is provided, every id is in the pool.
    4. Line numbers — every ``line`` is an integer in
       ``[1, len(original_lines)]``.
    5. Coverage — every non-blank source line appears in at least
       one thread. Lines may appear in multiple threads, in which
       case the thread should set ``multi: true``.
    6. No duplicate ids.

    Args:
        segmentation: The parsed JSON output from the LLM.
        original_lines: The list returned by :func:`number_lines`.
        id_pool: Optional list of legal thread ids.

    Returns:
        Dict with ``valid``, ``thread_count``, ``thread_ids``,
        ``errors``, ``warnings``.
    """
    errors: list[str] = []
    warnings: list[str] = []
    threads_raw = segmentation.get("threads") if isinstance(segmentation, dict) else None

    if not isinstance(threads_raw, list):
        return {
            "valid": False,
            "thread_count": 0,
            "thread_ids": [],
            "errors": ["Output missing 'threads' array"],
            "warnings": [],
        }

    pool_set = set(id_pool) if id_pool else None
    n_lines = len(original_lines)
    # Coverage is required for content lines only. Structural markers
    # (standalone ``---`` separators) carry boundary information but
    # are not content — the model MAY group them into an adjacent
    # thread, but it isn't required to. Forcing coverage on them
    # would spawn empty-looking threads around decorative lines.
    non_blank_lines = {
        i + 1
        for i, line in enumerate(original_lines)
        if line.strip() and not _SEPARATOR_RE.match(line.strip())
    }

    seen_ids: set[str] = set()
    cited: set[int] = set()
    cited_multi: set[int] = set()
    valid_threads: list[dict[str, Any]] = []

    for idx, raw in enumerate(threads_raw):
        if not isinstance(raw, dict):
            errors.append(f"Thread at index {idx} is not an object")
            continue

        tid = raw.get("id")
        if not isinstance(tid, str) or not _THREAD_ID_RE.match(tid):
            errors.append(f"Thread at index {idx} has invalid id: {tid!r}")
            continue
        if tid in seen_ids:
            errors.append(f"Duplicate thread id: {tid!r}")
            continue
        seen_ids.add(tid)

        if pool_set is not None and tid not in pool_set:
            errors.append(f"Thread id {tid!r} not in provided pool")

        lines = raw.get("lines")
        if not isinstance(lines, list) or not lines:
            errors.append(f"Thread {tid!r} has empty or invalid 'lines'")
            continue

        cleaned_lines: list[int] = []
        for ln in lines:
            if not isinstance(ln, int):
                errors.append(f"Thread {tid!r} has non-integer line: {ln!r}")
                continue
            if ln < 1 or ln > n_lines:
                errors.append(
                    f"Thread {tid!r} line {ln} out of range [1, {n_lines}]"
                )
                continue
            cleaned_lines.append(ln)

        if not cleaned_lines:
            continue

        is_multi = bool(raw.get("multi"))
        for ln in cleaned_lines:
            if ln in cited and not is_multi:
                # Line is in another thread already; flagging as multi
                # is required to allow overlap.
                other_tid = next(
                    (t["id"] for t in valid_threads if ln in t["lines"]),
                    "?",
                )
                errors.append(
                    f"Line {ln} cited by {tid!r} and {other_tid!r} "
                    f"but neither sets multi=true"
                )
            cited.add(ln)
            if is_multi:
                cited_multi.add(ln)

        valid_threads.append({
            "id": tid,
            "lines": sorted(set(cleaned_lines)),
            "multi": is_multi,
        })

    missing = sorted(non_blank_lines - cited)
    if missing:
        errors.append(
            f"{len(missing)} non-blank line(s) not assigned to any thread: "
            f"{missing[:10]}"
            + ("…" if len(missing) > 10 else "")
        )

    return {
        "valid": len(errors) == 0,
        "thread_count": len(valid_threads),
        "thread_ids": [t["id"] for t in valid_threads],
        "threads": valid_threads,
        "errors": errors,
        "warnings": warnings,
    }


def build_threads_from_line_ranges(
    validated: dict[str, Any],
    original_lines: list[str],
    banner_date_map: list[tuple[int, str]] | None = None,
) -> list[dict[str, Any]]:
    """Reconstruct thread objects from a validated line-range segmentation.

    Output shape matches :func:`extract_threads` so downstream code
    (adapters, UI, tests) doesn't care which segmentation path
    produced the threads.

    Args:
        validated: Output of :func:`validate_line_range_segmentation`
            (with ``valid=True``).
        original_lines: The list from :func:`number_lines`.
        banner_date_map: Optional ``(line_number, date_str)`` tuples
            from :func:`strip_banners` for source-date attribution.
            Same semantics as :func:`extract_threads`.

    Returns:
        List of thread dicts with ``id``, ``raw_text``, ``line_count``,
        ``source_dates``, ``has_multi_flag``.
    """
    threads: list[dict[str, Any]] = []
    for t in validated.get("threads", []) or []:
        lines = t.get("lines", []) or []
        if not lines:
            continue
        raw = "\n".join(original_lines[ln - 1] for ln in lines)
        non_empty = [ln for ln in lines if original_lines[ln - 1].strip()]
        start_line = min(lines) - 1
        end_line = max(lines) - 1
        source_dates = _attribute_dates(
            start_line, end_line, banner_date_map,
        )
        threads.append({
            "id": t["id"],
            "raw_text": raw,
            "line_count": len(non_empty),
            "source_dates": source_dates,
            "has_multi_flag": bool(t.get("multi")),
        })
    return threads


def repair_line_range_segmentation(
    segmentation: dict[str, Any],
    validation_result: dict[str, Any],
    original_lines: list[str],
    id_pool: list[str],
) -> dict[str, Any]:
    """Build a structured repair prompt from a failed line-range segmentation.

    Analogous to :func:`repair_segmentation` but for the line-range
    output format. Errors cluster around four patterns:

    - **missing_coverage**: one or more non-blank lines unassigned
    - **overlap_without_multi**: a line is in two threads, neither
      marks ``multi: true``
    - **bad_id**: id format invalid or outside the pool
    - **bad_line**: line number out of range or non-integer

    Returns a dict with ``should_retry``, ``errors_grouped``,
    ``available_ids``, ``instructions``, ``attempt``,
    ``original_lines``.
    """
    if validation_result.get("valid"):
        return {
            "should_retry": False,
            "errors_grouped": {},
            "available_ids": list(id_pool),
            "instructions": "",
            "attempt": segmentation,
            "original_lines": original_lines,
        }

    errors = validation_result.get("errors", []) or []
    grouped: dict[str, list[str]] = {
        "missing_coverage": [],
        "overlap_without_multi": [],
        "bad_id": [],
        "bad_line": [],
        "other": [],
    }
    for err in errors:
        lower = err.lower()
        if "not assigned to any thread" in lower:
            grouped["missing_coverage"].append(err)
        elif "cited by" in lower and "multi" in lower:
            grouped["overlap_without_multi"].append(err)
        elif "invalid id" in lower or "not in provided pool" in lower or "duplicate thread id" in lower:
            grouped["bad_id"].append(err)
        elif "out of range" in lower or "non-integer line" in lower:
            grouped["bad_line"].append(err)
        else:
            grouped["other"].append(err)
    grouped = {k: v for k, v in grouped.items() if v}

    # With structured output the only irrecoverable case is
    # pervasive missing-coverage — the model simply declined to
    # assign content. Everything else is usually a single-shot fix.
    coverage = grouped.get("missing_coverage", [])
    heavy_miss = any("100" in e or "99" in e for e in coverage) or len(coverage) > 3
    should_retry = not heavy_miss

    # Which ids did the model actually use?
    used_ids: set[str] = set()
    for t in (segmentation or {}).get("threads", []) or []:
        if isinstance(t, dict) and isinstance(t.get("id"), str):
            used_ids.add(t["id"])
    available_ids = [tid for tid in id_pool if tid not in used_ids]

    lines = [
        "Your previous segmentation failed validation. Return the JSON "
        "again, corrected. Every non-blank line number must appear in "
        "at least one thread's `lines` array.",
    ]
    if "missing_coverage" in grouped:
        lines.append(
            "• Some lines were unassigned. Cover ALL non-blank lines."
        )
    if "overlap_without_multi" in grouped:
        lines.append(
            "• Two threads claimed the same line. Either reassign, or "
            "set `multi: true` on BOTH threads sharing the line."
        )
    if "bad_id" in grouped:
        lines.append(
            "• Use only ids from the pool. IDs must match t_xxxxxx (6 hex)."
        )
    if "bad_line" in grouped:
        lines.append(
            "• Line numbers must be integers in "
            f"[1, {len(original_lines)}]."
        )
    if available_ids:
        lines.append(
            f"• {len(available_ids)} unused ids available: "
            f"{', '.join(available_ids[:12])}"
            + ("…" if len(available_ids) > 12 else "")
        )

    return {
        "should_retry": should_retry,
        "errors_grouped": grouped,
        "available_ids": available_ids,
        "instructions": "\n".join(lines),
        "attempt": segmentation,
        "original_lines": original_lines,
    }


def repair_segmentation(
    tagged_text: str,
    validation_result: dict[str, Any],
    original_text: str,
    id_pool: list[str],
) -> dict[str, Any]:
    """Build a structured repair prompt from a failed segmentation.

    Takes the output of ``validate_segmentation()`` when ``valid`` is
    False and produces a structured repair spec that a local LLM can
    consume as a one-shot retry. The spec separates the original
    (banner-stripped) text, the LLM's broken attempt, and a grouped
    error list so the retry prompt doesn't have to reconstruct the
    context from free-form strings.

    This is a pure helper — no LLM call is made here. It returns a
    prompt-ready payload that the caller feeds back into whichever
    local model is performing segmentation.

    Args:
        tagged_text: The LLM-produced tagged text that failed.
        validation_result: The dict returned by ``validate_segmentation``.
        original_text: The banner-stripped original text (same thing
            passed to the first segmentation attempt).
        id_pool: The thread-ID pool the LLM was given. A subset of
            these may already be consumed successfully; the repair
            prompt should tell the model which IDs are still free.

    Returns:
        Dict with:
            ``should_retry``:  bool — False when the errors are not
                               plausibly recoverable in one more shot
                               (e.g. massive content-drift).
            ``errors_grouped``: dict of ``{category: [messages]}``
                                categorizing the validation errors.
            ``available_ids``: list of thread IDs still unused in
                               ``tagged_text``.
            ``instructions``: str — a repair instruction block the
                              caller can splice into the system prompt.
            ``attempt``: str — the failed tagged text, passed through.
            ``original``: str — the banner-stripped source text.
    """
    if validation_result.get("valid"):
        return {
            "should_retry": False,
            "errors_grouped": {},
            "available_ids": list(id_pool),
            "instructions": "",
            "attempt": tagged_text,
            "original": original_text,
        }

    errors: list[str] = validation_result.get("errors", []) or []

    # --- Categorize errors into recoverable vs not ---
    # Categories drive the instruction block. Keep them narrow so the
    # model gets pointed feedback rather than a wall of strings.
    grouped: dict[str, list[str]] = {
        "unbalanced_tags": [],
        "nesting": [],
        "mismatched_close": [],
        "content_drift": [],
        "orphaned_content": [],
        "invalid_id": [],
        "other": [],
    }
    for err in errors:
        lower = err.lower()
        if "open tags without close" in lower or "close tags without open" in lower or "still open at end" in lower:
            grouped["unbalanced_tags"].append(err)
        elif "nested thread" in lower:
            grouped["nesting"].append(err)
        elif "mismatched close" in lower or "close tag" in lower and "without open" in lower:
            grouped["mismatched_close"].append(err)
        elif "content line count mismatch" in lower or "content modified at line" in lower:
            grouped["content_drift"].append(err)
        elif "orphaned content" in lower:
            grouped["orphaned_content"].append(err)
        elif "invalid thread id" in lower or "duplicate open tag" in lower:
            grouped["invalid_id"].append(err)
        else:
            grouped["other"].append(err)

    # Drop empty categories for a cleaner payload
    grouped = {k: v for k, v in grouped.items() if v}

    # --- Decide if a one-shot retry is worth attempting ---
    # Heavy content drift almost never survives a retry — the model has
    # paraphrased the input. Surface that rather than burning another
    # call.
    drift = grouped.get("content_drift", [])
    heavy_drift = len(drift) >= 5
    should_retry = not heavy_drift

    # --- Which IDs are still available ---
    used_ids: set[str] = set()
    for line in tagged_text.split("\n"):
        stripped = line.strip()
        om = _OPEN_TAG_RE.match(stripped)
        if om:
            used_ids.add(om.group(1))
        cm = _CLOSE_TAG_RE.match(stripped)
        if cm:
            used_ids.add(cm.group(1))
    available_ids = [tid for tid in id_pool if tid not in used_ids]

    # --- Build a terse instruction block for the retry prompt ---
    lines: list[str] = [
        "Your previous segmentation attempt failed validation. "
        "Re-emit the *entire* text with thread tags — do not paraphrase, "
        "do not drop or add lines.",
    ]
    if "unbalanced_tags" in grouped:
        lines.append(
            "• Every <!-- [t_xxxxxx] --> open tag must have a matching "
            "<!-- [/t_xxxxxx] --> close tag."
        )
    if "nesting" in grouped:
        lines.append(
            "• Threads cannot nest. Close the current thread before "
            "opening another."
        )
    if "mismatched_close" in grouped:
        lines.append(
            "• Close tags must match the id of the most recently opened "
            "thread."
        )
    if "content_drift" in grouped:
        lines.append(
            "• You changed the content. Emit every non-tag line exactly "
            "as it appeared in the original text — byte-for-byte."
        )
    if "orphaned_content" in grouped:
        lines.append(
            "• Every non-tag line must be inside a thread. Wrap "
            "orphaned content in an appropriate thread."
        )
    if "invalid_id" in grouped:
        lines.append(
            "• Use only thread IDs from the provided pool. Format is "
            "t_ + 6 hex chars."
        )
    if available_ids:
        lines.append(
            f"• {len(available_ids)} unused thread IDs are still "
            f"available: {', '.join(available_ids[:12])}"
            + ("…" if len(available_ids) > 12 else "")
        )

    return {
        "should_retry": should_retry,
        "errors_grouped": grouped,
        "available_ids": available_ids,
        "instructions": "\n".join(lines),
        "attempt": tagged_text,
        "original": original_text,
    }


def _attribute_dates(
    start_line: int,
    end_line: int,
    banner_date_map: list[tuple[int, str]] | None,
) -> list[str]:
    """Determine source dates for a thread based on banner positions."""
    if not banner_date_map:
        return []

    dates: list[str] = []
    for banner_line, date_str in banner_date_map:
        if banner_line <= end_line:
            if date_str not in dates:
                dates.append(date_str)

    return dates


def _content_lines(text: str) -> list[str]:
    """Extract non-empty, non-separator lines for comparison."""
    result = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and not _SEPARATOR_RE.match(stripped):
            result.append(stripped)
    return result


def _strip_all_tags(text: str) -> str:
    """Remove all thread tags and multi annotations from text."""
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if _OPEN_TAG_RE.match(stripped):
            continue
        if _CLOSE_TAG_RE.match(stripped):
            continue
        if _MULTI_TAG_RE.match(stripped):
            continue
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Manifest (JSONL) — structured metadata per thread
# ---------------------------------------------------------------------------


def validate_manifest(
    manifest_path: Path,
    thread_ids: list[str],
) -> dict[str, Any]:
    """Validate a JSONL manifest against extracted thread IDs.

    Each line must be valid JSON with at least ``id``, ``tags``, ``summary``.
    Every thread ID from the tagged text must have exactly one manifest entry.

    Args:
        manifest_path: Path to the ``.jsonl`` manifest file.
        thread_ids: Thread IDs from ``validate_segmentation`` or
            ``extract_threads``.

    Returns:
        Dict with ``valid``, ``entries`` (parsed list), ``errors``.
    """
    errors: list[str] = []
    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as e:
        return {"valid": False, "entries": [], "errors": [f"Read error: {e}"]}

    for line_num, line in enumerate(text.strip().split("\n"), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"Line {line_num}: invalid JSON — {e}")
            continue

        tid = entry.get("id")
        if not tid:
            errors.append(f"Line {line_num}: missing 'id' field")
            continue
        if tid in seen_ids:
            errors.append(f"Line {line_num}: duplicate id {tid!r}")
        seen_ids.add(tid)

        if "tags" not in entry:
            errors.append(f"Line {line_num} ({tid}): missing 'tags'")
        elif not isinstance(entry["tags"], list):
            errors.append(f"Line {line_num} ({tid}): 'tags' must be a list")

        if "summary" not in entry:
            errors.append(f"Line {line_num} ({tid}): missing 'summary'")

        entries.append(entry)

    # Cross-check against thread IDs
    expected = set(thread_ids)
    missing = expected - seen_ids
    extra = seen_ids - expected
    if missing:
        errors.append(f"Threads missing from manifest: {sorted(missing)}")
    if extra:
        errors.append(f"Manifest has unknown thread IDs: {sorted(extra)}")

    return {
        "valid": len(errors) == 0,
        "entries": entries,
        "errors": errors,
    }


def load_manifest(manifest_path: Path) -> list[dict[str, Any]]:
    """Load a JSONL manifest file, returning a list of entry dicts."""
    entries = []
    for line in manifest_path.read_text(encoding="utf-8").strip().split("\n"):
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def generate_review_doc(
    threads: list[dict[str, Any]],
    manifest_entries: list[dict[str, Any]],
    journal_date: str,
    source_dates: list[str],
) -> str:
    """Generate a markdown review document from threads and manifest.

    Groups threads by their tags (from manifest) rather than hardcoded
    categories.

    Args:
        threads: Thread objects from ``extract_threads()``.
        manifest_entries: Parsed manifest entries from ``load_manifest()``.
        journal_date: The source journal date (YYYY-MM-DD).
        source_dates: List of carried-over dates.

    Returns:
        Markdown string for the review document.
    """
    thread_map = {t["id"]: t for t in threads}
    manifest_map = {e["id"]: e for e in manifest_entries}

    # Group by primary tag (first tag in the list)
    groups: dict[str, list[str]] = {}
    for entry in manifest_entries:
        primary = entry["tags"][0] if entry.get("tags") else "#untagged"
        groups.setdefault(primary, []).append(entry["id"])

    lines = [
        "# Segmentation Review",
        "",
        f"**Source:** `journal/{journal_date}.md` Running Notes",
        f"**Threads:** {len(threads)}",
        f"**Carried-over dates:** {', '.join(source_dates[:5])}"
        + (f"... ({len(source_dates)} total)" if len(source_dates) > 5 else ""),
        "",
        "**Instructions:** Scan each thread. Mark any that should be:",
        "- **MERGE** with another thread (note both IDs)",
        "- **SPLIT** into multiple threads",
        "- Looks fine as-is? No action needed.",
        "",
    ]

    for tag, tids in groups.items():
        lines.append("---")
        lines.append(f"## {tag}")
        lines.append("")
        for tid in tids:
            t = thread_map.get(tid)
            m = manifest_map.get(tid)
            if not t or not m:
                continue

            extra_tags = " ".join(m["tags"][1:]) if len(m.get("tags", [])) > 1 else ""
            multi = " `[MULTI]`" if m.get("multi") else ""
            lines.append(f"### `{tid}`{multi} ({t['line_count']} lines)")
            if extra_tags:
                lines.append(f"Tags: {extra_tags}")
            lines.append(f"> {m['summary']}")
            lines.append("")

            content = t["raw_text"].strip()
            content_lines = content.split("\n")
            lines.append("```")
            if len(content_lines) > 15:
                lines.extend(content_lines[:10])
                lines.append(f"... ({len(content_lines)} lines total)")
            else:
                lines.append(content)
            lines.append("```")
            lines.append("")

    return "\n".join(lines)

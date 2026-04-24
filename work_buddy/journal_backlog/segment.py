"""Running-notes segmentation — group input lines into topic threads.

The only live segmentation path is **line-range**: the LLM partitions
numbered input lines into groups (each group is one thread's line
numbers); we reconstruct thread objects locally.

Exports:

- :func:`strip_banners` — remove carried-over banners from raw
  running-notes text; shared with the extract path.
- :func:`number_lines` — prefix each line with its 1-based number for
  the LLM prompt.
- :data:`LINE_RANGE_SCHEMA` — JSON Schema for the constrained model
  output (``{"groups": [[<int>, ...], ...]}``).
- :func:`validate_line_range_segmentation` — validate the parsed model
  output against an original-lines list.
- :func:`build_threads_from_line_ranges` — turn validated groups into
  the thread-dict shape the adapter expects (id, raw_text, line_count,
  source_dates, has_multi_flag). Ids are generated here; the model
  never emits them.

Manifest helpers (substrate-agnostic — operate on JSONL manifest format
the backlog pipeline writes after per-thread tag/summary generation):

- :func:`validate_manifest` — JSONL schema check against an expected
  thread-id set.
- :func:`load_manifest` — JSONL → list[dict].
- :func:`generate_review_doc` — markdown review grouped by primary tag
  for the cluster-review step of ``/wb-journal-backlog``.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

# Banner pattern: ***'Running Notes / Considerations' carried over from YYYY-MM-DD***
_BANNER_RE = re.compile(
    r"^\*{3}'Running Notes\s*/\s*Considerations'\s*carried over from\s+"
    r"(\d{4}-\d{2}-\d{2})\*{3}\s*$",
    re.MULTILINE,
)

# Structural separators (banner boundaries, not user content)
_SEPARATOR_RE = re.compile(r"^-{3,}\s*$")

# Line-entry parsers for the group output. An entry may be:
#   - a JSON integer (e.g., ``5``)
#   - a JSON string holding a single line number (e.g., ``"5"``)
#   - a JSON string holding an inclusive range (e.g., ``"3-5"``)
# Whitespace around numbers and the separating dash is tolerated so the
# parser stays robust when a model emits ``"3 - 5"`` or ``" 15 "``.
_INT_STR_RE = re.compile(r"^\s*(\d+)\s*$")
_RANGE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


# ---------------------------------------------------------------------------
# Banner stripping
# ---------------------------------------------------------------------------


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

    # Track whether we're in a "banner zone" (banner + surrounding separators)
    prev_was_banner_or_sep = False

    for i, line in enumerate(lines):
        banner_match = _BANNER_RE.match(line)
        if banner_match:
            date_str = banner_match.group(1)
            source_dates.append(date_str)
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


# ---------------------------------------------------------------------------
# Line-range segmentation
# ---------------------------------------------------------------------------
#
# The model sees numbered input lines and returns line-number groups:
#
#     {"groups": [[1, 2, 5], [3, 4]]}
#
# It never generates ids or flags. We assign ids locally (``t_`` + 6 hex)
# and derive the multi-thread flag from line-number overlap between
# groups. This keeps the model's job small (partition-only) and puts
# bookkeeping on our side where it belongs.


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


# JSON schema for constrained output. Each group entry may be either a
# positive integer (single line) or a string — ``"N"`` for a single line
# or ``"N-M"`` for an inclusive range. Mix freely within a group.
LINE_RANGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["groups"],
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "array",
                "items": {
                    "oneOf": [
                        {"type": "integer", "minimum": 1},
                        {
                            "type": "string",
                            "pattern": r"^\s*\d+(\s*-\s*\d+)?\s*$",
                        },
                    ],
                },
                "minItems": 1,
            },
        },
    },
    "additionalProperties": False,
}


def _expand_line_entry(
    entry: Any, n_lines: int,
) -> tuple[list[int], str | None]:
    """Expand one group entry into a list of line numbers.

    Accepts:
      - ``int`` ``N`` → ``[N]``
      - ``str`` ``"N"`` → ``[N]``  (tolerates whitespace)
      - ``str`` ``"N-M"`` → ``[N, N+1, ..., M]``  (inclusive; N ≤ M)

    The helper itself enforces ``1 ≤ N ≤ M ≤ n_lines``. Booleans are
    rejected even though Python's ``bool`` is a subclass of ``int`` —
    ``True`` silently coercing to line 1 would be a nasty foot-gun.

    Returns ``(lines, error)``. On success, ``error`` is ``None``.
    On failure, ``lines`` is ``[]`` and ``error`` carries a short
    message suitable for the validator's error list.
    """
    if isinstance(entry, bool):
        return [], f"boolean not allowed as line entry: {entry!r}"
    if isinstance(entry, int):
        if entry < 1 or entry > n_lines:
            return [], f"line {entry} out of range [1, {n_lines}]"
        return [entry], None
    if isinstance(entry, str):
        m = _INT_STR_RE.match(entry)
        if m:
            v = int(m.group(1))
            if v < 1 or v > n_lines:
                return [], f"line {v} out of range [1, {n_lines}]"
            return [v], None
        m = _RANGE_RE.match(entry)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                return [], f"reversed range {entry!r} (start > end)"
            if a < 1:
                return [], f"range {entry!r} starts below 1"
            if b > n_lines:
                return [], f"range {entry!r} exceeds {n_lines} lines"
            return list(range(a, b + 1)), None
        return [], f"unparseable line entry: {entry!r}"
    return [], f"non-integer line entry: {entry!r} ({type(entry).__name__})"


def validate_line_range_segmentation(
    segmentation: dict[str, Any],
    original_lines: list[str],
) -> dict[str, Any]:
    """Validate a line-range segmentation output.

    Checks:

    1. **Shape** — the response is a dict with a ``groups`` key whose
       value is a list of lists of integers.
    2. **Line range** — every line number is an integer in
       ``[1, len(original_lines)]``.
    3. **Coverage** — every non-blank source line appears in at least
       one group. Blank lines and standalone ``---`` separators do NOT
       need to be assigned (they carry boundary information, not
       content).

    Overlap between groups is allowed unconditionally — a line in two
    groups means the thread bridges both topics. No separate flag is
    required from the model; ``build_threads_from_line_ranges`` derives
    ``has_multi_flag`` from the overlap directly.

    Args:
        segmentation: The parsed JSON output from the LLM.
        original_lines: The list returned by :func:`number_lines`.

    Returns:
        Dict with ``valid``, ``group_count``, ``groups``, ``errors``,
        ``warnings``.
    """
    errors: list[str] = []
    warnings: list[str] = []

    groups_raw = (
        segmentation.get("groups") if isinstance(segmentation, dict) else None
    )
    if not isinstance(groups_raw, list):
        return {
            "valid": False,
            "group_count": 0,
            "groups": [],
            "errors": ["Output missing 'groups' array"],
            "warnings": [],
        }

    n_lines = len(original_lines)
    # Coverage is required for content lines only. Structural markers
    # (standalone ``---`` separators) carry boundary information but
    # are not content — the model MAY group them into an adjacent
    # thread, but it isn't required to.
    non_blank_lines = {
        i + 1
        for i, line in enumerate(original_lines)
        if line.strip() and not _SEPARATOR_RE.match(line.strip())
    }

    cited: set[int] = set()
    valid_groups: list[list[int]] = []

    for idx, raw in enumerate(groups_raw):
        if not isinstance(raw, list):
            errors.append(f"Group at index {idx} is not a list")
            continue

        cleaned_lines: list[int] = []
        entry_errored = False
        for entry in raw:
            expanded, err = _expand_line_entry(entry, n_lines)
            if err:
                errors.append(f"Group at index {idx}: {err}")
                entry_errored = True
                continue
            cleaned_lines.extend(expanded)

        if not cleaned_lines:
            if not entry_errored:
                errors.append(f"Group at index {idx} is empty after validation")
            continue

        deduped = sorted(set(cleaned_lines))
        cited.update(deduped)
        valid_groups.append(deduped)

    missing = sorted(non_blank_lines - cited)
    if missing:
        errors.append(
            f"{len(missing)} non-blank line(s) not assigned to any group: "
            f"{missing[:10]}"
            + ("…" if len(missing) > 10 else "")
        )

    return {
        "valid": len(errors) == 0,
        "group_count": len(valid_groups),
        "groups": valid_groups,
        "errors": errors,
        "warnings": warnings,
    }


def build_threads_from_line_ranges(
    validated: dict[str, Any],
    original_lines: list[str],
    banner_date_map: list[tuple[int, str]] | None = None,
) -> list[dict[str, Any]]:
    """Reconstruct thread objects from a validated line-range segmentation.

    Assigns a fresh ``t_xxxxxx`` id to each group (ids are our
    bookkeeping, never the model's job). Computes ``has_multi_flag``
    from line-number overlap: a group whose lines also appear in any
    other group is marked multi.

    Args:
        validated: Output of :func:`validate_line_range_segmentation`
            (with ``valid=True``). ``groups`` is a list of sorted,
            deduped line-number lists.
        original_lines: The list from :func:`number_lines`.
        banner_date_map: Optional ``(line_number, date_str)`` tuples
            from :func:`strip_banners` for source-date attribution.

    Returns:
        List of thread dicts with ``id``, ``raw_text``, ``line_count``,
        ``source_dates``, ``has_multi_flag``.
    """
    groups: list[list[int]] = validated.get("groups", []) or []

    # Multi detection: any line that appears in ≥2 groups marks all of
    # its containing groups as multi.
    line_counts: dict[int, int] = {}
    for group in groups:
        for ln in group:
            line_counts[ln] = line_counts.get(ln, 0) + 1
    multi_lines = {ln for ln, c in line_counts.items() if c > 1}

    threads: list[dict[str, Any]] = []
    for lines in groups:
        if not lines:
            continue
        raw = "\n".join(original_lines[ln - 1] for ln in lines)
        non_empty = [ln for ln in lines if original_lines[ln - 1].strip()]
        start_line = min(lines) - 1
        end_line = max(lines) - 1
        source_dates = _attribute_dates(start_line, end_line, banner_date_map)
        threads.append({
            "id": f"t_{uuid.uuid4().hex[:6]}",
            "raw_text": raw,
            "line_count": len(non_empty),
            "source_dates": source_dates,
            "has_multi_flag": any(ln in multi_lines for ln in lines),
        })
    return threads


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Manifest helpers (JSONL format — substrate-agnostic)
# ---------------------------------------------------------------------------
#
# The backlog pipeline writes a per-thread manifest entry of shape
# ``{"id": str, "tags": [str, ...], "summary": str}`` after the
# tag/summary generation step. These helpers validate, load, and render
# manifests for the cluster-review step. They don't care which
# segmentation substrate produced the underlying threads.


def validate_manifest(
    manifest_path: Path,
    thread_ids: list[str],
) -> dict[str, Any]:
    """Validate a JSONL manifest against an expected thread-id set.

    Each line must be valid JSON with at least ``id``, ``tags``, ``summary``.
    Every thread id in ``thread_ids`` must have exactly one manifest entry.

    Args:
        manifest_path: Path to the ``.jsonl`` manifest file.
        thread_ids: The full set of thread ids the manifest should cover
            (typically from :func:`build_threads_from_line_ranges` output).

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

    Groups threads by their primary tag (first tag in the list) so the
    user reviewing clusters can scan related threads together.

    Args:
        threads: Thread objects from :func:`build_threads_from_line_ranges`
            (or an equivalent producer of the standard thread-dict shape).
        manifest_entries: Parsed manifest entries from
            :func:`load_manifest`.
        journal_date: The source journal date (YYYY-MM-DD).
        source_dates: List of carried-over dates from
            :func:`strip_banners`.

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

            extra_tags = (
                " ".join(m["tags"][1:])
                if len(m.get("tags", [])) > 1
                else ""
            )
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

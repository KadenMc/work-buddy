"""Shape-invariant detectors for conductor responses.

Pure detection logic shared between the runtime conductor and the test suite.
The flagship invariant is *the response is a tree, not a graph*: no
non-trivial subtree should appear at two or more paths inside the same
response. ``find_duplicated_subtrees`` enforces that property.

The conductor itself imports from here when it wants to surface duplication
warnings to the sidecar log; the test invariant module wraps the same
detector with a pytest-flavored assertion.

This module deliberately has no test framework dependency — it is safe to
import from production code paths.
"""

from __future__ import annotations

import json
from typing import Any

# Subtrees serialized smaller than this contribute no real bloat; skip.
DEFAULT_MIN_SUBTREE = 200


def _walk(
    obj: Any,
    path: str,
    fingerprints: dict[str, list[str]],
    min_size: int,
) -> None:
    """Recursive walker that fingerprints every non-trivial subtree."""
    if isinstance(obj, dict):
        try:
            ser = json.dumps(obj, default=str, sort_keys=True)
        except (TypeError, ValueError):
            ser = None
        if ser is not None and len(ser) >= min_size:
            fingerprints.setdefault(ser, []).append(path or "<root>")
        for k, v in obj.items():
            _walk(v, f"{path}.{k}" if path else k, fingerprints, min_size)
    elif isinstance(obj, list):
        try:
            ser = json.dumps(obj, default=str, sort_keys=True)
        except (TypeError, ValueError):
            ser = None
        if ser is not None and len(ser) >= min_size:
            fingerprints.setdefault(ser, []).append(path or "<root>")
        for i, v in enumerate(obj):
            _walk(v, f"{path}[{i}]", fingerprints, min_size)


def find_duplicated_subtrees(
    resp: Any,
    min_size: int = DEFAULT_MIN_SUBTREE,
) -> list[tuple[int, list[str]]]:
    """Walk a response and return ``(size, [paths])`` for each duplicated subtree.

    A subtree is "duplicated" when its canonical JSON serialization appears
    at two or more paths in the response and its serialized size is at
    least ``min_size`` chars. Sorted biggest-first.
    """
    fingerprints: dict[str, list[str]] = {}
    _walk(resp, "", fingerprints, min_size)
    return sorted(
        (
            (len(ser), sorted(paths))
            for ser, paths in fingerprints.items()
            if len(paths) > 1
        ),
        key=lambda t: -t[0],
    )


def format_duplication_report(
    dupes: list[tuple[int, list[str]]],
    min_size: int = DEFAULT_MIN_SUBTREE,
    *,
    head_limit: int = 10,
) -> str:
    """Render a human-readable report of duplicated subtrees.

    Used both by the test assertion (as a pytest failure message) and by
    any caller that wants to log a structured warning.
    """
    if not dupes:
        return "No duplicated subtrees found."
    lines = [
        f"Response contains {len(dupes)} duplicated subtree(s) "
        f"(>= {min_size} chars):",
    ]
    for size, paths in dupes[:head_limit]:
        lines.append(f"  {size:,} chars x {len(paths)} copies:")
        for p in paths:
            lines.append(f"    - {p}")
    if len(dupes) > head_limit:
        lines.append(f"  ... and {len(dupes) - head_limit} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Subtree-containment detection — Problem C
# ---------------------------------------------------------------------------
#
# Duplication catches "this large value appears at two paths" — e.g., a step
# returning {items: [...]} where another step returns {items: [...]} with the
# same items list.  Containment is a *stricter* check that catches the more
# subtle "step B's whole dict is a key-by-key superset of step A's whole
# dict" case, even when no individual field is large enough to trip the
# duplication detector.  This is the categorize → rank → summarize
# accumulation pattern in Problem C: each step echoes the prior step's
# fields plus its own delta.

# Containment threshold defaults: the contained dict (A) must have at least
# this many keys to count.  Trivial dicts (e.g. ``{"id": "x"}``) are skipped
# to avoid false positives on coincidentally-shared schema fragments.
DEFAULT_MIN_CONTAINED_KEYS = 3


def _is_dict_subset(a: dict, b: dict) -> bool:
    """Return True iff every (k, v) pair in ``a`` exists in ``b`` with the same value.

    Equality compares serialized JSON (canonical sort) so dict ordering
    doesn't matter.  Used to detect "step B's result includes step A's
    result as a subset" — the cross-step accumulation pattern.
    """
    for k, v_a in a.items():
        if k not in b:
            return False
        try:
            ser_a = json.dumps(v_a, default=str, sort_keys=True)
            ser_b = json.dumps(b[k], default=str, sort_keys=True)
        except (TypeError, ValueError):
            return False
        if ser_a != ser_b:
            return False
    return True


def _walk_dicts(
    obj: Any,
    path: str,
    out: list[tuple[str, dict]],
) -> None:
    """Collect every dict subtree paired with its path."""
    if isinstance(obj, dict):
        out.append((path or "<root>", obj))
        for k, v in obj.items():
            _walk_dicts(v, f"{path}.{k}" if path else k, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk_dicts(v, f"{path}[{i}]", out)


def find_contained_subtrees(
    resp: Any,
    min_size: int = DEFAULT_MIN_SUBTREE,
    min_keys: int = DEFAULT_MIN_CONTAINED_KEYS,
) -> list[tuple[str, str, int]]:
    """Find ``(contained_path, container_path, contained_size)`` triples.

    A is "contained" in B when:
      * Both are dicts at non-overlapping paths in ``resp``.
      * A has at least ``min_keys`` keys.
      * A's serialized size is at least ``min_size`` chars.
      * Every (k, v) pair in A exists in B with the same value.
      * A is not the *same* dict as B (i.e., not a self-pair).
      * Neither is exactly equal to the other (those are duplication, not
        containment — caught separately).
      * A's path is not a prefix of B's path and vice versa (one being a
        nested subtree of the other is a different shape).

    Sorted by contained-size, biggest first.
    """
    dicts: list[tuple[str, dict]] = []
    _walk_dicts(resp, "", dicts)

    out: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str]] = set()
    for i, (path_a, dict_a) in enumerate(dicts):
        if len(dict_a) < min_keys:
            continue
        try:
            ser_a = json.dumps(dict_a, default=str, sort_keys=True)
        except (TypeError, ValueError):
            continue
        if len(ser_a) < min_size:
            continue
        for j, (path_b, dict_b) in enumerate(dicts):
            if i == j:
                continue
            if dict_a is dict_b:
                continue
            # Skip nested-path pairs (A inside B's tree or vice versa).
            if (
                path_a.startswith(path_b + ".")
                or path_a.startswith(path_b + "[")
                or path_b.startswith(path_a + ".")
                or path_b.startswith(path_a + "[")
            ):
                continue
            # Skip exact-equality pairs (those are duplication).
            try:
                ser_b = json.dumps(dict_b, default=str, sort_keys=True)
            except (TypeError, ValueError):
                continue
            if ser_a == ser_b:
                continue
            if not _is_dict_subset(dict_a, dict_b):
                continue
            key = (path_a, path_b)
            if key in seen:
                continue
            seen.add(key)
            out.append((path_a, path_b, len(ser_a)))

    out.sort(key=lambda t: -t[2])
    return out


def format_containment_report(
    triples: list[tuple[str, str, int]],
    *,
    head_limit: int = 10,
) -> str:
    """Render a human-readable report of contained subtrees."""
    if not triples:
        return "No contained subtrees found."
    lines = [
        f"Response contains {len(triples)} contained-subtree pair(s):",
    ]
    for path_a, path_b, size in triples[:head_limit]:
        lines.append(
            f"  {size:,} chars: {path_a}\n"
            f"            is a subset of: {path_b}"
        )
    if len(triples) > head_limit:
        lines.append(f"  ... and {len(triples) - head_limit} more")
    return "\n".join(lines)


def find_step_result_accumulations(
    step_results: dict[str, Any],
    *,
    min_size: int = DEFAULT_MIN_SUBTREE,
    min_keys: int = DEFAULT_MIN_CONTAINED_KEYS,
) -> list[tuple[str, str, int]]:
    """Specialized check: find ``(upstream_id, downstream_id, size)`` accumulations.

    Used by the conductor's runtime warning.  For every pair of step
    results (A, B), if A's dict is a non-trivial subset of B's dict, B is
    accumulating A's content.  Same threshold semantics as
    ``find_contained_subtrees``, but the pair-finding is restricted to the
    step_results dict's top-level entries — the primary surface where the
    cross-step accumulation bug shows up.
    """
    out: list[tuple[str, str, int]] = []
    for a_id, a_val in step_results.items():
        if not isinstance(a_val, dict) or len(a_val) < min_keys:
            continue
        try:
            ser_a = json.dumps(a_val, default=str, sort_keys=True)
        except (TypeError, ValueError):
            continue
        if len(ser_a) < min_size:
            continue
        for b_id, b_val in step_results.items():
            if a_id == b_id:
                continue
            if not isinstance(b_val, dict):
                continue
            try:
                ser_b = json.dumps(b_val, default=str, sort_keys=True)
            except (TypeError, ValueError):
                continue
            if ser_a == ser_b:
                continue  # duplication, not containment
            if _is_dict_subset(a_val, b_val):
                out.append((a_id, b_id, len(ser_a)))

    out.sort(key=lambda t: -t[2])
    return out

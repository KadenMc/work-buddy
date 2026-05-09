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

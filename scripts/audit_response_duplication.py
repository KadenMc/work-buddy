"""Find duplicated subtrees in a conductor response.

Walks any JSON-serializable response object, fingerprints every non-trivial
subtree by its canonical JSON serialization, and reports fingerprints that
appear at two or more paths.

Usage::

    python scripts/audit_response_duplication.py path/to/response.json

The input file may be either a raw response dict or the MCP tool-result
envelope (a list whose first element is ``{"text": "<json>"}``); both shapes
are handled.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Subtrees smaller than this contribute no real bloat; ignore.
_MIN_SUBTREE_CHARS = 200


def _walk(obj: Any, path: str, fingerprints: dict[str, list[str]]) -> None:
    if isinstance(obj, dict):
        try:
            ser = json.dumps(obj, default=str, sort_keys=True)
        except (TypeError, ValueError):
            ser = None
        if ser is not None and len(ser) >= _MIN_SUBTREE_CHARS:
            fingerprints.setdefault(ser, []).append(path or "<root>")
        for k, v in obj.items():
            _walk(v, f"{path}.{k}" if path else k, fingerprints)
    elif isinstance(obj, list):
        try:
            ser = json.dumps(obj, default=str, sort_keys=True)
        except (TypeError, ValueError):
            ser = None
        if ser is not None and len(ser) >= _MIN_SUBTREE_CHARS:
            fingerprints.setdefault(ser, []).append(path or "<root>")
        for i, v in enumerate(obj):
            _walk(v, f"{path}[{i}]", fingerprints)


def find_duplicates(resp: Any) -> list[tuple[int, list[str]]]:
    """Return (size_chars, [paths,...]) for each duplicated subtree, biggest first."""
    fingerprints: dict[str, list[str]] = {}
    _walk(resp, "", fingerprints)
    dupes = [
        (len(ser), sorted(paths))
        for ser, paths in fingerprints.items()
        if len(paths) > 1
    ]
    dupes.sort(reverse=True, key=lambda t: t[0])
    return dupes


def _load(path: Path) -> Any:
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Unwrap MCP tool-result envelopes — three observed shapes:
    #   1. [{"type": "text", "text": "<json>"}]
    #   2. {"result": [{"type": "text", "text": "<json>"}]}
    #   3. {"result": "<json string>"}
    # Pull until we land on a parsed dict.
    if isinstance(raw, dict) and "result" in raw:
        raw = raw["result"]
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "text" in raw[0]:
        raw = raw[0]["text"]
    if isinstance(raw, str):
        raw = json.loads(raw)
    return raw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument(
        "--min-size", type=int, default=_MIN_SUBTREE_CHARS,
        help="Ignore subtrees serialized smaller than this (chars).",
    )
    args = ap.parse_args()

    globals()["_MIN_SUBTREE_CHARS"] = args.min_size
    threshold = args.min_size

    resp = _load(args.path)
    total_size = len(json.dumps(resp, default=str))
    print(f"Response total size: {total_size:,} chars")

    dupes = find_duplicates(resp)
    if not dupes:
        print("No duplicated subtrees found above threshold.")
        return 0

    print(f"\n{len(dupes)} duplicated subtree(s) (>= {_MIN_SUBTREE_CHARS} chars):\n")
    waste = 0
    for size, paths in dupes:
        copies = len(paths)
        wasted = size * (copies - 1)
        waste += wasted
        print(f"  {size:>7,} chars x {copies} copies  (wastes {wasted:,})")
        for p in paths:
            print(f"    - {p}")
        print()
    print(f"Total wasted bytes from duplication: {waste:,} chars "
          f"({100*waste/total_size:.1f}% of response)")
    return 1 if dupes else 0


if __name__ == "__main__":
    sys.exit(main())

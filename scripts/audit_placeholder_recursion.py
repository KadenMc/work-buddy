"""Audit <<wb:path>> placeholder usage across the knowledge store.

Reports:
1. Total / --recursive / plain placeholder counts.
2. How many plain placeholders point at a target whose own content["full"]
   contains a <<wb: substring (i.e., where --recursive would have produced
   different output).
3. The (source -> target) pairs for that "would have differed" set, capped at 30.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

STORE_DIR = Path(__file__).resolve().parent.parent / "knowledge" / "store"
PLACEHOLDER_RE = re.compile(r"<<wb:(.*?)>>")


def load_units() -> dict[str, dict]:
    units: dict[str, dict] = {}
    for jf in sorted(STORE_DIR.glob("*.json")):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        # Skip generated index-style files that are not unit dicts.
        # Heuristic: a "unit" entry has "content" or "kind".
        for key, val in data.items():
            if isinstance(val, dict) and ("content" in val or "kind" in val):
                units[key] = val
    return units


def parse_placeholder(inner: str) -> tuple[str, bool]:
    """Return (path, recursive)."""
    parts = inner.strip().split()
    if not parts:
        return "", False
    path = parts[0]
    recursive = "--recursive" in parts[1:]
    return path, recursive


def main() -> None:
    units = load_units()
    total = 0
    recursive_n = 0
    plain_n = 0
    differ_pairs: list[tuple[str, str]] = []

    for src_path, unit in units.items():
        full = unit.get("content", {}).get("full", "")
        if not isinstance(full, str) or "<<wb:" not in full:
            continue
        for m in PLACEHOLDER_RE.finditer(full):
            inner = m.group(1)
            tgt_path, is_recursive = parse_placeholder(inner)
            if not tgt_path:
                continue
            total += 1
            if is_recursive:
                recursive_n += 1
                continue
            plain_n += 1
            tgt = units.get(tgt_path)
            if tgt is None:
                continue
            tgt_full = tgt.get("content", {}).get("full", "")
            if isinstance(tgt_full, str) and "<<wb:" in tgt_full:
                differ_pairs.append((src_path, tgt_path))

    print(f"Total placeholders:       {total}")
    print(f"  --recursive:            {recursive_n}")
    print(f"  plain:                  {plain_n}")
    print(f"Plain that would differ:  {len(differ_pairs)}")
    print()
    print("Source -> Target (capped at 30):")
    for src, tgt in differ_pairs[:30]:
        print(f"  {src}  ->  {tgt}")
    if len(differ_pairs) > 30:
        print(f"  ... and {len(differ_pairs) - 30} more")


if __name__ == "__main__":
    main()

"""One-shot migration: stamp every existing cost-log row with ``priced_with: "v2"``.

Why this exists
---------------

On 2026-04-25 we consolidated the LLM pricing tables. The per-call cost
log (``work_buddy.llm.cost``) was previously using a compact 3-model
table; the dashboard's transcript view was using a richer 9-model table
with cache_read / cache_write rates. Going forward, both share the
canonical table at ``work_buddy.llm.transcripts.pricing.calc_cost``.

**Cost numbers do NOT change for existing rows.** Old rows lack the
``cache_read_tokens`` / ``cache_creation_tokens`` fields entirely (they
were added in the same Phase-1 schema extension). Without those token
counts, re-pricing legacy rows under the new table produces identical
``input * input_rate + output * output_rate`` arithmetic — the cache
fields contribute zero to a sum where they're zero.

So the migration's only effect is bookkeeping: stamp every row that
doesn't already carry ``priced_with`` with ``"v2"`` (the new contract).
Future migrations can branch on this stamp instead of date-guessing.

Usage::

    python -m scripts.migrate_priced_with_v2             # dry-run
    python -m scripts.migrate_priced_with_v2 --apply     # writes
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _atomic_rewrite(path: Path, lines: list[str]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def stamp_one_file(path: Path, *, apply: bool, version: str = "v2") -> tuple[int, int]:
    """Stamp every row in one log file. Returns ``(scanned, modified)``."""
    if not path.exists():
        return 0, 0

    out_lines: list[str] = []
    scanned = 0
    modified = 0
    raw = path.read_text(encoding="utf-8").splitlines()
    for line in raw:
        line = line.rstrip("\n")
        if not line.strip():
            out_lines.append(line)
            continue
        scanned += 1
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            # Preserve malformed lines verbatim.
            out_lines.append(line)
            continue

        if row.get("priced_with") == version:
            out_lines.append(line)
            continue

        row["priced_with"] = version
        modified += 1
        out_lines.append(json.dumps(row, ensure_ascii=False))

    if apply and modified > 0:
        _atomic_rewrite(path, out_lines)

    return scanned, modified


def find_cost_logs(agents_dir: Path) -> list[Path]:
    if not agents_dir.exists():
        return []
    return sorted(agents_dir.glob("*/llm_costs.jsonl"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--apply", action="store_true",
        help="Write the changes (default: dry-run).",
    )
    parser.add_argument(
        "--agents-dir", type=Path, default=None,
        help="Override the agents directory (default: from work_buddy.paths).",
    )
    parser.add_argument(
        "--version", default="v2",
        help="Pricing version stamp to write (default: v2).",
    )
    args = parser.parse_args(argv)

    if args.agents_dir is None:
        from work_buddy.paths import data_dir
        agents_dir = data_dir("agents")
    else:
        agents_dir = args.agents_dir

    logs = find_cost_logs(agents_dir)
    print(f"Scanning {len(logs)} cost log files under {agents_dir}")

    total_scanned = 0
    total_modified = 0
    files_with_changes = 0
    for path in logs:
        scanned, modified = stamp_one_file(path, apply=args.apply, version=args.version)
        total_scanned += scanned
        if modified > 0:
            files_with_changes += 1
            print(f"  {'updated' if args.apply else 'would update'}: "
                  f"{path.parent.name}  ({modified}/{scanned} rows)")
        total_modified += modified

    verb = "Stamped" if args.apply else "Would stamp"
    print()
    print(f"{verb} priced_with={args.version!r} on {total_modified} rows "
          f"across {files_with_changes} files (scanned {total_scanned}).")
    if not args.apply and total_modified > 0:
        print("Re-run with --apply to write changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

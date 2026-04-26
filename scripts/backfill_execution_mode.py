"""One-shot migration: backfill ``execution_mode`` on legacy cost-log rows.

The ``execution_mode`` field was added to ``work_buddy.llm.cost.log_call``
sometime around 2026-04-12 to distinguish cloud (Anthropic) from local
(LM Studio etc.) calls. Rows written before that date lack the field
entirely, so the dashboard buckets them as "unknown" — visually
prominent but never representing real ambiguity.

Audit (2026-04-25):

* All 22 historical rows missing ``execution_mode`` have:
    - ``estimated_cost_usd > 0`` (the per-call log only computes a non-zero
      cost when ``execution_mode != "local"``, so cost > 0 implies cloud)
    - ``model`` starting with ``claude-`` (Sonnet or Haiku, no local model
      strings)

So the backfill is fully deterministic — those 22 rows are unambiguously
cloud calls. We stamp ``execution_mode: "cloud"`` on them, and the dashboard
no longer needs an "unknown" bucket.

Usage::

    python -m scripts.backfill_execution_mode             # dry-run, prints summary
    python -m scripts.backfill_execution_mode --apply     # writes the changes

Re-running with ``--apply`` is safe (idempotent): rows that already have
``execution_mode`` set are left untouched.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _atomic_rewrite(path: Path, lines: list[str]) -> None:
    """Atomic write of newline-terminated JSON lines to ``path``."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def backfill_one_file(path: Path, *, apply: bool) -> tuple[int, int]:
    """Backfill missing ``execution_mode`` in one cost log file.

    Returns ``(scanned, modified)``. When ``apply=False``, no file is
    written; ``modified`` reflects what *would* be changed.
    """
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
            # Preserve malformed lines verbatim — never lose data.
            out_lines.append(line)
            continue

        if "execution_mode" in row:
            out_lines.append(line)
            continue

        # Deterministic classification:
        #   cost > 0  → cloud  (the writer only sets cost > 0 when not local)
        #   cost == 0 → cloud (the legacy default for unknown models is the
        #              fallback rate; local mode wasn't a concept yet, so
        #              missing-execution_mode + cost==0 happens only when an
        #              unknown model name yielded a $0 fallback. Still cloud.)
        # We treat all missing-execution_mode rows as "cloud" — verified by
        # the 2026-04-25 audit (all 22 affected rows have cost > 0 and a
        # ``claude-*`` model name).
        row["execution_mode"] = "cloud"
        modified += 1
        out_lines.append(json.dumps(row, ensure_ascii=False))

    if apply and modified > 0:
        _atomic_rewrite(path, out_lines)

    return scanned, modified


def find_cost_logs(agents_dir: Path) -> list[Path]:
    """Return every ``llm_costs.jsonl`` under ``data/agents/``."""
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
        scanned, modified = backfill_one_file(path, apply=args.apply)
        total_scanned += scanned
        if modified > 0:
            files_with_changes += 1
            print(f"  {'updated' if args.apply else 'would update'}: "
                  f"{path.parent.name}  ({modified}/{scanned} rows)")
        total_modified += modified

    verb = "Updated" if args.apply else "Would update"
    print()
    print(f"{verb} {total_modified} rows across {files_with_changes} files "
          f"(scanned {total_scanned} rows total).")
    if not args.apply and total_modified > 0:
        print("Re-run with --apply to write changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Walk every sparse task and report density-promotion candidates (Slice 2).

Outputs a Markdown report — one row per flagged task — that the user
reviews to decide which tasks should be promoted from
``density='sparse'`` to ``density='developed'``. Pure flagging — never
auto-promotes (per the user's recorded hesitation about hallucinated
sub-items in the Slice 2 task note).

Usage::

    # Just print the report:
    python scripts/flag_density_candidates.py

    # Write to a file:
    python scripts/flag_density_candidates.py --out density-flags.md

The reviewer can then promote individual tasks with::

    python -c "from work_buddy.obsidian.tasks import store; \\
               store.update('t-XXXX', density='developed')"

A future ``/wb-task-promote-density`` workflow could surface these
through the Resolution Surface (Slice 1.5), but Slice 2 ships only
the heuristic + this CLI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from work_buddy.obsidian.tasks.density_heuristic import flag_all_sparse_tasks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Write report to file instead of stdout.",
    )
    parser.add_argument(
        "--bullet-threshold", type=int, default=3,
        help="Minimum bullets/section to fire the note signal (default 3).",
    )
    args = parser.parse_args(argv)

    flags = flag_all_sparse_tasks(bullet_threshold=args.bullet_threshold)

    lines: list[str] = []
    lines.append("# Density-promotion candidates")
    lines.append("")
    lines.append(
        f"Walked all sparse tasks; **{len(flags)}** flagged. "
        "Review each below; promote the ones that genuinely have "
        "structured action items already authored. Sparse tasks the "
        "user wants to KEEP sparse should be left alone — promotion "
        "is opt-in."
    )
    lines.append("")
    if not flags:
        lines.append("No candidates. All sparse tasks look genuinely sparse.")
    else:
        lines.append("| Task ID | Signals | Evidence |")
        lines.append("|---|---|---|")
        for f in flags:
            sig = ", ".join(f.signals)
            ev = (f.sample_evidence or "").replace("|", r"\|")
            lines.append(f"| `{f.task_id}` | {sig} | {ev} |")
        lines.append("")
        lines.append("## Promote (one task at a time)")
        lines.append("")
        lines.append("```python")
        lines.append("from work_buddy.obsidian.tasks import store")
        lines.append("# Repeat per approved task:")
        lines.append('store.update("t-XXXX", density="developed")')
        lines.append("```")

    report = "\n".join(lines) + "\n"

    if args.out:
        args.out.write_text(report, encoding="utf-8")
        print(f"Wrote report to {args.out} ({len(flags)} candidates)")
    else:
        sys.stdout.write(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

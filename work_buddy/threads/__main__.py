"""CLI entry points for the Threads system.

Currently exposes the v4 → Threads migration dry-run + cutover.
Usage::

    # Dry-run (writes to a sandboxed threads DB; doesn't touch real data)
    python -m work_buddy.threads migrate --dry-run --out /tmp/threads_dry.db

    # Live cutover (only after the dry-run output is clean AND the
    # pre-flight DB dump has been taken)
    python -m work_buddy.threads migrate --execute

The default is ``--dry-run``; ``--execute`` is required to write
to the live threads DB. Refusing to run with no flags is intentional.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from work_buddy.threads import migration


def _migrate_cmd(args: argparse.Namespace) -> int:
    if not (args.dry_run or args.execute):
        print(
            "Refusing to run without an explicit mode. Pick "
            "--dry-run (recommended for first run) or --execute "
            "(only after a DB dump and clean dry-run).",
            file=sys.stderr,
        )
        return 2
    if args.dry_run and args.execute:
        print("Cannot specify both --dry-run and --execute", file=sys.stderr)
        return 2

    if args.dry_run:
        out = Path(args.out) if args.out else Path("data/threads_dryrun.db")
        out.parent.mkdir(parents=True, exist_ok=True)
        # Wipe any prior dry-run DB so the run is fresh
        if out.exists():
            out.unlink()
        report = migration.run_migration(
            dry_run=True,
            v5_db_path=out,
            include_pool_entries=not args.skip_pool,
        )
    else:
        # LIVE cutover
        report = migration.run_migration(
            dry_run=False,
            include_pool_entries=not args.skip_pool,
            monkeypatch_threads_db=False,
        )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print(report.render())

    if report.errors:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m work_buddy.threads",
        description="Threads CLI tooling.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_mig = sub.add_parser(
        "migrate",
        help="Migrate v4 entities (tasks, action items, pool entries) into the Threads system.",
    )
    mode_group = p_mig.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run", action="store_true",
        help="Run against a sandboxed threads DB. Writes nothing to the live DB. (RECOMMENDED for first run.)",
    )
    mode_group.add_argument(
        "--execute", action="store_true",
        help="LIVE cutover. Writes to the live threads DB. Only run after a pre-flight DB dump AND a clean dry-run.",
    )
    p_mig.add_argument(
        "--out", type=str, default=None,
        help="Output path for the dry-run sandbox DB. Default: data/threads_dryrun.db. Ignored for --execute.",
    )
    p_mig.add_argument(
        "--skip-pool", action="store_true",
        help="Skip the ClarifyPool sweep (faster; useful when the pool is empty or the vault isn't available).",
    )
    p_mig.add_argument(
        "--json", action="store_true",
        help="Emit the migration report as JSON instead of human-readable text.",
    )
    p_mig.set_defaults(func=_migrate_cmd)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

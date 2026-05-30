"""Purge orphaned ``workflow_run:*`` consent grants from sidecar sessions.

Background: before the ConsentPrincipal fix, a sidecar-scheduled workflow run
that ended abnormally (server restart mid-run) left its ``workflow_run:<name>:
<run_id>`` grant (``mode=once``, no expiry) in the sidecar's session
``consent.db`` forever. Via workflow-carry, such a stale grant authorized every
moderate/low consent op for any check that resolved against the sidecar DB.

The sidecar now reconciles its own session at boot (``daemon.run`` →
``reconcile_workflow_consent``), so **a normal sidecar restart already purges
these**. This script is for purging WITHOUT a restart, or for auditing what's
present.

Usage (run in the work-buddy conda env):
    python -m scripts.purge_orphaned_workflow_run_grants            # dry-run
    python -m scripts.purge_orphaned_workflow_run_grants --apply    # revoke
    python -m scripts.purge_orphaned_workflow_run_grants --all-sessions  # not just sidecar

By default only sidecar sessions (session dirs whose recorded ``session_id``
starts with ``sidecar``) are touched — those are the ones that accumulate
orphans, since interactive agent sessions are reconciled at ``wb_init``. A
``workflow_run`` grant in an agent session usually belongs to a genuinely live
run, so we leave those alone unless ``--all-sessions`` is given.

This is intentionally conservative: it only ever removes ``workflow_run:*``
keys (never individual op grants, class grants, or the legacy blanket).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def _agents_dir() -> Path:
    from work_buddy.paths import data_dir
    return data_dir("agents")


def _session_id_for_dir(session_dir: Path) -> str | None:
    manifest = session_dir / "manifest.json"
    if not manifest.exists():
        return None
    try:
        return json.loads(manifest.read_text(encoding="utf-8")).get("session_id")
    except (OSError, json.JSONDecodeError):
        return None


def _workflow_run_grants(db_path: Path) -> list[tuple[str, str, str, str | None]]:
    """Return [(operation, mode, granted_at, expires_at)] for workflow_run keys."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        return list(conn.execute(
            "SELECT operation, mode, granted_at, expires_at FROM grants "
            "WHERE operation LIKE 'workflow_run:%'"
        ))
    except sqlite3.OperationalError:
        return []  # no grants table
    finally:
        conn.close()


def _parse_run_key(operation: str) -> tuple[str, str] | None:
    """``workflow_run:<name>:<run_id>`` -> (name, run_id). Splits on the LAST
    colon so workflow names containing ':' survive."""
    body = operation[len("workflow_run:"):]
    if ":" not in body:
        return None
    name, run_id = body.rsplit(":", 1)
    return name, run_id


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually revoke (default: dry-run / list only).")
    ap.add_argument("--all-sessions", action="store_true",
                    help="Include agent sessions, not just sidecar sessions.")
    args = ap.parse_args(argv)

    agents = _agents_dir()
    if not agents.exists():
        print(f"No agents dir at {agents}")
        return 0

    from work_buddy.consent import revoke_workflow_run

    total = 0
    for session_dir in sorted(agents.iterdir()):
        if not session_dir.is_dir():
            continue
        sid = _session_id_for_dir(session_dir)
        if sid is None:
            continue
        is_sidecar = sid.startswith("sidecar")
        if not is_sidecar and not args.all_sessions:
            continue
        grants = _workflow_run_grants(session_dir / "consent.db")
        if not grants:
            continue
        print(f"\n[{sid}]  ({session_dir.name})")
        for operation, mode, granted_at, expires_at in grants:
            total += 1
            print(f"  - {operation}  mode={mode} granted={granted_at} "
                  f"expires={expires_at}")
            if args.apply:
                parsed = _parse_run_key(operation)
                if parsed is None:
                    print(f"    ! could not parse run key, skipping")
                    continue
                name, run_id = parsed
                revoke_workflow_run(
                    name, run_id, session_id=sid, reason="orphan_purge",
                )
                print(f"    -> revoked")

    if total == 0:
        print("No workflow_run grants found.")
    else:
        verb = "Revoked" if args.apply else "Found"
        print(f"\n{verb} {total} workflow_run grant(s).")
        if not args.apply:
            print("Dry-run only. Re-run with --apply to revoke.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Snapshot the v1 summarization DB before flipping use_incremental.

Run BEFORE any code change that would produce v2-shape rows. Copies
`<data_root>/summarization/summarization.db` to
`<data_root>/eval/summarization-v1-baseline/summarization.db`. Safe to
re-run — overwrites the snapshot.

Usage:

    python -m eval.v1_baseline_snapshot
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def main() -> int:
    from work_buddy.paths import data_dir

    src = data_dir("summarization") / "summarization.db"
    if not src.exists():
        print(f"ERROR: no v1 DB found at {src}", file=sys.stderr)
        return 2

    dst_dir = data_dir("eval") / "summarization-v1-baseline"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "summarization.db"
    shutil.copy(src, dst)
    size_kb = dst.stat().st_size // 1024
    print(f"Baseline snapshot saved: {dst} ({size_kb} KB)")

    # Quick stats from the snapshot.
    import sqlite3
    conn = sqlite3.connect(str(dst))
    conn.row_factory = sqlite3.Row
    try:
        items = conn.execute(
            "SELECT COUNT(*) AS n FROM summary_items "
            "WHERE namespace = 'conversation_session'"
        ).fetchone()["n"]
        topics = conn.execute(
            "SELECT COUNT(*) AS n FROM summary_nodes "
            "WHERE namespace = 'conversation_session' AND level = 1"
        ).fetchone()["n"]
        first = conn.execute(
            "SELECT MIN(generated_at) AS first FROM summary_items "
            "WHERE namespace = 'conversation_session'"
        ).fetchone()["first"]
        last = conn.execute(
            "SELECT MAX(generated_at) AS last FROM summary_items "
            "WHERE namespace = 'conversation_session'"
        ).fetchone()["last"]
    finally:
        conn.close()

    print(f"  Sessions in baseline: {items}")
    print(f"  Topics across sessions: {topics}")
    print(f"  Date range: {first}  ->  {last}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

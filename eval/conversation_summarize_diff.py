"""Side-by-side v1 baseline vs. v2 fresh output for hand-scoring.

Reads from:
- `<data_root>/eval/summarization-v1-baseline/summarization.db` (the v1 snapshot)
- `<data_root>/summarization/summarization.db` (the live DB, where v2 outputs
  appear after the feature flag is flipped and the queue worker drains)

Produces a markdown file `<data_root>/eval/diff-<timestamp>.md` with one
section per session, side-by-side rendering of the v1 and v2 summaries,
and an empty rubric table for the user to score.

Usage:

    # Default — score every session present in BOTH DBs.
    python -m eval.conversation_summarize_diff

    # Restrict to a specific session set (eval set from EVAL-PLAN.md).
    python -m eval.conversation_summarize_diff --sessions <id1> <id2> ...

    # Custom output file.
    python -m eval.conversation_summarize_diff --out /path/to/diff.md
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# DB readers
# ---------------------------------------------------------------------------


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_session_summary(conn: sqlite3.Connection, sid: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM summary_items "
        "WHERE namespace = 'conversation_session' AND item_id = ?",
        (sid,),
    ).fetchone()
    if row is None:
        return None
    topics_rows = list(conn.execute(
        "SELECT * FROM summary_nodes "
        "WHERE namespace = 'conversation_session' AND item_id = ? AND level = 1 "
        "ORDER BY ordinal",
        (sid,),
    ))
    root_row = conn.execute(
        "SELECT summary FROM summary_nodes "
        "WHERE namespace = 'conversation_session' AND item_id = ? AND level = 0",
        (sid,),
    ).fetchone()
    tldr = root_row["summary"] if root_row else ""
    topics = []
    for t in topics_rows:
        try:
            extra = json.loads(t["extra_json"] or "{}")
        except Exception:
            extra = {}
        topics.append({
            "title": extra.get("title", "(untitled)"),
            "summary": t["summary"],
            "span_start": extra.get("span_start"),
            "span_end": extra.get("span_end"),
            "turn_start": extra.get("turn_start"),
            "turn_end": extra.get("turn_end"),
            "keywords": extra.get("keywords", []),
        })
    return {
        "tldr": tldr,
        "topics": topics,
        "model": row["model"],
        "generated_at": row["generated_at"],
        "status": row["status"],
    }


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_one(summary: dict[str, Any] | None, label: str) -> str:
    if summary is None:
        return f"### {label}\n_(no summary)_\n"
    if summary["status"] != "ok":
        return f"### {label}\n_(error row — status={summary['status']})_\n"

    lines = [f"### {label}"]
    lines.append(f"**Model:** `{summary['model']}` · **Generated:** {summary['generated_at']}")
    lines.append(f"**TL;DR:** {summary['tldr']}")
    lines.append("**Topics:**")
    for i, t in enumerate(summary["topics"]):
        title = t["title"]
        s_start = t["span_start"]
        s_end = t["span_end"]
        t_start = t["turn_start"]
        t_end = t["turn_end"]
        if isinstance(t_start, int) and isinstance(t_end, int):
            rng = f"turns {t_start}-{t_end}"
        elif isinstance(s_start, int) and isinstance(s_end, int):
            rng = f"spans {s_start}-{s_end}"
        else:
            rng = "?"
        lines.append(f"  {i+1}. **{title}** ({rng}) — {t['summary']}")
    return "\n".join(lines) + "\n"


_RUBRIC_TABLE = """
| Rubric | V1 (1-5) | V2 (1-5) | Notes |
|---|---|---|---|
| R1 Coverage (do topics span the whole session?) | _ | _ | |
| R2 Faithfulness (do topics match real stretches of work?) | _ | _ | |
| R3 Granularity (right level — not too coarse, not too fine?) | _ | _ | |
| R4 Title quality (recognizable a week later?) | _ | _ | |
| R5 TL;DR utility (useful as a one-liner?) | _ | _ | |
"""


def _render_session_block(sid: str, v1: dict[str, Any] | None, v2: dict[str, Any] | None) -> str:
    n_v1_topics = len(v1["topics"]) if v1 else 0
    n_v2_topics = len(v2["topics"]) if v2 else 0
    n_v1_tail = v1["topics"][-1].get("span_end") if v1 and v1["topics"] else None
    n_v2_tail = v2["topics"][-1].get("span_end") if v2 and v2["topics"] else None
    return (
        f"# Session `{sid[:12]}…`\n\n"
        f"V1 topics: {n_v1_topics} (last span_end={n_v1_tail}) · "
        f"V2 topics: {n_v2_topics} (last span_end={n_v2_tail})\n\n"
        + _render_one(v1, "V1 baseline")
        + "\n"
        + _render_one(v2, "V2 fresh")
        + "\n## Score\n" + _RUBRIC_TABLE
        + "\n---\n\n"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", nargs="*", default=None,
                        help="Specific session_ids to include (default: all in both DBs)")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output markdown file (default: <data_root>/eval/diff-<ts>.md)")
    args = parser.parse_args(argv)

    from work_buddy.paths import data_dir

    v1_path = data_dir("eval") / "summarization-v1-baseline" / "summarization.db"
    v2_path = data_dir("summarization") / "summarization.db"
    if not v1_path.exists():
        print(f"ERROR: no v1 baseline at {v1_path}. Run `python -m eval.v1_baseline_snapshot` first.", file=sys.stderr)
        return 2
    if not v2_path.exists():
        print(f"ERROR: no live v2 DB at {v2_path}.", file=sys.stderr)
        return 2

    out = args.out
    if out is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = data_dir("eval")
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"diff-{ts}.md"

    v1_conn = _open_ro(v1_path)
    v2_conn = _open_ro(v2_path)
    try:
        # Determine session set.
        if args.sessions:
            sids = list(args.sessions)
        else:
            v1_sids = {r["item_id"] for r in v1_conn.execute(
                "SELECT item_id FROM summary_items WHERE namespace='conversation_session'"
            )}
            v2_sids = {r["item_id"] for r in v2_conn.execute(
                "SELECT item_id FROM summary_items WHERE namespace='conversation_session'"
            )}
            sids = sorted(v1_sids & v2_sids)

        blocks = []
        blocks.append(
            f"# Conversation Summarization v1 vs v2 diff\n"
            f"\n"
            f"Generated: {datetime.now().isoformat()}\n"
            f"V1 baseline: {v1_path}\n"
            f"V2 live: {v2_path}\n"
            f"Sessions compared: {len(sids)}\n\n"
            f"Rubric (1-5 each; see EVAL-PLAN.md):\n"
            f"- **R1 Coverage** — do topics span the whole session?\n"
            f"- **R2 Faithfulness** — do topics match real stretches of work?\n"
            f"- **R3 Granularity** — right level?\n"
            f"- **R4 Title quality** — recognizable a week later?\n"
            f"- **R5 TL;DR utility** — useful one-liner?\n\n"
            f"Pass criterion: v2 ≥ v1 on every rubric mean; R1 long-session "
            f"mean improvement ≥ 0.5; no individual session regression ≥ 1.\n\n"
            f"---\n\n"
        )
        for sid in sids:
            v1 = _load_session_summary(v1_conn, sid)
            v2 = _load_session_summary(v2_conn, sid)
            blocks.append(_render_session_block(sid, v1, v2))
    finally:
        v1_conn.close()
        v2_conn.close()

    out.write_text("".join(blocks), encoding="utf-8")
    print(f"Diff written: {out}")
    print(f"Sessions compared: {len(sids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

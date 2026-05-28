"""Incremental vs. whole-session equivalence eval (PRD §8 + OQ14).

For each session in the eval set:

1. **Whole-session mode** — call v2's incremental refresh ONCE with an
   empty starting topic list and the entire session as fresh tail.
   Result: the model produced the summary in one pass.

2. **Replayed-incremental mode** — simulate the session growing in N
   stages (default 5). Stage 1 covers the first 20% of turns; each
   subsequent stage adds ~10-20%. Between stages, persist the topic
   list to a separate scratch namespace so the next stage sees it as
   "prior topics."

Renders side-by-side comparison in markdown for hand-scoring on R2
(faithfulness) and R4 (title quality). EVAL-PLAN.md pass criterion:
incremental within 0.5 of whole-session on each rubric.

Usage:

    python -m eval.replay_incremental --sessions <id1> <id2> ... [--stages 5]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def _build_summarizer_for_scratch(namespace: str):
    """Build a v2 incremental summarizer pointing at a scratch namespace
    in the live summarization DB (so we don't pollute the real
    conversation_session rows)."""
    from work_buddy.conversation_observability.summarizer_binding import (
        SessionSource,
    )
    from work_buddy.summarization.strategies import IncrementalLayeredStrategy
    from work_buddy.summarization.stores import DurableSummaryStore
    from work_buddy.summarization.summarizer import Summarizer

    return Summarizer(
        name=f"eval_scratch:{namespace}",
        source=SessionSource(),
        strategy=IncrementalLayeredStrategy(),
        store=DurableSummaryStore(
            namespace=namespace,
            selection_version=2,
            cache_version=2,
        ),
    )


def _whole_session_pass(sid: str) -> dict[str, Any]:
    """Run v2 once across the whole session."""
    summarizer = _build_summarizer_for_scratch("eval_whole")
    node = summarizer.refresh_one(sid, force=True)
    return _node_to_dict(node, summarizer.store, sid)


def _replayed_pass(sid: str, stages: int) -> dict[str, Any]:
    """Simulate the session growing in `stages` stages, refreshing v2
    after each stage."""
    from work_buddy.conversation_observability.summarizer_binding import (
        SessionSource,
    )
    source = SessionSource()
    total = source.total_turns(sid)
    if total is None or total < stages:
        return {"error": f"session {sid}: too few turns ({total}) for {stages} stages"}

    summarizer = _build_summarizer_for_scratch("eval_replayed")

    # We can't actually grow the session — instead we simulate by making
    # the source pretend the session has only N turns at each stage.
    # Use a wrapper that overrides total_turns / render_from / render_range.
    class _PartialSource:
        name = "eval_partial"
        capabilities = frozenset()

        def __init__(self, inner, cap):
            self._inner = inner
            self._cap = cap

        def discover(self, window):
            return self._inner.discover(window)

        def render(self, item_id):
            return self._inner.render_range(item_id, 0, self._cap)

        def render_batch(self, item_ids):
            return [self.render(i) for i in item_ids]

        def total_turns(self, item_id):
            return min(self._cap, self._inner.total_turns(item_id) or 0)

        def render_from(self, item_id, from_turn):
            return self._inner.render_range(item_id, from_turn, self._cap)

        def render_range(self, item_id, fr, to):
            return self._inner.render_range(item_id, fr, min(to, self._cap))

    # Per-stage caps: 20%, 40%, 60%, 80%, 100% (for stages=5).
    fractions = [(i + 1) / stages for i in range(stages)]
    caps = [max(1, int(total * f)) for f in fractions]
    caps[-1] = total  # ensure last stage covers everything

    for cap in caps:
        partial = _PartialSource(source, cap)
        # Swap the source temporarily.
        summarizer.source = partial
        # Bypass the cap-aware fresh-detection by forcing refresh.
        try:
            summarizer.refresh_one(sid, force=True)
        except Exception as exc:
            return {"error": f"stage cap={cap} failed: {exc}"}

    return _node_to_dict(summarizer.store.load(sid), summarizer.store, sid)


def _node_to_dict(node, store, sid: str) -> dict[str, Any]:
    if node is None:
        return {"error": "no result"}
    meta = store.load_item_meta(sid) or {}
    return {
        "tldr": node.summary,
        "topics": [
            {
                "title": c.extra.get("title", ""),
                "summary": c.summary,
                "span_start": c.extra.get("span_start"),
                "span_end": c.extra.get("span_end"),
                "keywords": c.extra.get("keywords", []),
            }
            for c in node.children
        ],
        "model": meta.get("model"),
        "pathway": meta.get("pathway"),
        "chunks_used": meta.get("chunks_used"),
        "activity_kind": meta.get("activity_kind"),
    }


def _render(d: dict[str, Any], label: str) -> str:
    if "error" in d:
        return f"### {label}\n_ERROR: {d['error']}_\n"
    lines = [f"### {label}"]
    lines.append(f"**Model:** `{d.get('model')}` · **Pathway:** {d.get('pathway')} · **Chunks:** {d.get('chunks_used')}")
    lines.append(f"**TL;DR:** {d.get('tldr', '')}")
    lines.append("**Topics:**")
    for i, t in enumerate(d.get("topics", [])):
        s_start = t.get("span_start")
        s_end = t.get("span_end")
        rng = f"spans {s_start}-{s_end}" if isinstance(s_start, int) else "?"
        lines.append(f"  {i+1}. **{t['title']}** ({rng}) — {t['summary']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", nargs="+", required=True,
                        help="Session ids to evaluate (3-5 recommended)")
    parser.add_argument("--stages", type=int, default=5,
                        help="Number of stages for replayed-incremental (default: 5)")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output markdown path (default: <data_root>/eval/equivalence-<ts>.md)")
    args = parser.parse_args(argv)

    from work_buddy.paths import data_dir

    out = args.out
    if out is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = data_dir("eval")
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"equivalence-{ts}.md"

    rubric_table = """
| Rubric | Whole (1-5) | Incremental (1-5) | Notes |
|---|---|---|---|
| R2 Faithfulness | _ | _ | |
| R4 Title quality | _ | _ | |
"""

    blocks = [
        "# Incremental vs. whole-session equivalence eval\n\n"
        f"Generated: {datetime.now().isoformat()}\n"
        f"Stages per session (replayed): {args.stages}\n\n"
        "Pass criterion: incremental scores within 0.5 of whole-session on R2 and R4.\n\n"
        "If incremental falls behind: feed more context (full prior topic bodies vs compressed forms; see OQ14 lever).\n\n"
        "---\n\n"
    ]

    for sid in args.sessions:
        print(f"Processing {sid[:12]}… (whole + {args.stages}-stage replayed)")
        whole = _whole_session_pass(sid)
        replayed = _replayed_pass(sid, args.stages)
        blocks.append(
            f"# Session `{sid[:12]}…`\n\n"
            + _render(whole, "Whole-session mode")
            + "\n"
            + _render(replayed, f"Replayed-incremental ({args.stages} stages)")
            + "\n## Score\n" + rubric_table
            + "\n---\n\n"
        )

    out.write_text("".join(blocks), encoding="utf-8")
    print(f"Equivalence eval written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

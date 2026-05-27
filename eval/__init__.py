"""Evaluation tooling for v2 conversation summarization.

These scripts are operator-facing — used to:

- Snapshot the v1 baseline before flipping `use_incremental: true`
  (`v1_baseline_snapshot.py`).
- Compare v1 baseline vs. v2 fresh outputs side-by-side for hand-scoring
  (`conversation_summarize_diff.py`).
- Run the equivalence check (whole-session vs replayed-incremental) on
  v2 (`replay_incremental.py`).

See `.data/designs/conversation-summarize/EVAL-PLAN.md` for the rubric
and pass criteria.
"""

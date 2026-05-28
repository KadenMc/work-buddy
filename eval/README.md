# Eval tooling — conversation summarization v2

Operator-facing scripts for the v2 flag-flip gate. See
`.data/designs/conversation-summarize/EVAL-PLAN.md` for the full rubric
and pass criteria.

## Quick-reference order

```bash
# 1. Snapshot the v1 baseline BEFORE flipping any flag.
python -m eval.v1_baseline_snapshot

# 2. Flip the flag in config.local.yaml:
#      conversation_observability.summaries.use_incremental: true
#    (with summaries.enabled: true)
#    Wait for the queue worker to drain — ~$0.40 of Haiku for the
#    current ~41 sessions. Monitor via `summarization_worker_tick`.

# 3. Once v2 has produced rows, generate the diff:
python -m eval.conversation_summarize_diff
# Output: <data_root>/eval/diff-<timestamp>.md — open and hand-score.

# 4. Equivalence eval — pick 3-5 representative sessions:
python -m eval.replay_incremental --sessions <id1> <id2> <id3>
# Output: <data_root>/eval/equivalence-<timestamp>.md — hand-score.

# 5. If both eval passes meet the criteria → leave flag on.
#    If quality fails → flip flag back off, debug, iterate.
```

## Pass criteria

**Quality eval** (`conversation_summarize_diff`):
- v2 ≥ v1 mean on every rubric (R1-R5)
- R1 long-session improvement ≥ 0.5
- No individual session regresses by ≥ 1 on any rubric

**Equivalence eval** (`replay_incremental`):
- Replayed-incremental within 0.5 of whole-session on R2 + R4
- If failing: switch from compressed prior topics → full prior bodies
  (OQ14 lever)

## Costs

Pulled from the v1 baseline (2026-05-27 measurement, 71 calls):
- Total v1 cost so far: ~$0.87
- v2 cutover sweep cost: ~$0.40 (re-summarize the 41 existing sessions
  once at Haiku rates)
- Steady-state v2: ~$0.07/day at current observation rate
- The daily budget circuit-breaker (default $1.00) keeps any runaway in
  check.

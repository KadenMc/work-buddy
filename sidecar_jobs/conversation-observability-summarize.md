---
schedule: "7 */2 * * *"  # every 2 hours at minute 07
recurring: true
jitter_seconds: 180
type: capability
capability: conversation_observability_summarize
params:
  days: 7
  max_sessions: 3
  force: false
feature_gated:
  config_path: conversation_observability.summaries.enabled
  default: false
---
Generate LLM topic summaries (tldr + bounded topic list with span
ranges) for stale Claude Code sessions. Disabled by default; opt in by
setting `conversation_observability.summaries.enabled: true` in
`config.local.yaml`.

Each call is bounded: at most 3 sessions per run, every 2 hours, with
~3 minutes of jitter to avoid colliding with the per-5-minute commit/
writes refresh. The summaries persist to `session_summaries` and
`topic_summaries` with full provenance (model, profile, backend,
prompt_version, schema_version, selection_version, cache_version) —
bump any version constant in
`work_buddy/conversation_observability/summaries.py` to invalidate
every cached summary.

Stale detection is multi-axis: missing row, version bump, or source
file mtime newer than `generated_at`. The job is safe to leave running
because failed calls don't overwrite prior good summaries — only the
status flag flips.

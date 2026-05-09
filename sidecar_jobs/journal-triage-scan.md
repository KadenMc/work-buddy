---
schedule: "7 * * * *"  # hourly at :07, off the top-of-hour pile-up
recurring: true
type: capability
capability: run_source_pipeline
params:
  source: journal_backlog
---
Background triage of the current day's Running Notes through the
unified source pipeline.

Pipeline stages:
1. Segment today's Running Notes into one CapturedItem per line-range
   cluster via the journal segmenter (local-first tier_chain).
2. Annotate each item with Haiku-generated tags + summary.
3. Algorithmic precluster (embedding-fused Louvain + tag signal).
4. LLM cluster refinement (local-first tier_chain via
   `triage.refine_clusters.tier_chain`) — names clusters and proposes
   per-cluster actions from the journal action library.
5. Spawn a group-relationship umbrella thread + N children with the
   journal segments as ContextItems. The user reviews + approves on
   the dashboard's Threads tab.

The cadence (`schedule:` above) is a job-level concern — change it
here without touching the capability. The capability itself has no
opinion on how often it runs.

Never mutates the vault directly — the journal-route actions
(`journal_route_to_tasks`, `journal_route_to_considerations`,
`journal_append_to_note`) are consent-gated and only fire when the
user approves them on the spawned threads.

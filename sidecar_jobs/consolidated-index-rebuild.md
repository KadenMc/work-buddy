---
schedule: "*/15 * * * *"  # every 15 minutes
recurring: true
jitter_seconds: 60  # spread 15-minute pile-ups
type: capability
capability: index_rebuild
params:
  partition: knowledge
  force: false
---
Keep the consolidated index's `knowledge` partition fresh so the activated
`/wb-dev-document` scan searches current docs. A **no-op while `index.enabled` is
false** (ships inert); once the flag is flipped it runs an incremental,
content-hash-diffed build (sub-second when nothing changed). Matches the
`docs-index-rebuild` cadence over the same `knowledge/store/` corpus — the live
knowledge index is reactive (rebuilt on edit), so this `*/15` sweep is what keeps
the separate consolidated DB in step.

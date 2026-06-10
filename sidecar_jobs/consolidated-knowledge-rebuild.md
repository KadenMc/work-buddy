---
schedule: "*/15 * * * *"  # knowledge cadence — matches docs-index-rebuild (same corpus)
recurring: true
jitter_seconds: 60  # spread 15-minute pile-ups
type: capability
capability: index_rebuild
params:
  partition: knowledge
  force: false
---
Keep the consolidated index's **`knowledge` partition** fresh (only) so the activated
`/wb-dev-document` scan searches current docs. A no-op while `index.enabled` is false (ships
inert); once on, an incremental content-hash-diffed build (sub-second when nothing changed).
`*/15` matches `docs-index-rebuild` over the same `knowledge/store/` corpus.

**One job per partition — by design.** This mirrors the live indexes, which each have their own
rebuild job (`docs-index-rebuild`, `ir-index-rebuild`, `vault-index-rebuild`, …) at their own
cadence. There is intentionally **no single "rebuild every consolidated partition" cron**: when
another partition is activated, add a sibling `consolidated-<partition>-rebuild` job with a
cadence matched to its corpus (conversation/vault are far less frequent + multi-hour). Do NOT
fold partitions into one `build_all` schedule — that would couple unrelated corpora, let a slow
build starve a fast one, and fail them together. (`op.wb.index_rebuild`'s `build_all` path is for
a deliberate one-shot full rebuild — e.g. the dashboard seam — never a recurring job.)

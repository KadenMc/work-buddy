---
schedule: "*/15 * * * *"  # knowledge cadence — matches the docs corpus refresh
recurring: true
jitter_seconds: 60  # spread 15-minute pile-ups
type: capability
capability: index_rebuild
params:
  partition: knowledge
  force: false
---
Keep the index's **`knowledge` partition** current so the `/wb-dev-document` scan searches
up-to-date docs. A no-op while `index.enabled` is false; once on, an incremental content-hash-diffed
pass — embeds only new/changed units (sub-second when nothing changed), full on the first run.
`*/15` matches the `knowledge/store/` corpus's refresh cadence. The `index_rebuild` op self-skips
(read-only advisory-lock probe) while any index build is running — all partitions share one
single-writer DB, so builds serialize on a DB-wide writer gate and a refresh never piles onto an
in-flight build.

**One job per partition — by design.** Sibling to the other `index-<partition>-refresh` jobs; each
runs at a cadence matched to its corpus (conversation/vault are far less frequent + heavier). Never
fold partitions into a single `build_all` schedule — that couples unrelated corpora, lets a slow
build starve a fast one, and fails them together. (`op.wb.index_rebuild`'s `build_all` path is for a
deliberate one-shot full build — e.g. the dashboard seam — never a recurring job.)

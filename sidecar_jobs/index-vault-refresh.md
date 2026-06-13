---
schedule: "0 */6 * * *"  # vault cadence — very large corpus, multi-hour first build, every 6h
recurring: true
jitter_seconds: 200  # distinct offset; spread the 6-hourly fire
type: capability
capability: index_rebuild
params:
  partition: vault
  force: false
---
Keep the index's **`vault` partition** current so vault chunks stay searchable. A no-op while
`index.enabled` is false; once on, an incremental mtime-diffed pass — embeds only new/changed chunks
(cheap when nothing changed), full on the first run. The corpus is very large and the first full
build is multi-hour, so this runs only every 6 hours. The `index_rebuild` op self-skips (read-only
advisory-lock probe) while any index build is running — all partitions share one single-writer DB,
so builds serialize on a DB-wide writer gate, an over-running build is never re-entered, and a
refresh never piles onto another partition's build.

**One job per partition — by design.** Sibling to the other `index-<partition>-refresh` jobs; never
folded into a single `build_all` cron.

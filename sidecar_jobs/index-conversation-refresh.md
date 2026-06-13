---
schedule: "0 * * * *"  # conversation cadence — heavy corpus (tens of thousands of spans), hourly
recurring: true
jitter_seconds: 150  # distinct offset; spread the top-of-hour fire
type: capability
capability: index_rebuild
params:
  partition: conversation
  force: false
---
Keep the index's **`conversation` partition** current so past sessions stay searchable. A no-op
while `index.enabled` is false; once on, an incremental content-hash-diffed pass — embeds only
new/changed spans (cheap when nothing changed), full on the first run. This is a heavy partition, so
it runs hourly — far less frequently than the small partitions. The `index_rebuild` op self-skips
(read-only advisory-lock probe) while any index build is running — all partitions share one
single-writer DB, so builds serialize on a DB-wide writer gate and a refresh never piles onto an
in-flight build.

**One job per partition — by design.** Sibling to the other `index-<partition>-refresh` jobs; never
folded into a single `build_all` cron.

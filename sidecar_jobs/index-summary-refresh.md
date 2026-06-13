---
schedule: "*/30 * * * *"  # summary cadence — 2h producer upstream, 30-min picker is ample
recurring: true
jitter_seconds: 135  # distinct offset; spread 30-minute pile-ups
type: capability
capability: index_rebuild
params:
  partition: summary
  force: false
---
Keep the index's **`summary` partition** current so summarization-framework summaries stay
searchable. A no-op while `index.enabled` is false; once on, an incremental content-hash-diffed
pass — embeds only new/changed items (cheap when nothing changed), full on the first run. Summaries
are produced on a 2-hour cadence, so a 30-minute picker is ample without needless churn. The
`index_rebuild` op self-skips (read-only advisory-lock probe) while any index build is running —
all partitions share one single-writer DB, so builds serialize on a DB-wide writer gate and a
refresh never piles onto an in-flight build.

**One job per partition — by design.** Sibling to the other `index-<partition>-refresh` jobs; never
folded into a single `build_all` cron.

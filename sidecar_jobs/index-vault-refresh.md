---
schedule: "23 */6 * * *"  # every 6h, at :23 — OFF the top-of-hour pileup (see body); very large corpus, multi-hour first build
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
build is multi-hour, so this runs only every 6 hours — at **:23** past the hour, deliberately off
the top-of-hour grid where the `*/15` (chrome, knowledge) and hourly (conversation) refreshes fire.
That offset matters because of the self-skip below: the `index_rebuild` op self-skips (read-only
advisory-lock probe) while any index build is running — all partitions share one single-writer DB,
so builds serialize on a DB-wide writer gate, an over-running build is never re-entered, and a
refresh never piles onto another partition's build. A job that always woke into the top-of-hour
pileup would therefore be *starved* (skipped every time) rather than queued; firing at :23 lets it
take the gate.

**One job per partition — by design.** Sibling to the other `index-<partition>-refresh` jobs; never
folded into a single `build_all` cron.

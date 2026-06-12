---
schedule: "*/15 * * * *"  # chrome cadence — tiny corpus, sub-second incremental
recurring: true
jitter_seconds: 120  # distinct offset; spread 15-minute pile-ups
type: capability
capability: index_rebuild
params:
  partition: chrome
  force: false
---
Keep the index's **`chrome` partition** current so browsing-history search serves recent pages.
A no-op while `index.enabled` is false; once on, an incremental content-hash/mtime-diffed pass —
embeds only new/changed items (sub-second when nothing changed), full on the first run. The corpus
is small, so `*/15` is cheap. The `index_rebuild` op self-skips (read-only advisory-lock probe) when
a build for this partition is already running.

**One job per partition — by design.** Sibling to the other `index-<partition>-refresh` jobs; never
folded into a single `build_all` cron.

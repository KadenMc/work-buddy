---
schedule: "*/5 * * * *"  # every 5 minutes
recurring: true
jitter_seconds: 75  # distinct offset from ir-index's 90 so the two 5-min jobs don't pile up
type: capability
capability: vault_index
params:
  action: build
---
Rebuild the vault semantic index (chunks + dense vectors) incrementally so notes
and repos stay searchable via `vault_search`. The `vault_index` capability does a
read-only advisory-lock check and self-skips when a build is already in progress;
otherwise it POSTs to the embedding service, which builds in-service so the encode
yields to interactive searches at BACKGROUND priority. Fast when nothing changed
(mtime check).

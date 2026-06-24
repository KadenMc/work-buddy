---
schedule: "4-59/5 * * * *"  # minute 4 of each 5-min cycle — heaviest index build, isolated on its own minute
recurring: true
jitter_seconds: 45  # intra-minute de-sync only; the minute-4 offset does the coarse spread off the other index crons
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

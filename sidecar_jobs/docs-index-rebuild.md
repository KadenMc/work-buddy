---
schedule: "*/15 * * * *"  # every 15 minutes
recurring: true
jitter_seconds: 60  # spread 15-minute pile-ups
type: capability
capability: ir_index
params:
  action: build
  source: docs
  days: 365  # knowledge units don't really expire — index everything in the last year
---
Rebuild the docs (knowledge-store) IR index so knowledge units are searchable via
`find(source="docs", query=...)` (the structured-result alternative to
`agent_docs(query=...)`). The `docs` source indexes every `.md` file under
`knowledge/store/` and `knowledge/store.local/`. Runs every 15 minutes — fast
when nothing has changed.

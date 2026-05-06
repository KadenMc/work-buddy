---
short: Inspect vault structure
---
Load directions via `mcp__work-buddy__wb_run("agent_docs", {"path": "vault/recon-directions", "depth": "full"})`, then read `.data/vault_recon/latest.json` (or run `mcp__work-buddy__wb_run("vault_recon")` if missing/stale). Read-only — do NOT run `vault_recon_collect` here. Present 3–5 striking findings in plain English; no proposals (that's the investigation agent's job).

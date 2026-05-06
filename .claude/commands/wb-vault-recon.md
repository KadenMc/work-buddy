---
short: Inspect vault structure
---
Load directions via `mcp__work-buddy__wb_run("agent_docs", {"path": "vault/recon-directions", "depth": "full"})`, then read the most recent ledger snapshot at `.data/vault_recon/latest.json` (written by the periodic `vault_recon_collect`).

If `latest.json` doesn't exist or is older than 24h, run `mcp__work-buddy__wb_run("vault_recon")` directly for a fresh snapshot. Do NOT run `vault_recon_collect` from the slash command — that's the cron path; it appends to the ledger and may escalate to an investigation agent. The slash command is read-only.

Present the 3-5 most striking findings in plain English (state machines, tag families, hot regions, anything stuck or notable). Do not propose actions; that's the investigation agent's job.

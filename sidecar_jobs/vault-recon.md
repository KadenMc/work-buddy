---
schedule: "0 2 * * *"
recurring: true
type: capability
capability: vault_recon_collect
params: {}
enabled: true
---

# Vault recon collector

Daily vault reconnaissance: snapshot the frontmatter / tag / structural state
of the vault, append to a 60-day rolling ledger, compute deltas vs prior
snapshots, and escalate significant changes (new types, new tag families,
stuck states, path activity spikes, growing status backlogs) to a one-shot
investigation agent that surfaces a proposal to the user via `request_send`.

Fires at 02:00 local time. The collector itself takes ~2-5 seconds (one
Datacore bridge call + ledger I/O). Investigation agents are spawned as
separate one-shot `type: prompt` jobs in `.data/user_jobs/` and are picked
up by the scheduler hot-reload (~30s) on the next tick.

See `vault/recon-directions` for how to read the cross-tabs and
`vault/investigation-directions` for the investigation protocol.

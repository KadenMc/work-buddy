---
schedule: "0 2 * * *"
recurring: true
type: capability
capability: vault_recon_collect
params: {}
enabled: false
---

# Vault recon collector (retired)

Disabled as part of the Obsidian authority retirement. The collector requires
the app-only Datacore bridge and must not run, probe, spawn investigation jobs,
or recommend Obsidian setup in the native content profile.

The implementation and stored snapshots remain available only for explicit
legacy inspection during the migration grace period. Re-enabling this system
job is not part of a domain authority cutover.

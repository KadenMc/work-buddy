---
name: Obsidian Integration
kind: integration
description: Explicit legacy compatibility for Obsidian import, export, bridge, and filesystem fallback; native Work Buddy domains do not require the app.
summary: The retained HTTP bridge and plugin adapters support opted-in legacy workflows and migration/rollback. Obsidian is disabled cleanly by preference and is not authority for native Journal, Tasks, Contracts, Projects, Personal Knowledge, or Calendar.
tags:
- obsidian
- vault
- bridge
- plugins
---

This subtree documents retained legacy compatibility. When explicitly enabled,
the HTTP bridge on port 27125 can support migration, rollback, import/export,
and old app-only adapters. With the feature opted out, Work Buddy does not
probe the bridge, retry its operations, bootstrap plugin listeners, or suggest
setup. Native Journal, Tasks, Contracts, Projects, Personal Knowledge, and
Calendar use their own database/provider authorities. Filesystem Vault search
also remains native and independent of the Obsidian app; see
`architecture/vault-index`.

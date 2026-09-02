---
name: Vault Investigation Agent Directions (Retired)
kind: directions
description: Historical documentation for the retired vault-recon investigation escalation; it must not spawn, probe Obsidian, or notify the user.
trigger: operator audits a retained legacy vault-recon escalation receipt
tags:
- vault
- investigation
- retired
- migration
aliases:
- vault investigation
- investigation agent
- vault delta
parents:
- vault
---

The scheduled Vault Recon collector and its generated one-shot investigation
jobs are retired. Do not create new investigation jobs, run Datacore, probe the
Obsidian bridge, add accepted queries, or send remediation notifications.

Retained snapshots, escalation history, and accepted-query files are migration
evidence only. They may be read explicitly by an operator during the grace
period; they are not native content authority and must not feed recurring
context or setup guidance.

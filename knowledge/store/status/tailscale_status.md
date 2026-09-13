---
name: Tailscale Status
kind: skill
description: 'Check Tailscale VPN status: daemon state, tailnet identity, online peers, and Serve configuration (published ports).'
category: status
op: op.wb.tailscale_status
schema_version: wb-skill/v1
slash_command: wb-tailscale-status
skill_name: tailscale_status
tags:
- status
- tailscale
aliases:
- vpn
- tailscale
- tailnet
- remote access
- serve
parents:
- status
---

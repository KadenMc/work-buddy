---
name: Tailscale Status
kind: capability
description: 'Check Tailscale VPN status: daemon state, tailnet identity, online peers, and Serve configuration (published ports).'
capability_name: tailscale_status
category: status
op: op.wb.tailscale_status
schema_version: wb-capability/v1
slash_command: wb-tailscale-status
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

---
name: Tailscale Status Directions
kind: directions
description: Check Tailscale VPN status — daemon state, tailnet identity, online peers, Serve config
summary: Pure capability call -- no special presentation required.
trigger: user asks about Tailscale, VPN status, or published ports
command: wb-tailscale-status
capabilities:
- status/tailscale_status
tags:
- status
- tailscale
- vpn
- directions
aliases:
- vpn status
- tailscale check
- tailnet status
- remote access check
parents:
- status
dev_notes: Tailscale serve CLI takes a single target argument (port or URL); HTTPS is the default mode. Older `tailscale serve --bg https <url>` syntax is rejected by current CLI versions. Fixer + fix_hint + this directions unit use port-only form (`tailscale serve --bg 5127`) for clarity.
---

Run `mcp__work-buddy__wb_run("tailscale_status")` and read the result as a diagnostic, not a state dump. The capability returns daemon state (`installed`, `running`, `backend_state`), this device's tailnet identity (`self.online`, `self.name`, `tailnet`), peers (`peers[].online`, `peers[].last_seen`), and Serve config (`serve.Web` handlers). Match what you see against the table below and lead with the fix.

## Common diagnoses

| Symptom | Likely cause | Fix |
|---|---|---|
| `installed=false` or `error` set | tailscale CLI missing / not on PATH | Run `setup_help` with `component="tailscale"` and click Fix on `integrations/tailscale/installed` (spawns a guided install). |
| `running=false` or `backend_state` ≠ `Running` | daemon stopped | Open the Tailscale tray app (Windows / macOS) or `sudo tailscale up` (Linux). On Windows, `net start Tailscale` from elevated PowerShell. |
| `self.online=false` | this device signed out / paused / node key expired | Open the Tailscale app and toggle on / sign in. If a long time has passed, reauthenticate at https://login.tailscale.com/admin/machines. |
| `serve` is null or has no Web handler matching the dashboard port | Serve config dropped (Windows daemon restarts can lose it) | Click Fix on `integrations/tailscale/serve-configured` in the dashboard Settings tab, or run `tailscale serve --bg 5127` manually (5127 is the dashboard's local port; HTTPS is Serve's default mode). |
| Peer with `online=false` and a stale `last_seen` (e.g. weeks old) | **device-side** Tailscale toggled off; on Android, often the app killed by battery optimization | Open Tailscale on **that device**, sign in / toggle on. On Android, set Settings → Apps → Tailscale → Battery → Unrestricted so the OS stops killing the background tunnel. *Work-buddy can't probe the remote device — this is observation only and lives here, not in the wizard.* |

## Surfaces and division of labour

* **For *this device's* setup** — run `setup_help(component="tailscale")` (or `setup_wizard(mode="diagnose", component="tailscale")`). That's where laptop-side requirements (CLI installed, Serve published) and runtime checks (daemon running, self online) live with click-to-fix affordances.
* **For peer-side problems** — stay in this directions unit. Work-buddy has no introspection into the remote device; the diagnosis is informational only.

This split is intentional. The wizard implies agency ("we can examine and recommend a fix"); for a phone with Tailscale toggled off, only the user, in person, can make it green again.

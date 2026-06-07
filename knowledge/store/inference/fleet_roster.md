---
name: Fleet Roster
kind: capability
description: 'Add/update or clear a machine''s entry in the inference.fleet roster (config.local.yaml) — its role label and optional hardware specs. Enriches live-discovered fleet machines, joined by device_id.'
capability_name: fleet_roster
category: inference
op: op.wb.fleet_roster
schema_version: wb-capability/v1
parameters:
  action:
    type: str
    description: '"set" (add/update) or "remove" (clear the entry). Default "set".'
    required: false
  device_id:
    type: str
    description: The machine's provider device identifier (the join key). Required.
    required: true
  role:
    type: str
    description: Human label for the machine (e.g. "Remote compute node"). The primary field.
    required: false
  gpus:
    type: list
    description: 'List of {name, vram_gb} for a peer — machines can have several GPUs. Omit to leave unchanged; pass [] to clear. (The local machine reports its own live.)'
    required: false
  ram_gb:
    type: float
    description: Optional system RAM in GB for a peer.
    required: false
tags:
- inference
- fleet
- roster
- config
aliases:
- set machine role
- edit fleet entry
- fleet roster edit
parents:
- architecture/inference
---

Writes a machine's entry in the `inference.fleet` roster (persisted to
`config.local.yaml`). The roster only *enriches* live-discovered fleet machines —
joined by `device_id` — so `device_id` is the only required field; `role` is the
primary human label and hardware specs are optional (peer hardware can't be
auto-detected; the local machine reports its own live, so its roster specs are
ignored for display).

`action: "set"` adds or updates the entry; `action: "remove"` clears it (the
machine still appears via live discovery). Backs the inline editor in the
dashboard's Settings › Inference fleet section; the dashboard route
(`POST /api/fleet/roster`) wraps this capability, gating on read-only mode and
busting the fleet cache + publishing `fleet.changed` on success.

Returns `{success, action, device_id, note}`, or `{success: false, errors_by_field}`
on validation failure (e.g. a non-numeric VRAM value) so the form can highlight
the offending input.

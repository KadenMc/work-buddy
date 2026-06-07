---
name: Local Model Fleet
kind: system
description: Per-machine "what's loaded on which box" view of the local-inference fleet — reachability, loaded models, and hardware (multi-GPU) — behind a provider-neutral seam with LM Studio as the first adapter.
dev_notes: |-
  ## Headless data-source map (LM Studio adapter)

  - ``lms link status --json`` → machines + reachability + loaded-model names. The
    local machine is the top-level ``deviceIdentifier``/``deviceName``, NOT in
    ``peers[]`` — ``merge_fleet`` synthesizes the local row from those top-level fields.
  - ``lms ps --json`` → loaded-model instance detail (quant, context, status),
    device-attributed and cross-peer; joined by ``deviceIdentifier``.
  - ``lms runtime survey --json`` → LOCAL machine hardware only; per-GPU dedicated
    VRAM via ``gpuInfo[].dedicatedMemoryCapacityBytes`` (fallback
    ``totalMemoryCapacityBytes``). There is NO headless path to remote-peer
    hardware — that is why peer specs live in the roster.

  ## Cache + poller

  ``get_fleet_summary`` serves a ~20s background-refreshed cache (the three
  subprocesses never run on a request thread). ``start_fleet_poller`` (25s,
  started in ``service.main``) refreshes the cache and publishes ``fleet.changed``
  only when the *material* fingerprint (per machine: reachability + sorted
  loaded-model names) changes — volatile fields (last-used time, queued, context)
  are excluded so the bus is not spammed. The fleet section subscribes to
  ``fleet.changed``; it does NOT subscribe to ``inference.call_logged``.

  ## Roster shape + merge semantics

  Canonical roster hardware is ``gpus: [{name, vram_gb}]``; ``merge_fleet`` and the
  ``fleet_roster`` capability accept a legacy scalar ``gpu``/``vram_gb`` as a
  single-GPU fallback, and ``fleet_roster`` migrates an entry off the scalar keys
  on edit. ``fleet_roster`` field semantics: ``None`` → omit (preserve), ``""``/``[]``
  → clear, value → set — so partial updates preserve unspecified fields and the
  dashboard form (which sends every field) is a full replace.

  ## lms binary resolution

  ``_resolve_lms_bin`` tries PATH then ``~/.lmstudio/bin/lms[.exe]`` (the
  dashboard/sidecar PATH differs from an interactive shell). A missing binary or
  unreadable link status → ``lms_available: false`` and the roster renders offline;
  never raises.
tags:
- inference
- fleet
- lmstudio
- lm-link
- dashboard
- gpu
- vram
- provider-neutral
aliases:
- local model fleet
- fleet view
- what's loaded on which machine
- per-machine models
parents:
- architecture/inference
---

## What it is

The Local model fleet is a per-machine view of the local-inference fleet: one
card per machine (the local host + every reachable LM Link peer) showing
reachability, the model(s) currently loaded (with quantization, status, and live
context-vs-max), and hardware (GPUs + RAM). It answers "what's loaded on which
box" — the per-machine question the [broker](architecture/inference/broker)
can't, since the broker only knows the *profile* (model), not the machine. It
renders as a section of the Settings › Inference sub-view, above the per-call
[provenance](architecture/inference/provenance) feed.

## Provider seam

Provider-neutral. `merge_fleet(link_status, ps, local_hardware, roster)`
(`work_buddy/inference/fleet.py`) is a pure function over already-parsed inputs
and knows nothing about any backend. A provider adapter is the only place that
talks to the backend; today that is LM Studio via the `lms` CLI. Swapping to
vLLM / Ollama / llama.cpp is a new adapter — not a change to `merge_fleet`, the
dashboard reader, the routes, or the capabilities.

## Data layers

- **Discovery (live):** machines + reachability + loaded models come from the
  provider. The local machine is always present; remote peers appear when reachable.
- **Hardware:** the local machine reports its own GPUs/RAM live. Remote-peer
  hardware is NOT readable headless, so it comes from the static `inference.fleet`
  config roster, joined by `device_id`. A rostered machine that isn't currently
  discovered shows as offline rather than vanishing.
- **Multi-GPU:** a machine carries a list of GPUs (`{name, vram_gb}`); the card
  sums a total VRAM.

## Config

`inference.fleet` is a list of `{device_id, role, ram_gb, gpus: [{name, vram_gb}]}`.
The roster only *enriches* discovered machines; zero config still renders
discovered machines with hardware "unknown". It is managed end-to-end from the
dashboard (the inline editor writes it) — users don't hand-edit it.

## Surfaces

- `GET /api/fleet` — the cached per-machine snapshot (read-only).
- `POST /api/fleet/roster` — add/update or clear a machine's roster entry
  (read-only-gated; the click is the consent; mirrors `/api/embeddings/vault`).
- `fleet_status` capability — read the snapshot (answers "what's on which box /
  can my laptop run model X").
- `fleet_roster` capability — edit a machine's roster entry.
- `fleet.changed` SSE event — published when a machine's reachability or
  loaded-model set changes; the section morphs the cards in.

## Key files

- `work_buddy/inference/fleet.py` — dataclasses, pure `merge_fleet`, LM Studio
  adapter, `read_fleet`.
- `work_buddy/dashboard/api.py` — `get_fleet_summary` (background cache) +
  `start_fleet_poller`.
- `work_buddy/dashboard/service.py` — `/api/fleet` + `/api/fleet/roster` + index
  pre-warm + poller start.
- `work_buddy/dashboard/frontend/scripts/tabs/fleet.py` — the section + inline editor.
- `work_buddy/mcp_server/ops/inference_ops.py` — `fleet_status` / `fleet_roster` ops.

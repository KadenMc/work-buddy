---
name: Fleet Status
kind: capability
description: 'Local model fleet snapshot — per-machine reachability, currently loaded model(s) with live context utilization, and hardware (GPU/VRAM/RAM), across the local-inference fleet (LM Studio + LM Link).'
capability_name: fleet_status
category: inference
op: op.wb.fleet_status
schema_version: wb-capability/v1
tags:
- inference
- fleet
- lmstudio
- lm-link
- models
- vram
aliases:
- model fleet
- what's loaded on which machine
- which box is running which model
- fleet status
- local model status
- can my laptop run this model
parents:
- architecture/inference
---

Returns the **local model fleet** snapshot: one entry per machine in the
local-inference fleet, with reachability, the model(s) currently loaded (with
quantization, status, and live context length vs the model's trained max), and
hardware (GPU / VRAM / RAM). Answers "what's running on which box" — the
per-machine question the inference broker can't, since the broker only knows the
*profile* (model), not the machine.

Takes no parameters. Reads are live (the configured local-inference provider —
LM Studio today — is enumerated via its CLI) and never raise: when the provider
is unreachable, each machine degrades to `reachable: false` and `lms_available`
is `false`. Remote-peer hardware comes from the `inference.fleet` config roster
(the local machine reports its own hardware live); each machine's `hardware.source`
records whether it was read `live` or came from the `roster`.

Use this to decide where a model should run — e.g. whether a model that won't fit
the local GPU should be routed to a higher-VRAM peer. The same snapshot backs the
dashboard's Settings › Inference fleet section.

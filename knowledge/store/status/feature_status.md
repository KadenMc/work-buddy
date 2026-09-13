---
name: Feature Status
kind: skill
description: Show which tools, features, and skills are available or disabled, and why. Use this to diagnose missing integrations.
category: status
op: op.wb.feature_status
schema_version: wb-skill/v1
parameters:
  verbose:
    type: bool
    description: Include probe timing and config details
    required: false
  force:
    type: bool
    description: Re-run probes for wanted tools instead of reading cached results. Force never overrides features.<component>.wanted=false and never probes an opted-out Obsidian bridge or child plugin.
    required: false
skill_name: feature_status
tags:
- status
- feature
aliases:
- tools
- integrations
- what's available
- feature toggle
- disabled
- unavailable
parents:
- status
---

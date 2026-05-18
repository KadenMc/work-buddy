---
name: Feature Status
kind: capability
description: Show which tools, features, and capabilities are available or disabled, and why. Use this to diagnose missing integrations.
capability_name: feature_status
category: status
parameters:
  verbose:
    type: bool
    description: Include probe timing and config details
    required: false
  force:
    type: bool
    description: Re-run all tool probes fresh instead of reading the cached result. Use when a previously-failed tool (e.g. Obsidian) may now be available.
    required: false
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
- status
---

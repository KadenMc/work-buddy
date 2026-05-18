---
name: Obsidian Retry
kind: capability
description: Synchronous bridge-aware retry for Obsidian-dependent capabilities. Checks bridge health before each attempt, waits between retries, and returns a structured result. Use when you need the result before proceeding (e.g., step 1 of a multi-step task). For fire-and-forget retries, the gateway's automatic background retry handles it.
capability_name: obsidian_retry
category: obsidian
parameters:
  operation_id:
    type: str
    required: true
    description: Operation ID from a previously failed or timed-out call (included in wb_run/consent_request timeout returns; visible via wb_status). Capability name and params are loaded from the record, so the agent does not re-supply them. If you don't have an operation_id you don't need retry — just call the capability directly; the gateway's automatic background retry handles transient bridge hiccups.
  max_retries:
    type: int
    required: false
    description: 'Maximum number of attempts including the first (default: 3)'
  wait_seconds:
    type: int
    required: false
    description: 'Seconds to wait between attempts (default: 60)'
tags:
- obsidian
- retry
aliases:
- obsidian retry
- bridge retry
- retry with bridge
- retry task create
- bridge failure
- obsidian unavailable
parents:
- obsidian
- obsidian
---

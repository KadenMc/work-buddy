---
name: Setup Help Directions
kind: directions
description: How to present component health diagnostics — structured output format, lead with the fix
summary: 'Pass component name as argument (or ''all''). Present: 1) Summary counts, 2) Per-component issues with root cause and fix, 3) Dependency chain if upstream-caused. Lead with the fix, not the architecture.'
trigger: user asks to diagnose a component or troubleshoot something not working
command: wb-setup-help
capabilities:
- status/setup_help
tags:
- status
- setup
- diagnostics
- health
- directions
aliases:
- diagnose component
- troubleshoot
- setup help
- component health
parents:
- status
---

Run mcp__work-buddy__wb_run("setup_help", {"component": "$ARGUMENTS"}). If no argument, pass "all".

Present:
1. Summary -- N healthy, N unhealthy, N disabled
2. Issues -- for each unhealthy component: name, status, root cause, fix suggestion
3. Dependency chain -- if failure is caused by a dependency, show the chain

Keep actionable. Lead with the fix, not the architecture.

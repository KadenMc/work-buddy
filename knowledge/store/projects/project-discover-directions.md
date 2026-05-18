---
name: Discovering Project Candidates
kind: directions
description: How to evaluate and triage project_discover candidates — create, alias, or ignore
summary: 'project_discover scans task tags (#projects/<slug>) and git repos for signals not matching any confirmed project. For each candidate: decide create / alias in config.yaml / ignore. Watch for artifact candidates (e.g., #projects/open-source is a tag, not a real project). Present candidates with your assessment and let the user decide.'
trigger: agent or user wants to find unregistered project candidates from vault signals
command: wb-project-discover
capabilities:
- projects/project_discover
- projects/project_create
tags:
- projects
- project_discover
- discovery
- triage
aliases:
- project_discover
- find projects
- unregistered projects
- project candidates
parents:
- projects
- projects
---

Call: mcp__work-buddy__wb_run("project_discover")

What is scanned:
- Task tags (#projects/<slug>)
- Git repos

For each candidate, evaluate and triage with one of three actions:
1. Create as a new project → mcp__work-buddy__wb_run("project_create", {...}) (or /wb-project-new)
2. Alias to an existing project → add to config.yaml under projects.aliases
3. Ignore → no action needed

Heuristics:
- Is this a real, distinct project? Or an artifact?
  - Example: #projects/open-source is likely a tag category, not a standalone project
- Does it have ongoing tasks or git activity that warrant tracking?

Presentation:
- Present all candidates to the user with your assessment (proposed action + reasoning)
- Let the user make the final call on each

---
name: Recording Project Observations
kind: directions
description: What makes a good observation, slug disambiguation, and existence prerequisite for project_observe
summary: 'Use project_observe for things that wouldn''t appear in code or task lists: supervisor feedback, strategic pivots, abandoned approaches, scope changes, deadlines, risk signals. If slug is ambiguous, call project_list first. Project must already exist — use /wb-project-new first if needed.'
trigger: user wants to log a decision, pivot, blocker, feedback, or insight about a project
command: wb-project-observe
capabilities:
- projects/project_observe
tags:
- projects
- project_observe
- hindsight
- memory
- observation
aliases:
- project_observe
- record observation
- project memory
- project note
- strategic pivot
parents:
- projects
- projects
---

Call: mcp__work-buddy__wb_run("project_observe", params)

Parameters:
- project (required): project slug. If ambiguous, call project_list to show options first.
- content (required): the observation.

What counts as a good observation (capture things that wouldn't appear in code or task lists):
- Supervisor feedback
- Strategic pivots or direction changes
- Abandoned approaches (what you tried and why it failed)
- Scope changes
- Deadlines or milestones
- Risk signals

Constraints:
- The project must already exist in the registry
- If it doesn't exist: use project_create first (wb-project-new)

Observations are retained into Hindsight for LLM-powered extraction and semantic search.

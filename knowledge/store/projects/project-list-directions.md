---
name: Listing Projects
kind: directions
description: How to list and present projects — grouping by status, detail drill-down via project_get
summary: Call project_list, then present results grouped by status (active, inferred, paused, future, past). Show slug, name, and description per project. For detail on a specific project, use project_get which includes Hindsight memory recall.
trigger: user asks to see their projects or wants a project overview
command: wb-project-list
capabilities:
- projects/project_list
- projects/project_get
tags:
- projects
- project_list
- project_get
- presentation
aliases:
- project_list
- show projects
- list projects
- project status
parents:
- projects
- projects
---

Call: mcp__work-buddy__wb_run("project_list")

Presentation rules:
- Group results by status in this order: active, paused, future, past
- For each project show: slug, name, description (if set), folder count, alias list (if any)
- Soft-deleted projects (status='deleted') are filtered by default. To inspect deleted rows, pass `include_deleted=True`.

For details on a specific project:
mcp__work-buddy__wb_run("project_get", {"slug": "..."})

Works with aliases too: passing `slug='ElectricRAG'` or `slug='ECG-CRED'` resolves to the canonical project via the alias table. The returned record includes folders (with archived flag) and aliases (display + normalized form). Hindsight memory recall is unioned across the project's slug and all its aliases — old memories tagged with prior slugs still surface.

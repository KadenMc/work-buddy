---
name: Creating a Project
kind: directions
description: Parameter defaults, slug rules, when to ask vs infer, and post-creation ritual for project_create
summary: 'Required: slug (lowercase-hyphens, infer from name if given) and name. Status defaults to ''active'' — only ask if ambiguous. Ask for description if not provided (it powers semantic search). After creation, confirm slug and suggest an initial project_observe to seed memory.'
trigger: user wants to register a new project
command: wb-project-new
capabilities:
- projects/project_create
- projects/project_observe
tags:
- projects
- project_create
- project_observe
- slug
aliases:
- project_create
- new project
- create project
- add project
parents:
- projects
- projects
---

Call: mcp__work-buddy__wb_run("project_create", params)

Parameters:
- slug (required): lowercase, hyphens. If the user gave a name, slugify it. Don't ask.
- name (required): human-readable project name.
- status: default "active". Only ask if ambiguous.
- description: a sentence or two about what this project is. Ask if not provided — descriptions power semantic search.

Don'ts:
- Do not over-prompt — infer slug from name, default status to active
- Do not skip asking for description — it is used for semantic search

Post-creation ritual:
1. Confirm with the slug.
2. Suggest adding an initial observation:
   mcp__work-buddy__wb_run("project_observe", {"project": "<slug>", "content": "..."})
   This seeds Hindsight memory for the project.

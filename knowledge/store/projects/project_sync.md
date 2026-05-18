---
name: Project Sync
kind: capability
description: 'Reconcile project markdown notes (work-buddy/projects/<slug>.md) against the projects SQLite registry: propagate out-of-band note edits into the store, create store rows for new notes. Markdown-canonical; never deletes a project. See architecture/markdown-db.'
capability_name: project_sync
category: projects
tags:
- projects
- project
- sync
aliases:
- sync projects
- reconcile projects
- project drift
- project markdown sync
- project note sync
parents:
- projects
- projects
---

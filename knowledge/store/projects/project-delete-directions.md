---
name: Deleting a Project
kind: directions
description: Pre-flight steps before deleting a project — confirm slug, explain impact, then call
summary: 'Before calling project_delete: (1) confirm the slug with the user, (2) explain that registry identity is removed but Hindsight memories are preserved. Then call — the consent system handles approval. If user wants to clear memories too, use memory_prune separately afterward.'
trigger: user wants to remove a project from the registry
command: wb-project-delete
capabilities:
- projects/project_delete
tags:
- projects
- project_delete
- consent
- destructive
aliases:
- project_delete
- delete project
- remove project
parents:
- projects
- projects
---

Pre-flight sequence (do not skip):
1. Confirm the slug with the user.
2. Show what will happen: the project identity is removed from the registry, but Hindsight memories are preserved.
3. Call: mcp__work-buddy__wb_run("project_delete", {"slug": "..."})
   The consent system will handle approval automatically.

Don'ts:
- Do not call project_delete without confirming the slug first
- Do not assume Hindsight memories are deleted — they are not

If the user also wants to clear Hindsight memories:
mcp__work-buddy__wb_run("memory_prune", {...})
This is a separate consent-gated operation.

---
name: Inline Commands Overview (Retired)
kind: concept
description: Historical architecture of the retired Obsidian menu and #wb/cmd/* tag framework.
summary: 'The app callbacks, MCP declarations, slash workflow, and scheduled reconciliation are outside the native product path. Python remains only for migration and dependency audit.'
tags:
- inline
- obsidian
- retired
- migration
aliases:
- inline architecture
- inline framework
parents:
- inline
---

# Inline Commands (retired)

Obsidian right-click commands and `#wb/cmd/*` tag activation are retired.
Native document selections, Tasks, Threads, and explicit application actions are
the supported interaction surfaces. Do not register new handlers, invoke the
legacy dispatcher, scan vault tags, or recommend enabling Obsidian.

`work_buddy/inline/` remains as compatibility code until its dashboard/plugin
call sites and retained watcher data have a separate deletion audit. Its
presence does not make it an active capability or authority.

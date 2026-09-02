---
name: Project Sync
kind: capability
description: 'Legacy pre-seal reconciliation for project Markdown. Once Projects SQLite is authoritative, returns a deterministic disabled result and performs no file reads or writes.'
capability_name: project_sync
category: projects
op: op.wb.project_sync
schema_version: wb-capability/v1
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
---

This capability exists only across the migration boundary. It must not be used
as an export mechanism or a second writable authority. After the SQLite seal,
project edits go through revisioned domain APIs and rich roles through explicit
Co-work bindings.

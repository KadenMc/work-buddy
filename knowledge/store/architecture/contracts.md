---
name: Contracts
kind: concept
description: Explicit work commitments — schema, lifecycle, bounded deliverables
summary: Contracts are revisioned records in the Contracts SQLite authority. Any bounded deliverable qualifies; rich narrative roles may bind explicitly to Co-work.
tags:
- contracts
- commitments
- deliverables
- deadlines
aliases:
- work commitments
- deliverables
- WIP limit
- contract schema
parents:
- architecture
- architecture
---

Contracts make work commitments explicit. Identity, status, dates, constraints,
WIP policy, evidence references, revisions, and tombstones live in the Contracts
SQLite authority. Any bounded deliverable qualifies (papers, deployments,
grants, admin). A rich brief can use an explicit Co-work body role; legacy
Markdown is accepted only by the deterministic pre-seal importer.

---
name: Conversation Observability
kind: concept
description: Durable session-attributed activity derived from Claude Code JSONL sessions — commits, writes, uncommitted work, topic summaries
tags:
- conversation_observability
- sessions
- attribution
- commits
- observability
---

A SQLite-backed observability layer over Claude Code session transcripts. A refresh pass scans recent session JSONL files and derives durable tables: observed-session metadata, session-attributed git commits, session-attributed file writes (with dirty-state snapshots), and per-session topic summaries. Queryable for "which session touched this", "what did this session leave uncommitted", and recency-sorted session listings.

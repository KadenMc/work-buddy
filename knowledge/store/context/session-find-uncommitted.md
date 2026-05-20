---
name: Session Find Uncommitted
kind: directions
description: How to invoke and present uncommitted session results — per-entry format and follow-up suggestion
summary: 'Run session_uncommitted (optional days param, default 7). For each result: show 8-char session ID prefix, repo name, dirty files with git status codes. If other sessions have uncommitted changes, suggest resuming them.'
trigger: user asks which sessions left uncommitted changes, or wants to audit agent writes
command: wb-session-find-uncommitted
capabilities:
- context/session_uncommitted
tags:
- context
- session
- uncommitted
- git
aliases:
- find uncommitted
- uncommitted sessions
- sessions with dirty files
- who didn't commit
parents:
- context
---

Find which agent sessions wrote files that are still uncommitted.

Run mcp__work-buddy__wb_run("session_uncommitted") with optional days parameter (default 7).

For each result entry, report:
- The session ID (8-char prefix is enough for session_get / session_search lookups)
- The repo name
- The dirty files and their git status (M = modified, ?? = untracked)

If any sessions besides the current one have uncommitted changes, suggest the user resume those sessions to commit or discard the changes.

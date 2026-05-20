---
name: Agent Sessions
kind: reference
description: Session ID setup, agent directories, Python conda environment, and Poetry dependency management
summary: Session ID is auto-set by SessionStart hook. Use powershell.exe wrapper for conda Python. Never pip install — use Poetry.
entry_points:
- work_buddy.agent_session
tags:
- session
- conda
- poetry
- python
- agent-directory
aliases:
- WORK_BUDDY_SESSION_ID
- conda activate
- poetry add
- session directory
parents:
- operations
- operations
dev_notes: Use work_buddy.compat.conda_activate_command(repo_root, module) for OS-portable Python execution. Never hardcode 'powershell.exe' in knowledge content or code — it breaks on macOS/Linux. The compat function handles Windows (PowerShell) and Unix (bash + conda hook) automatically. See work_buddy/compat.py lines 151-170. The hardcoded powershell.exe command in this unit's own content is a known Windows-only shortcut for the user — agent-facing code and docs should use compat instead.
---

WORK_BUDDY_SESSION_ID is set automatically by a SessionStart hook (.claude/hooks/session-init.sh). On Claude Code Desktop, the hook outputs it as context.

Agent directories live under data/agents/<session>/. Created automatically when wb_init is called. Contains: manifest.json, consent.db (SQLite), activity ledger (JSONL), context bundles, logs, and workflow DAG state.

To run Python in the conda env:
powershell.exe -Command "cd <repo-root>; conda activate work-buddy; <command>"

NEVER use pip install. Always use Poetry:
- Production deps: poetry add <package>
- Temporary deps: poetry add --group temp <package>
- Remove: poetry remove <package>

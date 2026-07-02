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
dev_notes: To spawn a work-buddy Python module OS-portably, resolve the interpreter with work_buddy.compat.resolve_child_python() (honors the sidecar.python_executable pin, else sys.executable) and run [python, '-u', '-m', module] with compat.build_child_env() and compat.detached_process_kwargs(). Never hardcode 'powershell.exe' or 'conda activate' in knowledge content or code; it breaks off-conda and on macOS/Linux. The hardcoded powershell.exe command in this unit's own content is a known Windows-only shortcut for the user; agent-facing code and docs should use the compat helpers instead.
---

WORK_BUDDY_SESSION_ID is set automatically by a SessionStart hook (.claude/hooks/session-init.sh). On Claude Code Desktop, the hook outputs it as context.

Agent directories live under data/agents/<session>/. Created automatically when wb_init is called. Contains: manifest.json, consent.db (SQLite), activity ledger (JSONL), context bundles, logs, and workflow DAG state.

To run Python in the conda env:
powershell.exe -Command "cd <repo-root>; conda activate work-buddy; <command>"

NEVER use pip install. Always use Poetry:
- Production deps: poetry add <package>
- Temporary deps: poetry add --group temp <package>
- Remove: poetry remove <package>

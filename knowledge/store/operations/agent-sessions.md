---
name: Agent Sessions
kind: reference
description: Session ID setup, agent directories, the uv-managed Python environment, and uv dependency management
summary: Session ID is auto-set by SessionStart hook. Run Python via uv. Never pip install, use uv (uv add).
entry_points:
- work_buddy.agent_session
tags:
- session
- uv
- python
- agent-directory
aliases:
- WORK_BUDDY_SESSION_ID
- uv run
- uv add
- session directory
parents:
- operations
- operations
dev_notes: To spawn a work-buddy Python module OS-portably, resolve the interpreter with work_buddy.compat.resolve_child_python() (honors the sidecar.python_executable pin, else sys.executable) and run [python, '-u', '-m', module] with compat.build_child_env() and compat.detached_process_kwargs(). Never hardcode a shell activation wrapper (e.g. 'powershell.exe' or 'conda activate') in knowledge content or code; it breaks cross-platform. Prefer `uv run` at the shell, and the compat helpers in agent-facing code and docs.
---

WORK_BUDDY_SESSION_ID is set automatically by a SessionStart hook (.claude/hooks/session-init.sh). On Claude Code Desktop, the hook outputs it as context.

Agent directories live under data/agents/<session>/. Created automatically when wb_init is called. Contains: manifest.json, consent.db (SQLite), activity ledger (JSONL), context bundles, logs, and workflow DAG state.

To run Python, use uv (it manages the project `.venv` for you):
uv run python <args>   # e.g. uv run python -m work_buddy.<module>

NEVER use pip install. Use uv:
- Production deps: uv add <package>
- Temporary deps: uv add --group dev <package>
- Remove: uv remove <package>

"""Back-compat shim — moved to :mod:`work_buddy.projects.sync`.

The project "collector" was always a sync job (scans signals, writes to
the SQLite registry, returns bundle markdown), not a context fetcher.
It now lives under ``work_buddy.projects.sync`` alongside the rest of
the project domain code. This module re-exports its public surface so
existing callers don't break.

New code should import from :mod:`work_buddy.projects.sync` (and prefer
``sync_projects`` over ``collect``).
"""

from work_buddy.projects.sync import (  # noqa: F401 — re-export
    collect,
    sync_projects,
    _scan_vault_projects,
    _scan_state_files,
    _scan_task_projects,
    _scan_git_activity,
    _scan_contracts,
    _merge_project_signals,
    _render_project,
    _render_markdown,
    _sync_to_store,
    _retain_state_file,
    _normalize_slug,
    _resolve_alias,
    _extract_state_fingerprint,
    _extract_status_table,
    _dir_mtime,
)

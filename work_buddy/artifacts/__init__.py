"""Centralized artifact lifecycle management for work-buddy.

This package replaces the original single-file
``work_buddy/artifacts.py`` with a composition-based system:

    Artifact = Storage × Lifecycle × (Provenance)

Each consumer (filesystem artifacts, messaging, caches, sessions, …)
registers an :class:`Artifact` describing how its data is stored, when
records expire, what happens at expiry, and which agent-facing
operations are exposed via MCP. The cleanup tick iterates the registry
and calls ``.prune()`` on each — one orchestrator for every persisted
resource in the system.

Backwards compatibility
-----------------------

Every name that used to be importable from ``work_buddy.artifacts`` is
still importable here:

* ``ArtifactStore`` (now an alias for :class:`FilesystemStorage`)
* ``ArtifactRecord``
* ``ARTIFACT_TYPES``
* Module-level convenience functions: ``save``, ``get``,
  ``list_artifacts``, ``read_content``, ``delete``, ``cleanup``
* All ``prune_*`` functions referenced by ``paths.PRUNERS``
* ``_run_pruners`` (legacy private name)

External code does not need to change for Phase A of the artifact
unification work.
"""

from __future__ import annotations

from typing import Any

# --- New composition API ---------------------------------------------------

from work_buddy.artifacts.protocol import (  # noqa: F401  (re-exports)
    Artifact,
    Capability,
    ExpiryAction,
    IncoherentComposition,
    Lifecycle,
    Operation,
    Provenance,
    Ref,
    Storage,
    SweepResult,
    Trigger,
    UnsupportedOperation,
)
from work_buddy.artifacts.registry import (  # noqa: F401  (re-exports)
    artifact_registry_dump,
    get_artifact,
    list_artifact_names,
    register_artifact,
    sweep_all,
)
from work_buddy.artifacts.expiry import (  # noqa: F401  (re-exports)
    expires_at_iso,
    format_for_user,
    is_expired,
)
from work_buddy.artifacts.io import (  # noqa: F401
    atomic_write_bytes,
    atomic_write_text,
)

# --- Backends --------------------------------------------------------------

from work_buddy.artifacts.backends.filesystem import (  # noqa: F401
    ARTIFACT_TYPES,
    ArtifactRecord,
    FilesystemStorage,
)
from work_buddy.artifacts.backends.sqlite_rows import (  # noqa: F401
    SqliteRowsStorage,
)
from work_buddy.artifacts.backends.json_records import (  # noqa: F401
    JsonRecordsShape,
    JsonRecordsStorage,
)
from work_buddy.artifacts.backends.jsonl import JsonlStorage  # noqa: F401
from work_buddy.artifacts.backends.sqlite_rollup import (  # noqa: F401
    SqliteRollupStorage,
)
from work_buddy.artifacts.backends.directory_tree import (  # noqa: F401
    DirectoryTreeStorage,
    DirShape,
)

# --- Lifecycle components (Phase C) ----------------------------------------

from work_buddy.artifacts.lifecycle.actions import (  # noqa: F401
    Delete,
    TransformAndDelete,
)
from work_buddy.artifacts.lifecycle.triggers import (  # noqa: F401
    MtimeWindow,
    NeverExpires,
    PerRecordTtl,
    PerTypeTtl,
    TimeWindow,
)
from work_buddy.artifacts.provenance import SessionTagged  # noqa: F401


# ---------------------------------------------------------------------------
# Lifecycle conveniences
# ---------------------------------------------------------------------------

# Single canonical "this data is durable" lifecycle. Pass to
# ``Artifact(lifecycle=INFINITE_LIFECYCLE)`` for subsystems whose data
# must outlive every cleanup tick. The trigger's ``is_expired`` always
# returns False, so the action — kept as :class:`Delete` for shape
# consistency with the rest of the registry — will never fire.
#
# A grep for ``INFINITE_LIFECYCLE`` across the codebase enumerates every
# artifact that opted into infinite retention, making intent auditable
# in a way a sentinel TTL is not.
INFINITE_LIFECYCLE = Lifecycle(trigger=NeverExpires(), action=Delete())

# Eagerly register the default artifacts (filesystem, logs/global) at
# package import time. Consumer-owned artifacts (chrome-ledger,
# llm-cache, segmentation-cache, escalations-log, agent-sessions,
# claude-code-usage, messages, llm-queue, notifications) register from
# their own modules; they appear in the registry the first time those
# modules are imported.
from work_buddy.artifacts.default_registrations import (  # noqa: E402
    register_default_artifacts,
)
register_default_artifacts()

# Legacy public name. ``FilesystemStorage`` and ``ArtifactStore`` are the
# same class — the rename happened to make room for sibling backends
# (SqliteRowsStorage, JsonRecordsStorage, etc.) that share the Storage
# protocol.
ArtifactStore = FilesystemStorage

# --- Legacy meta-pruner functions -----------------------------------------
#
# Referenced by string in ``work_buddy.paths.PRUNERS``
# (e.g. ``"work_buddy.artifacts.prune_chrome_ledger"``); these imports
# preserve those references through the refactor. Will be removed in
# Phase G after every consumer is migrated to the unified registry.

from work_buddy.artifacts.meta_pruners import (  # noqa: F401
    _run_pruners,
    prune_chrome_ledger,
    prune_claude_code_usage_db,
    prune_escalation_log,
    prune_llm_cache,
    prune_messages_db,
    prune_old_logs,
    prune_stale_sessions,
    run_pruners,
)


# ---------------------------------------------------------------------------
# Module-level convenience (lazy singleton) — preserves old API
# ---------------------------------------------------------------------------

_default_store: FilesystemStorage | None = None


def get_store() -> FilesystemStorage:
    """Return (or create) the default filesystem artifact store."""
    global _default_store
    if _default_store is None:
        _default_store = FilesystemStorage()
    return _default_store


def save(
    content: str | bytes,
    type: str,
    slug: str,
    ext: str = "json",
    **kwargs: Any,
) -> ArtifactRecord:
    """Save a filesystem artifact. See :meth:`FilesystemStorage.save`."""
    return get_store().save(content, type, slug, ext, **kwargs)


def list_artifacts(**kwargs: Any) -> list[ArtifactRecord]:
    """List filesystem artifacts. See :meth:`FilesystemStorage.list`."""
    return get_store().list(**kwargs)


def get(artifact_id: str) -> ArtifactRecord:
    """Get a filesystem artifact by ID. See :meth:`FilesystemStorage.get`."""
    return get_store().get(artifact_id)


def read_content(artifact_id: str) -> str:
    """Read filesystem artifact content. See :meth:`FilesystemStorage.read_content`."""
    return get_store().read_content(artifact_id)


def delete(artifact_id: str) -> bool:
    """Delete a filesystem artifact. See :meth:`FilesystemStorage.delete`."""
    return get_store().delete(artifact_id)


def cleanup(dry_run: bool = True) -> dict[str, Any]:
    """Run TTL-based cleanup. See :meth:`FilesystemStorage.cleanup`."""
    return get_store().cleanup(dry_run=dry_run)

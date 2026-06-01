"""Default Artifact registrations for consumers without a natural module home.

Two registrations live here:

* ``filesystem`` — the original ``.data/<type>/`` artifact storage.
  Doesn't have a natural "consumer module" since FilesystemStorage IS
  what was the artifact system. Registered here.
* ``logs/global`` — the rolling log-file pruner under ``.data/logs/``.
  Same situation — there's no consumer module that owns the log
  directory; it's a shared sink that gets cleaned up periodically.

This module is imported eagerly at the end of
:mod:`work_buddy.artifacts.__init__` so the registrations always run
on package import.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register_filesystem_artifact() -> None:
    """Register the default filesystem artifact ('.data/<type>/' blobs)."""
    try:
        from work_buddy.artifacts.backends.filesystem import (
            ARTIFACT_TYPES,
            FilesystemStorage,
        )
        from work_buddy.artifacts.lifecycle.actions import Delete
        from work_buddy.artifacts.lifecycle.triggers import PerTypeTtl
        from work_buddy.artifacts.protocol import (
            Artifact,
            Lifecycle,
            Operation,
        )
        from work_buddy.artifacts.provenance import SessionTagged
        from work_buddy.artifacts.registry import register_artifact

        register_artifact(Artifact(
            name="filesystem",
            storage=FilesystemStorage(),
            lifecycle=Lifecycle(
                trigger=PerTypeTtl(
                    ttl_days_by_type=dict(ARTIFACT_TYPES),
                    default_ttl_days=14,
                ),
                action=Delete(),
            ),
            provenance=SessionTagged(session_field="session_id"),
            exposed_operations=frozenset({
                Operation.SAVE,
                Operation.GET,
                Operation.LIST,
                Operation.DELETE,
                Operation.CLEANUP,
            }),
        ))
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Failed to register filesystem artifact: %s", exc)


def register_logs_global_artifact() -> None:
    """Register the default logs/global artifact (rolling log file cleanup)."""
    try:
        from work_buddy.artifacts.backends.directory_tree import (
            DirectoryTreeStorage,
            DirShape,
        )
        from work_buddy.artifacts.lifecycle.actions import Delete
        from work_buddy.artifacts.lifecycle.triggers import MtimeWindow
        from work_buddy.artifacts.protocol import (
            Artifact,
            Lifecycle,
            Operation,
        )
        from work_buddy.artifacts.registry import register_artifact
        from work_buddy.paths import data_dir

        logs_dir = data_dir("logs")

        register_artifact(Artifact(
            name="logs-global",
            storage=DirectoryTreeStorage(
                root=logs_dir,
                shape=DirShape.LOG_FILES,
                artifact_name="logs-global",
            ),
            lifecycle=Lifecycle(
                trigger=MtimeWindow(
                    mtime_field="_mtime",
                    max_age_days=7,
                ),
                action=Delete(),
            ),
            exposed_operations=frozenset({Operation.CLEANUP}),
        ))
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Failed to register logs-global artifact: %s", exc)


def _is_live_service_log(record: dict) -> bool:
    """True for a service's canonical live log (``<name>.log``).

    Used as the ``service-logs`` retention predicate so the *live* log is
    never reaped — only rolled-aside backups are age-eligible. A live log has
    no rotation suffix, so its stem contains no dot (``messaging`` →pinned),
    whereas backups carry a dotted stem (``messaging.20260601T…`` or the
    legacy ``messaging.1``). This is **name-based, not process-state-based**:
    a crashed/stopped service's last log is preserved too, matching
    logrotate's rule that ``maxage`` applies only to rotated files.
    """
    from pathlib import Path

    name = str(record.get("_file_name", ""))
    return name.endswith(".log") and "." not in Path(name).stem


def register_service_logs_artifact() -> None:
    """Register the service-logs artifact (subprocess stdout/stderr capture).

    Governs ``.data/runtime/service_logs/`` — the raw stdout/stderr capture of
    each sidecar child service. The daemon rolls an oversized *live* log aside
    at startup (``daemon._roll_oversize_log``); this artifact owns RETENTION,
    deleting rolled-aside backups older than the window while pinning each
    service's live log. Mirrors ``logs-global`` but for a different directory.
    """
    try:
        from work_buddy.artifacts.backends.directory_tree import (
            DirectoryTreeStorage,
            DirShape,
        )
        from work_buddy.artifacts.lifecycle.actions import Delete
        from work_buddy.artifacts.lifecycle.triggers import MtimeWindow
        from work_buddy.artifacts.protocol import (
            Artifact,
            Lifecycle,
            Operation,
        )
        from work_buddy.artifacts.registry import register_artifact
        from work_buddy.paths import data_dir

        service_logs_dir = data_dir("runtime/service_logs")

        register_artifact(Artifact(
            name="service-logs",
            storage=DirectoryTreeStorage(
                root=service_logs_dir,
                shape=DirShape.LOG_FILES,
                artifact_name="service-logs",
            ),
            lifecycle=Lifecycle(
                trigger=MtimeWindow(
                    mtime_field="_mtime",
                    max_age_days=7,
                ),
                action=Delete(),
                retention_predicate=_is_live_service_log,
            ),
            exposed_operations=frozenset({Operation.CLEANUP}),
        ))
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Failed to register service-logs artifact: %s", exc)


def register_default_artifacts() -> None:
    """Register all default artifacts that don't live in a consumer module."""
    register_filesystem_artifact()
    register_logs_global_artifact()
    register_service_logs_artifact()

"""Artifact registration for conversation-observability.

The DB is durable subsystem state — losing rows means re-scanning every
JSONL session and re-generating every summary, which is expensive and
unrecoverable for sessions whose source file has since been deleted.
Uses :data:`INFINITE_LIFECYCLE` to opt into infinite retention with
auditable intent. Pruning of derived rows (when a parent
``observed_sessions`` row is deleted) cascades via the
SqliteRowsStorage ``post_delete_sql`` hook so the four child tables
stay consistent without depending on SQLite FK enforcement.

The registration runs at package-import time — importing
``work_buddy.conversation_observability`` is sufficient to make the
artifact appear in ``artifact_registry_dump``. Mirror the messaging
pattern: callers don't need to invoke a registration function.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register_conversation_observability_artifact() -> None:
    """Register the ``conversation-observability`` artifact.

    Wrapped in a defensive try/except so an import error inside the
    artifact subsystem cannot cascade into making
    ``conversation_observability`` itself uninstallable. Matches the
    pattern used by messaging.
    """
    try:
        from work_buddy.artifacts import (
            Artifact,
            INFINITE_LIFECYCLE,
            register_artifact,
            SqliteRowsStorage,
        )
        from work_buddy.conversation_observability.db import db_path

        register_artifact(Artifact(
            name="conversation-observability",
            storage=SqliteRowsStorage(
                db_path=db_path(),
                table="observed_sessions",
                id_column="session_id",
                # When an observed_sessions row is removed, drop every
                # dependent row in the same transaction. Keeps the DB
                # consistent without relying on SQLite FK enforcement
                # (off by default; subtle to enable per-connection).
                post_delete_sql=[
                    "DELETE FROM session_commits "
                    "WHERE session_id NOT IN (SELECT session_id FROM observed_sessions)",
                    "DELETE FROM session_file_writes "
                    "WHERE session_id NOT IN (SELECT session_id FROM observed_sessions)",
                    "DELETE FROM topic_summaries "
                    "WHERE session_id NOT IN (SELECT session_id FROM observed_sessions)",
                    "DELETE FROM session_summaries "
                    "WHERE session_id NOT IN (SELECT session_id FROM observed_sessions)",
                ],
                vacuum_on_delete=False,
            ),
            lifecycle=INFINITE_LIFECYCLE,
        ))
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "Failed to register conversation-observability artifact: %s", exc,
        )

"""work-buddy MarkdownDB — two-way markdown ⇄ SQLite synchronisation.

A markdown-canonical sync abstraction: markdown files are the source of
truth, a SQLite store is a queryable projection. Subclass
:class:`MarkdownDB` per entity, declaring its fields and how its markdown
is parsed / rendered; the base class supplies orphan handling, the
per-field drift loop, conflict resolution, and dual-surface mutation.

See ``architecture/markdown-db`` for the subsystem reference.

Public API::

    from work_buddy.markdown_db import (
        MarkdownDB, FieldSpec, WriteProvenance, ReconcileReport,
        InMemoryLwwLog, NullLwwLog,
    )
"""

from work_buddy.markdown_db.base import MarkdownDB
from work_buddy.markdown_db.lww import (
    InMemoryLwwLog,
    LwwEntry,
    LwwLog,
    NullLwwLog,
)
from work_buddy.markdown_db.resolver import (
    Resolver,
    lww_markdown_wins,
    make_default_resolver,
)
from work_buddy.markdown_db.sqlite_lww import (
    LWW_META_DDL,
    SqliteLwwLog,
    ensure_lww_meta,
)
from work_buddy.markdown_db.storage_helpers import (
    atomic_write_text,
    file_lock,
    mtime_utc,
)
from work_buddy.markdown_db.types import (
    ACTOR_AGENT,
    ACTOR_SYSTEM,
    ACTOR_USER,
    PROCESS_DRIFT,
    PROCESS_MATERIALIZE,
    PROCESS_MIGRATION,
    PROCESS_MUTATION,
    SURFACE_DASHBOARD,
    SURFACE_EXTERNAL,
    SURFACE_MARKDOWN,
    SURFACE_STORE,
    Actor,
    Candidate,
    FieldSpec,
    ParsedFileRow,
    Process,
    ReconcileReport,
    Surface,
    WriteProvenance,
)

__all__ = [
    # Core
    "MarkdownDB",
    "FieldSpec",
    "ParsedFileRow",
    "Candidate",
    "ReconcileReport",
    "WriteProvenance",
    # LWW
    "LwwLog",
    "LwwEntry",
    "NullLwwLog",
    "InMemoryLwwLog",
    "SqliteLwwLog",
    "ensure_lww_meta",
    "LWW_META_DDL",
    # Resolver
    "Resolver",
    "lww_markdown_wins",
    "make_default_resolver",
    # Storage helpers
    "atomic_write_text",
    "file_lock",
    "mtime_utc",
    # Type aliases
    "Actor",
    "Process",
    "Surface",
    # Vocabulary constants
    "ACTOR_USER",
    "ACTOR_AGENT",
    "ACTOR_SYSTEM",
    "PROCESS_MUTATION",
    "PROCESS_DRIFT",
    "PROCESS_MATERIALIZE",
    "PROCESS_MIGRATION",
    "SURFACE_MARKDOWN",
    "SURFACE_STORE",
    "SURFACE_DASHBOARD",
    "SURFACE_EXTERNAL",
]

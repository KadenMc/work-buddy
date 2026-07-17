"""WAVE-1 SCAFFOLDING (WP-A2). TEST-ONLY. NOT production code.

This helper applies the frozen v2 co-work DDL (C1-kernel-contracts.md section 1)
verbatim to a v1 truth store so the shipped-module extensions this work package
owns can be exercised before WP-A1's real ``_m002`` migration and its store
durable-insert seam land. Six builders run in parallel in separate worktrees,
so those pieces are absent here.

The orchestrator REPLACES this helper with the real migration and store seam at
the join. Production code never imports this module. Its only jobs are:

- create a v2-shaped store (v1 schema plus the six co-work tables and triggers),
- install the WP-A1 store lookups the frozen lifecycle branch calls
  (``_get_proposal_locked``), and
- seed throwaway document, proposal, span, expression, status, and doc-event
  rows so subject-resolution, redaction, integrity, and profile tests can run.

Every seeded row is labeled throwaway test data per the live-test data rule.
"""

from __future__ import annotations

import sqlite3
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from work_buddy.truth.identity import new_id, sha256_text, utc_now
from work_buddy.truth.migrations import REDACTED_SELECTOR_JSON
from work_buddy.truth.store import TruthStore


# --- Frozen v2 DDL, transcribed verbatim from C1-kernel-contracts.md section 1.

_TABLES: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS documents (
        id                    TEXT PRIMARY KEY,
        path                  TEXT NOT NULL,
        title                 TEXT,
        document_class        TEXT NOT NULL,
        content_sha256        TEXT NOT NULL,
        ydoc_snapshot_sha256  TEXT,
        created_at            TEXT NOT NULL,
        created_by_kind       TEXT NOT NULL,
        created_by_ref        TEXT,
        meta_json             TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS document_spans (
        id               TEXT PRIMARY KEY,
        document_id      TEXT NOT NULL REFERENCES documents(id),
        selector_json    TEXT NOT NULL,
        quote_exact      TEXT,
        span_sha256      TEXT NOT NULL,
        author_kind      TEXT,
        author_ref       TEXT,
        created_at       TEXT NOT NULL,
        created_by_kind  TEXT NOT NULL,
        created_by_ref   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS expressions (
        id                    TEXT PRIMARY KEY,
        document_span_id      TEXT NOT NULL REFERENCES document_spans(id),
        claim_ref_kind        TEXT NOT NULL,
        claim_ref             TEXT NOT NULL,
        role                  TEXT NOT NULL,
        claim_canonical_sha256 TEXT NOT NULL,
        span_sha256           TEXT NOT NULL,
        created_at            TEXT NOT NULL,
        created_by_kind       TEXT NOT NULL,
        created_by_ref        TEXT,
        meta_json             TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proposals (
        id                  TEXT PRIMARY KEY,
        document_id         TEXT NOT NULL REFERENCES documents(id),
        base_content_sha256 TEXT NOT NULL,
        selector_json       TEXT NOT NULL,
        -- FROZEN-DDL CONTRADICTION (flag for WP-A1 / orchestrator): the
        -- contract declares quote_exact TEXT NOT NULL (line 81), but the
        -- proposals redaction carve-out trigger (line 193), the export
        -- redaction-shape check (line 336), and redact.py all require
        -- NEW.quote_exact IS NULL on redaction. A NOT NULL column cannot be
        -- nulled, so the three redaction requirements force this column to be
        -- nullable. This scaffolding applies the internally consistent shape
        -- so the redaction substrate can be tested. WP-A1 must drop NOT NULL
        -- from quote_exact in _m002 (or the redaction design must change).
        quote_exact         TEXT,
        span_sha256         TEXT NOT NULL,
        replacement         TEXT,
        rationale           TEXT,
        tldr                TEXT,
        claim_refs_json     TEXT,
        canonical_sha256    TEXT NOT NULL,
        dedup_key           TEXT NOT NULL,
        expires_at          TEXT,
        created_at          TEXT NOT NULL,
        created_by_kind     TEXT NOT NULL,
        created_by_ref      TEXT,
        meta_json           TEXT,
        redacted_at         TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proposal_status_events (
        seq          INTEGER PRIMARY KEY AUTOINCREMENT,
        id           TEXT NOT NULL UNIQUE,
        proposal_id  TEXT NOT NULL REFERENCES proposals(id),
        status       TEXT NOT NULL,
        decision     TEXT,
        at           TEXT NOT NULL,
        actor_kind   TEXT NOT NULL,
        actor_ref    TEXT,
        basis_kind   TEXT NOT NULL,
        basis_ref    TEXT,
        note         TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS doc_events (
        id                    TEXT PRIMARY KEY,
        document_id           TEXT NOT NULL REFERENCES documents(id),
        kind                  TEXT NOT NULL,
        at                    TEXT NOT NULL,
        actor_kind            TEXT NOT NULL,
        actor_ref             TEXT,
        content_sha256        TEXT,
        ydoc_snapshot_sha256  TEXT,
        detail                TEXT
    )
    """,
)

_INDEXES: tuple[str, ...] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_path ON documents(path)",
    "CREATE INDEX IF NOT EXISTS idx_documents_ydoc_snapshot "
    "ON documents(ydoc_snapshot_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_document_spans_document "
    "ON document_spans(document_id)",
    "CREATE INDEX IF NOT EXISTS idx_expressions_document_span "
    "ON expressions(document_span_id)",
    "CREATE INDEX IF NOT EXISTS idx_expressions_claim_ref "
    "ON expressions(claim_ref)",
    "CREATE INDEX IF NOT EXISTS idx_proposals_document ON proposals(document_id)",
    "CREATE INDEX IF NOT EXISTS idx_proposals_dedup "
    "ON proposals(document_id, dedup_key)",
    "CREATE INDEX IF NOT EXISTS idx_proposals_canonical "
    "ON proposals(canonical_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_proposal_status_proposal_seq "
    "ON proposal_status_events(proposal_id, seq DESC)",
    "CREATE INDEX IF NOT EXISTS idx_doc_events_document ON doc_events(document_id)",
)

_APPEND_ONLY_UPDATE = (
    "document_spans",
    "expressions",
    "proposal_status_events",
    "doc_events",
)
_APPEND_ONLY_DELETE = (
    "documents",
    "document_spans",
    "expressions",
    "proposals",
    "proposal_status_events",
    "doc_events",
)

_DOCUMENTS_CARVE_OUT = """
CREATE TRIGGER IF NOT EXISTS documents_append_only_update
BEFORE UPDATE ON documents
WHEN NOT (
    NEW.id IS OLD.id
    AND NEW.path IS OLD.path
    AND NEW.title IS OLD.title
    AND NEW.document_class IS OLD.document_class
    AND NEW.created_at IS OLD.created_at
    AND NEW.created_by_kind IS OLD.created_by_kind
    AND NEW.created_by_ref IS OLD.created_by_ref
    AND NEW.meta_json IS OLD.meta_json
)
BEGIN SELECT RAISE(ABORT, 'append-only'); END
"""

_PROPOSALS_CARVE_OUT = f"""
CREATE TRIGGER IF NOT EXISTS proposals_append_only_update
BEFORE UPDATE ON proposals
WHEN NOT (
    OLD.redacted_at IS NULL
    AND NEW.redacted_at IS NOT NULL
    AND NEW.quote_exact IS NULL
    AND NEW.replacement IS NULL
    AND NEW.rationale IS NULL
    AND NEW.tldr IS NULL
    AND NEW.claim_refs_json IS NULL
    AND NEW.selector_json = '{REDACTED_SELECTOR_JSON}'
    AND NEW.id IS OLD.id
    AND NEW.document_id IS OLD.document_id
    AND NEW.base_content_sha256 IS OLD.base_content_sha256
    AND NEW.span_sha256 IS OLD.span_sha256
    AND NEW.canonical_sha256 IS OLD.canonical_sha256
    AND NEW.dedup_key IS OLD.dedup_key
    AND NEW.expires_at IS OLD.expires_at
    AND NEW.created_at IS OLD.created_at
    AND NEW.created_by_kind IS OLD.created_by_kind
    AND NEW.created_by_ref IS OLD.created_by_ref
    AND NEW.meta_json IS OLD.meta_json
)
BEGIN SELECT RAISE(ABORT, 'append-only'); END
"""

_GESTURES_RECREATE = """
CREATE TRIGGER IF NOT EXISTS gestures_append_only_update
BEFORE UPDATE ON gestures
WHEN NOT (
    NEW.id IS OLD.id
    AND NEW.at IS OLD.at
    AND NEW.surface IS OLD.surface
    AND NEW.actor_ref IS OLD.actor_ref
    AND NEW.kind IS OLD.kind
    AND NEW.subject_ref IS OLD.subject_ref
    AND NEW.payload_sha256 IS OLD.payload_sha256
    AND NEW.context_sha256 IS OLD.context_sha256
    AND NEW.expires_at IS OLD.expires_at
    AND (
        (
            OLD.consumed_at IS NULL
            AND NEW.consumed_at IS NOT NULL
            AND NEW.payload_excerpt IS OLD.payload_excerpt
        )
        OR (
            NEW.consumed_at IS OLD.consumed_at
            AND OLD.payload_excerpt <> '[redacted]'
            AND NEW.payload_excerpt = '[redacted]'
            AND (
                EXISTS (
                    SELECT 1 FROM claims
                    WHERE id = OLD.subject_ref
                    AND redacted_at IS NOT NULL
                )
                OR EXISTS (
                    SELECT 1 FROM evidence
                    WHERE id = OLD.subject_ref
                    AND redacted_at IS NOT NULL
                )
                OR EXISTS (
                    SELECT 1 FROM evidence_spans
                    WHERE id = OLD.subject_ref
                    AND redacted_at IS NOT NULL
                )
                OR EXISTS (
                    SELECT 1 FROM proposals
                    WHERE id = OLD.subject_ref
                    AND redacted_at IS NOT NULL
                )
            )
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END
"""


def frozen_v2_statements() -> tuple[str, ...]:
    """Return every frozen v2 DDL statement in application order."""
    statements: list[str] = []
    statements.extend(_TABLES)
    statements.extend(_INDEXES)
    for table in _APPEND_ONLY_UPDATE:
        statements.append(
            f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_update "
            f"BEFORE UPDATE ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'append-only'); END"
        )
    for table in _APPEND_ONLY_DELETE:
        statements.append(
            f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_delete "
            f"BEFORE DELETE ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'append-only'); END"
        )
    statements.append(_DOCUMENTS_CARVE_OUT)
    statements.append(_PROPOSALS_CARVE_OUT)
    statements.append("DROP TRIGGER IF EXISTS gestures_append_only_update")
    statements.append(_GESTURES_RECREATE)
    return tuple(statements)


def apply_frozen_v2_ddl(store: TruthStore) -> None:
    """Apply the frozen v2 DDL to an already-created v1 store."""
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in frozen_v2_statements():
            conn.execute(statement)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# --- Store-seam stand-in the frozen lifecycle branch calls (WP-A1 owns the
#     real one). Only the columns lifecycle subject-resolution reads matter,
#     but the full row is carried so tests can inspect it.


@dataclass(frozen=True)
class FrozenProposalRow:
    """A minimal stand-in for WP-A1's ProposalRecord."""

    id: str
    document_id: str
    base_content_sha256: str
    selector_json: str
    quote_exact: str | None
    span_sha256: str
    replacement: str | None
    rationale: str | None
    tldr: str | None
    claim_refs_json: str | None
    canonical_sha256: str
    dedup_key: str
    expires_at: str | None
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    meta_json: str | None
    redacted_at: str | None


def _get_proposal_locked(
    self: TruthStore,
    conn: sqlite3.Connection,
    proposal_id: str,
) -> FrozenProposalRow | None:
    row = conn.execute(
        "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
    ).fetchone()
    return None if row is None else FrozenProposalRow(**dict(row))


def install_wave1_store_shims(store: TruthStore) -> None:
    """Attach the WP-A1 store lookups the frozen lifecycle branch calls."""
    store._get_proposal_locked = types.MethodType(_get_proposal_locked, store)


def create_document_store(
    root: str | Path,
    *,
    profile_name: str = "cothink-doc",
    store_id: str | None = None,
    document_surface: dict[str, Any] | None = None,
    rejected_content: str = "redact",
    proposal_max_age: str | None = "2h",
) -> TruthStore:
    """Create a v1 store, apply the frozen v2 DDL, and install the shims.

    The store keeps ``export_committed`` false so no post-commit export runs:
    the v3 export path that would serialize document rows is WP-A4's build and
    is absent in this worktree.
    """
    surface = document_surface or {
        "enabled": True,
        "allowed_document_classes": ["co_authored", "generated"],
        "feedback_capture": True,
    }
    profile: dict[str, Any] = {
        "store_id": store_id or new_id(),
        "profile": profile_name,
        "title": "Co-work document test store",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": rejected_content,
            "confirmation_surfaces": ["dashboard", "cli"],
            "block_materialize_on_flags": False,
        },
        "document_surface": surface,
        "projection": "none",
        "export_committed": False,
    }
    if proposal_max_age is not None:
        profile["proposal_max_age"] = proposal_max_age
    store = TruthStore.create(root, profile)
    apply_frozen_v2_ddl(store)
    install_wave1_store_shims(store)
    return store


# --- Throwaway-row seeding helpers.


def _commit(store: TruthStore, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for sql, params in statements:
            conn.execute(sql, params)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def seed_document(
    store: TruthStore,
    *,
    document_id: str | None = None,
    path: str = "docs/design.md",
    title: str = "Throwaway design doc",
    document_class: str = "co_authored",
    content_sha256: str | None = None,
    ydoc_snapshot_sha256: str | None = None,
    actor_kind: str = "human",
    actor_ref: str | None = "user-1",
    at: str | None = None,
    meta_json: str | None = None,
    ledger: bool = True,
) -> str:
    """Insert one throwaway document row (and its ledger record)."""
    document_id = document_id or new_id()
    content_sha256 = content_sha256 or sha256_text(path)
    at = at or utc_now()
    statements: list[tuple[str, tuple[Any, ...]]] = [
        (
            "INSERT INTO documents (id, path, title, document_class, "
            "content_sha256, ydoc_snapshot_sha256, created_at, created_by_kind, "
            "created_by_ref, meta_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                document_id,
                path,
                title,
                document_class,
                content_sha256,
                ydoc_snapshot_sha256,
                at,
                actor_kind,
                actor_ref,
                meta_json,
            ),
        )
    ]
    if ledger:
        statements.append(
            (
                "INSERT INTO ledger_records (record_type, record_key) "
                "VALUES ('document', ?)",
                (document_id,),
            )
        )
    _commit(store, statements)
    return document_id


def seed_document_span(
    store: TruthStore,
    *,
    document_id: str,
    span_id: str | None = None,
    selector_json: str = '[{"exact":"anchor","prefix":"","suffix":"",'
    '"type":"TextQuoteSelector"}]',
    quote_exact: str | None = "anchor",
    span_sha256: str | None = None,
    author_kind: str | None = "human",
    author_ref: str | None = "user-1",
    at: str | None = None,
    ledger: bool = True,
) -> str:
    """Insert one throwaway document_span row."""
    span_id = span_id or new_id()
    span_sha256 = span_sha256 or sha256_text(selector_json)
    at = at or utc_now()
    statements: list[tuple[str, tuple[Any, ...]]] = [
        (
            "INSERT INTO document_spans (id, document_id, selector_json, "
            "quote_exact, span_sha256, author_kind, author_ref, created_at, "
            "created_by_kind, created_by_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                span_id,
                document_id,
                selector_json,
                quote_exact,
                span_sha256,
                author_kind,
                author_ref,
                at,
                "human",
                author_ref,
            ),
        )
    ]
    if ledger:
        statements.append(
            (
                "INSERT INTO ledger_records (record_type, record_key) "
                "VALUES ('document_span', ?)",
                (span_id,),
            )
        )
    _commit(store, statements)
    return span_id


def seed_expression(
    store: TruthStore,
    *,
    document_span_id: str,
    claim_ref: str,
    role: str = "instantiation",
    claim_ref_kind: str = "local",
    claim_canonical_sha256: str,
    span_sha256: str,
    expression_id: str | None = None,
    at: str | None = None,
    ledger: bool = True,
) -> str:
    """Insert one throwaway expression row."""
    expression_id = expression_id or new_id()
    at = at or utc_now()
    statements: list[tuple[str, tuple[Any, ...]]] = [
        (
            "INSERT INTO expressions (id, document_span_id, claim_ref_kind, "
            "claim_ref, role, claim_canonical_sha256, span_sha256, created_at, "
            "created_by_kind, created_by_ref, meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                expression_id,
                document_span_id,
                claim_ref_kind,
                claim_ref,
                role,
                claim_canonical_sha256,
                span_sha256,
                at,
                "human",
                "user-1",
                None,
            ),
        )
    ]
    if ledger:
        statements.append(
            (
                "INSERT INTO ledger_records (record_type, record_key) "
                "VALUES ('expression', ?)",
                (expression_id,),
            )
        )
    _commit(store, statements)
    return expression_id


def seed_proposal(
    store: TruthStore,
    *,
    document_id: str,
    proposal_id: str | None = None,
    base_content_sha256: str | None = None,
    selector_json: str = '[{"exact":"target","prefix":"","suffix":"",'
    '"type":"TextQuoteSelector"}]',
    quote_exact: str = "target",
    span_sha256: str | None = None,
    replacement: str | None = "target improved",
    rationale: str | None = None,
    tldr: str | None = None,
    claim_refs_json: str | None = None,
    canonical_sha256: str | None = None,
    dedup_key: str | None = None,
    expires_at: str | None = None,
    created_by_kind: str = "agent_run",
    created_by_ref: str | None = "run-1",
    meta_json: str | None = None,
    redacted_at: str | None = None,
    at: str | None = None,
    ledger: bool = True,
) -> str:
    """Insert one throwaway proposal row (edit when replacement is set)."""
    proposal_id = proposal_id or new_id()
    base_content_sha256 = base_content_sha256 or sha256_text("base")
    span_sha256 = span_sha256 or sha256_text(selector_json)
    canonical_sha256 = canonical_sha256 or sha256_text(
        f"{document_id}:{quote_exact}:{replacement}"
    )
    dedup_key = dedup_key or sha256_text(f"{document_id}:{quote_exact}")
    at = at or utc_now()
    statements: list[tuple[str, tuple[Any, ...]]] = [
        (
            "INSERT INTO proposals (id, document_id, base_content_sha256, "
            "selector_json, quote_exact, span_sha256, replacement, rationale, "
            "tldr, claim_refs_json, canonical_sha256, dedup_key, expires_at, "
            "created_at, created_by_kind, created_by_ref, meta_json, redacted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                proposal_id,
                document_id,
                base_content_sha256,
                selector_json,
                quote_exact,
                span_sha256,
                replacement,
                rationale,
                tldr,
                claim_refs_json,
                canonical_sha256,
                dedup_key,
                expires_at,
                at,
                created_by_kind,
                created_by_ref,
                meta_json,
                redacted_at,
            ),
        )
    ]
    if ledger:
        statements.append(
            (
                "INSERT INTO ledger_records (record_type, record_key) "
                "VALUES ('proposal', ?)",
                (proposal_id,),
            )
        )
    _commit(store, statements)
    return proposal_id


def seed_proposal_status_event(
    store: TruthStore,
    *,
    proposal_id: str,
    status: str,
    event_id: str | None = None,
    decision: str | None = None,
    actor_kind: str = "human",
    actor_ref: str | None = "user-1",
    basis_kind: str = "gesture",
    basis_ref: str | None = None,
    note: str | None = None,
    at: str | None = None,
    ledger: bool = True,
) -> str:
    """Insert one throwaway proposal_status_event row."""
    event_id = event_id or new_id()
    at = at or utc_now()
    statements: list[tuple[str, tuple[Any, ...]]] = [
        (
            "INSERT INTO proposal_status_events (id, proposal_id, status, "
            "decision, at, actor_kind, actor_ref, basis_kind, basis_ref, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                proposal_id,
                status,
                decision,
                at,
                actor_kind,
                actor_ref,
                basis_kind,
                basis_ref,
                note,
            ),
        )
    ]
    if ledger:
        statements.append(
            (
                "INSERT INTO ledger_records (record_type, record_key) "
                "VALUES ('proposal_status_event', ?)",
                (event_id,),
            )
        )
    _commit(store, statements)
    return event_id


def seed_doc_event(
    store: TruthStore,
    *,
    document_id: str,
    kind: str = "registered",
    event_id: str | None = None,
    actor_kind: str = "human",
    actor_ref: str | None = "user-1",
    content_sha256: str | None = None,
    ydoc_snapshot_sha256: str | None = None,
    detail: str | None = None,
    at: str | None = None,
    ledger: bool = True,
) -> str:
    """Insert one throwaway doc_event row."""
    event_id = event_id or new_id()
    at = at or utc_now()
    statements: list[tuple[str, tuple[Any, ...]]] = [
        (
            "INSERT INTO doc_events (id, document_id, kind, at, actor_kind, "
            "actor_ref, content_sha256, ydoc_snapshot_sha256, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                document_id,
                kind,
                at,
                actor_kind,
                actor_ref,
                content_sha256,
                ydoc_snapshot_sha256,
                detail,
            ),
        )
    ]
    if ledger:
        statements.append(
            (
                "INSERT INTO ledger_records (record_type, record_key) "
                "VALUES ('doc_event', ?)",
                (event_id,),
            )
        )
    _commit(store, statements)
    return event_id

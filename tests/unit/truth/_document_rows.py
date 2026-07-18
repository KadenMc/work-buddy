"""Raw-row seeding helpers and the document-store factory for kernel tests.

Two jobs live here. The first is a standard document-store factory
(``create_document_store``) that builds a real v2 truth store through the engine
and hands it back ready for co-work kernel tests. The second is a set of
raw-row seeding utilities (``seed_document`` and friends) that write document,
span, expression, proposal, status, and doc-event rows directly.

The seeding utilities exist because trigger and integrity tests need rows the
engine refuses to create: dangling references, redacted-but-content-retained
proposals, canonical mismatches, status-basis violations, and stale bases. The
engine never writes those shapes, so the integrity sweep has nothing to detect
without a way to plant them. Every seeded row is labeled throwaway test data
per the live-test data rule.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from work_buddy.truth.identity import new_id, sha256_text, utc_now
from work_buddy.truth.proposals import proposal_canonical_sha256
from work_buddy.truth.store import TruthStore


# The six co-work base tables the v2 migration (_m002) creates.
_DOCUMENT_SURFACE_TABLES: tuple[str, ...] = (
    "documents",
    "document_spans",
    "expressions",
    "proposals",
    "proposal_status_events",
    "doc_events",
)


def create_document_store(
    root: str | Path,
    *,
    profile_name: str = "cothink-doc",
    store_id: str | None = None,
    document_surface: dict[str, Any] | None = None,
    rejected_content: str = "redact",
    proposal_max_age: str | None = "2h",
) -> TruthStore:
    """Create a real v2 document store through the engine.

    The engine runs the _m002 migration, so the six co-work base tables exist
    on the returned store. The factory verifies their presence before handing
    the store back.
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
        "export_committed": True,
    }
    if proposal_max_age is not None:
        profile["proposal_max_age"] = proposal_max_age
    store = TruthStore.create(root, profile)
    conn = store.connect()
    try:
        present = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        conn.close()
    missing = [name for name in _DOCUMENT_SURFACE_TABLES if name not in present]
    assert not missing, f"document surface tables missing from the store: {missing}"
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
    """Insert one throwaway proposal row (edit when replacement is set).

    The default canonical_sha256 is the engine hash of the seeded fields, so a
    raw-seeded proposal is canonical-consistent against the live integrity
    recompute. Pass an explicit canonical_sha256 to seed a deliberate mismatch.
    """
    proposal_id = proposal_id or new_id()
    base_content_sha256 = base_content_sha256 or sha256_text("base")
    span_sha256 = span_sha256 or sha256_text(selector_json)
    if canonical_sha256 is None:
        canonical_sha256 = proposal_canonical_sha256(
            document_id=document_id,
            base_content_sha256=base_content_sha256,
            selector=json.loads(selector_json),
            quote_exact=quote_exact,
            replacement=replacement,
            rationale=rationale,
            tldr=tldr,
            claim_refs=json.loads(claim_refs_json) if claim_refs_json else None,
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

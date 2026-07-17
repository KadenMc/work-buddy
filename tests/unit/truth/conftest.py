"""Shared isolated fixtures for truth-kernel unit tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from work_buddy.truth.contracts import Actor, StorePaths
from work_buddy.truth.identity import new_id, sha256_bytes


# ---------------------------------------------------------------------------
# Wave-1 parallelism stub. WP-A2 owns these proposal allowed-kind sets in
# lifecycle.py. They are injected here so WP-A3's proposal engine can be
# exercised in isolation until the join. The hasattr guard means A2's real
# constants win once landed, so this stub disappears at integration.
# ---------------------------------------------------------------------------
import work_buddy.truth.lifecycle as _lifecycle_module  # noqa: E402

if not hasattr(_lifecycle_module, "PROPOSAL_ACCEPT_KINDS"):
    _lifecycle_module.PROPOSAL_ACCEPT_KINDS = frozenset({"confirm", "edit_confirm"})
    _lifecycle_module.PROPOSAL_REJECT_KINDS = _lifecycle_module.REJECTION_CLASSES
    _lifecycle_module.PROPOSAL_ROUTING_KINDS = frozenset(
        {"redirect", "defer", "endorse"}
    )


NOW = "2026-07-17T12:00:00.000+00:00"
LATER = "2026-07-17T12:05:00.000+00:00"
HUMAN = Actor("human", "reviewer-kaden")
SYSTEM = Actor("system", "truth-cowork-test")
AGENT = Actor(
    "agent_run",
    "cowork-agent-run",
    {
        "model": "test-model",
        "harness": "pytest",
        "surface": "cowork",
        "session_id": "session-1",
        "call_id": "call-1",
    },
)


@pytest.fixture
def truth_root(tmp_path: Path) -> Path:
    """Return an isolated scope root for one truth store."""
    root = tmp_path / "scope"
    root.mkdir()
    return root


@pytest.fixture
def profile_writer() -> Callable[..., Path]:
    """Write a minimal store profile without importing the engine."""

    def _write(
        root: Path,
        *,
        profile: str = "test",
        store_id: str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> Path:
        from work_buddy.truth.identity import new_id

        sidecar = root / ".wb-truth"
        sidecar.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "store_id": store_id or new_id(),
            "profile": profile,
            "title": "Test truth store",
            "allowed_claim_kinds": ["fact", "preference"],
            "required_fields": {},
            "gate": {
                "rejected_content": "redact",
                "confirmation_surfaces": ["dashboard", "cli", "chat_consent"],
                "block_materialize_on_flags": False,
            },
            "projection": "none",
            "export_committed": True,
            "document_surface": {
                "enabled": True,
                "allowed_document_classes": ["co_authored", "generated"],
                "feedback_capture": True,
            },
        }
        if overrides:
            payload.update(overrides)
        path = sidecar / "store.yaml"
        path.write_text(
            yaml.safe_dump(payload, sort_keys=False),
            encoding="utf-8",
        )
        return path

    return _write


# ---------------------------------------------------------------------------
# Co-work document surface (K2) v2 DDL, applied test-only until WP-A1's
# _m002_document_surface migration lands. This is the FROZEN DDL text
# (C1-kernel-contracts.md section 1) minus the version bump: the six new base
# tables, their append-only triggers, the documents latest-pointer and
# proposals redaction carve-outs, and the indexes. The gestures-trigger
# recreation is WP-A1's and is not exercised by WP-A3, so it is not applied here.
# ---------------------------------------------------------------------------

_DOCUMENT_SURFACE_TABLES = (
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
        -- DEVIATION (frozen DDL self-contradiction): the frozen proposals DDL
        -- declares quote_exact NOT NULL, but the frozen redaction carve-out
        -- trigger, the redact.py proposal branch, and the export v3 redaction
        -- validation all require NEW.quote_exact IS NULL on rejection, matching
        -- the cited nullable evidence_spans.quote_exact precedent. NOT NULL
        -- cannot hold with a redaction that nulls the field. This test-only DDL
        -- applies quote_exact as nullable so the frozen redaction semantics run.
        -- WP-A1's production _m002 must drop NOT NULL on proposals.quote_exact.
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

_DOCUMENT_SURFACE_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_path ON documents(path)",
    "CREATE INDEX IF NOT EXISTS idx_documents_ydoc_snapshot "
    "ON documents(ydoc_snapshot_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_document_spans_document "
    "ON document_spans(document_id)",
    "CREATE INDEX IF NOT EXISTS idx_expressions_document_span "
    "ON expressions(document_span_id)",
    "CREATE INDEX IF NOT EXISTS idx_expressions_claim_ref ON expressions(claim_ref)",
    "CREATE INDEX IF NOT EXISTS idx_proposals_document ON proposals(document_id)",
    "CREATE INDEX IF NOT EXISTS idx_proposals_dedup "
    "ON proposals(document_id, dedup_key)",
    "CREATE INDEX IF NOT EXISTS idx_proposals_canonical ON proposals(canonical_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_proposal_status_proposal_seq "
    "ON proposal_status_events(proposal_id, seq DESC)",
    "CREATE INDEX IF NOT EXISTS idx_doc_events_document ON doc_events(document_id)",
)

_APPEND_ONLY_UPDATE_TABLES = (
    "document_spans",
    "expressions",
    "proposal_status_events",
    "doc_events",
)
_APPEND_ONLY_DELETE_TABLES = (
    "documents",
    "document_spans",
    "expressions",
    "proposals",
    "proposal_status_events",
    "doc_events",
)


def _document_surface_carveout_triggers() -> tuple[str, ...]:
    from work_buddy.truth.migrations import REDACTED_SELECTOR_JSON

    documents_trigger = """
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
    proposals_trigger = f"""
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
    return documents_trigger, proposals_trigger


def apply_document_surface_schema(db_path: str | Path) -> None:
    """Apply the frozen v2 document-surface DDL to an existing truth store.

    Test-only stand-in for WP-A1's _m002_document_surface migration. No
    user_version or store_info bump, so the version machinery still reports v1
    while the physical document tables exist for WP-A3's engine.
    """
    conn = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _DOCUMENT_SURFACE_TABLES:
            conn.execute(statement)
        for statement in _DOCUMENT_SURFACE_INDEXES:
            conn.execute(statement)
        for table in _APPEND_ONLY_UPDATE_TABLES:
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_update "
                f"BEFORE UPDATE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'append-only'); END"
            )
        for table in _APPEND_ONLY_DELETE_TABLES:
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_delete "
                f"BEFORE DELETE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'append-only'); END"
            )
        for trigger in _document_surface_carveout_triggers():
            conn.execute(trigger)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _document_profile(store_id: str | None = None) -> dict[str, Any]:
    return {
        "store_id": store_id or new_id(),
        "profile": "cothink-doc",
        "title": "Co-work document store",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": "redact",
            "confirmation_surfaces": ["dashboard", "cli", "chat_consent"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        # export_committed is False because the v2 export module cannot serialize
        # the v3 document ledger records until WP-A1 lands. The document-workload
        # export round-trip is a skip-until-join assertion.
        "export_committed": False,
        "document_surface": {
            "enabled": True,
            "allowed_document_classes": ["co_authored", "generated"],
            "feedback_capture": True,
        },
    }


@pytest.fixture
def document_store(truth_root: Path) -> tuple[Any, StorePaths]:
    """A created v2 store with the document_surface profile enabled.

    Returns (store, StorePaths). The engine reports SCHEMA_VERSION 1 in this
    worktree (WP-A1 owns the bump to 2); the frozen v2 DDL is applied test-only.
    """
    from work_buddy.truth.store import TruthStore

    store = TruthStore.create(truth_root, _document_profile())
    apply_document_surface_schema(store.paths.db)
    return store, store.paths


@pytest.fixture
def register_document() -> Callable[..., tuple[str, str, str]]:
    """Register a throwaway document from an in-memory .md body.

    Returns (document_id, content_sha256, ydoc_snapshot_sha256). Labeled
    throwaway per the live-test data rule.
    """

    def _register(
        store: Any,
        *,
        path: str = "docs/throwaway-fixture.md",
        body: str = "# Throwaway fixture\n\nOriginal sentence for co-work tests.\n",
        document_class: str = "co_authored",
        actor: Actor = HUMAN,
        at: str = NOW,
    ) -> tuple[str, str, str]:
        from work_buddy.truth import documents, ydoc_store

        content_sha256 = sha256_bytes(body.encode("utf-8"))
        snapshot_bytes = b"YDOC-THROWAWAY-SNAPSHOT:" + content_sha256.encode("ascii")
        ydoc_snapshot_sha256 = ydoc_store.write_snapshot(
            store, snapshot=snapshot_bytes
        )
        record = documents.register_document(
            store,
            path=path,
            title="Throwaway fixture",
            document_class=document_class,
            content_sha256=content_sha256,
            ydoc_snapshot_sha256=ydoc_snapshot_sha256,
            actor=actor,
            at=at,
        )
        return record.id, content_sha256, ydoc_snapshot_sha256

    return _register


@pytest.fixture
def mint_proposal_gesture() -> Callable[..., Any]:
    """Mint a per-item gesture bound to a proposal's canonical_sha256.

    Wave-1 stub: minting the real gesture goes through WP-A2's mint_gesture
    proposal subject branch (not in this worktree) via WP-A4's HTTP surface.
    Here the gesture row is inserted directly with a real (non-constant) actor
    ref, on surface dashboard, so WP-A3's decision engine can verify+consume it.
    """

    def _mint(
        store: Any,
        proposal: Any,
        *,
        kind: str,
        actor: Actor = HUMAN,
        surface: str = "dashboard",
        at: str = NOW,
        context_sha256: str | None = None,
        expires_at: str | None = None,
        gesture_id: str | None = None,
    ) -> Any:
        from work_buddy.truth.store import GestureRecord

        excerpt = (proposal.quote_exact or "") + " -> " + (
            proposal.replacement or "[flag] " + (proposal.rationale or "")
        )
        record = GestureRecord(
            id=gesture_id or new_id(),
            at=at,
            surface=surface,
            actor_ref=actor.ref,
            kind=kind,
            subject_ref=proposal.id,
            payload_sha256=proposal.canonical_sha256,
            payload_excerpt=" ".join(excerpt.split())[:240],
            context_sha256=context_sha256,
            expires_at=expires_at,
            consumed_at=None,
        )
        with store.write_transaction() as conn:
            return store._insert_gesture_locked(conn, record)

    return _mint

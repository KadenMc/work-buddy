"""Shared fixtures for the co-work HTTP surface tests.

Every fixture uses a temporary, isolated Truth registry and a real v2 store with
the document_surface profile enabled, so the routes resolve stores by id exactly
as they do in production. All document bodies are labeled throwaway per the
live-test data rule.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from work_buddy.truth import documents, proposals, ydoc_store
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor
from work_buddy.truth.identity import new_id, sha256_bytes
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.store import TruthStore

NOW = "2026-07-17T12:00:00.000+00:00"
USER_REF = "reviewer-kaden"
HUMAN = Actor("human", USER_REF)
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

DOC_REL = "docs/throwaway-fixture.md"
DOC_BODY = "# Throwaway fixture\n\nOriginal sentence for co-work tests.\n"
DOC_QUOTE = "Original sentence for co-work tests."


def _profile(store_id: str | None = None) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "store_id": store_id or new_id(),
        "profile": "cowork-doc-test",
        "title": "Co-work document test store",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": "retain",
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
    return profile


@pytest.fixture
def store_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A registered real v2 document store with the routes' registry redirected."""
    from work_buddy.cowork import api

    registry = TruthStoreRegistry(tmp_path / "truth-registry.db")
    monkeypatch.setattr(api, "_registry", lambda: registry)
    root = tmp_path / "scope"
    root.mkdir()
    store = TruthStore.create(root, _profile())
    registry.register(store)
    return {
        "registry": registry,
        "store": store,
        "store_id": store.store_id,
        "root": root,
    }


@pytest.fixture
def client(store_ctx: dict[str, Any]):
    """A Flask test client with only the co-work blueprint mounted."""
    from flask import Flask

    from work_buddy.cowork import api

    app = Flask(__name__)
    app.config.update(TESTING=True)
    api.register_routes(app)
    return app.test_client()


def write_doc_file(root: Path, *, rel: str = DOC_REL, body: str = DOC_BODY) -> str:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body.encode("utf-8"))
    return sha256_bytes(body.encode("utf-8"))


@pytest.fixture
def seeded(store_ctx: dict[str, Any]) -> dict[str, Any]:
    """A store with one registered document backed by an on-disk file."""
    store = store_ctx["store"]
    content_sha256 = write_doc_file(store_ctx["root"])
    snapshot_bytes = b"YDOC-THROWAWAY-SNAPSHOT:" + content_sha256.encode("ascii")
    snapshot_sha256 = ydoc_store.write_snapshot(store, snapshot=snapshot_bytes)
    record = documents.register_document(
        store,
        path=DOC_REL,
        title="Throwaway fixture",
        document_class="co_authored",
        content_sha256=content_sha256,
        ydoc_snapshot_sha256=snapshot_sha256,
        actor=HUMAN,
        at=NOW,
    )
    return {
        **store_ctx,
        "document": record,
        "content_sha256": content_sha256,
        "snapshot_sha256": snapshot_sha256,
        "snapshot_bytes": snapshot_bytes,
        "rel": DOC_REL,
    }


@pytest.fixture
def make_proposal(seeded: dict[str, Any]) -> Callable[..., Any]:
    """Author an agent edit proposal against the seeded document."""

    def _make(
        *,
        quote: str = DOC_QUOTE,
        replacement: str | None = "Revised sentence for co-work tests.",
        rationale: str = "Clarity.",
        tldr: str = "Tighten the sentence.",
        claim_refs: list[Any] | None = None,
        base_content_sha256: str | None = None,
        at: str = NOW,
    ) -> Any:
        store = seeded["store"]
        document = seeded["document"]
        selector = CompositeSelector(exact=quote)
        return proposals.propose_edit(
            store,
            document_id=document.id,
            base_content_sha256=base_content_sha256 or document.content_sha256,
            selector=selector,
            quote_exact=quote,
            replacement=replacement,
            rationale=rationale,
            tldr=tldr,
            claim_refs=claim_refs,
            actor=AGENT,
            at=at,
        )

    return _make


def gesture_actor_ref(store: TruthStore, gesture_id: str) -> str | None:
    with store.connect() as conn:
        row = conn.execute(
            "SELECT actor_ref FROM gestures WHERE id = ?", (gesture_id,)
        ).fetchone()
    return None if row is None else row["actor_ref"]


def gesture_count(store: TruthStore) -> int:
    with store.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM gestures").fetchone()[0])

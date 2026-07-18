"""Shared isolated fixtures for truth-kernel unit tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from work_buddy.truth.contracts import Actor, StorePaths
from work_buddy.truth.identity import new_id, sha256_bytes


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
        "export_committed": True,
        "document_surface": {
            "enabled": True,
            "allowed_document_classes": ["co_authored", "generated"],
            "feedback_capture": True,
        },
    }


@pytest.fixture
def document_store(truth_root: Path) -> tuple[Any, StorePaths]:
    """A real v2 store with the document_surface profile enabled.

    Returns (store, StorePaths). The engine runs the _m002 migration, so the
    store reports SCHEMA_VERSION 2 with the six co-work tables present.
    """
    from work_buddy.truth.store import TruthStore

    store = TruthStore.create(truth_root, _document_profile())
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

    Routes through the real lifecycle mint path with the proposal as subject,
    on surface dashboard with a real (non-constant) actor ref, so the decision
    engine can verify and consume it.
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
        from work_buddy.truth.lifecycle import TruthLifecycle

        return TruthLifecycle(store).mint_gesture(
            subject_ref=proposal.id,
            actor=actor,
            surface=surface,
            kind=kind,
            displayed_payload_sha256=proposal.canonical_sha256,
            context_sha256=context_sha256,
            expires_at=expires_at,
            gesture_id=gesture_id,
            at=at,
        )

    return _mint

"""Shared fixtures for the co-work HTTP surface tests.

Every fixture uses a temporary, isolated Truth registry and a real v2 store with
the document_surface profile enabled, so the routes resolve stores by id exactly
as they do in production. All document bodies are labeled throwaway per the
live-test data rule.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from work_buddy.cowork import document_agent
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


@pytest.fixture(autouse=True)
def fake_document_agent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Never let a Co-work route test launch a real background model process.

    Route tests exercise the lifecycle boundary through this deterministic
    module seam. Tests that need a failure or stopped state may replace the
    seam again locally.
    """
    calls: list[dict[str, Any]] = []

    def _ensure(**kwargs: Any) -> document_agent.DocumentAgentStatus:
        calls.append(dict(kwargs))
        return document_agent.DocumentAgentStatus(
            status="running",
            alive=True,
            started=True,
            error=None,
        )

    monkeypatch.setattr(document_agent, "ensure_document_agent", _ensure)
    from work_buddy.agent_execution import registry as execution_registry
    from work_buddy.agent_execution.models import (
        AgentExecutionCatalog,
        AgentExecutionSelection,
        ModelDescriptor,
        ProviderAvailability,
        ProviderDescriptor,
        UnknownModelError,
        UnknownProviderError,
    )

    default = AgentExecutionSelection(
        provider_id="claude-code",
        model_id="sonnet",
        provider_label="Claude Code",
        model_label="Sonnet",
    )
    catalog = AgentExecutionCatalog(
        providers=(
            ProviderDescriptor(
                id="claude-code",
                label="Claude Code",
                availability=ProviderAvailability.READY,
                auth_mode="claude_account",
                models=(
                    ModelDescriptor(id="sonnet", label="Sonnet", is_default=True),
                    ModelDescriptor(id="opus", label="Opus"),
                ),
            ),
            ProviderDescriptor(
                id="codex",
                label="Codex",
                availability=ProviderAvailability.READY,
                auth_mode="chatgpt_account",
                models=(
                    ModelDescriptor(
                        id="gpt-5.6-sol",
                        label="GPT-5.6 Sol",
                        is_default=True,
                    ),
                ),
            ),
        ),
        default_selection=default,
    )

    def _validate(selection, *, refresh=False):
        del refresh
        for provider in catalog.providers:
            if provider.id != selection.provider_id:
                continue
            for model in provider.models:
                if model.id == selection.model_id:
                    return AgentExecutionSelection(
                        provider_id=provider.id,
                        model_id=model.id,
                        provider_label=provider.label,
                        model_label=model.label,
                    )
            raise UnknownModelError(
                f"Unknown model for {provider.label}: {selection.model_id}"
            )
        raise UnknownProviderError(
            f"Unknown agent execution provider: {selection.provider_id}"
        )

    monkeypatch.setattr(execution_registry, "default_selection", lambda: default)
    monkeypatch.setattr(
        execution_registry,
        "get_catalog",
        lambda **_kwargs: catalog,
    )
    monkeypatch.setattr(execution_registry, "validate_selection", _validate)
    monkeypatch.setattr(execution_registry, "get_providers", lambda **_kwargs: catalog.providers)
    return calls


@pytest.fixture(autouse=True)
def fake_cowork_human_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep domain route tests focused; authority attacks have their own suite."""

    from work_buddy.cowork import api, chat_api, verify_api

    authority = SimpleNamespace(
        principal=SimpleNamespace(
            actor=SimpleNamespace(canonical_id=USER_REF),
            assurance="enrolled_local_session",
        ),
        to_input_ingress=lambda: {
            "schema": "wb.conversation-message-ingress/v1",
            "inputter": {
                "schema": "wb.actor-ref/v1",
                "issuer_authority_id": "issuer-cowork-tests",
                "subject": "human-cowork-tests",
                "kind": "human",
                "tenant_scope_id": "tenant-cowork-tests",
            },
            "session_id_sha256": "1" * 64,
            "gesture_id": "gesture-cowork-tests",
            "action": "cowork.test.action",
            "subject_sha256": "2" * 64,
            "context_sha256": "3" * 64,
            "assurance": "enrolled_local_session_gesture",
            "basis": "authenticated_loopback_ui_gesture",
            "threat_model_limit": "single_local_os_user_not_proven",
        },
    )
    require = lambda **_kwargs: (authority, HUMAN)
    monkeypatch.setattr(api, "_require_human_action", require)
    monkeypatch.setattr(chat_api, "_require_human_action", require)
    monkeypatch.setattr(verify_api, "_require_human_action", require)
    monkeypatch.setattr(api, "authenticate_request_session", lambda **_kwargs: authority.principal)


@pytest.fixture(autouse=True)
def isolated_cowork_disclosure(tmp_path: Path):
    """Keep exact worker disclosures inside each route test's temp authority."""

    from work_buddy.agent_execution.disclosure import (
        DisclosureGateway,
        DisclosureManifestStore,
    )
    from work_buddy.cowork.worker_disclosure import (
        CoworkWorkerDisclosureBoundary,
        configure_cowork_worker_disclosure,
    )
    from work_buddy.sources import ActorRef, SourceStore
    from work_buddy.sources.disclosure import SourcesDisclosureService

    source_store = SourceStore.create(tmp_path / "worker-sources")
    issuer = ActorRef(
        source_store.authority_id,
        "cowork-disclosure-tests",
        "service",
        "tenant-cowork-tests",
    )
    sources = SourcesDisclosureService(
        source_store,
        tenant_scope_id=issuer.tenant_scope_id,
        issuer=issuer,
    )
    configure_cowork_worker_disclosure(
        CoworkWorkerDisclosureBoundary(
            DisclosureGateway(
                DisclosureManifestStore(tmp_path / "worker-disclosures.db"),
                sources,
            ),
            sources,
        )
    )
    try:
        yield
    finally:
        configure_cowork_worker_disclosure(None)


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
    """A registered real v2 document store with the routes' registry redirected.

    The house conversations store is redirected to a throwaway database too, so
    the feedback route and the redirect/endorse routing land in an isolated
    conversation log rather than the real one (live-test data rule).
    """
    from work_buddy.cowork import api, conversation_source_dependencies
    from work_buddy.conversations import store as conversations_store

    registry = TruthStoreRegistry(tmp_path / "truth-registry.db")
    monkeypatch.setattr(api, "_registry", lambda: registry)
    conversations_db = tmp_path / "throwaway-conversations.db"
    monkeypatch.setattr(conversations_store, "_DB_PATH", conversations_db)
    monkeypatch.setattr(
        conversation_source_dependencies,
        "_DB_PATH",
        tmp_path / "throwaway-conversation-dependencies.db",
    )
    conversations_conn = conversations_store.get_connection()
    try:
        conversations_store._ensure_schema(conversations_conn)
    finally:
        conversations_conn.close()
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

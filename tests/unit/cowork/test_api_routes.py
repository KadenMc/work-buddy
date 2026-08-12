"""Flask test-client coverage for every /api/truth/doc/* route (R1-R10).

The client mounts only the co-work blueprint against a temporary registry, so no
live port is bound and the routes resolve stores exactly as in production.
"""

from __future__ import annotations

import io
import json
import os
import struct
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from work_buddy.conversations import store as conversation_store
from work_buddy.conversations.execution import EXECUTION_METADATA_KEY
from work_buddy.cowork import api, transport
from work_buddy.cowork import conversations, document_agent, provenance
from work_buddy.truth import documents, expressions, proposals, ydoc_store
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor
from work_buddy.truth.identity import new_id, sha256_bytes, sha256_text, truth_uri
from work_buddy.truth.lifecycle import TruthLifecycle

from .conftest import (
    AGENT,
    DOC_QUOTE,
    DOC_REL,
    HUMAN,
    NOW,
    write_doc_file,
)


def _url(path: str, store_id: str) -> str:
    return f"{path}?store_id={store_id}"


def test_cowork_host_file_routes_reject_non_loopback_callers(client, store_ctx):
    remote = {"REMOTE_ADDR": "100.64.0.42"}

    folders = client.get(
        "/api/truth/cowork/folders",
        environ_overrides=remote,
    )
    documents = client.get(
        _url("/api/truth/doc/list", store_ctx["store_id"]),
        environ_overrides=remote,
    )
    current_actor = client.get(
        "/api/truth/cowork/current-actor",
        environ_overrides=remote,
    )

    for response in (folders, documents, current_actor):
        assert response.status_code == 403
        assert response.get_json() == {
            "ok": False,
            "error": {
                "code": "cowork_local_only",
                "message": (
                    "Co-work can access host files and is available only "
                    "from this machine."
                ),
                "retryable": False,
            },
        }


def test_cowork_host_file_routes_reject_loopback_reverse_proxies(client):
    scenarios = (
        {
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_HOST": "workstation.example.ts.net",
        },
        {
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_HOST": "localhost:5127",
            "HTTP_TAILSCALE_USER_LOGIN": "reviewer@example.com",
        },
        {
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_HOST": "localhost:5127",
            "HTTP_FORWARDED": "for=100.64.0.42",
        },
        {
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_HOST": "localhost:5127",
            "HTTP_VIA": "1.1 proxy.example",
        },
        {
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_HOST": "localhost:5127",
            "HTTP_X_FORWARDED_FOR": "100.64.0.42",
        },
        {
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_HOST": "localhost:5127",
            "HTTP_X_FORWARDED_HOST": "workstation.example.ts.net",
        },
        {
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_HOST": "localhost:5127",
            "HTTP_X_FORWARDED_PROTO": "https",
        },
        {
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_HOST": "localhost:5127",
            "HTTP_X_REAL_IP": "100.64.0.42",
        },
    )

    for environ in scenarios:
        response = client.get(
            "/api/truth/cowork/folders",
            environ_overrides=environ,
        )
        assert response.status_code == 403
        assert response.get_json()["error"]["code"] == "cowork_local_only"


def test_cowork_host_file_routes_allow_direct_loopback_host(client, store_ctx):
    response = client.get(
        _url("/api/truth/doc/list", store_ctx["store_id"]),
        environ_overrides={
            "REMOTE_ADDR": "::1",
            "HTTP_HOST": "[::1]:5127",
        },
    )

    assert response.status_code == 200


def _bootstrap_ready(client, store_ctx, *, path: str, key: str):
    source = b"# Two-phase route fixture\n\nOriginal body.\n"
    prepared_response = client.post(
        _url("/api/truth/doc/bootstrap", store_ctx["store_id"]),
        data={
            "metadata": json.dumps(
                {
                    "mode": "create",
                    "path": path,
                    "idempotency_key": key,
                }
            ),
            "source": (io.BytesIO(source), "source.md"),
        },
        content_type="multipart/form-data",
    )
    assert prepared_response.status_code == 201
    prepared = prepared_response.get_json()
    snapshot = b"YDOC:" + source
    committed = client.put(
        _url(
            f"/api/truth/doc/bootstrap/{prepared['bootstrap_id']}",
            store_ctx["store_id"],
        ),
        data=snapshot,
        content_type="application/octet-stream",
        headers={
            "X-WB-Source-Sha256": sha256_bytes(source),
            "X-WB-Snapshot-Sha256": sha256_bytes(snapshot),
            "X-WB-Ydoc-Schema": "cowork-yjs/v1",
        },
    )
    assert committed.status_code == 200
    return committed.get_json(), source


# --- R10 register ----------------------------------------------------------


def test_legacy_register_requires_two_phase_bootstrap_for_fresh_path(client, store_ctx):
    write_doc_file(store_ctx["root"])
    body = {"path": DOC_REL, "title": "Throwaway fixture", "profile": "co_authored"}
    response = client.post(
        _url("/api/truth/doc/register", store_ctx["store_id"]), json=body
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "bootstrap_required"
    with store_ctx["store"]._read_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_legacy_register_is_read_only_lookup_for_ready_path(client, seeded):
    resp = client.post(
        _url("/api/truth/doc/register", seeded["store_id"]),
        # Legacy callers may still send profile/title; lookup never uses them
        # to create or redefine the already-ready document.
        json={"path": DOC_REL, "title": "ignored", "profile": "bogus"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["document_id"] == seeded["document"].id
    assert payload["imported"] is False
    assert payload["readiness"]["initialization_state"] == "ready"


# --- R1 list / R2 get ------------------------------------------------------


def test_list_returns_registered_docs(client, seeded):
    resp = client.get(_url("/api/truth/doc/list", seeded["store_id"]))
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["count"] == 1
    entry = payload["docs"][0]
    assert entry["document_id"] == seeded["document"].id
    assert entry["profile"] == "co_authored"
    assert entry["drift_state"] == "clean"
    assert entry["last_materialized_sha256"] == seeded["content_sha256"]
    assert entry["import_source_sha256"] is None
    assert entry["observed_source_file_sha256"] == seeded["content_sha256"]


def test_get_returns_open_proposals_and_hashes(client, seeded, make_proposal):
    proposal = make_proposal()
    resp = client.get(
        _url(f"/api/truth/doc/{seeded['document'].id}", seeded["store_id"])
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["hashes"]["ydoc_snapshot_sha256"] == seeded["snapshot_sha256"]
    assert payload["import_source_sha256"] is None
    assert payload["observed_source_file_sha256"] == seeded["content_sha256"]
    assert payload["drift"]["state"] == "clean"
    assert len(payload["open_proposals"]) == 1
    entry = payload["open_proposals"][0]
    assert entry["proposal_id"] == proposal.id
    assert entry["canonical_sha256"] == proposal.canonical_sha256
    assert entry["base_ok"] is True
    assert entry["applicability"] == {
        "status": "applicable",
        "reason": "same_materialized_baseline",
        "current_structured_head_sha256": payload["structured_head_sha256"],
    }
    assert entry["quote_anchor"]["exact"] == DOC_QUOTE
    assert entry["kind"] == "edit"


def test_get_uses_live_structured_head_not_materialized_baseline_for_applicability(
    client, seeded
):
    store = seeded["store"]
    document = seeded["document"]
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    proposal = proposals.propose_edit(
        store,
        document_id=document.id,
        base_content_sha256=sha256_text("a newer browser projection"),
        base_structured_head_sha256=head,
        selector=CompositeSelector(exact=DOC_QUOTE),
        quote_exact=DOC_QUOTE,
        replacement="A clearer sentence.",
        actor=AGENT,
        at=NOW,
    )

    response = client.get(
        _url(f"/api/truth/doc/{document.id}", store.store_id)
    )

    assert response.status_code == 200
    entry = next(
        item
        for item in response.get_json()["open_proposals"]
        if item["proposal_id"] == proposal.id
    )
    assert entry["base_ok"] is True
    assert entry["applicability"]["status"] == "applicable"
    assert entry["applicability"]["reason"] == "same_structured_head"


def test_list_and_get_distinguish_recorded_import_source_from_observed_file(
    client,
    store_ctx,
):
    source_path = store_ctx["root"] / "drafts" / "detached-source.md"
    source_path.parent.mkdir(parents=True)
    original_source = b"# Original detached import\n"
    source_path.write_bytes(original_source)
    recorded_source_sha256 = sha256_bytes(original_source)
    snapshot = b"YDOC-DETACHED-IMPORT-SNAPSHOT"
    snapshot_sha256 = ydoc_store.write_snapshot(
        store_ctx["store"],
        snapshot=snapshot,
    )
    structured_head_sha256 = ydoc_store.structured_head_from_segments(
        snapshot,
        (),
    )
    document, _, _ = documents.register_ready_document(
        store_ctx["store"],
        path="drafts/detached-source.md",
        title="Detached source",
        document_class="co_authored",
        projection_bytes=original_source,
        ydoc_snapshot_sha256=snapshot_sha256,
        structured_head_sha256=structured_head_sha256,
        actor=HUMAN,
        mode="import",
        document_meta={
            "source": {
                "kind": "file_import",
                "writeback_policy": "never",
                "sha256": recorded_source_sha256,
                "importer_id": "markdown/v1",
                "media_type": "text/markdown",
            }
        },
        at=NOW,
    )
    changed_source = b"# The source changed after import\n"
    observed_source_sha256 = sha256_bytes(changed_source)
    source_path.write_bytes(changed_source)

    listed = client.get(_url("/api/truth/doc/list", store_ctx["store_id"]))
    fetched = client.get(
        _url(f"/api/truth/doc/{document.id}", store_ctx["store_id"])
    )

    assert listed.status_code == 200
    list_entry = listed.get_json()["docs"][0]
    assert list_entry["source_writeback"] == "never"
    assert list_entry["current_file_sha256"] == recorded_source_sha256
    assert list_entry["import_source_sha256"] == recorded_source_sha256
    assert list_entry["observed_source_file_sha256"] == observed_source_sha256

    assert fetched.status_code == 200
    fetched_payload = fetched.get_json()
    assert fetched_payload["source_writeback"] == "never"
    assert fetched_payload["import_source_sha256"] == recorded_source_sha256
    assert (
        fetched_payload["observed_source_file_sha256"]
        == observed_source_sha256
    )
    assert fetched_payload["hashes"]["current_file_sha256"] == recorded_source_sha256
    assert (
        fetched_payload["hashes"]["import_source_sha256"]
        == recorded_source_sha256
    )
    assert (
        fetched_payload["hashes"]["observed_source_file_sha256"]
        == observed_source_sha256
    )


def test_get_returns_empty_replacement_as_deletion_edit(
    client, seeded, make_proposal
):
    proposal = make_proposal(replacement="")

    resp = client.get(
        _url(f"/api/truth/doc/{seeded['document'].id}", seeded["store_id"])
    )

    assert resp.status_code == 200
    entry = resp.get_json()["open_proposals"][0]
    assert entry["proposal_id"] == proposal.id
    assert entry["kind"] == "edit"
    assert entry["replacement"] == ""


def test_get_reads_r2_ledger_projection_from_one_explicit_snapshot(
    client,
    seeded,
    monkeypatch,
):
    store = seeded["store"]
    claim = store.propose_claim(
        proposition="Snapshot transaction fixture.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    span = expressions.ensure_document_span(
        store,
        document_id=seeded["document"].id,
        selector=CompositeSelector(exact=DOC_QUOTE),
        quote_exact=DOC_QUOTE,
        actor=HUMAN,
        at=NOW,
    )
    expressions.mark_expression(
        store,
        document_span_id=span.id,
        claim_ref=claim.id,
        role="instantiation",
        actor=HUMAN,
        at=NOW,
    )
    connection_ids: set[int] = set()
    observed: set[str] = set()

    def record(name, conn):
        assert conn is not None
        assert conn.in_transaction is True
        connection_ids.add(id(conn))
        observed.add(name)

    original_get_document = api.documents.get_document
    original_open_proposals = api.proposals.open_proposals
    original_expressions = api.expressions.expressions_for_document
    original_claim_states = api.queries.resolve_claim_states
    original_lifecycle = api.documents.current_lifecycle
    original_readiness = api.readiness.classify_document
    original_events = type(seeded["store"])._document_events_locked

    def tracked_get_document(store, document_id, *, conn=None):
        record("document", conn)
        return original_get_document(store, document_id, conn=conn)

    def tracked_open_proposals(store, *, document_id, conn=None):
        record("proposals", conn)
        return original_open_proposals(
            store,
            document_id=document_id,
            conn=conn,
        )

    def tracked_expressions(store, document_id, *, conn=None):
        record("expressions", conn)
        return original_expressions(store, document_id, conn=conn)

    def tracked_claim_states(store, *, belief_at=None, conn=None):
        record("claim_states", conn)
        return original_claim_states(
            store,
            belief_at=belief_at,
            conn=conn,
        )

    def tracked_lifecycle(store, document_id, *, conn=None):
        record("lifecycle", conn)
        return original_lifecycle(store, document_id, conn=conn)

    def tracked_readiness(store, document, **kwargs):
        record("readiness", kwargs.get("conn"))
        return original_readiness(store, document, **kwargs)

    def tracked_events(store, conn, document_id):
        record("events", conn)
        return original_events(store, conn, document_id)

    monkeypatch.setattr(api.documents, "get_document", tracked_get_document)
    monkeypatch.setattr(api.proposals, "open_proposals", tracked_open_proposals)
    monkeypatch.setattr(
        api.expressions,
        "expressions_for_document",
        tracked_expressions,
    )
    monkeypatch.setattr(api.queries, "resolve_claim_states", tracked_claim_states)
    monkeypatch.setattr(api.documents, "current_lifecycle", tracked_lifecycle)
    monkeypatch.setattr(api.readiness, "classify_document", tracked_readiness)
    monkeypatch.setattr(
        type(seeded["store"]),
        "_document_events_locked",
        tracked_events,
    )

    response = client.get(
        _url(
            f"/api/truth/doc/{seeded['document'].id}",
            seeded["store_id"],
        )
    )

    assert response.status_code == 200
    assert observed == {
        "document",
        "proposals",
        "expressions",
        "claim_states",
        "lifecycle",
        "readiness",
        "events",
    }
    assert len(connection_ids) == 1


def test_get_projects_expression_claim_status_and_kind(client, seeded):
    store = seeded["store"]
    document = seeded["document"]
    lifecycle = TruthLifecycle(store)
    confirmed = store.propose_claim(
        proposition="The document contains a test sentence.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    needs_review = store.propose_claim(
        proposition="The test sentence should remain.",
        claim_kind="preference",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim

    for selector, claim_ref in (
        (
            CompositeSelector(
                exact=DOC_QUOTE,
                prefix="Original body: ",
                suffix=" End.",
            ),
            confirmed.id,
        ),
        (
            CompositeSelector(
                exact="Throwaway fixture",
                prefix="Title: ",
                suffix=".",
            ),
            truth_uri(store.store_id, "claim", needs_review.id),
        ),
    ):
        span = expressions.ensure_document_span(
            store,
            document_id=document.id,
            selector=selector,
            quote_exact=selector.exact,
            actor=HUMAN,
            at=NOW,
        )
        expressions.mark_expression(
            store,
            document_span_id=span.id,
            claim_ref=claim_ref,
            role="instantiation",
            actor=HUMAN,
            at=NOW,
        )

    for claim in (confirmed, needs_review):
        gesture = lifecycle.mint_gesture(
            subject_ref=claim.id,
            actor=HUMAN,
            surface="dashboard",
            kind="confirm",
            displayed_payload_sha256=claim.canonical_sha256,
            at="2026-07-17T12:01:00.000+00:00",
        )
        lifecycle.confirm_claim(
            claim_id=claim.id,
            gesture_id=gesture.id,
            actor=HUMAN,
            expected_context_sha256=None,
            observed_at="2026-07-17T12:01:00.000+00:00",
            at="2026-07-17T12:01:00.000+00:00",
        )

    lifecycle.mark_needs_review(
        claim_id=needs_review.id,
        actor=Actor("system", "cowork-route-test"),
        basis_kind="sweep",
        basis_ref=new_id(),
        at="2026-07-17T12:02:00.000+00:00",
    )

    resp = client.get(
        _url(f"/api/truth/doc/{document.id}", seeded["store_id"])
    )

    assert resp.status_code == 200
    by_ref = {
        entry["claim_ref"]: entry for entry in resp.get_json()["expressions"]
    }
    assert by_ref[confirmed.id]["claim_status"] == "confirmed"
    assert by_ref[confirmed.id]["claim_kind"] == "fact"
    assert by_ref[confirmed.id]["quote_anchor"] == {
        "exact": DOC_QUOTE,
        "prefix": "Original body: ",
        "suffix": " End.",
    }
    needs_review_ref = truth_uri(store.store_id, "claim", needs_review.id)
    assert by_ref[needs_review_ref]["claim_status"] == "needs_review"
    assert by_ref[needs_review_ref]["claim_kind"] == "preference"
    assert by_ref[needs_review_ref]["quote_anchor"] == {
        "exact": "Throwaway fixture",
        "prefix": "Title: ",
        "suffix": ".",
    }


def test_get_emits_ai_confirmed_only_for_human_accepted_agent_span(
    client,
    seeded,
    make_proposal,
):
    store = seeded["store"]
    document = seeded["document"]
    agent_only = expressions.ensure_document_span(
        store,
        document_id=document.id,
        selector=CompositeSelector(
            exact="Agent-marked existing prose",
            prefix="Before ",
            suffix=" after",
        ),
        quote_exact="Agent-marked existing prose",
        actor=AGENT,
        author_kind="agent_run",
        author_ref=AGENT.ref,
        at=NOW,
    )
    claim = store.propose_claim(
        proposition="The accepted replacement states the result.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    proposal = make_proposal(
        replacement="Human-approved agent prose.",
        claim_refs=[{"claim": claim.id, "role": "instantiation"}],
    )
    accepted_at = "2026-07-17T12:03:00.000+00:00"
    gesture = TruthLifecycle(store).mint_gesture(
        subject_ref=proposal.id,
        actor=HUMAN,
        surface="dashboard",
        kind="confirm",
        displayed_payload_sha256=proposal.canonical_sha256,
        at=accepted_at,
    )
    decision = proposals.accept_proposal(
        store,
        proposal_id=proposal.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        observed_at=accepted_at,
        at=accepted_at,
    )
    accepted_span_id = decision.expressions[0].document_span_id

    with store._read_connection() as conn:
        accepted_span = store._get_document_span_locked(conn, accepted_span_id)
    assert accepted_span is not None
    assert accepted_span.author_kind == "agent_run"
    assert accepted_span.author_ref == AGENT.ref
    assert accepted_span.created_by_kind == "human"
    assert accepted_span.created_by_ref == HUMAN.ref

    response = client.get(
        _url(f"/api/truth/doc/{document.id}", seeded["store_id"])
    )

    assert response.status_code == 200
    provenance = {
        entry["span_id"]: entry
        for entry in response.get_json()["provenance_spans"]
    }
    assert agent_only.id not in provenance
    assert provenance[accepted_span_id] == {
        "span_id": accepted_span_id,
        "quote": "Human-approved agent prose.",
        "quote_anchor": {
            "exact": "Human-approved agent prose.",
            "prefix": "",
            "suffix": "",
        },
        "trust_state": "ai_confirmed",
        "producer": None,
        "approval_gesture_id": None,
    }


def test_get_leaves_unresolvable_expression_claim_metadata_empty(client, seeded):
    store = seeded["store"]
    document = seeded["document"]
    span = expressions.ensure_document_span(
        store,
        document_id=document.id,
        selector=CompositeSelector(exact=DOC_QUOTE),
        quote_exact=DOC_QUOTE,
        actor=HUMAN,
        at=NOW,
    )
    refs = (
        ("uri", truth_uri(new_id(), "claim", new_id())),
        ("uri", "not-a-wb-truth-uri"),
        ("local", new_id()),
    )
    # These rows model legacy/import corruption that the normal write path
    # correctly refuses. Bypass only the post-commit export validator so the
    # read route can prove it degrades safely instead of raising.
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for claim_ref_kind, claim_ref in refs:
            expression_id = new_id()
            conn.execute(
                "INSERT INTO expressions (id, document_span_id, claim_ref_kind, "
                "claim_ref, role, claim_canonical_sha256, span_sha256, "
                "created_at, created_by_kind, created_by_ref) "
                "VALUES (?, ?, ?, ?, 'instantiation', ?, ?, ?, 'human', ?)",
                (
                    expression_id,
                    span.id,
                    claim_ref_kind,
                    claim_ref,
                    sha256_text(f"unresolvable:{claim_ref}"),
                    span.span_sha256,
                    NOW,
                    HUMAN.ref,
                ),
            )
            conn.execute(
                "INSERT INTO ledger_records (record_type, record_key) "
                "VALUES ('expression', ?)",
                (expression_id,),
            )
        conn.execute("COMMIT")
    finally:
        conn.close()

    resp = client.get(
        _url(f"/api/truth/doc/{document.id}", seeded["store_id"])
    )

    assert resp.status_code == 200
    by_ref = {
        entry["claim_ref"]: entry for entry in resp.get_json()["expressions"]
    }
    for _kind, claim_ref in refs:
        assert by_ref[claim_ref]["claim_status"] is None
        assert by_ref[claim_ref]["claim_kind"] is None


def test_get_unknown_document_is_404(client, seeded):
    resp = client.get(_url("/api/truth/doc/" + "0" * 32, seeded["store_id"]))
    assert resp.status_code == 404


def test_current_actor_exposes_the_binding_provenance_clients_must_freeze(
    client,
):
    response = client.get(
        "/api/truth/cowork/current-actor",
        headers={"X-WB-User-Ref": "local-author"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "kind": "human",
        "ref": "reviewer-kaden",
        "identity_status": "local_actor_ref",
    }


def test_paste_authorship_attestation_targets_exact_structured_head(
    client,
    seeded,
):
    document = seeded["document"]
    head = ydoc_store.current_structured_head(
        seeded["store"],
        document_id=document.id,
        snapshot_sha256=seeded["snapshot_sha256"],
    )
    url = _url(
        f"/api/truth/doc/{document.id}/authorship-attestations",
        seeded["store_id"],
    )
    body = {
        "span": {
            "exact": DOC_QUOTE,
            "prefix": "before ",
            "suffix": " after",
        },
        "attestation": {
            "schema": "cowork-authorship-attestation/v1",
            "authorship": {
                "kind": "human",
                "contributors": [
                    {
                        "kind": "current_user",
                        "ref": "reviewer-kaden",
                        "identity_status": "local_actor_ref",
                    }
                ],
            },
            "human_review": {
                "status": "not_applicable",
                "reviewers": [],
            },
        },
        "basis_kind": "automatic_short_text_attribution",
        "expected_structured_head_sha256": head,
        "idempotency_key": "paste-route-0001",
    }

    recorded = client.post(
        url,
        json=body,
        headers={"X-WB-User-Ref": "local-author"},
    )

    assert recorded.status_code == 201
    receipt = recorded.get_json()
    assert receipt["target_structured_head_sha256"] == head
    listed = provenance.list_attestations(seeded["store"], document.id)
    assert len(listed) == 1
    assert listed[0]["attestation_id"] == receipt["attestation_id"]
    assert listed[0]["scope"]["document_span_id"] == receipt["document_span_id"]
    assert listed[0]["authorship"]["contributors"] == [
        {
            "identity_status": "local_actor_ref",
            "kind": "human",
            "ref": "reviewer-kaden",
        }
    ]
    assert listed[0]["basis"]["kind"] == "automatic_short_text_attribution"

    replay = client.post(
        url,
        json=body,
        headers={"X-WB-User-Ref": "local-author"},
    )
    assert replay.status_code == 201
    assert replay.get_json()["attestation_id"] == receipt["attestation_id"]
    assert len(provenance.list_attestations(seeded["store"], document.id)) == 1

    forged_header_replay = client.post(
        url,
        json=body,
        headers={"X-WB-User-Ref": "different-local-author"},
    )
    assert forged_header_replay.status_code == 201
    assert forged_header_replay.get_json()["attestation_id"] == receipt["attestation_id"]
    assert len(provenance.list_attestations(seeded["store"], document.id)) == 1

    opened = client.get(
        _url(f"/api/truth/doc/{document.id}", seeded["store_id"])
    )
    assert opened.status_code == 200
    assert opened.get_json()["authorship_attestations"] == listed


def test_paste_authorship_attestation_rejects_stale_structured_head(
    client,
    seeded,
):
    document = seeded["document"]
    response = client.post(
        _url(
            f"/api/truth/doc/{document.id}/authorship-attestations",
            seeded["store_id"],
        ),
        json={
            "span": {"exact": DOC_QUOTE, "prefix": "", "suffix": ""},
            "attestation": {
                "schema": "cowork-authorship-attestation/v1",
                "authorship": {"kind": "unknown", "contributors": []},
                "human_review": {
                    "status": "not_applicable",
                    "reviewers": [],
                },
            },
            "expected_structured_head_sha256": "0" * 64,
            "idempotency_key": "paste-route-stale-0001",
        },
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "provenance_target_changed"
    assert provenance.list_attestations(seeded["store"], document.id) == []
    with seeded["store"]._read_connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM document_spans WHERE document_id = ?",
                (document.id,),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    ("exact", "attestation", "message"),
    [
        (
            "x" * 600,
            {
                "schema": "cowork-authorship-attestation/v1",
                "authorship": {
                    "kind": "human",
                    "contributors": [
                        {
                            "kind": "current_user",
                            "ref": "reviewer-kaden",
                            "identity_status": "local_actor_ref",
                        }
                    ],
                },
                "human_review": {
                    "status": "not_applicable",
                    "reviewers": [],
                },
            },
            "short text authored by the acting user",
        ),
        (
            DOC_QUOTE,
            {
                "schema": "cowork-authorship-attestation/v1",
                "authorship": {"kind": "ai", "contributors": []},
                "human_review": {
                    "status": "not_reviewed",
                    "reviewers": [],
                },
            },
            "short text authored by the acting user",
        ),
        (
            DOC_QUOTE,
            {
                "authorship": {"kind": "unknown", "contributors": []},
                "human_review": {
                    "status": "not_applicable",
                    "reviewers": [],
                },
            },
            "attestation.schema",
        ),
    ],
)
def test_automatic_paste_attribution_is_server_constrained(
    client,
    seeded,
    exact,
    attestation,
    message,
):
    document = seeded["document"]
    head = ydoc_store.current_structured_head(
        seeded["store"],
        document_id=document.id,
        snapshot_sha256=seeded["snapshot_sha256"],
    )
    response = client.post(
        _url(
            f"/api/truth/doc/{document.id}/authorship-attestations",
            seeded["store_id"],
        ),
        json={
            "span": {"exact": exact, "prefix": "", "suffix": ""},
            "attestation": attestation,
            "basis_kind": "automatic_short_text_attribution",
            "expected_structured_head_sha256": head,
            "idempotency_key": "automatic-paste-guard-0001",
        },
        headers={"X-WB-User-Ref": "local-author"},
    )

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert message in (error["message"] if isinstance(error, dict) else error)
    assert provenance.list_attestations(
        seeded["store"],
        document.id,
    ) == []


# --- R3 / R4 transport -----------------------------------------------------


def test_ydoc_pull_streams_octet_snapshot(client, seeded):
    resp = client.get(
        _url(f"/api/truth/doc/{seeded['document'].id}/ydoc", seeded["store_id"])
    )
    assert resp.status_code == 200
    assert resp.mimetype == "application/octet-stream"
    assert resp.headers["X-WB-Snapshot-Sha256"] == seeded["snapshot_sha256"]
    assert resp.headers["X-WB-Ydoc-Generation"] == (
        documents.current_ydoc_generation(
            seeded["store"], seeded["document"].id
        )
    )
    assert resp.headers["X-WB-Doc-Sha256"] == seeded["content_sha256"]
    # The framed body carries exactly the snapshot segment.
    assert seeded["snapshot_bytes"] in resp.data


def test_ydoc_push_appends_and_guards_stale_base(client, seeded):
    url = _url(f"/api/truth/doc/{seeded['document'].id}/ydoc", seeded["store_id"])
    ok = client.post(
        url,
        data=b"human-edit-batch",
        content_type="application/octet-stream",
        headers={
            "X-WB-Base-Sha256": seeded["content_sha256"],
            "X-WB-Base-Ydoc-Generation": documents.current_ydoc_generation(
                seeded["store"], seeded["document"].id
            ),
        },
    )
    assert ok.status_code == 200
    assert ok.get_json()["applied"] is True
    stale = client.post(
        url,
        data=b"another-batch",
        content_type="application/octet-stream",
        headers={
            "X-WB-Base-Sha256": "0" * 64,
            "X-WB-Base-Ydoc-Generation": documents.current_ydoc_generation(
                seeded["store"], seeded["document"].id
            ),
        },
    )
    assert stale.status_code == 409
    assert stale.get_json()["error"] == "stale_base"


def test_bound_ydoc_push_uses_document_kernel_receipt_and_projection(
    client, seeded, monkeypatch
):
    document = seeded["document"]
    store = seeded["store"]
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=seeded["snapshot_sha256"],
    )
    generation = documents.current_ydoc_generation(store, document.id)
    binding = SimpleNamespace(content_authority="co_work")
    monkeypatch.setattr(api, "current_domain_binding", lambda *_args: binding)
    source_store = object()
    monkeypatch.setattr(api.SourceStore, "create", lambda *_args: source_store)
    observed = {}

    def apply_bound(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return SimpleNamespace(
            change=SimpleNamespace(
                result_structured_head_sha256="f" * 64,
                change_id="1" * 32,
            ),
            next_offset="42",
            projection=SimpleNamespace(status="committed"),
        )

    monkeypatch.setattr(api, "apply_bound_direct_push", apply_bound)
    response = client.post(
        _url(f"/api/truth/doc/{document.id}/ydoc", seeded["store_id"]),
        data=b"kernel-verified-update",
        content_type="application/octet-stream",
        headers={
            "X-WB-Base-Ydoc-Sha256": head,
            "X-WB-Base-Ydoc-Generation": generation,
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "applied": True,
        "doc_sha256": document.content_sha256,
        "projection_sha256": document.content_sha256,
        "structured_head_sha256": "f" * 64,
        "ydoc_head_sha256": "f" * 64,
        "ydoc_generation": generation,
        "next_offset": "42",
        "document_change_id": "1" * 32,
        "domain_projection_status": "committed",
    }
    assert observed["kwargs"]["update"] == b"kernel-verified-update"
    assert observed["kwargs"]["source_store"] is source_store
    assert observed["kwargs"]["input_assurance"] == "enrolled_local_session"


def test_ydoc_capture_compaction_returns_projection_receipt(client, seeded):
    document = seeded["document"]
    store = seeded["store"]
    snapshot = b"YDOC-HTTP-CAPTURE"
    projection = b"# Exact HTTP projection\n"
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=seeded["snapshot_sha256"],
    )
    response = client.post(
        _url(f"/api/truth/doc/{document.id}/ydoc", seeded["store_id"]),
        data=transport.frame_segments([b"final", snapshot, projection]),
        content_type="application/octet-stream",
        headers={
            "X-WB-Base-Ydoc-Sha256": head,
            "X-WB-Base-Ydoc-Generation": (
                documents.current_ydoc_generation(store, document.id)
            ),
            "X-WB-Compacted-Snapshot-Sha256": sha256_bytes(snapshot),
            "X-WB-Compacted-Projection-Sha256": sha256_bytes(projection),
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["projection_sha256"] == document.content_sha256
    assert payload["compacted_projection_sha256"] == sha256_bytes(projection)
    assert payload["projection_receipt_id"]


def test_ydoc_push_size_limits_return_typed_413_without_mutation(
    client, seeded, monkeypatch
):
    document = seeded["document"]
    store = seeded["store"]
    url = _url(f"/api/truth/doc/{document.id}/ydoc", seeded["store_id"])
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=seeded["snapshot_sha256"],
    )
    monkeypatch.setattr(ydoc_store, "MAX_OPAQUE_SEGMENT_BYTES", 8)

    update = client.post(
        url,
        data=b"123456789",
        content_type="application/octet-stream",
        headers={
            "X-WB-Base-Ydoc-Sha256": head,
            "X-WB-Base-Ydoc-Generation": documents.current_ydoc_generation(
                store, document.id
            ),
        },
    )
    assert update.status_code == 413
    assert update.get_json()["error"]["code"] == "update_too_large"

    oversized_snapshot = b"abcdefghi"
    compacted = (
        struct.pack(">I", 1)
        + b"u"
        + struct.pack(">I", len(oversized_snapshot))
        + oversized_snapshot
    )
    snapshot = client.post(
        url,
        data=compacted,
        content_type="application/octet-stream",
        headers={
            "X-WB-Base-Ydoc-Sha256": head,
            "X-WB-Base-Ydoc-Generation": documents.current_ydoc_generation(
                store, document.id
            ),
            "X-WB-Compacted-Snapshot-Sha256": sha256_bytes(
                oversized_snapshot
            ),
        },
    )
    assert snapshot.status_code == 413
    assert snapshot.get_json()["error"]["code"] == "snapshot_too_large"

    assert (
        documents.get_document(store, document.id).ydoc_snapshot_sha256
        == seeded["snapshot_sha256"]
    )
    assert ydoc_store.read_updates(store, document_id=document.id)[0] == ()


# --- R5 legacy marks -------------------------------------------------------


def test_legacy_marks_requires_two_phase_sitting(client, seeded):
    resp = client.post(
        _url(f"/api/truth/doc/{seeded['document'].id}/marks", seeded["store_id"]),
        json={"items": [{"proposal_id": "unsafe-one-shot-request"}]},
    )
    assert resp.status_code == 410
    assert resp.get_json()["error"]["code"] == "two_phase_sitting_required"


# --- R6 materialize --------------------------------------------------------


def test_materialize_verifies_snapshot_hash(client, seeded):
    url = _url(
        f"/api/truth/doc/{seeded['document'].id}/materialize", seeded["store_id"]
    )
    new_body = "# Throwaway fixture\n\nDirect materialize.\n"
    structured_head = api.readiness.classify_document(
        seeded["store"], seeded["document"]
    ).structured_head_sha256
    assert structured_head
    mismatch = client.post(
        url,
        json={
            "rendered_markdown": new_body,
            "rendered_sha256": sha256_text(new_body),
            "expected_file_sha256": seeded["content_sha256"],
            "expected_ydoc_head_sha256": "0" * 64,
            "snapshot_sha256": seeded["snapshot_sha256"],
        },
    )
    assert mismatch.status_code == 409
    assert mismatch.get_json()["error"]["code"] == "stale_structured_head"
    ok = client.post(
        url,
        json={
            "rendered_markdown": new_body,
            "rendered_sha256": sha256_text(new_body),
            "expected_file_sha256": seeded["content_sha256"],
            "expected_ydoc_head_sha256": structured_head,
            "snapshot_sha256": seeded["snapshot_sha256"],
        },
    )
    assert ok.status_code == 200
    payload = ok.get_json()
    assert payload["new_file_sha256"] == sha256_text(new_body)
    assert (seeded["root"] / seeded["rel"]).read_text(encoding="utf-8") == new_body


# --- R7 drift / R8 reimport ------------------------------------------------


def test_drift_reports_out_of_band_edit(client, store_ctx):
    ready, _source = _bootstrap_ready(
        client,
        store_ctx,
        path="docs/drift-route.md",
        key="drift-route-bootstrap-0001",
    )
    url = _url(
        f"/api/truth/doc/{ready['document_id']}/drift", store_ctx["store_id"]
    )
    clean = client.get(url).get_json()
    assert clean["state"] == "clean"
    assert clean["can_reimport"] is False
    # Edit the file out of band.
    drifted_body = "# Throwaway fixture\n\nEdited outside the editor.\n"
    (store_ctx["root"] / "docs" / "drift-route.md").write_bytes(
        drifted_body.encode("utf-8")
    )
    drifted = client.get(url).get_json()
    assert drifted["state"] == "drifted"
    assert drifted["can_reimport"] is True
    assert drifted["current_file_sha256"] == sha256_bytes(drifted_body.encode("utf-8"))


def test_reimport_records_change_set(client, store_ctx):
    ready, _source = _bootstrap_ready(
        client,
        store_ctx,
        path="docs/reimport-route.md",
        key="reimport-route-bootstrap-0001",
    )
    drifted_body = "# Throwaway fixture\n\nEdited outside the editor.\n"
    (store_ctx["root"] / "docs" / "reimport-route.md").write_bytes(
        drifted_body.encode("utf-8")
    )
    prepare = client.post(
        _url(
            f"/api/truth/doc/{ready['document_id']}/reimport",
            store_ctx["store_id"],
        ),
        json={"idempotency_key": "reimport-route-0001"},
    )
    assert prepare.status_code == 201
    intent = prepare.get_json()
    source = client.get(
        _url(
            f"/api/truth/doc/{ready['document_id']}/reimport/"
            f"{intent['intent_id']}/source",
            store_ctx["store_id"],
        )
    )
    assert source.status_code == 200
    assert source.data == drifted_body.encode("utf-8")

    replacement_snapshot = b"YDOC:" + drifted_body.encode("utf-8")
    commit = client.put(
        _url(
            f"/api/truth/doc/{ready['document_id']}/reimport/"
            f"{intent['intent_id']}/commit",
            store_ctx["store_id"],
        ),
        data={
            "metadata": json.dumps(
                {"snapshot_sha256": sha256_bytes(replacement_snapshot)}
            ),
            "snapshot": (io.BytesIO(replacement_snapshot), "snapshot.bin"),
        },
        content_type="multipart/form-data",
    )
    assert commit.status_code == 200
    payload = commit.get_json()
    assert payload["document_version_id"]
    assert payload["source_sha256"] == sha256_bytes(drifted_body.encode("utf-8"))
    # The content pointer advanced, so the document reads clean again.
    drift = client.get(
        _url(
            f"/api/truth/doc/{ready['document_id']}/drift", store_ctx["store_id"]
        )
    ).get_json()
    assert drift["state"] == "clean"


# --- R9 feedback -----------------------------------------------------------


def test_conversation_get_is_read_only_and_post_lazily_creates_real_binding(
    client,
    seeded,
    fake_document_agent,
):
    url = _url(
        f"/api/truth/doc/{seeded['document'].id}/conversation",
        seeded["store_id"],
    )
    conn = conversation_store.get_connection()
    try:
        before = {
            "conversations": conn.execute(
                "SELECT COUNT(*) FROM conversations"
            ).fetchone()[0],
            "leases": conn.execute(
                "SELECT COUNT(*) FROM conversation_agent_leases"
            ).fetchone()[0],
        }
    finally:
        conn.close()

    opened = client.get(url)
    assert opened.status_code == 200
    opened_payload = opened.get_json()
    assert opened_payload["ok"] is True
    assert opened_payload["conversation_id"] is None
    assert opened_payload["agent"] == {
        "status": "not_started",
        "alive": None,
        "started": False,
        "error": None,
    }
    assert opened_payload["feedback"] == []
    assert opened_payload["execution"]["selection"] == {
        "schema_version": 1,
        "provider_id": "claude-code",
        "model_id": "sonnet",
        "provider_label": "Claude Code",
        "model_label": "Sonnet",
        "revision": "",
        "persisted": False,
    }
    assert [provider["id"] for provider in opened_payload["execution"]["providers"]] == [
        "claude-code",
        "codex",
    ]
    conn = conversation_store.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == before["conversations"]
        assert conn.execute("SELECT COUNT(*) FROM conversation_agent_leases").fetchone()[0] == before["leases"]
    finally:
        conn.close()
    assert fake_document_agent == []

    started = client.post(url)
    assert started.status_code == 200
    payload = started.get_json()
    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["conversation_id"]
    assert payload["feedback"] == []
    assert payload["agent"]["status"] == "running"
    assert payload["execution"]["selection"]["persisted"] is True
    assert payload["execution"]["selection"]["revision"]
    assert len(fake_document_agent) == 1
    assert fake_document_agent[0]["conversation_id"] == payload["conversation_id"]
    assert fake_document_agent[0]["execution"].provider_id == "claude-code"

    repeated = client.post(url).get_json()
    assert repeated["conversation_id"] == payload["conversation_id"]
    assert repeated["created"] is False


def test_conversation_bind_mounts_chat_without_running_or_fencing_a_model(
    client,
    seeded,
    fake_document_agent,
    monkeypatch,
):
    from work_buddy import consent

    boundaries: list[str] = []

    @contextmanager
    def _observed_user_action(label):
        boundaries.append(label)
        yield

    def _unexpected_fence(**_kwargs):
        raise AssertionError("binding Chat must not fence a document agent")

    monkeypatch.setattr(consent, "user_initiated", _observed_user_action)
    monkeypatch.setattr(
        document_agent,
        "fence_document_agent",
        _unexpected_fence,
    )
    url = _url(
        f"/api/truth/doc/{seeded['document'].id}/conversation/bind",
        seeded["store_id"],
    )
    conn = conversation_store.get_connection()
    try:
        before_conversations = conn.execute(
            "SELECT COUNT(*) FROM conversations"
        ).fetchone()[0]
        before_leases = conn.execute(
            "SELECT COUNT(*) FROM conversation_agent_leases"
        ).fetchone()[0]
    finally:
        conn.close()

    bound = client.post(url)

    assert bound.status_code == 200
    payload = bound.get_json()
    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["conversation_id"]
    assert payload["agent"] == {
        "status": "not_started",
        "alive": None,
        "started": False,
        "error": None,
    }
    assert payload["feedback"] == []
    assert payload["execution"]["selection"]["persisted"] is True
    assert payload["execution"]["selection"]["revision"]
    assert fake_document_agent == []

    repeated = client.post(url)

    assert repeated.status_code == 200
    repeated_payload = repeated.get_json()
    assert repeated_payload["conversation_id"] == payload["conversation_id"]
    assert repeated_payload["created"] is False
    assert repeated_payload["agent"]["status"] == "not_started"
    assert (
        repeated_payload["execution"]["selection"]["revision"]
        == payload["execution"]["selection"]["revision"]
    )
    assert fake_document_agent == []
    assert boundaries == [
        "dashboard.cowork.conversation_bind",
        "dashboard.cowork.conversation_bind",
    ]
    conn = conversation_store.get_connection()
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            == before_conversations + 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM conversation_agent_leases"
            ).fetchone()[0]
            == before_leases
        )
    finally:
        conn.close()


def test_conversation_bind_rejects_a_closed_canonical_conversation(
    client,
    seeded,
    fake_document_agent,
):
    binding = conversations.ensure_document_conversation(
        document_id=seeded["document"].id,
        store_id=seeded["store_id"],
    )
    assert conversation_store.close_conversation(binding.conversation_id)

    response = client.post(
        _url(
            f"/api/truth/doc/{seeded['document'].id}/conversation/bind",
            seeded["store_id"],
        )
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == {
        "code": "conversation_closed",
        "message": (
            "This document's conversation is closed and cannot be reopened."
        ),
        "details": {},
        "retryable": False,
    }
    assert fake_document_agent == []


@pytest.mark.parametrize(
    ("gate", "expected_status", "expected_error"),
    [
        ("read_only", 403, "Dashboard is in read-only mode"),
        ("retired", 409, "Chat cannot be opened for a retired document."),
        (
            "surface",
            403,
            "This document is not available in Co-work for this folder.",
        ),
    ],
)
def test_conversation_bind_preserves_document_mutation_gates(
    client,
    seeded,
    fake_document_agent,
    monkeypatch,
    gate,
    expected_status,
    expected_error,
):
    if gate == "read_only":
        monkeypatch.setattr(api, "_is_read_only", lambda: True)
    elif gate == "retired":
        monkeypatch.setattr(
            api.documents,
            "current_lifecycle",
            lambda *_args, **_kwargs: "retired",
        )
    else:
        monkeypatch.setattr(
            api,
            "document_surface_allowed",
            lambda *_args, **_kwargs: False,
        )

    response = client.post(
        _url(
            f"/api/truth/doc/{seeded['document'].id}/conversation/bind",
            seeded["store_id"],
        )
    )

    assert response.status_code == expected_status
    assert response.get_json()["error"] == expected_error
    assert (
        conversations.find_document_conversation(
            document_id=seeded["document"].id,
            store_id=seeded["store_id"],
        )
        is None
    )
    assert fake_document_agent == []


def test_conversation_catalog_refresh_is_explicit(
    client,
    seeded,
    monkeypatch,
):
    from work_buddy.agent_execution import registry as execution_registry

    original = execution_registry.get_catalog
    refreshes: list[bool] = []

    def _recording_catalog(*, refresh: bool = False):
        refreshes.append(refresh)
        return original(refresh=refresh)

    monkeypatch.setattr(
        execution_registry,
        "get_catalog",
        _recording_catalog,
    )
    base = _url(
        f"/api/truth/doc/{seeded['document'].id}/conversation",
        seeded["store_id"],
    )

    assert client.get(base).status_code == 200
    assert client.get(f"{base}&refresh_execution=1").status_code == 200

    assert refreshes == [False, True]


def test_closed_document_conversation_cannot_spawn_again(
    client,
    seeded,
    fake_document_agent,
):
    binding = conversations.ensure_document_conversation(
        document_id=seeded["document"].id,
        store_id=seeded["store_id"],
    )
    assert conversation_store.close_conversation(binding.conversation_id)
    fake_document_agent.clear()

    response = client.post(
        _url(
            f"/api/truth/doc/{seeded['document'].id}/conversation",
            seeded["store_id"],
        )
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "conversation_closed"
    assert fake_document_agent == []


def test_closed_document_conversation_cannot_change_execution(
    client,
    seeded,
    fake_document_agent,
):
    binding = conversations.ensure_document_conversation(
        document_id=seeded["document"].id,
        store_id=seeded["store_id"],
    )
    assert conversation_store.close_conversation(binding.conversation_id)
    fake_document_agent.clear()

    response = client.patch(
        _url(
            (
                f"/api/truth/doc/{seeded['document'].id}"
                "/conversation/execution"
            ),
            seeded["store_id"],
        ),
        json={
            "provider_id": "codex",
            "model_id": "gpt-5.6-sol",
            "expected_revision": "",
        },
    )

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["error"] == {
        "code": "conversation_closed",
        "message": (
            "This document's conversation is closed and cannot change models."
        ),
        "details": {},
        "retryable": False,
    }
    assert fake_document_agent == []


def test_execution_picker_can_select_before_chat_starts(
    client,
    seeded,
    fake_document_agent,
):
    base = f"/api/truth/doc/{seeded['document'].id}/conversation"
    selected = client.patch(
        _url(f"{base}/execution", seeded["store_id"]),
        json={
            "provider_id": "codex",
            "model_id": "gpt-5.6-sol",
            "expected_revision": "",
        },
    )
    assert selected.status_code == 200
    payload = selected.get_json()
    assert payload["created"] is True
    assert payload["agent"]["status"] == "not_started"
    assert payload["execution"]["selection"]["provider_id"] == "codex"
    assert payload["execution"]["selection"]["model_id"] == "gpt-5.6-sol"
    assert payload["execution"]["selection"]["revision"]
    assert fake_document_agent == []

    opened = client.get(_url(base, seeded["store_id"])).get_json()
    assert opened["conversation_id"] == payload["conversation_id"]
    assert opened["execution"]["selection"]["provider_id"] == "codex"

    started = client.post(_url(base, seeded["store_id"]))
    assert started.status_code == 200
    assert len(fake_document_agent) == 1
    assert fake_document_agent[0]["execution"].provider_id == "codex"
    assert fake_document_agent[0]["execution"].model_id == "gpt-5.6-sol"


def test_execution_picker_same_pair_retry_is_idempotent_after_response_loss(
    client,
    seeded,
    fake_document_agent,
):
    base = f"/api/truth/doc/{seeded['document'].id}/conversation"
    request_body = {
        "provider_id": "codex",
        "model_id": "gpt-5.6-sol",
        "expected_revision": "",
    }
    selected = client.patch(
        _url(f"{base}/execution", seeded["store_id"]),
        json=request_body,
    )
    assert selected.status_code == 200
    selected_payload = selected.get_json()
    revision = selected_payload["execution"]["selection"]["revision"]
    assert revision

    replayed = client.patch(
        _url(f"{base}/execution", seeded["store_id"]),
        json=request_body,
    )

    assert replayed.status_code == 200
    replayed_payload = replayed.get_json()
    assert replayed_payload["execution"]["selection"]["revision"] == revision
    assert replayed_payload["execution"]["selection"]["provider_id"] == "codex"
    assert replayed_payload["execution"]["selection"]["model_id"] == "gpt-5.6-sol"
    assert replayed_payload["agent"]["status"] == "not_started"
    assert fake_document_agent == []


def test_execution_routes_fail_closed_for_a_corrupt_saved_selection(
    client,
    seeded,
    fake_document_agent,
):
    binding = conversations.ensure_document_conversation(
        document_id=seeded["document"].id,
        store_id=seeded["store_id"],
    )
    conn = conversation_store.get_connection()
    try:
        row = conn.execute(
            "SELECT metadata FROM conversations WHERE conversation_id = ?",
            (binding.conversation_id,),
        ).fetchone()
        assert row is not None
        metadata = json.loads(row["metadata"])
        metadata[EXECUTION_METADATA_KEY] = {"schema_version": 999}
        conn.execute(
            "UPDATE conversations SET metadata = ? WHERE conversation_id = ?",
            (json.dumps(metadata), binding.conversation_id),
        )
        conn.commit()
    finally:
        conn.close()

    base = f"/api/truth/doc/{seeded['document'].id}/conversation"
    responses = (
        client.get(_url(base, seeded["store_id"])),
        client.post(_url(f"{base}/bind", seeded["store_id"])),
        client.patch(
            _url(f"{base}/execution", seeded["store_id"]),
            json={
                "provider_id": "codex",
                "model_id": "gpt-5.6-sol",
                "expected_revision": "",
            },
        ),
        client.post(_url(base, seeded["store_id"])),
    )

    for response in responses:
        assert response.status_code == 409
        assert response.get_json()["error"] == {
            "code": "execution_selection_corrupt",
            "message": (
                "This chat's saved provider and model choice could not be read."
            ),
            "details": {},
            "retryable": False,
        }
    assert fake_document_agent == []


def test_execution_routes_fail_closed_for_malformed_conversation_metadata(
    client,
    seeded,
):
    binding = conversations.ensure_document_conversation(
        document_id=seeded["document"].id,
        store_id=seeded["store_id"],
    )
    conn = conversation_store.get_connection()
    try:
        conn.execute(
            "UPDATE conversations SET metadata = ? WHERE conversation_id = ?",
            ('{"broken"', binding.conversation_id),
        )
        conn.commit()
    finally:
        conn.close()

    base = f"/api/truth/doc/{seeded['document'].id}/conversation"
    responses = (
        client.get(_url(base, seeded["store_id"])),
        client.post(_url(f"{base}/bind", seeded["store_id"])),
        client.post(_url(base, seeded["store_id"])),
        client.patch(
            _url(f"{base}/execution", seeded["store_id"]),
            json={
                "provider_id": "codex",
                "model_id": "gpt-5.6-sol",
                "expected_revision": "",
            },
        ),
    )

    for response in responses:
        assert response.status_code == 409
        assert response.get_json()["error"] == {
            "code": "execution_selection_corrupt",
            "message": (
                "This chat's saved provider and model choice could not be read."
            ),
            "details": {},
            "retryable": False,
        }
    conn = conversation_store.get_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE source = ?",
            (conversations.CONVERSATION_SOURCE,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_execution_switch_fences_existing_generation_and_restarts(
    client,
    seeded,
    fake_document_agent,
):
    base = f"/api/truth/doc/{seeded['document'].id}/conversation"
    started = client.post(_url(base, seeded["store_id"])).get_json()
    conversation_id = started["conversation_id"]
    revision = started["execution"]["selection"]["revision"]
    consumer = document_agent.document_agent_consumer(
        seeded["store_id"],
        seeded["document"].id,
    )
    lease = conversation_store.claim_agent_lease(
        conversation_id,
        consumer,
        "generation-before-switch",
        execution={
            "schema_version": 1,
            "provider_id": "claude-code",
            "model_id": "sonnet",
            "provider_label": "Claude Code",
            "model_label": "Sonnet",
        },
    )
    assert lease is not None and lease["claimed"] is True
    fake_document_agent.clear()

    switched = client.patch(
        _url(f"{base}/execution", seeded["store_id"]),
        json={
            "provider_id": "codex",
            "model_id": "gpt-5.6-sol",
            "expected_revision": revision,
        },
    )
    assert switched.status_code == 200
    payload = switched.get_json()
    assert payload["execution"]["selection"]["provider_id"] == "codex"
    assert len(fake_document_agent) == 1
    assert fake_document_agent[0]["execution"].provider_id == "codex"
    fenced = conversation_store.get_agent_lease(conversation_id, consumer)
    assert fenced is not None
    assert fenced["status"] == "stopped"


def test_execution_switch_fences_raw_running_lease_even_when_projection_is_stale(
    client,
    seeded,
    fake_document_agent,
    monkeypatch,
):
    base = f"/api/truth/doc/{seeded['document'].id}/conversation"
    started = client.post(_url(base, seeded["store_id"])).get_json()
    conversation_id = started["conversation_id"]
    revision = started["execution"]["selection"]["revision"]
    consumer = document_agent.document_agent_consumer(
        seeded["store_id"],
        seeded["document"].id,
    )
    generation = "stale-heartbeat-before-switch"
    lease = conversation_store.claim_agent_lease(
        conversation_id,
        consumer,
        generation,
        execution={
            "schema_version": 1,
            "provider_id": "claude-code",
            "model_id": "sonnet",
            "provider_label": "Claude Code",
            "model_label": "Sonnet",
        },
    )
    assert lease is not None and lease["claimed"] is True
    assert conversation_store.activate_agent_lease(
        conversation_id,
        consumer,
        generation,
        os.getpid(),
    )
    conn = conversation_store.get_connection()
    try:
        conn.execute(
            """UPDATE conversation_agent_leases
               SET heartbeat_at = ?, updated_at = ?
               WHERE conversation_id = ? AND consumer = ?""",
            (
                "2000-01-01T00:00:00+00:00",
                "2000-01-01T00:00:00+00:00",
                conversation_id,
                consumer,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    assert (
        document_agent.inspect_document_agent(
            conversation_id,
            consumer=consumer,
        ).status
        == "stopped"
    )
    fake_document_agent.clear()
    observed = {}
    restarted_generation = "generation-after-switch"

    def _restart(**kwargs):
        observed["before_restart"] = conversation_store.get_agent_lease(
            conversation_id,
            consumer,
        )
        selection = kwargs["execution"]
        claimed = conversation_store.claim_agent_lease(
            conversation_id,
            consumer,
            restarted_generation,
            execution={
                "schema_version": 1,
                **selection.to_dict(),
            },
        )
        fake_document_agent.append(dict(kwargs))
        assert claimed is not None and claimed["claimed"] is True
        return document_agent.DocumentAgentStatus(
            status="running",
            alive=None,
            started=True,
            error=None,
        )

    monkeypatch.setattr(document_agent, "ensure_document_agent", _restart)

    switched = client.patch(
        _url(f"{base}/execution", seeded["store_id"]),
        json={
            "provider_id": "codex",
            "model_id": "gpt-5.6-sol",
            "expected_revision": revision,
        },
    )

    assert switched.status_code == 200
    assert switched.get_json()["execution"]["selection"]["provider_id"] == "codex"
    assert len(fake_document_agent) == 1
    assert fake_document_agent[0]["execution"].provider_id == "codex"
    assert observed["before_restart"]["status"] == "stopped"
    assert observed["before_restart"]["generation"] == generation
    restarted = conversation_store.get_agent_lease(conversation_id, consumer)
    assert restarted is not None
    assert restarted["status"] == "starting"
    assert restarted["generation"] == restarted_generation
    assert restarted["execution"]["provider_id"] == "codex"


def test_execution_picker_rejects_stale_revision_without_fencing(
    client,
    seeded,
):
    base = f"/api/truth/doc/{seeded['document'].id}/conversation"
    started = client.post(_url(base, seeded["store_id"])).get_json()
    response = client.patch(
        _url(f"{base}/execution", seeded["store_id"]),
        json={
            "provider_id": "codex",
            "model_id": "gpt-5.6-sol",
            "expected_revision": "stale-revision",
        },
    )
    assert response.status_code == 409
    payload = response.get_json()
    assert payload["error"]["code"] == "execution_selection_changed"
    assert (
        payload["execution"]["selection"]["revision"]
        == started["execution"]["selection"]["revision"]
    )
    assert payload["execution"]["selection"]["provider_id"] == "claude-code"
    assert payload["agent"]["status"] == "not_started"
    opened = client.get(_url(base, seeded["store_id"])).get_json()
    assert (
        opened["execution"]["selection"]["revision"]
        == started["execution"]["selection"]["revision"]
    )
    assert opened["execution"]["selection"]["provider_id"] == "claude-code"


def test_execution_picker_rejects_unknown_model(
    client,
    seeded,
):
    base = f"/api/truth/doc/{seeded['document'].id}/conversation"
    response = client.patch(
        _url(f"{base}/execution", seeded["store_id"]),
        json={
            "provider_id": "codex",
            "model_id": "invented-model",
            "expected_revision": "",
        },
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "unknown_model"


def test_feedback_captures_user_authored_utterance(
    client,
    seeded,
    fake_document_agent,
):
    resp = client.post(
        _url(f"/api/truth/doc/{seeded['document'].id}/feedback", seeded["store_id"]),
        json={
            "span": {"exact": DOC_QUOTE, "prefix": "", "suffix": ""},
            "text": "This sentence needs a citation.",
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    # The conversation is resolved server-side (one per document), not echoed
    # from the request, and the verbatim feedback is posted into it.
    assert payload["conversation_id"]
    assert payload["message_id"]
    assert payload["span_id"]
    assert payload["agent"]["status"] == "running"
    assert len(fake_document_agent) == 1
    assert fake_document_agent[0]["feedback"].message_id == payload["message_id"]
    with seeded["store"].connect() as conn:
        row = conn.execute(
            "SELECT kind, trust_class, content FROM evidence WHERE id = ?",
            (payload["evidence_id"],),
        ).fetchone()
    assert row["kind"] == "utterance"
    assert row["trust_class"] == "user_authored"
    assert row["content"] == "This sentence needs a citation."
    from work_buddy.conversations.store import get_conversation_with_messages

    conversation = get_conversation_with_messages(payload["conversation_id"])
    assert conversation is not None
    assert any(
        message["content"] == "This sentence needs a citation."
        for message in conversation["messages"]
    )


def test_feedback_spawn_failure_keeps_authored_turn_and_returns_safe_status(
    client,
    seeded,
    monkeypatch,
):
    def _raise(**_kwargs):
        raise RuntimeError("C:\\private\\launcher --token raw-secret")

    monkeypatch.setattr(document_agent, "ensure_document_agent", _raise)
    response = client.post(
        _url(f"/api/truth/doc/{seeded['document'].id}/feedback", seeded["store_id"]),
        json={
            "span": {"exact": DOC_QUOTE, "prefix": "", "suffix": ""},
            "text": "Keep this feedback even if chat cannot start.",
        },
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["agent"] == {
        "status": "spawn_failed",
        "alive": False,
        "started": False,
        "error": "Chat couldn’t start. Try again.",
    }
    assert "secret" not in payload["agent"]["error"]
    bundle = conversation_store.get_conversation_with_messages(
        payload["conversation_id"]
    )
    assert bundle is not None
    authored = [
        item
        for item in bundle["messages"]
        if item["message_id"] == payload["message_id"]
    ]
    assert len(authored) == 1
    assert authored[0]["role"] == "user"
    assert authored[0]["content"] == (
        "Keep this feedback even if chat cannot start."
    )


def test_feedback_keeps_authored_turn_when_saved_execution_is_corrupt(
    client,
    seeded,
    fake_document_agent,
):
    binding = conversations.ensure_document_conversation(
        document_id=seeded["document"].id,
        store_id=seeded["store_id"],
    )
    conn = conversation_store.get_connection()
    try:
        row = conn.execute(
            "SELECT metadata FROM conversations WHERE conversation_id = ?",
            (binding.conversation_id,),
        ).fetchone()
        assert row is not None
        metadata = json.loads(row["metadata"])
        metadata[EXECUTION_METADATA_KEY] = {"schema_version": 999}
        conn.execute(
            "UPDATE conversations SET metadata = ? WHERE conversation_id = ?",
            (json.dumps(metadata), binding.conversation_id),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.post(
        _url(
            f"/api/truth/doc/{seeded['document'].id}/feedback",
            seeded["store_id"],
        ),
        json={
            "span": {"exact": DOC_QUOTE, "prefix": "", "suffix": ""},
            "text": "Keep this even though the model choice is unreadable.",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["agent"]["status"] == "spawn_failed"
    assert payload["execution"]["read_only"] is True
    assert payload["execution"]["error"]["code"] == (
        "execution_selection_corrupt"
    )
    assert payload["execution"]["selection"]["provider_id"] == (
        "execution-unavailable"
    )
    assert fake_document_agent == []
    bundle = conversation_store.get_conversation_with_messages(
        binding.conversation_id
    )
    assert bundle is not None
    assert any(
        item["message_id"] == payload["message_id"]
        and item["role"] == "user"
        and item["content"]
        == "Keep this even though the model choice is unreadable."
        for item in bundle["messages"]
    )


def test_repeated_identical_feedback_remains_keyed_to_distinct_truth_anchors(
    client,
    seeded,
):
    base = f"/api/truth/doc/{seeded['document'].id}"
    identical = "Please tighten this."
    first = client.post(
        _url(f"{base}/feedback", seeded["store_id"]),
        json={
            "span": {
                "exact": "Throwaway fixture",
                "prefix": "# ",
                "suffix": "\n\n",
            },
            "text": identical,
        },
    ).get_json()
    second = client.post(
        _url(f"{base}/feedback", seeded["store_id"]),
        json={
            "span": {
                "exact": DOC_QUOTE,
                "prefix": "\n\n",
                "suffix": "\n",
            },
            "text": identical,
        },
    ).get_json()
    assert first["message_id"] != second["message_id"]

    binding = client.get(
        _url(f"{base}/conversation", seeded["store_id"])
    ).get_json()
    by_message = {item["message_id"]: item for item in binding["feedback"]}
    assert by_message[first["message_id"]]["text"] == identical
    assert by_message[first["message_id"]]["anchor"] == {
        "exact": "Throwaway fixture",
        "prefix": "# ",
        "suffix": "\n\n",
        "node_id_hint": None,
    }
    assert by_message[second["message_id"]]["text"] == identical
    assert by_message[second["message_id"]]["anchor"]["exact"] == DOC_QUOTE


def test_start_after_another_tab_feedback_adopts_binding_and_annotations(
    client,
    seeded,
):
    base = f"/api/truth/doc/{seeded['document'].id}"
    initial = client.get(
        _url(f"{base}/conversation", seeded["store_id"])
    ).get_json()
    assert initial["conversation_id"] is None

    captured = client.post(
        _url(f"{base}/feedback", seeded["store_id"]),
        json={
            "span": {"exact": DOC_QUOTE, "prefix": "", "suffix": ""},
            "text": "Feedback from another tab.",
        },
    ).get_json()
    adopted = client.post(
        _url(f"{base}/conversation", seeded["store_id"])
    ).get_json()
    assert adopted["created"] is False
    assert adopted["conversation_id"] == captured["conversation_id"]
    assert [item["message_id"] for item in adopted["feedback"]] == [
        captured["message_id"]
    ]


def test_feedback_requires_document_surface_capture(client, store_ctx, tmp_path):
    # A second store in the same registry with feedback_capture turned off. The
    # profile is read from disk on each open, so the route sees it disabled.
    from work_buddy.truth import documents
    from work_buddy.truth.contracts import Actor
    from work_buddy.truth.store import TruthStore

    from .conftest import DOC_BODY, USER_REF, _profile

    profile = _profile()
    profile["document_surface"]["feedback_capture"] = False
    root = tmp_path / "scope-no-feedback"
    root.mkdir()
    store = TruthStore.create(root, profile)
    store_ctx["registry"].register(store)
    content_sha256 = write_doc_file(root)
    record = documents.register_document(
        store,
        path=DOC_REL,
        title="Throwaway fixture",
        document_class="co_authored",
        content_sha256=content_sha256,
        actor=Actor("human", USER_REF),
        at=NOW,
    )
    resp = client.post(
        _url(f"/api/truth/doc/{record.id}/feedback", store.store_id),
        json={"span": {"exact": DOC_QUOTE}, "text": "hi"},
    )
    assert resp.status_code == 403

"""Flask test-client coverage for every /api/truth/doc/* route (R1-R10).

The client mounts only the co-work blueprint against a temporary registry, so no
live port is bound and the routes resolve stores exactly as in production.
"""

from __future__ import annotations

import io
import json
import struct

from work_buddy.conversations import store as conversation_store
from work_buddy.cowork import api
from work_buddy.cowork import conversations, document_agent
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

    for response in (folders, documents):
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


def test_get_returns_open_proposals_and_hashes(client, seeded, make_proposal):
    proposal = make_proposal()
    resp = client.get(
        _url(f"/api/truth/doc/{seeded['document'].id}", seeded["store_id"])
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["hashes"]["ydoc_snapshot_sha256"] == seeded["snapshot_sha256"]
    assert payload["drift"]["state"] == "clean"
    assert len(payload["open_proposals"]) == 1
    entry = payload["open_proposals"][0]
    assert entry["proposal_id"] == proposal.id
    assert entry["canonical_sha256"] == proposal.canonical_sha256
    assert entry["base_ok"] is True
    assert entry["quote_anchor"]["exact"] == DOC_QUOTE
    assert entry["kind"] == "edit"


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
    assert opened.get_json() == {
        "ok": True,
        "conversation_id": None,
        "agent": {
            "status": "not_started",
            "alive": None,
            "started": False,
            "error": None,
        },
        "feedback": [],
    }
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
    assert len(fake_document_agent) == 1
    assert fake_document_agent[0]["conversation_id"] == payload["conversation_id"]

    repeated = client.post(url).get_json()
    assert repeated["conversation_id"] == payload["conversation_id"]
    assert repeated["created"] is False


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

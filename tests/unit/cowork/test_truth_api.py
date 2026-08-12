"""Co-work Truth observability and guarded-mutation route coverage."""

from __future__ import annotations

import pytest

from work_buddy.cowork import api, truth_api
from work_buddy.security.actors import ActorRef
from work_buddy.security.local_identity import (
    HUMAN_AUTHORITY_ASSURANCE,
    HUMAN_AUTHORITY_BASIS,
    HumanAuthorityContext,
    LocalIdentityError,
    LocalPrincipal,
)
from work_buddy.truth import documents, expressions, queries, ydoc_store
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor
from work_buddy.truth.identity import sha256_bytes, utc_now
from work_buddy.truth.lifecycle import TruthLifecycle
from work_buddy.truth.source_provenance import (
    provenance_for_subject,
    validate_actor_ref_json,
)
from work_buddy.truth.store import AcquisitionOrigin, PostCommitHookError, TruthStore

from .conftest import AGENT, DOC_BODY, DOC_QUOTE, HUMAN, NOW


@pytest.fixture(autouse=True)
def _authenticated_truth_authority(monkeypatch):
    issued = 0
    calls: list[tuple[str, str, str]] = []

    def authorize(*, action: str, subject: str, context_sha256: str):
        nonlocal issued
        issued += 1
        calls.append((action, subject, context_sha256))
        actor = ActorRef(
            issuer_authority_id="truth-test-issuer",
            subject="truth-reviewer",
            kind="human",
            tenant_scope_id="truth-test-tenant",
        )
        return HumanAuthorityContext(
            principal=LocalPrincipal(
                actor=actor,
                session_id="truth-test-session",
                origin="http://localhost",
                audience="work-buddy-dashboard",
                session_expires_at=9_999_999_999.0,
                rotation_due_at=9_999_999_000.0,
            ),
            action=action,
            subject_sha256=sha256_bytes(subject.encode("utf-8")),
            context_sha256=context_sha256,
            gesture_id=f"truth-test-gesture-{issued}",
            assurance=HUMAN_AUTHORITY_ASSURANCE,
            basis=HUMAN_AUTHORITY_BASIS,
        )

    monkeypatch.setattr(truth_api, "require_human_authority_request", authorize)
    return calls


def _url(seeded, suffix: str = "", **query: str) -> str:
    document_id = seeded["document"].id
    params = {"store_id": seeded["store_id"], **query}
    encoded = "&".join(f"{key}={value}" for key, value in params.items())
    return f"/api/truth/doc/{document_id}/truth{suffix}?{encoded}"


def _connectable_projection(seeded) -> tuple[str, str]:
    """Publish an exact Markdown checkpoint for selection-bound writes."""

    store = seeded["store"]
    document = seeded["document"]
    current_head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=seeded["snapshot_sha256"],
    )
    snapshot = b"YDOC-TRUTH-SURFACE-CHECKPOINT"
    _, structured_head, _, receipt = ydoc_store.compact_and_advance(
        store,
        document_id=document.id,
        snapshot=snapshot,
        expected_snapshot_sha256=sha256_bytes(snapshot),
        expected_structured_head_sha256=current_head,
        projection_sha256=sha256_bytes(DOC_BODY.encode("utf-8")),
        actor=HUMAN,
        at=NOW,
    )
    assert receipt is not None
    return structured_head, documents.current_ydoc_generation(store, document.id)


def _selection_body(seeded) -> dict[str, object]:
    structured_head, generation = _connectable_projection(seeded)
    return {
        "selector": {"exact": DOC_QUOTE},
        "role": "quote",
        "expected_structured_head_sha256": structured_head,
        "expected_ydoc_generation_sha256": generation,
        "expected_projection_sha256": sha256_bytes(DOC_BODY.encode("utf-8")),
    }


def _confirm(client, seeded, claim_id: str) -> None:
    binding = client.get(_url(seeded, f"/claims/{claim_id}")).get_json()[
        "decision_binding"
    ]
    response = client.post(
        _url(seeded, f"/claims/{claim_id}/decisions"),
        headers={"X-WB-User-Ref": "truth-reviewer"},
        json={
            "action": "confirm",
            "expected_canonical_sha256": binding["payload_sha256"],
            "expected_context_sha256": binding["context_sha256"],
        },
    )
    assert response.status_code == 200


def test_truth_list_separates_document_connections_from_folder_truth(
    client,
    seeded,
):
    store = seeded["store"]
    connected = store.propose_claim(
        proposition="The fixture contains an original sentence.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    unconnected = store.propose_claim(
        proposition="The fixture belongs to the test folder.",
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
        claim_ref=connected.id,
        role="quote",
        actor=HUMAN,
        at=NOW,
    )

    document_response = client.get(_url(seeded))
    folder_response = client.get(
        _url(seeded, view="folder", filter="unconnected")
    )

    assert document_response.status_code == 200
    document_payload = document_response.get_json()
    assert [item["claim_id"] for item in document_payload["claims"]] == [
        connected.id
    ]
    assert document_payload["claims"][0]["document_connections"][0][
        "selector"
    ]["exact"] == DOC_QUOTE
    assert folder_response.status_code == 200
    folder_payload = folder_response.get_json()
    assert [item["claim_id"] for item in folder_payload["claims"]] == [
        unconnected.id
    ]
    assert folder_payload["counts"]["unconnected"] == 1


def test_truth_detail_is_observational_and_exposes_exact_decision_binding(
    client,
    seeded,
):
    claim = seeded["store"].propose_claim(
        proposition="The fixture has a heading.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim

    response = client.get(_url(seeded, f"/claims/{claim.id}"))

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["claim"]["claim_id"] == claim.id
    assert payload["claim"]["base_status"] == "proposed"
    assert payload["status_history"][0]["status"] == "proposed"
    assert payload["decision_binding"] == {
        "agent_authored_only": False,
        "context_sha256": payload["decision_binding"]["context_sha256"],
        "payload_sha256": claim.canonical_sha256,
    }
    assert payload["support"]["support_span_ids"] == []


def test_truth_can_atomically_propose_and_connect_selected_prose(
    client,
    seeded,
    _authenticated_truth_authority,
):
    structured_head, generation = _connectable_projection(seeded)
    request_body = {
        "claim": {
            "proposition": "The fixture includes its original sentence.",
            "claim_kind": "fact",
        },
        "selector": {"exact": DOC_QUOTE},
        "role": "quote",
        "expected_structured_head_sha256": structured_head,
        "expected_ydoc_generation_sha256": generation,
        "expected_projection_sha256": sha256_bytes(DOC_BODY.encode("utf-8")),
    }

    created = client.post(_url(seeded, "/claims"), json=request_body)
    repeated = client.post(_url(seeded, "/claims"), json=request_body)

    assert created.status_code == 201
    created_payload = created.get_json()
    assert created_payload["claim_created"] is True
    assert created_payload["expression_created"] is True
    assert repeated.status_code == 200
    repeated_payload = repeated.get_json()
    assert repeated_payload["claim_id"] == created_payload["claim_id"]
    assert repeated_payload["expression_id"] == created_payload["expression_id"]
    assert repeated_payload["expression_created"] is False

    listed = client.get(_url(seeded)).get_json()
    assert listed["claims"][0]["claim_id"] == created_payload["claim_id"]
    assert listed["claims"][0]["connected_to_document"] is True
    expected_context = truth_api.truth_mutation_context_sha256(
        operation="propose",
        store_id=seeded["store_id"],
        document_id=seeded["document"].id,
        payload={
            "selector": request_body["selector"],
            "role": request_body["role"],
            "expected_structured_head_sha256": structured_head,
            "expected_ydoc_generation_sha256": generation,
            "expected_projection_sha256": request_body[
                "expected_projection_sha256"
            ],
            "claim": request_body["claim"],
            "claim_id": None,
        },
    )
    assert _authenticated_truth_authority[0] == (
        truth_api.TRUTH_PROPOSE_ACTION,
        truth_api.truth_mutation_subject(
            operation="propose",
            store_id=seeded["store_id"],
            document_id=seeded["document"].id,
        ),
        expected_context,
    )
    claim = seeded["store"].get_claim(created_payload["claim_id"])
    assert claim is not None
    authenticated_actor = ActorRef(
        issuer_authority_id="truth-test-issuer",
        subject="truth-reviewer",
        kind="human",
        tenant_scope_id="truth-test-tenant",
    )
    assert claim.created_by_ref == authenticated_actor.canonical_id
    candidate_events = queries.candidate_decisions(
        seeded["store"], claim_id=claim.id
    )
    assert {event.decision for event in candidate_events} == {"add", "connect"}
    assert all(
        validate_actor_ref_json(event.actor_ref_json) == authenticated_actor
        for event in candidate_events
    )
    roles = {
        event.role
        for event in provenance_for_subject(
            seeded["store"], subject_kind="claim", subject_ref=claim.id
        ).events
    }
    assert {"semantic_producer", "candidate_decision_actor"} <= roles


def test_truth_mutation_fails_closed_without_authenticated_local_identity(
    client,
    seeded,
    monkeypatch,
):
    monkeypatch.setattr(
        truth_api,
        "require_human_authority_request",
        lambda **_kwargs: (_ for _ in ()).throw(
            LocalIdentityError(
                "local_session_required",
                "An authenticated local session is required.",
                status=401,
            )
        ),
    )

    response = client.post(
        _url(seeded, "/claims"),
        headers={"X-WB-User-Ref": "spoofed-human"},
        json={
            **_selection_body(seeded),
            "actor": {"kind": "human", "ref": "spoofed-human"},
            "claim": {
                "proposition": "This unauthenticated claim must not be stored.",
                "claim_kind": "fact",
            },
        },
    )

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "local_session_required"
    assert queries.resolve_claim_states(seeded["store"]) == ()


def test_truth_decisions_are_hash_bound_and_project_confirmed_facts(
    client,
    seeded,
):
    claim = seeded["store"].propose_claim(
        proposition="The fixture is a throwaway test document.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    detail_url = _url(seeded, f"/claims/{claim.id}")
    binding = client.get(detail_url).get_json()["decision_binding"]

    stale = client.post(
        _url(seeded, f"/claims/{claim.id}/decisions"),
        json={
            "action": "confirm",
            "expected_canonical_sha256": "0" * 64,
            "expected_context_sha256": binding["context_sha256"],
        },
    )
    confirmed = client.post(
        _url(seeded, f"/claims/{claim.id}/decisions"),
        headers={"X-WB-User-Ref": "truth-reviewer"},
        json={
            "action": "confirm",
            "expected_canonical_sha256": binding["payload_sha256"],
            "expected_context_sha256": binding["context_sha256"],
        },
    )

    assert stale.status_code == 409
    assert stale.get_json()["error"]["code"] == "stale_claim"
    assert confirmed.status_code == 200
    assert confirmed.get_json()["action"] == "confirm"
    current = queries.current_claims(seeded["store"])
    assert [item.claim_id for item in current] == [claim.id]
    lifecycle_events = [
        event
        for event in provenance_for_subject(
            seeded["store"], subject_kind="claim", subject_ref=claim.id
        ).events
        if event.role == "lifecycle_decision_actor"
    ]
    assert len(lifecycle_events) == 1
    assert validate_actor_ref_json(lifecycle_events[0].actor_ref_json).subject == (
        "truth-reviewer"
    )
    facts = client.get(_url(seeded, view="folder", filter="facts")).get_json()
    assert [item["claim_id"] for item in facts["claims"]] == [claim.id]


def test_truth_reads_remain_available_while_mutations_fail_closed_read_only(
    client,
    seeded,
    monkeypatch,
):
    claim = seeded["store"].propose_claim(
        proposition="The read-only surface remains observable.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    monkeypatch.setattr(api, "_is_read_only", lambda: True)

    listed = client.get(_url(seeded, view="folder"))
    blocked = client.post(
        _url(seeded, f"/claims/{claim.id}/decisions"),
        json={},
    )

    assert listed.status_code == 200
    assert listed.get_json()["capabilities"]["can_modify"] is False
    assert blocked.status_code == 403
    assert blocked.get_json()["error"]["code"] == "read_only"


def test_truth_facts_are_current_authoritative_claims_not_fact_kind_labels(
    client,
    seeded,
):
    store = seeded["store"]
    confirmed_preference = store.propose_claim(
        proposition="The reviewer prefers concise headings.",
        claim_kind="preference",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    proposed_fact = store.propose_claim(
        proposition="A fact-shaped proposal is not authoritative yet.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    review_fact = store.propose_claim(
        proposition="A confirmed claim under review is not a current fact.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    past_fact = store.propose_claim(
        proposition="A historically valid claim is not a current fact.",
        claim_kind="fact",
        actor=AGENT,
        valid_to="2020-01-01",
        created_at=NOW,
        status_at=NOW,
    ).claim
    _confirm(client, seeded, confirmed_preference.id)
    _confirm(client, seeded, review_fact.id)
    _confirm(client, seeded, past_fact.id)
    TruthLifecycle(store).mark_needs_review(
        claim_id=review_fact.id,
        actor=Actor("system", "truth-surface-test"),
        basis_kind="sweep",
        basis_ref="truth-surface-regression",
        at="2099-07-17T12:01:00.000+00:00",
    )

    response = client.get(_url(seeded, view="folder", filter="facts"))

    assert response.status_code == 200
    payload = response.get_json()
    assert [item["claim_id"] for item in payload["claims"]] == [
        confirmed_preference.id
    ]
    by_id = {
        item["claim_id"]: item
        for item in client.get(_url(seeded, view="folder")).get_json()["claims"]
    }
    assert by_id[confirmed_preference.id]["is_fact"] is True
    assert by_id[proposed_fact.id]["is_fact"] is False
    assert by_id[review_fact.id]["is_fact"] is False
    assert by_id[past_fact.id]["is_fact"] is False


def test_truth_connection_rejects_a_stale_projection_without_partial_writes(
    client,
    seeded,
):
    body = {
        **_selection_body(seeded),
        "expected_projection_sha256": "0" * 64,
        "claim": {
            "proposition": "This write must roll back.",
            "claim_kind": "fact",
        },
    }

    response = client.post(_url(seeded, "/claims"), json=body)

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "stale_document"
    assert queries.resolve_claim_states(seeded["store"]) == ()


def test_truth_connection_rolls_back_an_invalid_new_claim(
    client,
    seeded,
):
    response = client.post(
        _url(seeded, "/claims"),
        json={
            **_selection_body(seeded),
            "claim": {
                "proposition": "This unsupported claim must not leak a span.",
                "claim_kind": "unsupported-kind",
            },
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_truth_connection"
    assert queries.resolve_claim_states(seeded["store"]) == ()
    assert expressions.expressions_for_document(
        seeded["store"], seeded["document"].id
    ) == ()


def test_truth_connection_revalidates_a_stale_candidate_inside_the_write(
    client,
    seeded,
):
    store = seeded["store"]
    claim = store.propose_claim(
        proposition="This candidate becomes terminal before connection.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    binding = client.get(_url(seeded, f"/claims/{claim.id}")).get_json()[
        "decision_binding"
    ]
    rejected = client.post(
        _url(seeded, f"/claims/{claim.id}/decisions"),
        headers={"X-WB-User-Ref": "truth-reviewer"},
        json={
            "action": "reject",
            "expected_canonical_sha256": binding["payload_sha256"],
            "expected_context_sha256": binding["context_sha256"],
        },
    )
    assert rejected.status_code == 200

    response = client.post(
        _url(seeded, "/connections"),
        json={**_selection_body(seeded), "claim_id": claim.id},
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "claim_not_connectable"
    assert expressions.expressions_for_claim(store, claim.id) == ()


def test_truth_decision_binding_invalidates_when_visible_lifecycle_context_changes(
    client,
    seeded,
):
    store = seeded["store"]
    claim = store.propose_claim(
        proposition="This claim changes after the user reviews it.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    binding = client.get(_url(seeded, f"/claims/{claim.id}")).get_json()[
        "decision_binding"
    ]
    TruthLifecycle(store).mark_needs_review(
        claim_id=claim.id,
        actor=Actor("system", "truth-surface-test"),
        basis_kind="rule",
        basis_ref="context-changed",
        at="2026-07-17T12:01:00.000+00:00",
    )

    response = client.post(
        _url(seeded, f"/claims/{claim.id}/decisions"),
        headers={"X-WB-User-Ref": "truth-reviewer"},
        json={
            "action": "confirm",
            "expected_canonical_sha256": binding["payload_sha256"],
            "expected_context_sha256": binding["context_sha256"],
        },
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "stale_truth_context"
    assert queries.resolve_claim_states(store)[0].base_status == "proposed"


def test_truth_redaction_with_live_editor_tail_keeps_folder_observable(
    client,
    seeded,
):
    claim = seeded["store"].propose_claim(
        proposition="This readable claim will be redacted for privacy.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    detail_url = _url(seeded, f"/claims/{claim.id}")
    binding = client.get(detail_url).get_json()["decision_binding"]
    current_head = ydoc_store.current_structured_head(
        seeded["store"],
        document_id=seeded["document"].id,
        snapshot_sha256=seeded["snapshot_sha256"],
    )
    ydoc_store.append_update_cas(
        seeded["store"],
        document_id=seeded["document"].id,
        update=b"opaque-live-edit-before-redaction",
        snapshot_sha256=seeded["snapshot_sha256"],
        expected_structured_head_sha256=current_head,
    )

    redacted = client.post(
        _url(seeded, f"/claims/{claim.id}/decisions"),
        headers={"X-WB-User-Ref": "truth-reviewer"},
        json={
            "action": "redact",
            "reason": "privacy",
            "expected_canonical_sha256": binding["payload_sha256"],
            "expected_context_sha256": binding["context_sha256"],
        },
    )
    observed = client.get(detail_url)
    catalog = client.get(
        f"/api/truth/doc/list?store_id={seeded['store_id']}"
    )

    assert redacted.status_code == 200
    assert observed.status_code == 200
    assert catalog.status_code == 200
    assert [item["document_id"] for item in catalog.get_json()["docs"]] == [
        seeded["document"].id
    ]
    payload = observed.get_json()
    assert payload["claim"]["redacted"] is True
    assert payload["claim"]["proposition"] is None
    assert payload["claim"]["available_actions"] == []
    assert payload["decision_binding"] is None
    assert payload["status_history"][0]["status"] == "proposed"
    assert not seeded["store"]._pending_redaction_recovery_paths()
    assert not seeded["store"].paths.claims_export.exists()


def test_truth_decision_reports_a_committed_post_commit_failure_as_saved(
    client,
    seeded,
    monkeypatch,
):
    claim = seeded["store"].propose_claim(
        proposition="This claim is committed before recovery fails.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    binding = client.get(_url(seeded, f"/claims/{claim.id}")).get_json()[
        "decision_binding"
    ]

    def fail_post_commit_export(_store: TruthStore) -> None:
        raise PostCommitHookError("simulated post-commit recovery failure")

    monkeypatch.setattr(
        TruthStore,
        "_publish_recovery_export",
        fail_post_commit_export,
    )
    response = client.post(
        _url(seeded, f"/claims/{claim.id}/decisions"),
        headers={"X-WB-User-Ref": "truth-reviewer"},
        json={
            "action": "redact",
            "reason": "privacy",
            "expected_canonical_sha256": binding["payload_sha256"],
            "expected_context_sha256": binding["context_sha256"],
        },
    )

    assert response.status_code == 202
    assert response.get_json() == {
        "ok": True,
        "action": "redact",
        "claim_id": claim.id,
        "status": "committed_with_recovery_warning",
        "warning": {
            "code": "post_commit_recovery_failed",
            "message": (
                "Your decision was saved, but some background recovery work "
                "still needs attention."
            ),
            "retryable": False,
        },
    }
    assert seeded["store"].get_claim(claim.id).proposition == "[redacted]"


def test_truth_retired_documents_stay_observable_but_expose_no_mutations(
    client,
    seeded,
):
    claim = seeded["store"].propose_claim(
        proposition="Retired documents retain visible Truth.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    documents.retire_document(
        seeded["store"],
        document_id=seeded["document"].id,
        actor=HUMAN,
        at="2026-07-17T12:01:00.000+00:00",
    )

    listed = client.get(_url(seeded, view="folder"))
    detail = client.get(_url(seeded, f"/claims/{claim.id}"))

    assert listed.status_code == 200
    assert listed.get_json()["capabilities"]["can_observe"] is True
    assert listed.get_json()["capabilities"]["can_modify"] is False
    assert listed.get_json()["claims"][0]["available_actions"] == []
    assert detail.status_code == 200
    assert detail.get_json()["decision_binding"] is None


def test_truth_connection_boundary_rejects_malformed_claim_targets(
    client,
    seeded,
):
    malformed_claim = client.post(
        _url(seeded, "/claims"),
        json={"claim": "not-an-object"},
    )
    malformed_existing = client.post(
        _url(seeded, "/connections"),
        json={"claim_id": {"not": "an id"}},
    )

    assert malformed_claim.status_code == 400
    assert malformed_claim.get_json()["error"]["code"] == "invalid_claim"
    assert malformed_existing.status_code == 400
    assert malformed_existing.get_json()["error"]["code"] == "claim_id_required"


def test_truth_connection_boundary_rejects_malformed_scalar_fields(
    client,
    seeded,
):
    base = _selection_body(seeded)
    valid_claim = {
        "proposition": "A well-formed claim used only as a request fixture.",
        "claim_kind": "fact",
    }
    cases = (
        (
            {**base, "claim": {**valid_claim, "proposition": []}},
            "invalid_claim",
        ),
        (
            {**base, "claim": {**valid_claim, "scope": {"bad": "scope"}}},
            "invalid_claim",
        ),
        (
            {**base, "claim": valid_claim, "role": ["quote"]},
            "invalid_role",
        ),
        (
            {
                **base,
                "claim": valid_claim,
                "selector": {"exact": [DOC_QUOTE]},
            },
            "invalid_selector",
        ),
    )

    for body, expected_code in cases:
        response = client.post(_url(seeded, "/claims"), json=body)
        assert response.status_code == 400
        assert response.get_json()["error"]["code"] == expected_code

    assert queries.resolve_claim_states(seeded["store"]) == ()
    assert expressions.expressions_for_document(
        seeded["store"], seeded["document"].id
    ) == ()


def test_truth_decision_and_challenge_boundaries_reject_structured_text_fields(
    client,
    seeded,
):
    store = seeded["store"]
    target = store.propose_claim(
        proposition="The challenge target remains unchanged.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    challenger = store.propose_claim(
        proposition="The challenger remains unchanged.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    binding = client.get(_url(seeded, f"/claims/{target.id}")).get_json()[
        "decision_binding"
    ]

    malformed_gesture = client.post(
        _url(seeded, f"/claims/{target.id}/decisions"),
        json={
            "action": "confirm",
            "gesture_kind": {"not": "text"},
            "expected_canonical_sha256": binding["payload_sha256"],
            "expected_context_sha256": binding["context_sha256"],
        },
    )
    malformed_reason = client.post(
        _url(seeded, f"/claims/{target.id}/decisions"),
        json={
            "action": "redact",
            "reason": ["privacy"],
            "expected_canonical_sha256": binding["payload_sha256"],
            "expected_context_sha256": binding["context_sha256"],
        },
    )
    malformed_note = client.post(
        _url(seeded, f"/claims/{target.id}/challenges"),
        json={
            "challenging_claim_id": challenger.id,
            "expected_canonical_sha256": target.canonical_sha256,
            "expected_challenger_sha256": challenger.canonical_sha256,
            "note": {"not": "text"},
        },
    )

    assert malformed_gesture.status_code == 400
    assert malformed_gesture.get_json()["error"]["code"] == "invalid_gesture_kind"
    assert malformed_reason.status_code == 400
    assert malformed_reason.get_json()["error"]["code"] == "invalid_redaction_reason"
    assert malformed_note.status_code == 400
    assert malformed_note.get_json()["error"]["code"] == "invalid_note"
    states = {item.claim_id: item for item in queries.resolve_claim_states(store)}
    assert states[target.id].base_status == "proposed"
    assert states[challenger.id].base_status == "proposed"


def test_truth_challenge_rejects_a_redacted_target_explicitly(
    client,
    seeded,
):
    store = seeded["store"]
    target = store.propose_claim(
        proposition="This target is redacted before a stale challenge arrives.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    challenger = store.propose_claim(
        proposition="This claim must not attach to redacted content.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    binding = client.get(_url(seeded, f"/claims/{target.id}")).get_json()[
        "decision_binding"
    ]
    redacted = client.post(
        _url(seeded, f"/claims/{target.id}/decisions"),
        json={
            "action": "redact",
            "reason": "privacy",
            "expected_canonical_sha256": binding["payload_sha256"],
            "expected_context_sha256": binding["context_sha256"],
        },
    )
    assert redacted.status_code == 200

    response = client.post(
        _url(seeded, f"/claims/{target.id}/challenges"),
        json={
            "challenging_claim_id": challenger.id,
            "expected_canonical_sha256": target.canonical_sha256,
            "expected_challenger_sha256": challenger.canonical_sha256,
        },
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "claim_not_challengeable"


def test_truth_challenge_is_hash_bound_and_records_the_supported_conflict(
    client,
    seeded,
):
    store = seeded["store"]
    target = store.propose_claim(
        proposition="The original account is accurate.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    challenger = store.propose_claim(
        proposition="A supported account contradicts the original.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    _confirm(client, seeded, target.id)
    quote = "independent support for the competing account"
    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///truth-challenge-support.txt",
        actor=HUMAN,
        acquisition_method="paste",
        content=quote,
        created_at=NOW,
        acquired_at=NOW,
    )
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact=quote),
        actor=HUMAN,
        created_at=NOW,
    )
    store.add_link(
        from_claim_id=challenger.id,
        link_type="supports_span",
        to_kind="evidence_span",
        to_ref=span.id,
        actor=HUMAN,
        created_at=NOW,
    )
    challenge_url = _url(seeded, f"/claims/{target.id}/challenges")
    request_body = {
        "challenging_claim_id": challenger.id,
        "expected_canonical_sha256": target.canonical_sha256,
        "expected_challenger_sha256": challenger.canonical_sha256,
        "note": "The supported account should be considered.",
    }

    stale = client.post(
        challenge_url,
        json={**request_body, "expected_challenger_sha256": "0" * 64},
    )
    challenged = client.post(challenge_url, json=request_body)

    assert stale.status_code == 409
    assert stale.get_json()["error"]["code"] == "stale_challenger"
    assert challenged.status_code == 200
    assert challenged.get_json()["action"] == "challenge"
    states = {item.claim_id: item for item in queries.resolve_claim_states(store)}
    assert states[target.id].base_status == "challenged"
    conflict = queries.conflicts(store, claim_id=target.id)
    assert len(conflict) == 1
    assert conflict[0].from_claim_id == challenger.id
    assert conflict[0].to_claim_id == target.id


def test_truth_confirmation_action_must_match_the_current_claim_ceremony(
    client,
    seeded,
):
    store = seeded["store"]
    claim = store.propose_claim(
        proposition="Decision labels must match the actual lifecycle ceremony.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    detail_url = _url(seeded, f"/claims/{claim.id}")
    binding = client.get(detail_url).get_json()["decision_binding"]

    premature_reaffirm = client.post(
        _url(seeded, f"/claims/{claim.id}/decisions"),
        json={
            "action": "reaffirm",
            "expected_canonical_sha256": binding["payload_sha256"],
            "expected_context_sha256": binding["context_sha256"],
        },
    )
    mismatched_gesture = client.post(
        _url(seeded, f"/claims/{claim.id}/decisions"),
        json={
            "action": "confirm",
            "gesture_kind": "reaffirm",
            "expected_canonical_sha256": binding["payload_sha256"],
            "expected_context_sha256": binding["context_sha256"],
        },
    )
    extraneous_reject_gesture = client.post(
        _url(seeded, f"/claims/{claim.id}/decisions"),
        json={
            "action": "reject",
            "gesture_kind": "confirm",
            "expected_canonical_sha256": binding["payload_sha256"],
            "expected_context_sha256": binding["context_sha256"],
        },
    )

    assert premature_reaffirm.status_code == 409
    assert premature_reaffirm.get_json()["error"]["code"] == "invalid_decision_state"
    assert mismatched_gesture.status_code == 400
    assert mismatched_gesture.get_json()["error"]["code"] == "invalid_gesture_kind"
    assert extraneous_reject_gesture.status_code == 400
    assert (
        extraneous_reject_gesture.get_json()["error"]["code"]
        == "invalid_gesture_kind"
    )

    _confirm(client, seeded, claim.id)
    TruthLifecycle(store).mark_needs_review(
        claim_id=claim.id,
        actor=Actor("system", "truth-surface-test"),
        basis_kind="rule",
        basis_ref="reaffirmation-required",
        at=utc_now(),
    )
    review_binding = client.get(detail_url).get_json()["decision_binding"]
    mislabeled_confirm = client.post(
        _url(seeded, f"/claims/{claim.id}/decisions"),
        json={
            "action": "confirm",
            "expected_canonical_sha256": review_binding["payload_sha256"],
            "expected_context_sha256": review_binding["context_sha256"],
        },
    )
    reaffirmed = client.post(
        _url(seeded, f"/claims/{claim.id}/decisions"),
        json={
            "action": "reaffirm",
            "expected_canonical_sha256": review_binding["payload_sha256"],
            "expected_context_sha256": review_binding["context_sha256"],
        },
    )

    assert mislabeled_confirm.status_code == 409
    assert mislabeled_confirm.get_json()["error"]["code"] == "invalid_decision_state"
    assert reaffirmed.status_code == 200
    assert reaffirmed.get_json()["action"] == "reaffirm"


def test_truth_detail_suppresses_quarantined_only_confirmation(
    client,
    seeded,
):
    store = seeded["store"]
    claim = store.propose_claim(
        proposition="An unreviewed external report makes this assertion.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    quote = "unreviewed external report"
    evidence = store.capture_evidence(
        kind="document",
        source_locator="https://example.invalid/report",
        actor=HUMAN,
        acquisition_method="fetch",
        content=quote,
        origin=AcquisitionOrigin.EXTERNAL,
        created_at=NOW,
        acquired_at=NOW,
    )
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact=quote),
        actor=HUMAN,
        created_at=NOW,
    )
    store.add_link(
        from_claim_id=claim.id,
        link_type="supports_span",
        to_kind="evidence_span",
        to_ref=span.id,
        actor=HUMAN,
        created_at=NOW,
    )
    detail_url = _url(seeded, f"/claims/{claim.id}")
    detail = client.get(detail_url).get_json()

    assert detail["support"]["quarantined_only"] is True
    assert "confirm" not in detail["claim"]["available_actions"]
    assert set(detail["claim"]["available_actions"]) == {"reject", "redact"}
    binding = detail["decision_binding"]
    response = client.post(
        _url(seeded, f"/claims/{claim.id}/decisions"),
        json={
            "action": "confirm",
            "expected_canonical_sha256": binding["payload_sha256"],
            "expected_context_sha256": binding["context_sha256"],
        },
    )

    assert response.status_code == 409
    assert (
        response.get_json()["error"]["code"]
        == "quarantined_confirmation_unavailable"
    )
    assert queries.resolve_claim_states(store)[0].base_status == "proposed"

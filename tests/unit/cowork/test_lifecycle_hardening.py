"""Failure-safe two-phase sitting, reimport, retirement, and drift tests."""

from __future__ import annotations

import io
import json

import pytest

from work_buddy.conversations import store as conversation_store
from work_buddy.cowork import (
    bootstrap,
    conversations,
    materialization,
    provenance,
    reimport,
    retirement,
    sitting_lifecycle,
    transport,
)
from work_buddy.cowork.file_importers import MARKDOWN_MAX_SOURCE_BYTES
from work_buddy.cowork.lifecycle_state import inspect_lifecycle_state
from work_buddy.cowork.proposal_applicability import (
    assess_proposal_applicability,
    load_current_projection,
)
from work_buddy.truth import documents, proposals, ydoc_store
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.identity import canonical_json, sha256_bytes

from .conftest import AGENT, HUMAN


def _ready(
    store_ctx,
    *,
    path: str = "docs/lifecycle.md",
    source: bytes = b"# Lifecycle\n\nOriginal sentence.\n",
    key: str = "lifecycle-ready-0001",
):
    store = store_ctx["store"]
    intent, _ = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "create",
            "path": path,
            "title": "Lifecycle",
            "initial_source_sha256": sha256_bytes(source),
            "idempotency_key": key,
        },
        source=source,
        actor=HUMAN,
    )
    snapshot = b"YDOC:" + source
    receipt = bootstrap.commit_bootstrap(
        store,
        bootstrap_id=intent.id,
        snapshot=snapshot,
        source_sha256=intent.source_sha256,
        snapshot_sha256=sha256_bytes(snapshot),
        ydoc_schema=bootstrap.YDOC_SCHEMA,
        actor=HUMAN,
    )
    return documents.get_document(store, receipt["document_id"]), source, snapshot


def _ready_import(
    store_ctx,
    *,
    path: str = "imports/lifecycle.md",
    source: bytes = b"# Lifecycle\n\nOriginal sentence.\n",
    key: str = "lifecycle-import-ready-0001",
):
    store = store_ctx["store"]
    target = store_ctx["root"] / path
    target.parent.mkdir(parents=True)
    target.write_bytes(source)
    intent, _ = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "import",
            "path": path,
            "title": "Imported lifecycle",
            "expected_file_sha256": sha256_bytes(source),
            "idempotency_key": key,
        },
        source=None,
        actor=HUMAN,
    )
    snapshot = b"YDOC:" + source
    receipt = bootstrap.commit_bootstrap(
        store,
        bootstrap_id=intent.id,
        snapshot=snapshot,
        source_sha256=intent.source_sha256,
        snapshot_sha256=sha256_bytes(snapshot),
        ydoc_schema=bootstrap.YDOC_SCHEMA,
        actor=HUMAN,
        projection=source,
        projection_sha256=sha256_bytes(source),
    )
    return documents.get_document(store, receipt["document_id"]), source, snapshot


def _proposal(
    store,
    document,
    head,
    *,
    exact="Original sentence.",
    replacement="Replacement sentence.",
):
    return proposals.propose_edit(
        store,
        document_id=document.id,
        base_content_sha256=document.content_sha256,
        base_structured_head_sha256=head,
        selector=CompositeSelector(exact=exact),
        quote_exact=exact,
        replacement=replacement,
        rationale="Focused lifecycle test.",
        tldr="Replace sentence.",
        actor=AGENT,
    )


def _gesture_count(store) -> int:
    with store._read_connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM gestures").fetchone()[0])


def test_sitting_prepare_is_pure_and_commit_is_atomic_and_idempotent(store_ctx):
    store = store_ctx["store"]
    document, source, old_snapshot = _ready(store_ctx)
    old_head = ydoc_store.current_structured_head(
        store, document_id=document.id, snapshot_sha256=document.ydoc_snapshot_sha256
    )
    proposal = _proposal(store, document, old_head)
    item = {
        "proposal_id": proposal.id,
        "verb": "confirm",
        "canonical_sha256": proposal.canonical_sha256,
    }
    intent, created = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[item],
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=old_head,
        idempotency_key="sitting-atomic-0001",
    )
    assert created and intent.has_apply
    assert _gesture_count(store) == 0
    assert proposals.latest_proposal_status(store, proposal.id).status == "open"
    assert documents.get_document(store, document.id).ydoc_snapshot_sha256 == sha256_bytes(old_snapshot)
    assert (store_ctx["root"] / document.path).read_bytes() == source

    rendered = b"# Lifecycle\n\nReplacement sentence.\n"
    new_snapshot = b"YDOC:" + rendered
    receipt, _ = sitting_lifecycle.commit_sitting(
        store,
        document_id=document.id,
        intent_id=intent.id,
        actor=HUMAN,
        snapshot=new_snapshot,
        snapshot_sha256=sha256_bytes(new_snapshot),
        rendered_markdown=rendered.decode(),
        rendered_sha256=sha256_bytes(rendered),
    )
    assert receipt["results"][0]["result"] == "applied"
    assert _gesture_count(store) == 1
    assert proposals.latest_proposal_status(store, proposal.id).status == "applied"
    refreshed = documents.get_document(store, document.id)
    assert refreshed.ydoc_snapshot_sha256 == sha256_bytes(new_snapshot)
    assert refreshed.content_sha256 == sha256_bytes(rendered)
    assert (store_ctx["root"] / document.path).read_bytes() == rendered
    initial_attestations = store.list_document_provenance_attestations(
        document.id
    )
    assert len(initial_attestations) == 1
    assert receipt["results"][0]["provenance_attestation_ids"] == [
        initial_attestations[0].id
    ]
    pure_replacement = initial_attestations[0]
    assert pure_replacement.authorship_kind == "ai"
    assert pure_replacement.review_status == "not_reviewed"
    with store._read_connection() as conn:
        pure_replacement_span = store._get_document_span_locked(
            conn,
            pure_replacement.document_span_id,
        )
    assert pure_replacement_span is not None
    assert pure_replacement_span.quote_exact == "Replacement sentence."
    projected = provenance.project_attestations(
        store,
        document.id,
        current_structured_head_sha256=receipt["structured_head_sha256"],
    )
    assert projected["spans"][0]["span"]["exact"] == (
        "Replacement sentence."
    )

    repeated, events = sitting_lifecycle.commit_sitting(
        store,
        document_id=document.id,
        intent_id=intent.id,
        actor=HUMAN,
        snapshot=new_snapshot,
        snapshot_sha256=sha256_bytes(new_snapshot),
        rendered_markdown=rendered.decode(),
        rendered_sha256=sha256_bytes(rendered),
    )
    assert canonical_json(repeated) == canonical_json(receipt)
    assert events == receipt["post_commit_events"]
    assert _gesture_count(store) == 1
    assert (
        store.list_document_provenance_attestations(document.id)
        == initial_attestations
    )


def test_confirmed_duplicate_replacement_uses_frozen_application_position(
    store_ctx,
):
    store = store_ctx["store"]
    source = b"# Lifecycle\n\nTarget phrase.\n\nShared replacement.\n"
    document, _source, _old_snapshot = _ready(
        store_ctx,
        source=source,
        path="docs/duplicate-proposal-provenance.md",
        key="duplicate-proposal-provenance-ready-0001",
    )
    old_head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    original = "Target phrase."
    replacement = "Shared replacement."
    start = source.decode().index(original)
    proposal = proposals.propose_edit(
        store,
        document_id=document.id,
        base_content_sha256=document.content_sha256,
        base_structured_head_sha256=old_head,
        selector=CompositeSelector(
            exact=original,
            start=start,
            end=start + len(original),
        ),
        quote_exact=original,
        replacement=replacement,
        rationale="Exercise position-bound proposal provenance.",
        tldr="Use the repeated wording.",
        actor=AGENT,
    )
    intent, _created = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "confirm",
                "canonical_sha256": proposal.canonical_sha256,
            }
        ],
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=old_head,
        idempotency_key="duplicate-proposal-provenance-sitting-0001",
    )
    rendered = source.decode().replace(original, replacement, 1).encode()
    new_snapshot = b"YDOC:" + rendered
    receipt, _events = sitting_lifecycle.commit_sitting(
        store,
        document_id=document.id,
        intent_id=intent.id,
        actor=HUMAN,
        snapshot=new_snapshot,
        snapshot_sha256=sha256_bytes(new_snapshot),
        rendered_markdown=rendered.decode(),
        rendered_sha256=sha256_bytes(rendered),
    )

    assert receipt["results"][0]["result"] == "applied"
    rows = store.list_document_provenance_attestations(document.id)
    assert len(rows) == 1
    with store._read_connection() as conn:
        span = store._get_document_span_locked(
            conn,
            rows[0].document_span_id,
        )
    assert span is not None
    selector = CompositeSelector.from_json(span.selector_json)
    assert selector.exact == replacement
    assert (selector.start, selector.end) == (
        start,
        start + len(replacement),
    )
    assert rendered.decode().find(replacement) == start
    assert rendered.decode().rfind(replacement) > start


def test_confirmed_duplicate_replacement_at_wrong_position_rolls_back(
    store_ctx,
):
    store = store_ctx["store"]
    source = b"# Lifecycle\n\nTarget phrase.\n\nShared replacement.\n"
    document, _source, old_snapshot = _ready(
        store_ctx,
        source=source,
        path="docs/misplaced-proposal-provenance.md",
        key="misplaced-proposal-provenance-ready-0001",
    )
    old_head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    original = "Target phrase."
    replacement = "Shared replacement."
    start = source.decode().index(original)
    proposal = proposals.propose_edit(
        store,
        document_id=document.id,
        base_content_sha256=document.content_sha256,
        base_structured_head_sha256=old_head,
        selector=CompositeSelector(
            exact=original,
            start=start,
            end=start + len(original),
        ),
        quote_exact=original,
        replacement=replacement,
        rationale="Exercise position-bound proposal provenance.",
        tldr="Use the repeated wording.",
        actor=AGENT,
    )
    intent, _created = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "confirm",
                "canonical_sha256": proposal.canonical_sha256,
            }
        ],
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=old_head,
        idempotency_key="misplaced-proposal-provenance-sitting-0001",
    )
    # The replacement exists, but only at the pre-existing second occurrence;
    # accepting this payload must never attribute that unrelated text to AI.
    malicious_snapshot = b"YDOC:MISPLACED:" + source
    with pytest.raises(sitting_lifecycle.SittingError) as failure:
        sitting_lifecycle.commit_sitting(
            store,
            document_id=document.id,
            intent_id=intent.id,
            actor=HUMAN,
            snapshot=malicious_snapshot,
            snapshot_sha256=sha256_bytes(malicious_snapshot),
            rendered_markdown=source.decode(),
            rendered_sha256=sha256_bytes(source),
        )

    assert failure.value.code == "proposal_provenance_unsafe"
    assert proposals.latest_proposal_status(store, proposal.id).status == "open"
    assert _gesture_count(store) == 0
    assert store.list_document_provenance_attestations(document.id) == ()
    refreshed = documents.get_document(store, document.id)
    assert refreshed.ydoc_snapshot_sha256 == sha256_bytes(old_snapshot)
    assert (store_ctx["root"] / document.path).read_bytes() == source


def test_confirmed_agent_insertion_records_only_new_text_in_provenance_get(
    store_ctx,
    client,
):
    store = store_ctx["store"]
    document, _source, _old_snapshot = _ready(
        store_ctx,
        path="docs/proposal-provenance.md",
        key="proposal-provenance-ready-0001",
    )
    old_head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    claim = store.propose_claim(
        proposition="The inserted sentence demonstrates AI provenance.",
        claim_kind="fact",
        actor=AGENT,
    ).claim
    ai_text = "This sentence was generated to demonstrate AI provenance."
    replacement = f"{ai_text}\n\nOriginal sentence."
    proposal = proposals.propose_edit(
        store,
        document_id=document.id,
        base_content_sha256=document.content_sha256,
        base_structured_head_sha256=old_head,
        selector=CompositeSelector(exact="Original sentence."),
        quote_exact="Original sentence.",
        replacement=replacement,
        rationale="Demonstrate accepted AI-authored provenance.",
        tldr="Insert an AI provenance demonstration.",
        claim_refs=[{"claim": claim.id, "role": "instantiation"}],
        actor=AGENT,
    )
    intent, created = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "confirm",
                "canonical_sha256": proposal.canonical_sha256,
            }
        ],
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=old_head,
        idempotency_key="proposal-provenance-sitting-0001",
    )
    assert created is True

    rendered = f"# Lifecycle\n\n{replacement}\n".encode()
    new_snapshot = b"YDOC:" + rendered
    receipt, events = sitting_lifecycle.commit_sitting(
        store,
        document_id=document.id,
        intent_id=intent.id,
        actor=HUMAN,
        snapshot=new_snapshot,
        snapshot_sha256=sha256_bytes(new_snapshot),
        rendered_markdown=rendered.decode(),
        rendered_sha256=sha256_bytes(rendered),
    )

    result = receipt["results"][0]
    assert result["result"] == "applied"
    assert len(result["provenance_attestation_ids"]) == 1
    assert any(
        event["event_type"] == "truth.doc_provenance_attested"
        for event in events
    )
    rows = store.list_document_provenance_attestations(document.id)
    assert len(rows) == 1
    attestation = rows[0]
    assert attestation.id == result["provenance_attestation_ids"][0]
    assert attestation.target_structured_head_sha256 == receipt[
        "structured_head_sha256"
    ]
    assert attestation.authorship_kind == "ai"
    assert attestation.review_status == "not_reviewed"
    assert json.loads(attestation.human_reviewers_json) == []
    assert attestation.source_kind == "proposal_acceptance"
    assert attestation.basis_kind == "proposal_acceptance"
    assert attestation.basis_ref == result["gesture_id"]
    source = json.loads(attestation.source_json)
    assert source["proposal_id"] == proposal.id
    assert source["acceptance_gesture_id"] == result["gesture_id"]
    assert source["producer"] == {
        "kind": "agent_run",
        "ref": AGENT.ref,
        "model": "test-model",
        "harness": "pytest",
        "surface": "cowork",
        "session_id": "session-1",
    }

    with store._read_connection() as conn:
        exact_span = store._get_document_span_locked(
            conn,
            attestation.document_span_id,
        )
        expression_spans = conn.execute(
            "SELECT s.quote_exact, s.author_kind, s.author_ref "
            "FROM expressions e JOIN document_spans s "
            "ON s.id = e.document_span_id"
        ).fetchall()
    assert exact_span is not None
    assert exact_span.quote_exact == ai_text
    assert exact_span.author_kind == "agent_run"
    assert exact_span.author_ref == AGENT.ref
    # The expression may cover the complete accepted passage, but the
    # preserved original is never exposed as wholly AI-authored provenance.
    assert [dict(row) for row in expression_spans] == [
        {
            "quote_exact": replacement,
            "author_kind": "unknown",
            "author_ref": None,
        }
    ]

    response = client.get(
        f"/api/truth/doc/{document.id}?store_id={store.store_id}"
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["provenance"]["summary"]["ai_unreviewed_count"] == 1
    assert len(payload["provenance"]["spans"]) == 1
    target = payload["provenance"]["spans"][0]
    assert target["target"]["currentness"] == "current"
    assert target["span"]["exact"] == ai_text
    assert target["effective_attestation"]["attestation_id"] == attestation.id
    assert target["effective_attestation"]["authorship"] == {
        "kind": "ai",
        "contributors": [],
    }
    assert target["effective_attestation"]["human_review"] == {
        "status": "not_reviewed",
        "reviewers": [],
    }
    legacy_quotes = {
        entry["quote"] for entry in payload["provenance_spans"]
    }
    assert legacy_quotes == {ai_text}


def test_confirmed_sandwich_insertion_records_two_ai_only_segments(store_ctx):
    store = store_ctx["store"]
    document, _source, _old_snapshot = _ready(
        store_ctx,
        path="docs/sandwich-proposal-provenance.md",
        key="sandwich-proposal-provenance-ready-0001",
    )
    old_head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    prefix = "AI-authored preface."
    suffix = "AI-authored follow-up."
    replacement = f"{prefix}\n\nOriginal sentence.\n\n{suffix}"
    proposal = _proposal(
        store,
        document,
        old_head,
        replacement=replacement,
    )
    intent, _created = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "confirm",
                "canonical_sha256": proposal.canonical_sha256,
            }
        ],
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=old_head,
        idempotency_key="sandwich-proposal-provenance-sitting-0001",
    )
    rendered = f"# Lifecycle\n\n{replacement}\n".encode()
    new_snapshot = b"YDOC:" + rendered
    receipt, _events = sitting_lifecycle.commit_sitting(
        store,
        document_id=document.id,
        intent_id=intent.id,
        actor=HUMAN,
        snapshot=new_snapshot,
        snapshot_sha256=sha256_bytes(new_snapshot),
        rendered_markdown=rendered.decode(),
        rendered_sha256=sha256_bytes(rendered),
    )

    result = receipt["results"][0]
    rows = store.list_document_provenance_attestations(document.id)
    assert result["provenance_attestation_ids"] == [row.id for row in rows]
    assert len(rows) == 2
    assert {
        row.target_structured_head_sha256 for row in rows
    } == {receipt["structured_head_sha256"]}
    assert {row.basis_ref for row in rows} == {result["gesture_id"]}
    assert [
        json.loads(row.source_json)["segment_index"] for row in rows
    ] == [0, 1]
    assert {
        json.loads(row.source_json)["segment_count"] for row in rows
    } == {2}
    with store._read_connection() as conn:
        exacts = [
            store._get_document_span_locked(conn, row.document_span_id).quote_exact
            for row in rows
        ]
    assert exacts == [prefix, suffix]
    assert "Original sentence." not in exacts

    projected = provenance.project_attestations(
        store,
        document.id,
        current_structured_head_sha256=receipt["structured_head_sha256"],
    )
    projected_segments = sorted(
        projected["spans"],
        key=lambda target: target["effective_attestation"]["source"][
            "segment_index"
        ],
    )
    assert [target["span"]["exact"] for target in projected_segments] == [
        prefix,
        suffix,
    ]


def test_edit_confirm_human_amendment_does_not_claim_ai_authorship(store_ctx):
    store = store_ctx["store"]
    document, _source, _old_snapshot = _ready(
        store_ctx,
        path="docs/amended-proposal-provenance.md",
        key="amended-proposal-provenance-ready-0001",
    )
    old_head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    proposal = _proposal(store, document, old_head)
    human_amendment = "The human supplied this final sentence."
    intent, _created = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "edit_confirm",
                "canonical_sha256": proposal.canonical_sha256,
                "amend_content": human_amendment,
            }
        ],
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=old_head,
        idempotency_key="amended-proposal-provenance-sitting-0001",
    )
    rendered = f"# Lifecycle\n\n{human_amendment}\n".encode()
    new_snapshot = b"YDOC:" + rendered
    receipt, _events = sitting_lifecycle.commit_sitting(
        store,
        document_id=document.id,
        intent_id=intent.id,
        actor=HUMAN,
        snapshot=new_snapshot,
        snapshot_sha256=sha256_bytes(new_snapshot),
        rendered_markdown=rendered.decode(),
        rendered_sha256=sha256_bytes(rendered),
    )

    result = receipt["results"][0]
    assert result["result"] == "applied"
    assert "provenance_attestation_ids" not in result
    assert store.list_document_provenance_attestations(document.id) == ()


def test_ambiguous_preserved_quote_rolls_back_acceptance_and_provenance(
    store_ctx,
):
    store = store_ctx["store"]
    document, source, old_snapshot = _ready(
        store_ctx,
        path="docs/ambiguous-proposal-provenance.md",
        key="ambiguous-proposal-provenance-ready-0001",
    )
    old_head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    replacement = (
        "AI preface. Original sentence. AI bridge. Original sentence."
    )
    proposal = _proposal(
        store,
        document,
        old_head,
        replacement=replacement,
    )
    intent, _created = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "confirm",
                "canonical_sha256": proposal.canonical_sha256,
            }
        ],
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=old_head,
        idempotency_key="ambiguous-proposal-provenance-sitting-0001",
    )
    rendered = f"# Lifecycle\n\n{replacement}\n".encode()
    new_snapshot = b"YDOC:" + rendered

    with pytest.raises(
        sitting_lifecycle.SittingError,
        match="cannot be identified without guessing",
    ) as failure:
        sitting_lifecycle.commit_sitting(
            store,
            document_id=document.id,
            intent_id=intent.id,
            actor=HUMAN,
            snapshot=new_snapshot,
            snapshot_sha256=sha256_bytes(new_snapshot),
            rendered_markdown=rendered.decode(),
            rendered_sha256=sha256_bytes(rendered),
        )

    assert failure.value.code == "proposal_provenance_unsafe"
    assert proposals.latest_proposal_status(store, proposal.id).status == "open"
    assert _gesture_count(store) == 0
    assert store.list_document_provenance_attestations(document.id) == ()
    refreshed = documents.get_document(store, document.id)
    assert refreshed.ydoc_snapshot_sha256 == sha256_bytes(old_snapshot)
    assert (store_ctx["root"] / document.path).read_bytes() == source


def test_detached_sitting_replaces_exact_durable_update_tail(store_ctx):
    store = store_ctx["store"]
    document, source, _old_snapshot = _ready_import(store_ctx)
    base_head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    _cursor, edited_head = ydoc_store.append_update_cas(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
        update=b"durable-human-edit-before-review",
        expected_structured_head_sha256=base_head,
    )
    proposal = _proposal(store, document, edited_head)
    intent, created = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "confirm",
                "canonical_sha256": proposal.canonical_sha256,
            }
        ],
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=edited_head,
        idempotency_key="detached-sitting-after-edit-0001",
    )
    assert created and intent.has_apply

    rendered = b"# Lifecycle\n\nReplacement sentence.\n"
    replacement_snapshot = b"YDOC:" + rendered
    receipt, _events = sitting_lifecycle.commit_sitting(
        store,
        document_id=document.id,
        intent_id=intent.id,
        actor=HUMAN,
        snapshot=replacement_snapshot,
        snapshot_sha256=sha256_bytes(replacement_snapshot),
        rendered_markdown=rendered.decode(),
        rendered_sha256=sha256_bytes(rendered),
    )

    refreshed = documents.get_document(store, document.id)
    assert receipt["results"][0]["result"] == "applied"
    assert receipt["source_writeback"] == "never"
    assert refreshed.ydoc_snapshot_sha256 == sha256_bytes(replacement_snapshot)
    assert refreshed.content_sha256 == sha256_bytes(rendered)
    assert not ydoc_store.update_tail_present(store, document_id=document.id)
    assert (store_ctx["root"] / document.path).read_bytes() == source


def test_file_backed_sitting_replaces_exact_durable_update_tail(store_ctx):
    store = store_ctx["store"]
    document, _source, _old_snapshot = _ready(
        store_ctx,
        path="docs/review-after-edit.md",
        key="file-backed-sitting-after-edit-ready-0001",
    )
    base_head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    _cursor, edited_head = ydoc_store.append_update_cas(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
        update=b"durable-human-edit-before-file-backed-review",
        expected_structured_head_sha256=base_head,
    )
    proposal = _proposal(store, document, edited_head)
    intent, created = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "confirm",
                "canonical_sha256": proposal.canonical_sha256,
            }
        ],
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=edited_head,
        idempotency_key="file-backed-sitting-after-edit-0001",
    )
    assert created and intent.has_apply

    rendered = b"# Lifecycle\n\nReplacement sentence.\n"
    replacement_snapshot = b"YDOC:" + rendered
    receipt, _events = sitting_lifecycle.commit_sitting(
        store,
        document_id=document.id,
        intent_id=intent.id,
        actor=HUMAN,
        snapshot=replacement_snapshot,
        snapshot_sha256=sha256_bytes(replacement_snapshot),
        rendered_markdown=rendered.decode(),
        rendered_sha256=sha256_bytes(rendered),
    )

    refreshed = documents.get_document(store, document.id)
    assert receipt["results"][0]["result"] == "applied"
    assert refreshed.ydoc_snapshot_sha256 == sha256_bytes(replacement_snapshot)
    assert refreshed.content_sha256 == sha256_bytes(rendered)
    assert not ydoc_store.update_tail_present(store, document_id=document.id)
    assert (store_ctx["root"] / document.path).read_bytes() == rendered


def test_sitting_edit_confirm_admits_and_commits_empty_deletion(store_ctx):
    store = store_ctx["store"]
    document, _source, _ = _ready(
        store_ctx,
        key="lifecycle-ready-amended-deletion",
    )
    old_head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    proposal = _proposal(store, document, old_head)
    intent, created = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "edit_confirm",
                "canonical_sha256": proposal.canonical_sha256,
                "amend_content": "",
            }
        ],
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=old_head,
        idempotency_key="sitting-amended-deletion-0001",
    )

    assert created is True
    assert intent.admitted[0]["item"]["amend_content"] == ""
    assert intent.failed == ()
    rendered = b"# Lifecycle\n\n"
    new_snapshot = b"YDOC:" + rendered
    receipt, _ = sitting_lifecycle.commit_sitting(
        store,
        document_id=document.id,
        intent_id=intent.id,
        actor=HUMAN,
        snapshot=new_snapshot,
        snapshot_sha256=sha256_bytes(new_snapshot),
        rendered_markdown=rendered.decode(),
        rendered_sha256=sha256_bytes(rendered),
    )

    assert receipt["results"][0]["result"] == "applied"
    assert (store_ctx["root"] / document.path).read_bytes() == rendered


def test_sitting_reanchors_after_an_unrelated_structured_edit(store_ctx):
    store = store_ctx["store"]
    document, _source, _ = _ready(
        store_ctx,
        key="lifecycle-ready-reanchored-proposal",
    )
    old_head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    proposal = _proposal(store, document, old_head)
    current_projection = b"# Lifecycle\n\nUnrelated preface. Original sentence.\n"
    current_snapshot = b"YDOC:" + current_projection
    pushed, status = transport.push_ydoc(
        store,
        document,
        HUMAN,
        body=transport.frame_segments([b"", current_snapshot, current_projection]),
        base_structured_head_sha256=old_head,
        base_ydoc_generation=documents.current_ydoc_generation(store, document.id),
        compacted_snapshot_sha256=sha256_bytes(current_snapshot),
        compacted_projection_sha256=sha256_bytes(current_projection),
    )
    assert status == 200
    current_head = pushed["structured_head_sha256"]

    intent, created = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "confirm",
                "canonical_sha256": proposal.canonical_sha256,
            }
        ],
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=current_head,
        idempotency_key="sitting-reanchored-proposal-0001",
    )

    assert created is True
    assert intent.failed == ()
    assert intent.admitted[0]["item"]["_applicability"]["reason"] == "reanchored"

    rendered = b"# Lifecycle\n\nUnrelated preface. Replacement sentence.\n"
    final_snapshot = b"YDOC:" + rendered
    receipt, _ = sitting_lifecycle.commit_sitting(
        store,
        document_id=document.id,
        intent_id=intent.id,
        actor=HUMAN,
        snapshot=final_snapshot,
        snapshot_sha256=sha256_bytes(final_snapshot),
        rendered_markdown=rendered.decode(),
        rendered_sha256=sha256_bytes(rendered),
    )

    assert receipt["results"][0]["result"] == "applied"
    projection_receipt = ydoc_store.current_projection_receipt(
        store,
        document_id=document.id,
    )
    assert projection_receipt is not None
    assert projection_receipt.projection_sha256 == sha256_bytes(rendered)
    assert (store_ctx["root"] / document.path).read_bytes() == rendered


def test_accepting_one_imported_proposal_keeps_other_targets_verifiable(store_ctx):
    store = store_ctx["store"]
    source = b"# Lifecycle\n\nFirst target. Second target.\n"
    document, _source, snapshot = _ready_import(
        store_ctx,
        source=source,
        key="lifecycle-import-proposal-receipt-0001",
    )
    initial_head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    first = _proposal(
        store,
        document,
        initial_head,
        exact="First target.",
        replacement="First replacement.",
    )
    second = _proposal(
        store,
        document,
        initial_head,
        exact="Second target.",
        replacement="Second replacement.",
    )
    pushed, status = transport.push_ydoc(
        store,
        document,
        HUMAN,
        body=transport.frame_segments([b"", snapshot, source]),
        base_structured_head_sha256=initial_head,
        base_ydoc_generation=documents.current_ydoc_generation(store, document.id),
        compacted_snapshot_sha256=sha256_bytes(snapshot),
        compacted_projection_sha256=sha256_bytes(source),
    )
    assert status == 200

    intent, created = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[
            {
                "proposal_id": first.id,
                "verb": "confirm",
                "canonical_sha256": first.canonical_sha256,
            }
        ],
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=pushed["structured_head_sha256"],
        idempotency_key="sitting-import-proposal-receipt-0001",
    )
    assert created is True

    rendered = b"# Lifecycle\n\nFirst replacement. Second target.\n"
    final_snapshot = b"YDOC:" + rendered
    receipt, _events = sitting_lifecycle.commit_sitting(
        store,
        document_id=document.id,
        intent_id=intent.id,
        actor=HUMAN,
        snapshot=final_snapshot,
        snapshot_sha256=sha256_bytes(final_snapshot),
        rendered_markdown=rendered.decode(),
        rendered_sha256=sha256_bytes(rendered),
    )
    refreshed = documents.get_document(store, document.id)
    projection_receipt = ydoc_store.current_projection_receipt(
        store,
        document_id=document.id,
    )
    current_projection, projection_reason = load_current_projection(
        store,
        refreshed,
        structured_head_sha256=receipt["structured_head_sha256"],
    )
    applicability = assess_proposal_applicability(
        second,
        refreshed,
        structured_head_sha256=receipt["structured_head_sha256"],
        current_projection=current_projection,
        projection_unavailable_reason=projection_reason,
    )

    assert receipt["results"][0]["result"] == "applied"
    assert projection_receipt is not None
    assert projection_receipt.projection_sha256 == sha256_bytes(rendered)
    assert projection_receipt.ydoc_snapshot_sha256 == sha256_bytes(final_snapshot)
    assert projection_reason == "available"
    assert current_projection is not None
    assert current_projection.text == rendered.decode()
    assert (applicability.status, applicability.reason) == (
        "applicable",
        "reanchored",
    )
    materialized_version = documents.current_document_version(store, document.id)
    assert materialized_version is not None
    assert materialized_version.kind == "materialized"

    # Reproduce the state written by snapshot replacement before it carried
    # projection receipts: the new epoch survived, but the receipt did not.
    ydoc_store._write_epoch(
        store,
        document.id,
        ydoc_store._read_epoch(store, document.id),
    )
    assert ydoc_store.current_projection_receipt(
        store,
        document_id=document.id,
    ) is None
    recovered_projection, recovered_reason = load_current_projection(
        store,
        refreshed,
        structured_head_sha256=receipt["structured_head_sha256"],
    )
    recovered_applicability = assess_proposal_applicability(
        second,
        refreshed,
        structured_head_sha256=receipt["structured_head_sha256"],
        current_projection=recovered_projection,
        projection_unavailable_reason=recovered_reason,
    )

    assert recovered_reason == "available"
    assert recovered_projection is not None
    assert recovered_projection.text == rendered.decode()
    assert (
        recovered_applicability.status,
        recovered_applicability.reason,
    ) == ("applicable", "reanchored")


def test_cancelled_batch_after_sequential_accept_can_commit_safe_subset(store_ctx):
    """A client-side batch preparation failure never consumes admitted marks.

    The browser prepares its replacement snapshot only after the server admits a
    sitting.  If that local preparation cannot place one target, it may cancel
    the pure prepared intent and submit the placeable subset under a new key.
    This covers the exact sequential-then-batch shape used by Review, including
    a blockquote hard-break target whose Markdown selector remains server-valid.
    """

    store = store_ctx["store"]
    source = (
        b"# Lifecycle\r\n\r\n"
        b"> First  \r\n> target. Strain of  \r\n> working this way. "
        b"Tail  \r\n> target.\r\n"
    )
    document, _source, snapshot = _ready_import(
        store_ctx,
        source=source,
        key="lifecycle-import-sequential-batch-0001",
    )
    initial_head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    first = _proposal(
        store,
        document,
        initial_head,
        exact="First  \r\n> target.",
        replacement="First target.",
    )
    blockquote_hard_break = _proposal(
        store,
        document,
        initial_head,
        exact="of  \r\n> working this way.",
        replacement="of working this way.",
    )
    tail = _proposal(
        store,
        document,
        initial_head,
        exact="Tail  \r\n> target.",
        replacement="Tail target.",
    )
    pushed, status = transport.push_ydoc(
        store,
        document,
        HUMAN,
        body=transport.frame_segments([b"", snapshot, source]),
        base_structured_head_sha256=initial_head,
        base_ydoc_generation=documents.current_ydoc_generation(store, document.id),
        compacted_snapshot_sha256=sha256_bytes(snapshot),
        compacted_projection_sha256=sha256_bytes(source),
    )
    assert status == 200

    first_intent, _ = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[
            {
                "proposal_id": first.id,
                "verb": "confirm",
                "canonical_sha256": first.canonical_sha256,
            }
        ],
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=pushed["structured_head_sha256"],
        idempotency_key="sitting-sequential-before-batch-0001",
    )
    after_first = source.replace(b"First  \r\n> target.", b"First target.")
    after_first_snapshot = b"YDOC:" + after_first
    first_receipt, _ = sitting_lifecycle.commit_sitting(
        store,
        document_id=document.id,
        intent_id=first_intent.id,
        actor=HUMAN,
        snapshot=after_first_snapshot,
        snapshot_sha256=sha256_bytes(after_first_snapshot),
        rendered_markdown=after_first.decode(),
        rendered_sha256=sha256_bytes(after_first),
    )
    assert first_receipt["results"][0]["result"] == "applied"
    assert _gesture_count(store) == 1

    refreshed = documents.get_document(store, document.id)
    batch_intent, _ = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[
            {
                "proposal_id": blockquote_hard_break.id,
                "verb": "confirm",
                "canonical_sha256": blockquote_hard_break.canonical_sha256,
            },
            {
                "proposal_id": tail.id,
                "verb": "confirm",
                "canonical_sha256": tail.canonical_sha256,
            },
        ],
        expected_file_sha256=refreshed.content_sha256,
        expected_structured_head_sha256=first_receipt["structured_head_sha256"],
        idempotency_key="sitting-batch-client-prepare-0001",
    )

    assert len(batch_intent.admitted) == 2
    assert batch_intent.failed == ()
    ranges = [
        (
            entry["item"]["_applicability"]["resolved_start"],
            entry["item"]["_applicability"]["resolved_end"],
        )
        for entry in batch_intent.admitted
    ]
    assert all(
        entry["item"]["_applicability"]["reason"] == "reanchored"
        for entry in batch_intent.admitted
    )
    assert ranges[0][1] <= ranges[1][0]

    cancelled = sitting_lifecycle.cancel_sitting(
        store,
        document_id=document.id,
        intent_id=batch_intent.id,
        actor=HUMAN,
    )
    assert cancelled["state"] == "cancelled"
    assert _gesture_count(store) == 1
    assert (
        proposals.latest_proposal_status(store, blockquote_hard_break.id).status
        == "open"
    )
    assert proposals.latest_proposal_status(store, tail.id).status == "open"

    safe_intent, _ = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[
            {
                "proposal_id": tail.id,
                "verb": "confirm",
                "canonical_sha256": tail.canonical_sha256,
            }
        ],
        expected_file_sha256=refreshed.content_sha256,
        expected_structured_head_sha256=first_receipt["structured_head_sha256"],
        idempotency_key="sitting-batch-safe-subset-0001",
    )
    after_safe_subset = after_first.replace(
        b"Tail  \r\n> target.", b"Tail target."
    )
    safe_snapshot = b"YDOC:" + after_safe_subset
    safe_receipt, _ = sitting_lifecycle.commit_sitting(
        store,
        document_id=document.id,
        intent_id=safe_intent.id,
        actor=HUMAN,
        snapshot=safe_snapshot,
        snapshot_sha256=sha256_bytes(safe_snapshot),
        rendered_markdown=after_safe_subset.decode(),
        rendered_sha256=sha256_bytes(after_safe_subset),
    )

    assert safe_receipt["results"][0]["result"] == "applied"
    assert _gesture_count(store) == 2
    assert proposals.latest_proposal_status(store, tail.id).status == "applied"
    assert (
        proposals.latest_proposal_status(store, blockquote_hard_break.id).status
        == "open"
    )


def test_sitting_file_race_leaves_ledger_and_structured_state_unchanged(store_ctx):
    store = store_ctx["store"]
    document, _source, _ = _ready(store_ctx, key="lifecycle-ready-race")
    old_head = ydoc_store.current_structured_head(
        store, document_id=document.id, snapshot_sha256=document.ydoc_snapshot_sha256
    )
    proposal = _proposal(store, document, old_head)
    intent, _ = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[{"proposal_id": proposal.id, "verb": "confirm", "canonical_sha256": proposal.canonical_sha256}],
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=old_head,
        idempotency_key="sitting-race-0001",
    )
    external = b"# External writer won\n"
    (store_ctx["root"] / document.path).write_bytes(external)
    new_snapshot = b"new snapshot"
    with pytest.raises(sitting_lifecycle.SittingError, match="outside Co-work") as caught:
        sitting_lifecycle.commit_sitting(
            store,
            document_id=document.id,
            intent_id=intent.id,
            actor=HUMAN,
            snapshot=new_snapshot,
            snapshot_sha256=sha256_bytes(new_snapshot),
            rendered_markdown="# Accepted\n",
            rendered_sha256=sha256_bytes(b"# Accepted\n"),
        )
    assert caught.value.code == "stale_file"
    assert (store_ctx["root"] / document.path).read_bytes() == external
    assert documents.get_document(store, document.id) == document
    assert proposals.latest_proposal_status(store, proposal.id).status == "open"
    assert _gesture_count(store) == 0


def test_sitting_partial_prepare_commits_only_admitted_decisions(store_ctx):
    store = store_ctx["store"]
    document, _source, _ = _ready(store_ctx, key="lifecycle-ready-partial")
    head = ydoc_store.current_structured_head(
        store, document_id=document.id, snapshot_sha256=document.ydoc_snapshot_sha256
    )
    accepted = _proposal(store, document, head)
    failed = _proposal(store, document, head, replacement="Another replacement.")
    intent, _ = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[
            {"proposal_id": accepted.id, "verb": "reject_plain", "canonical_sha256": accepted.canonical_sha256},
            {"proposal_id": failed.id, "verb": "reject_plain", "canonical_sha256": "0" * 64},
        ],
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=head,
        idempotency_key="sitting-partial-0001",
    )
    assert len(intent.admitted) == 1 and len(intent.failed) == 1
    receipt, _ = sitting_lifecycle.commit_sitting(
        store,
        document_id=document.id,
        intent_id=intent.id,
        actor=HUMAN,
    )
    assert receipt["partial"] is True
    assert [item["result"] for item in receipt["results"]] == ["closed", "rejected_stale_view"]
    assert proposals.latest_proposal_status(store, accepted.id).status == "closed"
    assert proposals.latest_proposal_status(store, failed.id).status == "open"
    assert _gesture_count(store) == 1


def test_reimport_replaces_snapshot_without_writing_source_and_stales_proposals(store_ctx):
    store = store_ctx["store"]
    document, _source, _ = _ready(store_ctx, key="lifecycle-ready-reimport")
    generation_before = documents.current_ydoc_generation(store, document.id)
    head = ydoc_store.current_structured_head(
        store, document_id=document.id, snapshot_sha256=document.ydoc_snapshot_sha256
    )
    proposal = _proposal(store, document, head)
    external = b"# External revision\r\n\r\nExact bytes.\r\n"
    target = store_ctx["root"] / document.path
    target.write_bytes(external)
    intent, _ = reimport.prepare_reimport(
        store,
        document_id=document.id,
        actor=HUMAN,
        idempotency_key="reimport-replace-0001",
    )
    replacement_snapshot = b"YDOC-EXTERNAL:" + external
    receipt = reimport.commit_reimport(
        store,
        document_id=document.id,
        intent_id=intent.id,
        actor=HUMAN,
        replacement_snapshot=replacement_snapshot,
        replacement_snapshot_sha256=sha256_bytes(replacement_snapshot),
    )
    assert target.read_bytes() == external
    assert "proposal_count" not in receipt
    assert receipt["staled_proposal_ids"] == [proposal.id]
    assert proposals.latest_proposal_status(store, proposal.id).status == "expired"
    refreshed = documents.get_document(store, document.id)
    assert refreshed.content_sha256 == sha256_bytes(external)
    assert refreshed.ydoc_snapshot_sha256 == sha256_bytes(replacement_snapshot)
    assert documents.current_document_version(store, document.id).kind == "reimported"
    generation_after = documents.current_ydoc_generation(store, document.id)
    assert generation_after != generation_before
    assert reimport.commit_reimport(
        store,
        document_id=document.id,
        intent_id=intent.id,
        actor=HUMAN,
        replacement_snapshot=replacement_snapshot,
        replacement_snapshot_sha256=sha256_bytes(replacement_snapshot),
    ) == receipt
    assert documents.current_ydoc_generation(store, document.id) == generation_after


def test_reimport_blocks_unmaterialized_structured_tail(store_ctx):
    store = store_ctx["store"]
    document, _source, _ = _ready(store_ctx, key="lifecycle-ready-tail")
    head = ydoc_store.current_structured_head(
        store, document_id=document.id, snapshot_sha256=document.ydoc_snapshot_sha256
    )
    ydoc_store.append_update_cas(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
        update=b"pending update",
        expected_structured_head_sha256=head,
    )
    (store_ctx["root"] / document.path).write_bytes(b"# External revision\n")
    with pytest.raises(reimport.ReimportError) as caught:
        reimport.prepare_reimport(
            store,
            document_id=document.id,
            actor=HUMAN,
            idempotency_key="reimport-tail-0001",
        )
    assert caught.value.code == "unmaterialized_structured_edits"


def test_reimport_rejects_detached_sources_at_prepare_and_commit(
    store_ctx,
    monkeypatch,
):
    store = store_ctx["store"]
    document, source, _ = _ready_import(
        store_ctx,
        path="imports/no-refresh.md",
        key="detached-no-refresh-import-0001",
    )
    target = store_ctx["root"] / document.path
    changed_source = b"# Changed external source\n"
    target.write_bytes(changed_source)

    with pytest.raises(reimport.ReimportError) as prepare_error:
        reimport.prepare_reimport(
            store,
            document_id=document.id,
            actor=HUMAN,
            idempotency_key="detached-no-refresh-prepare-0001",
        )
    assert prepare_error.value.code == "source_writeback_forbidden"
    assert prepare_error.value.status == 409

    # Simulate a legacy prepared intent that predates the migration which
    # marked imports as detached. Commit repeats the guard and cannot refresh
    # the managed copy even when such an intent survives.
    original_source_is_detached = documents.source_is_detached
    monkeypatch.setattr(documents, "source_is_detached", lambda _document: False)
    legacy_intent, _ = reimport.prepare_reimport(
        store,
        document_id=document.id,
        actor=HUMAN,
        idempotency_key="detached-no-refresh-legacy-0001",
    )
    monkeypatch.setattr(
        documents,
        "source_is_detached",
        original_source_is_detached,
    )
    replacement_snapshot = b"opaque-forbidden-detached-refresh"
    replacement_sha256 = sha256_bytes(replacement_snapshot)
    with pytest.raises(reimport.ReimportError) as commit_error:
        reimport.commit_reimport(
            store,
            document_id=document.id,
            intent_id=legacy_intent.id,
            actor=HUMAN,
            replacement_snapshot=replacement_snapshot,
            replacement_snapshot_sha256=replacement_sha256,
        )
    assert commit_error.value.code == "source_writeback_forbidden"
    assert commit_error.value.status == 409
    assert not store.resolve_blob_path(f"blobs/{replacement_sha256}").exists()
    assert documents.get_document(store, document.id).content_sha256 == sha256_bytes(
        source
    )
    assert target.read_bytes() == changed_source


def test_retired_import_path_stays_reserved_while_a_copied_source_can_get_new_identity(
    store_ctx, client
):
    store = store_ctx["store"]
    document, source, _snapshot = _ready_import(
        store_ctx,
        path="imports/retired-source.md",
        key="retired-source-import-0001",
    )
    target = store_ctx["root"] / document.path
    documents.retire_document(store, document_id=document.id, actor=HUMAN)
    with store._read_connection() as conn:
        history_before = [
            tuple(row)
            for row in conn.execute(
                "SELECT id, kind, detail FROM doc_events "
                "WHERE document_id = ? ORDER BY rowid",
                (document.id,),
            ).fetchall()
        ]
        path_key_before = conn.execute(
            "SELECT path_key FROM document_path_keys WHERE document_id = ?",
            (document.id,),
        ).fetchone()[0]

    with pytest.raises(bootstrap.BootstrapError) as blocked:
        bootstrap.prepare_bootstrap(
            store,
            metadata={
                "mode": "import",
                "path": document.path,
                "expected_file_sha256": sha256_bytes(source),
                "idempotency_key": "retired-source-reimport-0001",
            },
            source=None,
            actor=HUMAN,
        )

    assert blocked.value.code == "retired_path"
    assert blocked.value.status == 409
    assert blocked.value.retryable is False
    assert blocked.value.details == {
        "document_id": document.id,
        "lifecycle": "retired",
        "path_reuse": "forbidden",
        "recovery_action": "choose_different_path",
    }
    assert target.read_bytes() == source
    assert documents.current_lifecycle(store, document.id) == "retired"

    changed_source = b"# Retired source changed outside Co-work\n"
    target.write_bytes(changed_source)
    changed_response = client.post(
        f"/api/truth/doc/bootstrap?store_id={store.store_id}",
        data={
            "metadata": json.dumps(
                {
                    "mode": "import",
                    "path": document.path,
                    "expected_file_sha256": sha256_bytes(changed_source),
                    "idempotency_key": "retired-source-changed-reimport-0001",
                }
            )
        },
        content_type="multipart/form-data",
    )
    assert changed_response.status_code == 409
    changed_error = changed_response.get_json()["error"]
    assert changed_error["code"] == "retired_path"
    assert changed_error["retryable"] is False
    assert changed_error["details"] == blocked.value.details
    assert target.read_bytes() == changed_source

    with store._read_connection() as conn:
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT id, kind, detail FROM doc_events "
                "WHERE document_id = ? ORDER BY rowid",
                (document.id,),
            ).fetchall()
        ] == history_before
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute(
            "SELECT path_key FROM document_path_keys WHERE document_id = ?",
            (document.id,),
        ).fetchone()[0] == path_key_before

    copied_path = "imports/retired-source-copy.md"
    copied_target = store_ctx["root"] / copied_path
    copied_target.write_bytes(changed_source)
    new_intent, created = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "import",
            "path": copied_path,
            "expected_file_sha256": sha256_bytes(changed_source),
            "idempotency_key": "retired-source-copy-import-0001",
        },
        source=None,
        actor=HUMAN,
    )
    assert created is True
    assert new_intent.document_id != document.id
    assert new_intent.path_key != path_key_before
    assert copied_target.read_bytes() == changed_source
    assert bootstrap.cancel_bootstrap(
        store, bootstrap_id=new_intent.id, actor=HUMAN
    )


def test_oversized_detached_source_does_not_break_managed_lifecycle(store_ctx):
    store = store_ctx["store"]
    document, _source, _snapshot = _ready_import(
        store_ctx,
        path="imports/oversized-lifecycle.md",
        key="oversized-lifecycle-import-0001",
    )
    target = store_ctx["root"] / document.path
    with target.open("wb") as stream:
        stream.truncate(MARKDOWN_MAX_SOURCE_BYTES + 1)

    state = inspect_lifecycle_state(store, document)
    assert state.initialization_state == "ready"
    assert state.current_file_sha256 is None
    assert state.file_path == target.resolve()
    intent, created = retirement.prepare_retirement(
        store,
        document_id=document.id,
        actor=HUMAN,
        idempotency_key="retire-oversized-detached-0001",
    )
    assert created is True
    assert intent.document_id == document.id
    assert target.stat().st_size == MARKDOWN_MAX_SOURCE_BYTES + 1


def test_retirement_requires_prepared_clean_confirmation_and_retains_file(
    store_ctx, client
):
    store = store_ctx["store"]
    document, source, _snapshot = _ready(store_ctx, key="lifecycle-ready-retire")
    ready_head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    intent, _ = retirement.prepare_retirement(
        store,
        document_id=document.id,
        actor=HUMAN,
        idempotency_key="retire-confirm-0001",
    )
    target = store_ctx["root"] / document.path
    receipt = retirement.commit_retirement(
        store,
        document_id=document.id,
        intent_id=intent.id,
        actor=HUMAN,
    )
    assert receipt["file_retained"] and receipt["history_retained"]
    assert target.read_bytes() == source
    assert documents.current_lifecycle(store, document.id) == "retired"
    assert document.id not in {item.id for item in documents.list_documents(store)}
    assert retirement.commit_retirement(
        store,
        document_id=document.id,
        intent_id=intent.id,
        actor=HUMAN,
    ) == receipt
    query = f"?store_id={store_ctx['store_id']}"
    default_docs = client.get(f"/api/truth/doc/list{query}").get_json()["docs"]
    assert document.id not in {item["document_id"] for item in default_docs}
    recovery_docs = client.get(
        f"/api/truth/doc/list{query}&include_retired=1"
    ).get_json()["docs"]
    assert document.id in {item["document_id"] for item in recovery_docs}

    before_document = documents.get_document(store, document.id)
    before_versions = documents.document_versions(store, document.id)
    before_file = target.read_bytes()
    rejected_snapshot = b"YDOC:retired-save-must-not-write"
    rejected_snapshot_sha = sha256_bytes(rejected_snapshot)
    rejected_blob = store.resolve_blob_path(f"blobs/{rejected_snapshot_sha}")
    rejected_render = b"# Retired Save\n"
    with pytest.raises(materialization.MaterializationError) as save_error:
        materialization.publish_projection(
            store,
            document_id=document.id,
            rendered_markdown=rejected_render.decode(),
            rendered_sha256=sha256_bytes(rejected_render),
            expected_file_sha256=document.content_sha256,
            expected_structured_head_sha256=ready_head,
            snapshot_sha256=document.ydoc_snapshot_sha256,
            replacement_snapshot=rejected_snapshot,
            replacement_snapshot_sha256=rejected_snapshot_sha,
            actor=HUMAN,
        )
    assert save_error.value.code == "document_retired"
    assert not rejected_blob.exists()
    assert documents.get_document(store, document.id) == before_document
    assert documents.document_versions(store, document.id) == before_versions
    assert target.read_bytes() == before_file

    save_response = client.post(
        f"/api/truth/doc/{document.id}/materialize{query}",
        json={
            "rendered_markdown": rejected_render.decode(),
            "rendered_sha256": sha256_bytes(rejected_render),
            "expected_file_sha256": document.content_sha256,
            "expected_structured_head_sha256": ready_head,
            "snapshot_sha256": document.ydoc_snapshot_sha256,
            "idempotency_key": "retired-save-0001",
        },
    )
    assert save_response.status_code == 409
    assert save_response.get_json()["error"]["code"] == "document_retired"
    assert documents.get_document(store, document.id) == before_document
    assert documents.document_versions(store, document.id) == before_versions
    assert target.read_bytes() == before_file
    assert not ydoc_store.update_tail_present(store, document_id=document.id)
    push = client.post(
        f"/api/truth/doc/{document.id}/ydoc{query}",
        data=b"retired-update",
        headers={
            "X-WB-Base-Ydoc-Sha256": ready_head,
            "X-WB-Base-Ydoc-Generation": documents.current_ydoc_generation(
                store, document.id
            ),
        },
    )
    assert push.status_code == 409
    assert push.get_json()["error"]["code"] == "document_retired"
    assert not ydoc_store.update_tail_present(store, document_id=document.id)
    with store._read_connection() as conn:
        before_spans = conn.execute("SELECT COUNT(*) FROM document_spans").fetchone()[0]
        before_evidence = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    feedback_response = client.post(
        f"/api/truth/doc/{document.id}/feedback{query}",
        json={
            "span": {"exact": "Original sentence.", "prefix": "", "suffix": ""},
            "text": "This must not be recorded.",
        },
    )
    assert feedback_response.status_code == 409
    assert feedback_response.get_json()["error"]["code"] == "document_retired"
    with store._read_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM document_spans").fetchone()[0] == before_spans
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == before_evidence


def test_retirement_route_revokes_bound_agent_lease_and_prevents_respawn(
    store_ctx,
    client,
    fake_document_agent,
):
    store = store_ctx["store"]
    document, _source, _snapshot = _ready(
        store_ctx,
        key="lifecycle-retire-agent-lease",
        path="docs/retire-agent-lease.md",
    )
    binding = conversations.ensure_document_conversation(
        document_id=document.id,
        store_id=store.store_id,
    )
    consumer = f"cowork-document:{store.store_id}:{document.id}"
    generation = "retirement-generation"
    claim = conversation_store.claim_agent_lease(
        binding.conversation_id,
        consumer,
        generation,
    )
    assert claim is not None and claim["claimed"] is True
    assert conversation_store.activate_agent_lease(
        binding.conversation_id,
        consumer,
        generation,
        81234,
    )

    url = f"/api/truth/doc/{document.id}/retire?store_id={store.store_id}"
    prepared = client.post(
        url,
        json={"idempotency_key": "retire-agent-lease-0001"},
    )
    assert prepared.status_code == 201
    committed = client.post(
        url,
        json={"intent_id": prepared.get_json()["intent_id"]},
    )
    assert committed.status_code == 200
    assert committed.get_json()["lifecycle"] == "retired"

    lease = conversation_store.get_agent_lease(
        binding.conversation_id,
        consumer,
    )
    assert lease is not None
    assert lease["status"] == "stopped"
    assert conversation_store.receive_user_message(
        binding.conversation_id,
        consumer,
        generation,
    ) == {"status": "lease_lost"}
    assert (
        conversation_store.get_conversation(binding.conversation_id).status
        == "closed"
    )
    assert fake_document_agent == []

    restart = client.post(
        f"/api/truth/doc/{document.id}/conversation?store_id={store.store_id}"
    )
    assert restart.status_code == 409
    assert fake_document_agent == []


def test_retirement_stale_confirmation_fails_without_retiring(store_ctx):
    store = store_ctx["store"]
    document, _source, _ = _ready(store_ctx, key="lifecycle-ready-retire-stale")
    intent, _ = retirement.prepare_retirement(
        store,
        document_id=document.id,
        actor=HUMAN,
        idempotency_key="retire-stale-0001",
    )
    (store_ctx["root"] / document.path).write_bytes(b"# changed after confirmation\n")
    with pytest.raises(retirement.RetirementError) as caught:
        retirement.commit_retirement(
            store,
            document_id=document.id,
            intent_id=intent.id,
            actor=HUMAN,
        )
    assert caught.value.code == "confirmation_stale"
    assert documents.current_lifecycle(store, document.id) == "active"


def test_prepared_mutations_cannot_commit_after_retirement(store_ctx):
    store = store_ctx["store"]
    document, _source, _snapshot = _ready(
        store_ctx,
        path="docs/retired-sitting.md",
        key="retired-sitting-bootstrap-0001",
    )
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    proposal = _proposal(store, document, head)
    sitting, _ = sitting_lifecycle.prepare_sitting(
        store,
        document_id=document.id,
        actor=HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "defer",
                "canonical_sha256": proposal.canonical_sha256,
            }
        ],
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=head,
        idempotency_key="retired-sitting-0001",
    )
    retire_intent, _ = retirement.prepare_retirement(
        store,
        document_id=document.id,
        actor=HUMAN,
        idempotency_key="retired-after-sitting-0001",
    )
    retirement.commit_retirement(
        store,
        document_id=document.id,
        intent_id=retire_intent.id,
        actor=HUMAN,
    )
    with pytest.raises(sitting_lifecycle.SittingError) as sitting_error:
        sitting_lifecycle.commit_sitting(
            store,
            document_id=document.id,
            intent_id=sitting.id,
            actor=HUMAN,
        )
    assert sitting_error.value.code == "document_retired"
    assert _gesture_count(store) == 0
    assert proposals.latest_proposal_status(store, proposal.id).status == "open"

    reimport_document, original, _snapshot = _ready(
        store_ctx,
        path="docs/retired-reimport.md",
        key="retired-reimport-bootstrap-0001",
    )
    target = store_ctx["root"] / reimport_document.path
    drifted = b"# Retired external replacement\n"
    target.write_bytes(drifted)
    reimport_intent, _ = reimport.prepare_reimport(
        store,
        document_id=reimport_document.id,
        actor=HUMAN,
        idempotency_key="retired-reimport-0001",
    )
    documents.retire_document(
        store,
        document_id=reimport_document.id,
        actor=HUMAN,
    )
    before_document = documents.get_document(store, reimport_document.id)
    before_versions = documents.document_versions(store, reimport_document.id)
    replacement_snapshot = b"YDOC:" + drifted
    replacement_sha = sha256_bytes(replacement_snapshot)
    replacement_blob = store.resolve_blob_path(f"blobs/{replacement_sha}")
    with pytest.raises(reimport.ReimportError) as reimport_error:
        reimport.commit_reimport(
            store,
            document_id=reimport_document.id,
            intent_id=reimport_intent.id,
            actor=HUMAN,
            replacement_snapshot=replacement_snapshot,
            replacement_snapshot_sha256=replacement_sha,
        )
    assert reimport_error.value.code == "document_retired"
    assert not replacement_blob.exists()
    assert documents.get_document(store, reimport_document.id) == before_document
    assert documents.document_versions(store, reimport_document.id) == before_versions
    assert target.read_bytes() == drifted
    assert original != drifted


def test_http_fail_closes_legacy_marks_and_exposes_enriched_drift(
    store_ctx, client
):
    document, _source, _ = _ready(store_ctx, key="lifecycle-ready-http")
    query = f"?store_id={store_ctx['store_id']}"
    legacy = client.post(
        f"/api/truth/doc/{document.id}/marks{query}",
        json={"items": [{"proposal_id": "unsafe"}]},
    )
    assert legacy.status_code == 410
    assert legacy.get_json()["error"]["code"] == "two_phase_sitting_required"
    drift = client.get(f"/api/truth/doc/{document.id}/drift{query}")
    assert drift.status_code == 200
    payload = drift.get_json()
    assert payload["state"] == "clean"
    assert payload["baseline"]["available"] is True
    assert payload["source"]["etag"]
    assert payload["unmaterialized_structured_edits"] is False

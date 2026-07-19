"""Co-work document-surface integrity checks.

Stores come from the document-store factory, which builds a real v2 store
through the engine. The document-side checks run only when the six co-work
tables exist (a pre-v2 store skips them), which is why the existing v1-store
integrity tests are unaffected. Proposal decision flows live in proposals.py.
"""

from __future__ import annotations

from pathlib import Path

from work_buddy.truth.contracts import Actor
from work_buddy.truth.identity import new_id, sha256_text, truth_uri
from work_buddy.truth.lifecycle import TruthLifecycle
from work_buddy.truth.queries import (
    _recompute_proposal_canonical,
    integrity_findings,
)

from ._document_rows import (
    create_document_store,
    seed_doc_event,
    seed_document,
    seed_document_span,
    seed_expression,
    seed_proposal,
    seed_proposal_status_event,
)


_HUMAN = Actor("human", "user-1")
_AGENT = Actor(
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
_NOW = "2026-07-14T10:00:00.000+00:00"
_LATER = "2026-07-14T11:00:00.000+00:00"
_DOC_CODES = frozenset(
    {
        "document-dangling-ref",
        "ydoc-snapshot-blob-missing",
        "proposal-subject-collision",
        "proposal-redacted-content-retained",
        "proposal-canonical-mismatch",
        "proposal-status-basis",
        "proposal-stale-base",
        "expression-claim-side-stale",
        "expression-span-side-stale",
        "document-dangling-claim-ref",
    }
)


def _doc_codes(findings) -> set[str]:
    return {finding.code for finding in findings if finding.code in _DOC_CODES}


def _codes(findings) -> set[str]:
    return {finding.code for finding in findings}


def _insert_fk_off(store, sql, params, ledger_type, ledger_key) -> None:
    # Foreign keys are enforced on normal store connections, so a genuinely
    # dangling ref (the shape import or corruption can leave) is seeded with
    # enforcement off for this one write.
    conn = store.connect()
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(sql, params)
        conn.execute(
            "INSERT INTO ledger_records (record_type, record_key) VALUES (?, ?)",
            (ledger_type, ledger_key),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def _clean_graph(store):
    content_hash = sha256_text("doc-content")
    span_hash = sha256_text("span-anchor")
    document_id = seed_document(
        store, path="docs/design.md", content_sha256=content_hash
    )
    span_id = seed_document_span(store, document_id=document_id, span_sha256=span_hash)
    claim = store.propose_claim(
        proposition="the design is sound",
        claim_kind="fact",
        actor=_HUMAN,
    ).claim
    seed_expression(
        store,
        document_span_id=span_id,
        claim_ref=claim.id,
        claim_canonical_sha256=claim.canonical_sha256,
        span_sha256=span_hash,
    )
    proposal_id = seed_proposal(
        store, document_id=document_id, base_content_sha256=content_hash
    )
    seed_proposal_status_event(
        store,
        proposal_id=proposal_id,
        status="open",
        basis_kind="rule",
        basis_ref="init",
    )
    seed_doc_event(store, document_id=document_id, kind="registered")
    return document_id, span_id, claim, content_hash, span_hash


def test_document_integrity_clean_graph_has_no_document_findings(
    truth_root: Path,
) -> None:
    store = create_document_store(truth_root)
    _clean_graph(store)
    findings = integrity_findings(store)
    assert _doc_codes(findings) == set()
    # The six co-work ledger types are recognized, not reported as unknown.
    assert "unknown_ledger_record_type" not in _codes(findings)
    assert "missing_ledger_record" not in _codes(findings)


def test_dangling_document_graph_refs_are_errors(truth_root: Path) -> None:
    store = create_document_store(truth_root)
    ghost = "0" * 32
    span_id = new_id()
    _insert_fk_off(
        store,
        "INSERT INTO document_spans (id, document_id, selector_json, quote_exact, "
        "span_sha256, author_kind, author_ref, created_at, created_by_kind, "
        "created_by_ref) VALUES (?, ?, '[]', 'q', ?, 'human', 'u', ?, 'human', 'u')",
        (span_id, ghost, sha256_text("s"), _NOW),
        "document_span",
        span_id,
    )
    proposal_id = new_id()
    _insert_fk_off(
        store,
        "INSERT INTO proposals (id, document_id, base_content_sha256, selector_json, "
        "quote_exact, span_sha256, replacement, canonical_sha256, dedup_key, "
        "created_at, created_by_kind, created_by_ref) "
        "VALUES (?, ?, ?, '[]', 'q', ?, 'r', ?, ?, ?, 'agent_run', 'run')",
        (
            proposal_id,
            ghost,
            sha256_text("b"),
            sha256_text("s"),
            sha256_text("c"),
            sha256_text("d"),
            _NOW,
        ),
        "proposal",
        proposal_id,
    )
    event_id = new_id()
    _insert_fk_off(
        store,
        "INSERT INTO doc_events (id, document_id, kind, at, actor_kind, actor_ref) "
        "VALUES (?, ?, 'registered', ?, 'human', 'u')",
        (event_id, ghost, _NOW),
        "doc_event",
        event_id,
    )
    expression_id = new_id()
    _insert_fk_off(
        store,
        "INSERT INTO expressions (id, document_span_id, claim_ref_kind, claim_ref, "
        "role, claim_canonical_sha256, span_sha256, created_at, created_by_kind, "
        "created_by_ref) VALUES (?, ?, 'local', ?, 'instantiation', ?, ?, ?, "
        "'human', 'u')",
        (expression_id, ghost, new_id(), sha256_text("cc"), sha256_text("s"), _NOW),
        "expression",
        expression_id,
    )

    findings = integrity_findings(store)
    dangling = {
        (finding.subject_kind, finding.subject_ref)
        for finding in findings
        if finding.code == "document-dangling-ref"
    }
    assert ("document_span", span_id) in dangling
    assert ("proposal", proposal_id) in dangling
    assert ("doc_event", event_id) in dangling
    assert ("expression", expression_id) in dangling


def test_proposal_id_colliding_with_a_claim_is_an_error(truth_root: Path) -> None:
    store = create_document_store(truth_root)
    document_id, _span, claim, content_hash, _ = _clean_graph(store)
    seed_proposal(
        store,
        document_id=document_id,
        proposal_id=claim.id,
        base_content_sha256=content_hash,
    )
    findings = integrity_findings(store)
    assert any(
        finding.code == "proposal-subject-collision"
        and finding.subject_ref == claim.id
        for finding in findings
    )


def test_redacted_proposal_retaining_content_is_an_error(truth_root: Path) -> None:
    store = create_document_store(truth_root)
    document_id = seed_document(store)
    proposal_id = seed_proposal(
        store,
        document_id=document_id,
        replacement="still here",
        redacted_at=_NOW,
    )
    findings = integrity_findings(store)
    assert any(
        finding.code == "proposal-redacted-content-retained"
        and finding.subject_ref == proposal_id
        for finding in findings
    )


def test_missing_ydoc_snapshot_blob_is_an_error(truth_root: Path) -> None:
    store = create_document_store(truth_root)
    document_id = seed_document(
        store, path="docs/design.md", ydoc_snapshot_sha256="a" * 64
    )
    findings = integrity_findings(store)
    assert any(
        finding.code == "ydoc-snapshot-blob-missing"
        and finding.subject_ref == document_id
        for finding in findings
    )


def test_proposal_status_basis_discipline(truth_root: Path) -> None:
    store = create_document_store(truth_root)
    document_id = seed_document(store, content_sha256=sha256_text("h"))

    # applied without a bound consumed gesture -> error
    applied = seed_proposal(
        store, document_id=document_id, base_content_sha256=sha256_text("h")
    )
    seed_proposal_status_event(
        store, proposal_id=applied, status="applied", basis_kind="rule", basis_ref="x"
    )
    # expired with a gesture basis instead of rule/sweep -> error
    expired = seed_proposal(
        store, document_id=document_id, base_content_sha256=sha256_text("h")
    )
    seed_proposal_status_event(
        store,
        proposal_id=expired,
        status="expired",
        basis_kind="gesture",
        basis_ref=new_id(),
    )

    findings = integrity_findings(store)
    flagged = {
        finding.subject_ref
        for finding in findings
        if finding.code == "proposal-status-basis"
    }
    assert applied in flagged
    assert expired in flagged


def test_applied_proposal_with_a_consumed_gesture_is_clean(truth_root: Path) -> None:
    store = create_document_store(truth_root)
    lifecycle = TruthLifecycle(store)
    content_hash = sha256_text("h")
    document_id = seed_document(store, content_sha256=content_hash)
    proposal_id = seed_proposal(
        store, document_id=document_id, base_content_sha256=content_hash
    )
    conn = store.connect()
    try:
        proposal = store._get_proposal_locked(conn, proposal_id)
    finally:
        conn.close()
    gesture = lifecycle.mint_gesture(
        subject_ref=proposal_id,
        actor=_HUMAN,
        surface="dashboard",
        kind="confirm",
        displayed_payload_sha256=proposal.canonical_sha256,
        at=_NOW,
    )
    lifecycle.verify_and_consume_gesture(
        gesture.id,
        actor=_HUMAN,
        subject_ref=proposal_id,
        payload_sha256=proposal.canonical_sha256,
        expected_context_sha256=None,
        allowed_kinds={"confirm"},
        observed_at=_LATER,
    )
    seed_proposal_status_event(
        store,
        proposal_id=proposal_id,
        status="applied",
        decision="confirm",
        basis_kind="gesture",
        basis_ref=gesture.id,
    )
    findings = integrity_findings(store)
    # A clean applied proposal (consumed confirm gesture bound to the proposal)
    # must leave NO error-severity finding, not merely no proposal-status-basis
    # one. The proposal-aware gesture-subject resolver makes this hold.
    errors = [f for f in findings if f.severity == "error"]
    assert errors == [], f"applied proposal store is not clean: {errors!r}"


def test_proposal_stale_base_is_a_warning(truth_root: Path) -> None:
    store = create_document_store(truth_root)
    document_id = seed_document(store, content_sha256=sha256_text("latest"))
    proposal_id = seed_proposal(
        store, document_id=document_id, base_content_sha256=sha256_text("older")
    )
    seed_proposal_status_event(
        store, proposal_id=proposal_id, status="open", basis_kind="rule", basis_ref="i"
    )
    findings = integrity_findings(store)
    stale = [f for f in findings if f.code == "proposal-stale-base"]
    assert stale and stale[0].subject_ref == proposal_id
    assert stale[0].severity == "warning"


def test_expression_fingerprint_staleness_warnings(truth_root: Path) -> None:
    store = create_document_store(truth_root)
    span_hash = sha256_text("span-anchor")
    document_id = seed_document(store, content_sha256=sha256_text("h"))
    span_id = seed_document_span(store, document_id=document_id, span_sha256=span_hash)
    claim = store.propose_claim(
        proposition="a claim", claim_kind="fact", actor=_HUMAN
    ).claim
    # claim-side stale: stored fingerprint differs from the current claim hash.
    seed_expression(
        store,
        document_span_id=span_id,
        claim_ref=claim.id,
        claim_canonical_sha256=sha256_text("outdated"),
        span_sha256=span_hash,
    )
    # span-side stale: stored span fingerprint differs from the current span.
    seed_expression(
        store,
        document_span_id=span_id,
        claim_ref=claim.id,
        claim_canonical_sha256=claim.canonical_sha256,
        span_sha256=sha256_text("drifted"),
    )
    findings = integrity_findings(store)
    codes = _codes(findings)
    assert "expression-claim-side-stale" in codes
    assert "expression-span-side-stale" in codes
    for finding in findings:
        if finding.code in {
            "expression-claim-side-stale",
            "expression-span-side-stale",
        }:
            assert finding.severity == "warning"


def test_dangling_local_claim_refs_are_warnings(truth_root: Path) -> None:
    store = create_document_store(truth_root)
    span_hash = sha256_text("span-anchor")
    document_id = seed_document(store, content_sha256=sha256_text("h"))
    span_id = seed_document_span(store, document_id=document_id, span_sha256=span_hash)
    missing_claim = new_id()
    seed_expression(
        store,
        document_span_id=span_id,
        claim_ref=missing_claim,
        claim_canonical_sha256=sha256_text("x"),
        span_sha256=span_hash,
    )
    other_missing = new_id()
    seed_proposal(
        store,
        document_id=document_id,
        base_content_sha256=sha256_text("h"),
        claim_refs_json='[{"claim": "%s", "role": "instantiation"}]' % other_missing,
    )
    findings = integrity_findings(store)
    dangling = {
        (f.subject_kind, f.subject_ref)
        for f in findings
        if f.code == "document-dangling-claim-ref"
    }
    assert any(kind == "expression" for kind, _ in dangling)
    assert any(kind == "proposal" for kind, _ in dangling)
    for finding in findings:
        if finding.code == "document-dangling-claim-ref":
            assert finding.severity == "warning"


def test_cross_store_uri_claim_refs_are_not_flagged(truth_root: Path) -> None:
    store = create_document_store(truth_root)
    document_id = seed_document(store, content_sha256=sha256_text("h"))
    uri = truth_uri(new_id(), "claim", new_id())
    seed_proposal(
        store,
        document_id=document_id,
        base_content_sha256=sha256_text("h"),
        claim_refs_json='[{"claim": "%s", "role": "quote"}]' % uri,
    )
    findings = integrity_findings(store)
    assert not [f for f in findings if f.code == "document-dangling-claim-ref"]


def test_proposal_canonical_mismatch_uses_the_engine_hash(
    truth_root: Path, monkeypatch
) -> None:
    store = create_document_store(truth_root)
    document_id = seed_document(store, content_sha256=sha256_text("h"))
    proposal_id = seed_proposal(
        store, document_id=document_id, base_content_sha256=sha256_text("h")
    )

    # A recompute that disagrees with the stored hash is a blocking error.
    monkeypatch.setattr(
        "work_buddy.truth.queries._recompute_proposal_canonical",
        lambda row: "f" * 64,
    )
    findings = integrity_findings(store)
    assert any(
        finding.code == "proposal-canonical-mismatch"
        and finding.subject_ref == proposal_id
        for finding in findings
    )

    # A recompute that agrees produces no finding.
    conn = store.connect()
    try:
        stored = conn.execute(
            "SELECT canonical_sha256 FROM proposals WHERE id = ?", (proposal_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    monkeypatch.setattr(
        "work_buddy.truth.queries._recompute_proposal_canonical",
        lambda row: stored,
    )
    findings = integrity_findings(store)
    assert not [f for f in findings if f.code == "proposal-canonical-mismatch"]


def test_unrecomputable_proposal_is_not_a_false_mismatch(truth_root: Path) -> None:
    # A proposal the recompute cannot reconstruct (here a malformed selector_json)
    # makes _recompute_proposal_canonical return None, so no false blocking
    # mismatch is ever manufactured.
    store = create_document_store(truth_root)
    document_id = seed_document(store, content_sha256=sha256_text("h"))
    seed_proposal(
        store,
        document_id=document_id,
        base_content_sha256=sha256_text("h"),
        selector_json="{not valid json",
        canonical_sha256=sha256_text("stored-canonical"),
    )
    findings = integrity_findings(store)
    assert not [f for f in findings if f.code == "proposal-canonical-mismatch"]


def test_document_rows_without_ledger_records_are_flagged(truth_root: Path) -> None:
    store = create_document_store(truth_root)
    document_id = seed_document(store, path="docs/x.md", ledger=False)
    findings = integrity_findings(store)
    assert any(
        finding.code == "missing_ledger_record"
        and finding.subject_ref == document_id
        for finding in findings
    )


def test_canonical_recompute_matches_a_real_engine_proposal(truth_root: Path) -> None:
    """The proposal-canonical-mismatch check runs live against the engine hash.

    A proposal composed through the engine recomputes to exactly its stored
    canonical_sha256, so the sweep reports no canonical mismatch. This proves
    the check is live rather than inert.
    """
    from work_buddy.truth import documents, proposals

    store = create_document_store(truth_root)
    content_hash = sha256_text("design body v0")
    document = documents.register_document(
        store,
        path="docs/live-proof.md",
        title="Live proof document",
        document_class="co_authored",
        content_sha256=content_hash,
        actor=_HUMAN,
        at=_NOW,
    )
    proposal = proposals.propose_edit(
        store,
        document_id=document.id,
        base_content_sha256=content_hash,
        selector=[
            {"type": "TextQuoteSelector", "exact": "old", "prefix": "", "suffix": ""}
        ],
        quote_exact="old",
        replacement="new",
        actor=_AGENT,
        at=_NOW,
    )

    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT * FROM proposals WHERE id = ?", (proposal.id,)
        ).fetchone()
    finally:
        conn.close()

    recomputed = _recompute_proposal_canonical(row)
    assert recomputed is not None
    assert recomputed == row["canonical_sha256"]
    assert recomputed == proposal.canonical_sha256

    findings = integrity_findings(store)
    assert not [
        finding
        for finding in findings
        if finding.code == "proposal-canonical-mismatch"
    ]

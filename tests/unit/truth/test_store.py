"""Invariant tests for the truth store append layer."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor, InvariantViolation, StoreVersionError
from work_buddy.truth.identity import canonical_json, new_id, sha256_bytes, truth_uri
from work_buddy.truth.lifecycle import TruthLifecycle
from work_buddy.truth.store import (
    AcquisitionOrigin,
    PremiseRef,
    TruthStore,
)


NOW = "2026-07-14T12:00:00.000+00:00"
LATER = "2026-07-14T12:01:00.000+00:00"
HUMAN = Actor("human", "user-1")
SYSTEM = Actor("system", "truth-test")
AGENT = Actor(
    "agent_run",
    "run-1",
    {
        "model": "test-model",
        "harness": "pytest",
        "surface": "unit",
        "session_id": "session-1",
        "call_id": "call-1",
    },
)


def _profile(*, store_id: str | None = None) -> dict[str, object]:
    return {
        "store_id": store_id or new_id(),
        "profile": "test",
        "title": "Test truth store",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": "redact",
            "confirmation_surfaces": ["dashboard", "cli"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": True,
    }


@pytest.fixture
def store(truth_root: Path) -> TruthStore:
    return TruthStore.create(truth_root, _profile())


def _capture(
    store: TruthStore,
    content: str = "alpha beta",
    **kwargs,
):
    locator = kwargs.pop("source_locator", "file:///source.md")
    return store.capture_evidence(
        kind="document",
        source_locator=locator,
        actor=HUMAN,
        acquisition_method="paste",
        content=content,
        **kwargs,
    )


def _claim(store: TruthStore, proposition: str = "Alpha beta", **kwargs):
    return store.propose_claim(
        proposition=proposition,
        claim_kind="fact",
        actor=HUMAN,
        **kwargs,
    ).claim


def test_create_open_is_idempotent_and_configures_sqlite(truth_root: Path):
    calls: list[str] = []
    profile = _profile()
    store = TruthStore.create(
        truth_root,
        profile,
        on_commit=lambda item: calls.append(item.store_id),
    )
    assert calls == [profile["store_id"]]
    calls.clear()

    reopened = TruthStore.create(
        truth_root,
        profile,
        on_commit=lambda item: calls.append(item.store_id),
    )
    assert reopened.store_id == store.store_id
    assert calls == []
    assert TruthStore.open(store.paths.sidecar).store_id == store.store_id
    assert store.paths.config.is_file()
    assert store.paths.db.is_file()
    assert store.paths.blobs.is_dir()
    assert store.paths.export_dir.is_dir()

    conn = store.connect()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000
        assert conn.execute("SELECT COUNT(*) FROM ledger_records").fetchone()[0] == 0
    finally:
        conn.close()


def test_create_refuses_identity_mismatch_and_open_refuses_future_schema(
    truth_root: Path,
):
    store = TruthStore.create(truth_root, _profile())
    with pytest.raises(InvariantViolation, match="identity"):
        TruthStore.create(truth_root, _profile())

    profile_text = store.paths.config.read_text(encoding="utf-8")
    store.paths.config.write_text(
        profile_text.replace("Test truth store", "Renamed behind the ledger"),
        encoding="utf-8",
    )
    with pytest.raises(InvariantViolation, match="identity"):
        TruthStore.open(truth_root)
    store.paths.config.write_text(profile_text, encoding="utf-8")

    conn = store.connect()
    conn.execute("PRAGMA user_version = 999")
    conn.close()
    with pytest.raises(StoreVersionError, match="only knows up to"):
        TruthStore.open(truth_root)


def test_supplied_connection_must_be_active_and_target_this_store(
    store: TruthStore,
    tmp_path: Path,
):
    conn = store.connect()
    with pytest.raises(InvariantViolation, match="already own"):
        with store.write_transaction(conn):
            pass
    conn.close()

    foreign = sqlite3.connect(tmp_path / "foreign.db", isolation_level=None)
    foreign.execute("BEGIN")
    try:
        with pytest.raises(InvariantViolation, match="different truth store"):
            with store.write_transaction(foreign):
                pass
    finally:
        foreign.rollback()
        foreign.close()


def test_outer_transaction_commits_once_and_rollback_is_total(truth_root: Path):
    calls: list[str] = []
    store = TruthStore.create(
        truth_root,
        _profile(),
        on_commit=lambda item: calls.append(item.store_id),
    )
    calls.clear()
    with store.write_transaction() as conn:
        _capture(store, conn=conn)
        _claim(store, conn=conn)
    assert len(calls) == 1

    calls.clear()
    evidence_id = new_id()
    with pytest.raises(RuntimeError, match="rollback"):
        with store.write_transaction() as conn:
            _capture(store, record_id=evidence_id, conn=conn)
            raise RuntimeError("rollback")
    assert store.get_evidence(evidence_id) is None
    assert calls == []


def test_evidence_inline_blob_hash_only_and_safe_resolution(truth_root: Path):
    store = TruthStore.create(truth_root, _profile(), inline_content_bytes=4)
    inline = _capture(store, "four")
    assert inline.content == "four"
    assert inline.content_path is None

    blob = _capture(store, "five!")
    assert blob.content is None
    assert blob.content_path == f"blobs/{blob.content_sha256}"
    assert store.read_evidence_bytes(blob) == b"five!"
    assert store.read_evidence_text(blob.id) == "five!"
    assert store.resolve_blob_path(blob.content_path).is_file()

    digest = sha256_bytes(b"not retained")
    hash_only = store.capture_evidence(
        kind="import",
        source_locator="doi:10.1/example",
        actor=SYSTEM,
        acquisition_method="import",
        content_sha256=digest,
    )
    assert hash_only.content is None
    assert hash_only.content_path is None
    assert store.read_evidence_bytes(hash_only.id) is None
    with pytest.raises(InvariantViolation, match="form blobs"):
        store.resolve_blob_path("../escape")


def test_evidence_hash_validation_corruption_and_reference_count(truth_root: Path):
    store = TruthStore.create(truth_root, _profile(), inline_content_bytes=0)
    with pytest.raises(InvariantViolation, match="does not match"):
        _capture(store, "payload", content_sha256="0" * 64)

    first = _capture(store, "shared")
    second = _capture(store, "shared")
    assert store.blob_reference_count(first.content_sha256) == 2
    assert second.content_path == first.content_path
    path = store.resolve_blob_path(first.content_path)
    path.write_bytes(b"corrupt")
    with pytest.raises(InvariantViolation, match="hash mismatch"):
        store.read_evidence_bytes(first.id)


def test_failed_owned_capture_removes_only_new_unreferenced_blob(truth_root: Path):
    store = TruthStore.create(truth_root, _profile(), inline_content_bytes=0)
    existing = _capture(store, "kept", record_id=new_id())
    duplicate_id = existing.id
    failed_digest = sha256_bytes(b"rolled back")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _capture(store, "rolled back", record_id=duplicate_id)
    assert not store.resolve_blob_path(f"blobs/{failed_digest}").exists()
    assert store.resolve_blob_path(existing.content_path).exists()


def test_new_blob_capture_refuses_caller_owned_transaction(truth_root: Path):
    store = TruthStore.create(truth_root, _profile(), inline_content_bytes=0)
    digest = sha256_bytes(b"outer rollback")
    conn = store.connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(InvariantViolation, match="caller-owned transaction"):
            _capture(store, "outer rollback", conn=conn)
    finally:
        conn.execute("ROLLBACK")
        conn.close()
    assert not store.resolve_blob_path(f"blobs/{digest}").exists()

    shared = _capture(store, "already durable")
    conn = store.connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        duplicate = _capture(store, "already durable", conn=conn)
        assert duplicate.content_path == shared.content_path
    finally:
        conn.execute("ROLLBACK")
        conn.close()
    assert store.blob_reference_count(shared.content_sha256) == 1
    assert store.resolve_blob_path(shared.content_path).exists()


@pytest.mark.parametrize(
    ("actor", "method", "origin", "reviewed", "expected"),
    (
        (HUMAN, "paste", None, False, "user_authored"),
        (AGENT, "paste", None, False, "agent_authored"),
        (SYSTEM, "paste", None, False, "unattested"),
        (HUMAN, "file_read", None, False, "unattested"),
        (AGENT, "import", None, False, "unattested"),
        (SYSTEM, "said_in_chat", None, False, "mixed"),
        (AGENT, "fetch", None, False, "external_quarantined"),
        (HUMAN, "fetch", None, True, "external"),
        (
            HUMAN,
            "paste",
            AcquisitionOrigin.HUMAN_CURATED,
            False,
            "user_curated",
        ),
        (
            HUMAN,
            "paste",
            AcquisitionOrigin.AGENT_GENERATED,
            False,
            "agent_authored",
        ),
    ),
)
def test_trust_class_is_derived_from_frozen_acquisition_context(
    store: TruthStore,
    actor: Actor,
    method: str,
    origin: AcquisitionOrigin | None,
    reviewed: bool,
    expected: str,
):
    record = store.capture_evidence(
        kind="document",
        source_locator="https://example.test/source",
        actor=actor,
        acquisition_method=method,
        content="source",
        origin=origin,
        external_reviewed=reviewed,
    )
    assert record.trust_class == expected


def test_agent_cannot_launder_human_or_clear_external_trust(store: TruthStore):
    with pytest.raises(InvariantViolation, match="human surface"):
        store.capture_evidence(
            kind="document",
            source_locator="file:///agent.md",
            actor=AGENT,
            acquisition_method="paste",
            content="agent text",
            origin="user_input",
        )
    with pytest.raises(InvariantViolation, match="cannot clear"):
        store.capture_evidence(
            kind="web",
            source_locator="https://example.test",
            actor=AGENT,
            acquisition_method="fetch",
            content="external",
            external_reviewed=True,
        )
    incomplete = Actor("agent_run", "run-bad", {"model": "only-one"})
    with pytest.raises(InvariantViolation, match="missing producer identity"):
        store.capture_evidence(
            kind="document",
            source_locator="file:///bad.md",
            actor=incomplete,
            acquisition_method="paste",
            content="bad",
        )


def test_agent_producer_identity_is_durable_and_locator_requires_scheme(
    store: TruthStore,
):
    record = store.capture_evidence(
        kind="artifact",
        source_locator="artifact://one",
        actor=AGENT,
        acquisition_method="paste",
        content="generated",
        meta={"purpose": "test"},
    )
    assert json.loads(record.meta_json) == {
        **dict(AGENT.meta),
        "purpose": "test",
    }
    with pytest.raises(InvariantViolation, match="named URI scheme"):
        _capture(store, source_locator="relative/source.md")


def test_span_reanchors_exact_whitespace_and_hash_only_snapshot(store: TruthStore):
    evidence = _capture(store, "prefix alpha   beta suffix")
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact="alpha beta"),
        actor=HUMAN,
    )
    assert span.quote_exact == "alpha   beta"
    assert span.span_sha256 == sha256_bytes(b"alpha   beta")
    assert span.author_kind == "human"

    snapshot = "hash only passage"
    hash_only = store.capture_evidence(
        kind="import",
        source_locator="doi:10.1/hash",
        actor=SYSTEM,
        acquisition_method="import",
        content_sha256=sha256_bytes(snapshot.encode()),
    )
    marked = store.mark_span(
        evidence_id=hash_only.id,
        selector=CompositeSelector(exact="only"),
        actor=SYSTEM,
        snapshot_text=snapshot,
    )
    assert marked.quote_exact == "only"
    with pytest.raises(InvariantViolation, match="does not match"):
        store.mark_span(
            evidence_id=hash_only.id,
            selector=CompositeSelector(exact="only"),
            actor=SYSTEM,
            snapshot_text="wrong only",
        )


def test_span_authorship_laws_for_curated_mixed_and_agents(store: TruthStore):
    curated = store.capture_evidence(
        kind="document",
        source_locator="file:///curated.md",
        actor=HUMAN,
        acquisition_method="paste",
        origin="human_curated",
        content="curated passage",
    )
    assert (
        store.mark_span(
            evidence_id=curated.id,
            selector=CompositeSelector(exact="passage"),
            actor=HUMAN,
        ).author_kind
        == "unknown"
    )

    mixed = store.capture_evidence(
        kind="chat",
        source_locator="wb-chat://session/message",
        actor=SYSTEM,
        acquisition_method="said_in_chat",
        origin="mixed_transcript",
        content="human and agent",
    )
    with pytest.raises(InvariantViolation, match="explicit span author"):
        store.mark_span(
            evidence_id=mixed.id,
            selector=CompositeSelector(exact="human"),
            actor=SYSTEM,
        )
    with pytest.raises(InvariantViolation, match="cannot assert human"):
        store.mark_span(
            evidence_id=mixed.id,
            selector=CompositeSelector(exact="human"),
            actor=AGENT,
            author_kind="human",
            author_ref="user-1",
        )
    agent_span = store.mark_span(
        evidence_id=mixed.id,
        selector=CompositeSelector(exact="agent"),
        actor=AGENT,
        author_kind="agent_run",
        author_ref="run-1",
    )
    assert agent_span.author_ref == "run-1"

    human_captured_agent_text = store.capture_evidence(
        kind="document",
        source_locator="file:///human-captured-agent.md",
        actor=HUMAN,
        acquisition_method="paste",
        origin=AcquisitionOrigin.AGENT_GENERATED,
        content="generated elsewhere",
    )
    with pytest.raises(InvariantViolation, match="requires author_ref"):
        store.mark_span(
            evidence_id=human_captured_agent_text.id,
            selector=CompositeSelector(exact="generated elsewhere"),
            actor=HUMAN,
        )
    attributed = store.mark_span(
        evidence_id=human_captured_agent_text.id,
        selector=CompositeSelector(exact="generated elsewhere"),
        actor=HUMAN,
        author_ref="run-original-author",
    )
    assert attributed.author_kind == "agent_run"
    assert attributed.author_ref == "run-original-author"


def test_propose_normalizes_validates_profile_and_deduplicates(store: TruthStore):
    first = store.propose_claim(
        proposition="  Alpha   beta  ",
        claim_kind="fact",
        actor=HUMAN,
        structured={"answer": "  forty   two "},
    )
    second = store.propose_claim(
        proposition="Alpha beta",
        claim_kind="fact",
        actor=HUMAN,
        structured={"answer": "forty two"},
    )
    assert first.created is True
    assert second.created is False
    assert second.claim.id == first.claim.id
    assert first.claim.proposition == "Alpha beta"
    assert json.loads(first.claim.structured_json) == {"answer": "forty two"}

    with pytest.raises(InvariantViolation):
        store.propose_claim(
            proposition="Not permitted",
            claim_kind="measurement",
            actor=HUMAN,
        )
    with pytest.raises(InvariantViolation, match="valid_to"):
        _claim(store, "Bad interval", valid_from="2026-07-15", valid_to="2026-07-14")
    with pytest.raises(InvariantViolation, match="status_at"):
        _claim(store, "Bad clock", created_at=LATER, status_at=NOW)


def test_terminal_claim_can_be_reproposed_and_duplicate_live_rows_fail_closed(
    store: TruthStore,
):
    original = _claim(store, "Terminal reproposal")
    with store.write_transaction() as conn:
        store._insert_status_event_locked(
            conn,
            claim_id=original.id,
            status="rejected",
            actor=SYSTEM,
            basis_kind="rule",
            basis_ref="test",
        )
    replacement = store.propose_claim(
        proposition="Terminal reproposal",
        claim_kind="fact",
        actor=HUMAN,
    )
    assert replacement.created is True
    assert replacement.claim.id != original.id

    with store.write_transaction() as conn:
        clone = replacement.claim.__class__(
            **{
                **{
                    field: getattr(replacement.claim, field)
                    for field in replacement.claim.__dataclass_fields__
                },
                "id": new_id(),
            }
        )
        store._insert_claim_locked(conn, clone)
        store._insert_status_event_locked(
            conn,
            claim_id=clone.id,
            status="proposed",
            actor=SYSTEM,
            basis_kind="import",
            basis_ref="corrupt-fixture",
        )
    with pytest.raises(InvariantViolation, match="multiple live claims"):
        store.propose_claim(
            proposition="Terminal reproposal",
            claim_kind="fact",
            actor=HUMAN,
        )


def test_concurrent_proposals_create_one_live_claim(store: TruthStore):
    def propose(_: int):
        return store.propose_claim(
            proposition="Concurrent canonical claim",
            claim_kind="fact",
            actor=HUMAN,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(propose, range(8)))
    assert sum(item.created for item in results) == 1
    assert len({item.claim.id for item in results}) == 1


@pytest.mark.parametrize(
    ("link_type", "to_kind"),
    (
        ("supports_span", "evidence_span"),
        ("about_entity", "entity"),
        ("supersedes", "claim"),
        ("conflicts_with", "claim"),
        ("refutes", "claim"),
        ("cites_external", "external_uri"),
        ("relates_to", "claim"),
        ("relates_to", "entity"),
        ("relates_to", "external_uri"),
    ),
)
def test_link_target_matrix_and_fingerprint_scope(
    store: TruthStore,
    link_type: str,
    to_kind: str,
):
    source = _claim(store, f"Source {new_id()}")
    target_claim = _claim(store, f"Target {new_id()}")
    evidence = _capture(store, f"evidence {new_id()}")
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact="evidence"),
        actor=HUMAN,
    )
    refs = {
        "claim": target_claim.id,
        "evidence_span": span.id,
        "entity": "wb-entity://project-1",
        "external_uri": "https://example.test/source",
    }
    role = {"supersession_reason": "corrected"} if link_type == "supersedes" else None
    mutable = link_type in {"about_entity", "cites_external"}
    link = store.add_link(
        from_claim_id=source.id,
        link_type=link_type,
        to_kind=to_kind,
        to_ref=refs[to_kind],
        actor=HUMAN,
        role=role,
        target_content="reviewed target" if mutable else None,
    )
    assert (link.target_fingerprint is not None) is mutable
    assert (link.fingerprint_reviewed_at is not None) is mutable


def test_mutable_links_allow_unreviewed_targets_and_immutable_refuse_content(
    store: TruthStore,
):
    source = _claim(store, "Link source")
    unreviewed = store.add_link(
        from_claim_id=source.id,
        link_type="about_entity",
        to_kind="entity",
        to_ref="wb-entity://one",
        actor=HUMAN,
    )
    assert unreviewed.target_fingerprint is None
    assert unreviewed.fingerprint_reviewed_at is None
    with pytest.raises(InvariantViolation, match="immutable"):
        store.add_link(
            from_claim_id=source.id,
            link_type="relates_to",
            to_kind="entity",
            to_ref="wb-entity://one",
            actor=HUMAN,
            target_content="wrong scope",
        )
    with pytest.raises(InvariantViolation, match="supported supersession_reason"):
        store.add_link(
            from_claim_id=source.id,
            link_type="supersedes",
            to_kind="claim",
            to_ref=_claim(store, "Old").id,
            actor=HUMAN,
            role={"supersession_reason": "typo"},
        )


def test_link_retraction_is_append_only_and_idempotent(store: TruthStore):
    source = _claim(store, "Retraction source")
    link = store.add_link(
        from_claim_id=source.id,
        link_type="relates_to",
        to_kind="entity",
        to_ref="wb-entity://one",
        actor=HUMAN,
    )
    first = store.retract_link(link_id=link.id, actor=HUMAN, reason="mistake")
    second = store.retract_link(link_id=link.id, actor=HUMAN, reason="ignored")
    assert second == first


def test_link_retraction_cannot_predate_its_link(store: TruthStore):
    source = _claim(store, "Chronological retraction source")
    link = store.add_link(
        from_claim_id=source.id,
        link_type="relates_to",
        to_kind="entity",
        to_ref="wb-entity://chronology",
        actor=HUMAN,
        created_at=LATER,
    )

    with pytest.raises(InvariantViolation, match="cannot predate"):
        store.retract_link(link_id=link.id, actor=HUMAN, at=NOW)

    assert store.get_link_retraction(link.id) is None
    assert store.retract_link(link_id=link.id, actor=HUMAN, at=LATER).at == LATER


def test_current_supersession_authority_link_cannot_be_retracted(
    store: TruthStore,
):
    predecessor = _claim(store, "Old wording", created_at=NOW, status_at=NOW)
    successor = _claim(store, "Correct wording", created_at=NOW, status_at=NOW)
    lifecycle = TruthLifecycle(store)
    predecessor_gesture = lifecycle.mint_gesture(
        subject_ref=predecessor.id,
        actor=HUMAN,
        surface="dashboard",
        kind="confirm",
        displayed_payload_sha256=predecessor.canonical_sha256,
        at=LATER,
    )
    lifecycle.confirm_claim(
        claim_id=predecessor.id,
        gesture_id=predecessor_gesture.id,
        actor=HUMAN,
        expected_context_sha256=None,
        observed_at=LATER,
        at=LATER,
    )
    link = store.add_link(
        from_claim_id=successor.id,
        link_type="supersedes",
        to_kind="claim",
        to_ref=predecessor.id,
        actor=HUMAN,
        role={"supersession_reason": "corrected"},
    )
    successor_at = "2026-07-14T12:02:00.000+00:00"
    gesture = lifecycle.mint_gesture(
        subject_ref=successor.id,
        actor=HUMAN,
        surface="dashboard",
        kind="confirm",
        displayed_payload_sha256=successor.canonical_sha256,
        at=successor_at,
    )
    lifecycle.confirm_claim(
        claim_id=successor.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        expected_context_sha256=None,
        observed_at=successor_at,
        at=successor_at,
    )

    with pytest.raises(InvariantViolation, match="current superseded status"):
        store.retract_link(link_id=link.id, actor=HUMAN, reason="break history")
    assert store.get_link_retraction(link.id) is None


def test_current_challenge_authority_link_cannot_be_retracted(
    store: TruthStore,
):
    target = _claim(
        store,
        "The deployment is healthy",
        created_at=NOW,
        status_at=NOW,
    )
    challenger = _claim(
        store,
        "The deployment is unhealthy",
        created_at=NOW,
        status_at=NOW,
    )
    lifecycle = TruthLifecycle(store)
    gesture = lifecycle.mint_gesture(
        subject_ref=target.id,
        actor=HUMAN,
        surface="dashboard",
        kind="confirm",
        displayed_payload_sha256=target.canonical_sha256,
        at=LATER,
    )
    lifecycle.confirm_claim(
        claim_id=target.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        expected_context_sha256=None,
        observed_at=LATER,
        at=LATER,
    )
    evidence = _capture(store, "health check failed")
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact="health check failed"),
        actor=HUMAN,
    )
    store.add_link(
        from_claim_id=challenger.id,
        link_type="supports_span",
        to_kind="evidence_span",
        to_ref=span.id,
        actor=HUMAN,
    )
    challenged = lifecycle.challenge_claim(
        claim_id=target.id,
        challenging_claim_id=challenger.id,
        actor=HUMAN,
        at="2026-07-14T12:02:00.000+00:00",
    )
    assert challenged.event.basis_ref is not None

    with pytest.raises(InvariantViolation, match="current challenged status"):
        store.retract_link(
            link_id=challenged.event.basis_ref,
            actor=HUMAN,
            reason="break challenge history",
        )
    assert store.get_link_retraction(challenged.event.basis_ref) is None


def test_derivations_validate_and_preserve_local_and_uri_premises(store: TruthStore):
    premise = _claim(store, "Premise")
    conclusion = _claim(store, "Conclusion")
    remote = truth_uri(new_id(), "claim", new_id())
    derivation = store.add_derivation(
        claim_id=conclusion.id,
        method="deterministic_template",
        premises=[premise.id, PremiseRef("uri", remote)],
        actor=AGENT,
        confidence=0.9,
        rationale="Template joins accepted facts",
    )
    assert store.get_derivation(derivation.id) == derivation
    assert set(derivation.premises) == {
        PremiseRef("local", premise.id),
        PremiseRef("uri", remote),
    }

    with pytest.raises(InvariantViolation, match="conclusion"):
        store.add_derivation(
            claim_id=conclusion.id,
            method="entailment",
            premises=[conclusion.id],
            actor=SYSTEM,
        )
    with pytest.raises(InvariantViolation, match="unique"):
        store.add_derivation(
            claim_id=conclusion.id,
            method="entailment",
            premises=[premise.id, premise.id],
            actor=SYSTEM,
        )
    with pytest.raises(InvariantViolation, match="does not exist"):
        store.add_derivation(
            claim_id=conclusion.id,
            method="entailment",
            premises=[new_id()],
            actor=SYSTEM,
        )


def test_every_store_insert_gets_one_global_ledger_record(store: TruthStore):
    evidence = _capture(store, "ledger source")
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact="ledger"),
        actor=HUMAN,
    )
    premise = _claim(store, "Ledger premise")
    claim = _claim(store, "Ledger conclusion")
    link = store.add_link(
        from_claim_id=claim.id,
        link_type="supports_span",
        to_kind="evidence_span",
        to_ref=span.id,
        actor=HUMAN,
    )
    derivation = store.add_derivation(
        claim_id=claim.id,
        method="entailment",
        premises=[premise.id],
        actor=SYSTEM,
    )
    retraction = store.retract_link(link_id=link.id, actor=HUMAN)

    conn = store.connect()
    try:
        rows = {
            (row["record_type"], row["record_key"])
            for row in conn.execute(
                "SELECT record_type, record_key FROM ledger_records"
            )
        }
    finally:
        conn.close()
    assert ("evidence", evidence.id) in rows
    assert ("evidence_span", span.id) in rows
    assert ("claim", claim.id) in rows
    assert ("claim_link", link.id) in rows
    assert ("derivation", derivation.id) in rows
    assert ("link_retraction", retraction.link_id) in rows
    assert (
        "derivation_premise",
        canonical_json({"derivation_id": derivation.id, "premise_ref": premise.id}),
    ) in rows
    assert sum(kind == "claim_status_event" for kind, _ in rows) == 2


def test_explicit_ledger_seq_is_import_only_locked_seam(store: TruthStore):
    conn = store.connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        assert (
            store._insert_ledger_record_locked(
                conn,
                "import_marker",
                "first",
                seq=100,
            )
            == 100
        )
        conn.commit()
    finally:
        conn.close()
    verify = store.connect()
    try:
        assert (
            verify.execute(
                "SELECT seq FROM ledger_records WHERE record_type = 'import_marker'"
            ).fetchone()[0]
            == 100
        )
    finally:
        verify.close()

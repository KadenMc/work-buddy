from __future__ import annotations

from base64 import b64encode
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor, GestureError, InvariantViolation
from work_buddy.truth.export import export_store
from work_buddy.truth.identity import new_id, utc_now
from work_buddy.truth.lifecycle import TruthLifecycle
from work_buddy.truth.migrations import REDACTED_SELECTOR_JSON
from work_buddy.truth.redact import TruthRedactor, policy_basis_ref
from work_buddy.truth.store import GestureRecord, PostCommitHookError, TruthStore


HUMAN = Actor("human", "human:test")
SYSTEM = Actor("system", "system:truth-policy")


class _SimulatedCrash(BaseException):
    """Interrupt execution without exercising normal exception cleanup."""


def _profile(**overrides):
    profile = {
        "store_id": new_id(),
        "profile": "redaction-test",
        "title": "Redaction test store",
        "allowed_claim_kinds": ["fact"],
        "required_fields": {},
        "gate": {
            "rejected_content": "redact",
            "confirmation_surfaces": ["dashboard"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": True,
        "proposal_max_age": "2h",
        "extensions": {"expired_proposal_content": "redact"},
    }
    profile.update(overrides)
    return profile


class _Lifecycle:
    """A strict local double for the separately tested lifecycle component."""

    def __init__(self, store: TruthStore) -> None:
        self.store = store

    def verify_and_consume_gesture(
        self,
        *,
        gesture_id,
        actor,
        subject_ref,
        payload_sha256,
        expected_context_sha256,
        allowed_kinds,
        observed_at,
        conn,
    ):
        gesture = self.store._get_gesture_locked(conn, gesture_id)
        if gesture is None:
            raise GestureError("gesture does not exist")
        if actor.kind != "human" or gesture.actor_ref != actor.ref:
            raise GestureError("gesture actor mismatch")
        if gesture.kind not in allowed_kinds:
            raise GestureError("gesture kind mismatch")
        if gesture.subject_ref != subject_ref:
            raise GestureError("gesture subject mismatch")
        if gesture.payload_sha256 != payload_sha256:
            raise GestureError("gesture payload mismatch")
        if gesture.context_sha256 != expected_context_sha256:
            raise GestureError("gesture context mismatch")
        return self.store._consume_gesture_locked(conn, gesture_id, observed_at)

    def transition_claim(
        self,
        *,
        claim_id,
        status,
        actor,
        basis_kind,
        basis_ref,
        note,
        at,
        conn,
    ):
        event = self.store._insert_status_event_locked(
            conn,
            claim_id=claim_id,
            status=status,
            actor=actor,
            basis_kind=basis_kind,
            basis_ref=basis_ref,
            note=note,
            at=at,
        )
        return SimpleNamespace(event=event, created=True)


@pytest.fixture
def store(truth_root: Path) -> TruthStore:
    return TruthStore.create(truth_root, _profile(), inline_content_bytes=8)


@pytest.fixture
def redactor(store: TruthStore) -> TruthRedactor:
    return TruthRedactor(store, lifecycle=_Lifecycle(store))


def _claim(store: TruthStore, proposition: str = "Keep this claim"):
    return store.propose_claim(
        proposition=proposition,
        claim_kind="fact",
        actor=HUMAN,
    ).claim


def _status(store: TruthStore, claim_id: str, status: str):
    with store.write_transaction() as conn:
        return store._insert_status_event_locked(
            conn,
            claim_id=claim_id,
            status=status,
            actor=SYSTEM,
            basis_kind="rule",
            basis_ref=f"test:{status}",
        )


def _gesture(
    store: TruthStore,
    *,
    subject_ref: str,
    payload_sha256: str,
    context_sha256: str | None = None,
    actor_ref: str = HUMAN.ref or "",
    payload_excerpt: str = "content shown to the human",
) -> GestureRecord:
    record = GestureRecord(
        id=new_id(),
        at=utc_now(),
        surface="dashboard",
        actor_ref=actor_ref,
        kind="redact",
        subject_ref=subject_ref,
        payload_sha256=payload_sha256,
        payload_excerpt=payload_excerpt,
        context_sha256=context_sha256,
        expires_at=None,
        consumed_at=None,
    )
    with store.write_transaction() as conn:
        return store._insert_gesture_locked(conn, record)


def _begin_inflight_claim_redaction(
    store: TruthStore,
    claim_id: str,
    event_id: str,
):
    """Publish a recovery marker while retaining the SQLite writer lock."""

    at = utc_now()
    conn = store.connect()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE claims SET proposition = '[redacted]', structured_json = NULL, "
        "redacted_at = ? WHERE id = ?",
        (at, claim_id),
    )
    conn.execute(
        "INSERT INTO redaction_events "
        "(id, subject_kind, subject_ref, at, actor_ref, basis_kind, "
        "basis_ref, reason) VALUES (?, 'claim', ?, ?, ?, 'gesture', ?, 'privacy')",
        (event_id, claim_id, at, HUMAN.ref, f"gesture:{event_id}"),
    )
    store._insert_ledger_record_locked(conn, "redaction_event", event_id)
    store._queue_redaction_recovery_locked(conn, event_id)
    return conn


def test_gesture_redaction_retains_claim_identity_hash_and_appends_costatus(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    claim = _claim(store)
    _status(store, claim.id, "confirmed")
    gesture = _gesture(
        store,
        subject_ref=claim.id,
        payload_sha256=claim.canonical_sha256,
        context_sha256="ab" * 32,
    )

    result = redactor.redact(
        subject_kind="claim",
        subject_ref=claim.id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=gesture.id,
        expected_context_sha256="ab" * 32,
    )

    redacted = store.get_claim(claim.id)
    assert redacted is not None
    assert redacted.proposition == "[redacted]"
    assert redacted.structured_json is None
    assert redacted.canonical_sha256 == claim.canonical_sha256
    assert redacted.redacted_at == result.event.at
    assert result.status_event is not None
    assert result.status_event.status == "retracted"
    assert result.status_event.basis_kind == "redaction"
    assert result.status_event.basis_ref == result.event.id
    with store.connect() as conn:
        stored_gesture = store._get_gesture_locked(conn, gesture.id)
        assert stored_gesture.consumed_at is not None
        assert stored_gesture.payload_excerpt == "[redacted]"
        assert stored_gesture.payload_sha256 == claim.canonical_sha256

    with store.connect() as conn:
        ledger_types = [
            row[0]
            for row in conn.execute(
                "SELECT record_type FROM ledger_records ORDER BY seq"
            )
        ]
    assert ledger_types[-2:] == ["redaction_event", "claim_status_event"]


def test_redaction_composes_with_the_real_lifecycle_gesture_seam(
    store: TruthStore,
) -> None:
    lifecycle = TruthLifecycle(store)
    redactor = TruthRedactor(store, lifecycle=lifecycle)
    claim = _claim(store, "One exact private statement")
    gesture = lifecycle.mint_gesture(
        subject_ref=claim.id,
        actor=HUMAN,
        surface="dashboard",
        kind="redact",
        displayed_payload_sha256=claim.canonical_sha256,
    )

    result = redactor.redact(
        subject_kind="claim",
        subject_ref=claim.id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=gesture.id,
    )

    assert result.status_event is not None
    assert lifecycle.latest_status(claim.id).status == "retracted"
    with store.connect() as conn:
        stored_gesture = store._get_gesture_locked(conn, gesture.id)
        assert stored_gesture.consumed_at is not None
        assert stored_gesture.payload_excerpt == "[redacted]"


@pytest.mark.parametrize(
    ("terminal", "reason"),
    [("rejected", "rejected_content"), ("expired", "expired_content")],
)
def test_profile_policy_redacts_never_confirmed_terminal_claims(
    store: TruthStore,
    redactor: TruthRedactor,
    terminal: str,
    reason: str,
) -> None:
    claim = _claim(store, f"Policy {terminal}")
    _status(store, claim.id, terminal)

    result = redactor.redact(
        subject_kind="claim",
        subject_ref=claim.id,
        actor=SYSTEM,
        reason=reason,
        basis_kind="policy",
        basis_ref=policy_basis_ref(store, reason),
    )

    assert store.get_claim(claim.id).proposition == "[redacted]"
    assert result.status_event is None


def test_expiry_and_policy_redaction_compose_in_one_outer_transaction(
    store: TruthStore,
) -> None:
    lifecycle = TruthLifecycle(store)
    redactor = TruthRedactor(store, lifecycle=lifecycle)
    claim = store.propose_claim(
        proposition="Discard this untouched option",
        claim_kind="fact",
        actor=HUMAN,
        created_at="2026-07-14T10:00:00+00:00",
        status_at="2026-07-14T10:00:00+00:00",
    ).claim
    observed = "2026-07-14T12:00:01+00:00"

    with store.write_transaction() as conn:
        lifecycle.expire_claim(
            claim_id=claim.id,
            actor=SYSTEM,
            observed_at=observed,
            conn=conn,
        )
        result = redactor.redact(
            subject_kind="claim",
            subject_ref=claim.id,
            actor=SYSTEM,
            reason="expired_content",
            basis_kind="policy",
            basis_ref=policy_basis_ref(store, "expired_content"),
            at=observed,
            conn=conn,
        )

    assert result.status_event is None
    assert lifecycle.latest_status(claim.id).status == "expired"
    assert store.get_claim(claim.id).proposition == "[redacted]"


def test_policy_cannot_redact_content_that_was_ever_confirmed(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    claim = _claim(store)
    _status(store, claim.id, "confirmed")
    _status(store, claim.id, "retracted")

    with pytest.raises(InvariantViolation, match="ever confirmed"):
        redactor.redact(
            subject_kind="claim",
            subject_ref=claim.id,
            actor=SYSTEM,
            reason="rejected_content",
            basis_kind="policy",
            basis_ref=policy_basis_ref(store, "rejected_content"),
        )


def test_policy_is_exact_reason_status_and_profile_bound(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    claim = _claim(store)
    _status(store, claim.id, "rejected")

    with pytest.raises(InvariantViolation, match="exactly"):
        redactor.redact(
            subject_kind="claim",
            subject_ref=claim.id,
            actor=SYSTEM,
            reason="rejected_content",
            basis_kind="policy",
            basis_ref="profile:anything:gate.rejected_content",
        )
    with pytest.raises(InvariantViolation, match="standing policies"):
        redactor.redact(
            subject_kind="claim",
            subject_ref=claim.id,
            actor=SYSTEM,
            reason="privacy",
            basis_kind="policy",
            basis_ref="profile:redaction-test:privacy",
        )


def test_gesture_mismatch_rolls_back_content_event_and_consumption(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    claim = _claim(store)
    gesture = _gesture(
        store,
        subject_ref=claim.id,
        payload_sha256="ff" * 32,
    )

    with pytest.raises(GestureError, match="payload mismatch"):
        redactor.redact(
            subject_kind="claim",
            subject_ref=claim.id,
            actor=HUMAN,
            reason="privacy",
            basis_kind="gesture",
            basis_ref=gesture.id,
        )

    assert store.get_claim(claim.id).redacted_at is None
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM redaction_events").fetchone()[0] == 0
        assert store._get_gesture_locked(conn, gesture.id).consumed_at is None


def test_evidence_redaction_cascades_quotes_and_deletes_unshared_blob(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///source.bin",
        actor=HUMAN,
        acquisition_method="paste",
        content=b"private source bytes",
    )
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact="private"),
        actor=HUMAN,
        snapshot_text="private source bytes",
    )
    path = store.resolve_blob_path(evidence.content_path or "")
    gesture = _gesture(
        store,
        subject_ref=evidence.id,
        payload_sha256=evidence.content_sha256,
    )

    result = redactor.redact(
        subject_kind="evidence",
        subject_ref=evidence.id,
        actor=HUMAN,
        reason="source_takedown",
        basis_kind="gesture",
        basis_ref=gesture.id,
    )

    assert store.get_evidence(evidence.id).content_path is None
    redacted_span = store.get_span(span.id)
    assert redacted_span.quote_exact is None
    assert redacted_span.selector_json == REDACTED_SELECTOR_JSON
    assert result.cascade_events[0].subject_ref == span.id
    assert result.blob_deleted is True
    assert not path.exists()
    with store.connect() as conn:
        assert (
            store._get_gesture_locked(conn, gesture.id).payload_excerpt == "[redacted]"
        )


def test_post_commit_hook_failure_still_deletes_redacted_blob(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///hook-failure.bin",
        actor=HUMAN,
        acquisition_method="paste",
        content=b"sensitive bytes must not survive",
    )
    path = store.resolve_blob_path(evidence.content_path or "")
    gesture = _gesture(
        store,
        subject_ref=evidence.id,
        payload_sha256=evidence.content_sha256,
    )

    def fail_observer(_store: TruthStore) -> None:
        raise RuntimeError("observer failed")

    store._on_commit = fail_observer
    with pytest.raises(PostCommitHookError, match="post-commit hook failed"):
        redactor.redact(
            subject_kind="evidence",
            subject_ref=evidence.id,
            actor=HUMAN,
            reason="privacy",
            basis_kind="gesture",
            basis_ref=gesture.id,
        )

    assert store.get_evidence(evidence.id).redacted_at is not None
    assert not path.exists()


def test_commit_boundary_crash_recovers_export_marker_and_blob_on_open(
    store: TruthStore,
    redactor: TruthRedactor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = b"COMMIT-BOUNDARY-BLOB-PRIVATE-93f7a2"
    locator = "file:///commit-boundary-private.bin"
    evidence = store.capture_evidence(
        kind="document",
        source_locator=locator,
        actor=HUMAN,
        acquisition_method="paste",
        content=secret,
    )
    blob = store.resolve_blob_path(evidence.content_path or "")
    gesture = _gesture(
        store,
        subject_ref=evidence.id,
        payload_sha256=evidence.content_sha256,
    )
    event_id = new_id()
    encoded_secret = b64encode(secret)
    assert encoded_secret in store.paths.claims_export.read_bytes()

    def crash_before_post_commit_hook(**_kwargs) -> None:
        raise _SimulatedCrash("immediately after SQLite commit")

    monkeypatch.setattr(store, "_run_on_commit", crash_before_post_commit_hook)
    with pytest.raises(_SimulatedCrash, match="after SQLite commit"):
        redactor.redact(
            subject_kind="evidence",
            subject_ref=evidence.id,
            actor=HUMAN,
            reason="privacy",
            basis_kind="gesture",
            basis_ref=gesture.id,
            event_id=event_id,
        )

    recovery = store._redaction_recovery_intent_path(event_id)
    blob_intent = store._blob_cleanup_intent_path(evidence.content_sha256)
    current = store.get_evidence(evidence.id)
    assert current is not None and current.redacted_at is not None
    assert current.content_path is None
    assert recovery.name == event_id
    assert recovery.read_bytes() == b""
    assert secret.decode() not in str(recovery)
    assert locator not in str(recovery)
    assert blob_intent.is_file()
    assert blob.is_file()
    # The pre-redaction projection is destroyed before COMMIT. A hard crash
    # can leave it absent, never stale with readable content.
    assert not store.paths.claims_export.exists()

    reopened = TruthStore.open(store.paths.sidecar, inline_content_bytes=8)

    assert not recovery.exists()
    assert not blob_intent.exists()
    assert not blob.exists()
    assert encoded_secret not in reopened.paths.claims_export.read_bytes()


def test_commit_boundary_crash_recovers_claim_export_on_open(
    store: TruthStore,
    redactor: TruthRedactor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "COMMIT-BOUNDARY-CLAIM-PRIVATE-8b41df"
    claim = _claim(store, secret)
    gesture = _gesture(
        store,
        subject_ref=claim.id,
        payload_sha256=claim.canonical_sha256,
    )
    event_id = new_id()
    assert secret.encode() in store.paths.claims_export.read_bytes()

    monkeypatch.setattr(
        store,
        "_run_on_commit",
        lambda **_kwargs: (_ for _ in ()).throw(_SimulatedCrash()),
    )
    with pytest.raises(_SimulatedCrash):
        redactor.redact(
            subject_kind="claim",
            subject_ref=claim.id,
            actor=HUMAN,
            reason="privacy",
            basis_kind="gesture",
            basis_ref=gesture.id,
            event_id=event_id,
        )

    recovery = store._redaction_recovery_intent_path(event_id)
    assert store.get_claim(claim.id).proposition == "[redacted]"
    assert recovery.read_bytes() == b""
    assert secret not in str(recovery)
    assert not store.paths.claims_export.exists()

    reopened = TruthStore.open(store.paths.sidecar, inline_content_bytes=8)

    assert not recovery.exists()
    assert secret.encode() not in reopened.paths.claims_export.read_bytes()


def test_export_disabled_recovery_observer_waits_for_inflight_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TruthStore.create(
        tmp_path / "observer-barrier",
        _profile(export_committed=False),
        inline_content_bytes=8,
    )
    claim = _claim(store, "OBSERVER-MUST-NOT-SEE-PRECOMMIT-SECRET")
    observed: list[str] = []
    observer_called = Event()

    def observer(recovered: TruthStore) -> None:
        observed.append(recovered.get_claim(claim.id).proposition)
        observer_called.set()

    recovery = TruthStore.open(
        store.paths.sidecar,
        inline_content_bytes=8,
        on_commit=observer,
    )
    event_id = new_id()
    conn = _begin_inflight_claim_redaction(store, claim.id, event_id)
    barrier_entered = Event()
    release_barrier = Event()

    def controlled_barrier() -> None:
        barrier_entered.set()
        assert release_barrier.wait(timeout=10)

    monkeypatch.setattr(recovery, "_writer_barrier", controlled_barrier)
    results: list[tuple[str, ...]] = []
    failures: list[BaseException] = []

    def recover() -> None:
        try:
            results.append(recovery.recover_pending_redactions())
        except BaseException as exc:  # pragma: no cover - assertion reports detail
            failures.append(exc)

    worker = Thread(target=recover)
    worker.start()
    assert barrier_entered.wait(timeout=10)
    assert not observer_called.is_set()

    conn.execute("COMMIT")
    conn.close()
    release_barrier.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert failures == []
    assert results == [(event_id,)]
    assert observed == ["[redacted]"]
    assert not store._redaction_recovery_intent_path(event_id).exists()


def test_recovery_recheck_avoids_duplicate_export_disabled_observer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TruthStore.create(
        tmp_path / "observer-recheck",
        _profile(export_committed=False),
        inline_content_bytes=8,
    )
    claim = _claim(store, "ORIGINAL-HOOK-WINS-WHILE-RECOVERY-WAITS")
    observed: list[str] = []

    def observer(recovered: TruthStore) -> None:
        observed.append(recovered.get_claim(claim.id).proposition)

    store._on_commit = observer
    recovery = TruthStore.open(
        store.paths.sidecar,
        inline_content_bytes=8,
        on_commit=observer,
    )
    event_id = new_id()
    conn = _begin_inflight_claim_redaction(store, claim.id, event_id)
    barrier_entered = Event()
    release_barrier = Event()

    def controlled_barrier() -> None:
        barrier_entered.set()
        assert release_barrier.wait(timeout=10)

    monkeypatch.setattr(recovery, "_writer_barrier", controlled_barrier)
    failures: list[BaseException] = []

    def recover() -> None:
        try:
            recovery.recover_pending_redactions()
        except BaseException as exc:  # pragma: no cover - assertion reports detail
            failures.append(exc)

    worker = Thread(target=recover)
    worker.start()
    assert barrier_entered.wait(timeout=10)

    conn.execute("COMMIT")
    conn.close()
    store._run_on_commit(required=False)
    assert observed == ["[redacted]"]

    release_barrier.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert failures == []
    assert observed == ["[redacted]"]
    assert not store._redaction_recovery_intent_path(event_id).exists()


def test_rolled_back_redaction_rebuilds_export_without_commit_observer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TruthStore.create(
        tmp_path / "observer-rollback",
        _profile(export_committed=True),
        inline_content_bytes=8,
    )
    secret = "ROLLED-BACK-REDACTION-REMAINS-LIVE"
    claim = _claim(store, secret)
    observed: list[str] = []

    def observer(recovered: TruthStore) -> None:
        observed.append(recovered.get_claim(claim.id).proposition)

    recovery = TruthStore.open(
        store.paths.sidecar,
        inline_content_bytes=8,
        on_commit=observer,
    )
    event_id = new_id()
    conn = _begin_inflight_claim_redaction(store, claim.id, event_id)
    assert not store.paths.claims_export.exists()
    barrier_entered = Event()
    release_barrier = Event()

    def controlled_barrier() -> None:
        barrier_entered.set()
        assert release_barrier.wait(timeout=10)

    monkeypatch.setattr(recovery, "_writer_barrier", controlled_barrier)
    results: list[tuple[str, ...]] = []
    failures: list[BaseException] = []

    def recover() -> None:
        try:
            results.append(recovery.recover_pending_redactions())
        except BaseException as exc:  # pragma: no cover - assertion reports detail
            failures.append(exc)

    worker = Thread(target=recover)
    worker.start()
    assert barrier_entered.wait(timeout=10)
    assert observed == []

    conn.execute("ROLLBACK")
    conn.close()
    release_barrier.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert failures == []
    assert results == [(event_id,)]
    assert observed == []
    assert store.get_claim(claim.id).proposition == secret
    assert secret.encode() in store.paths.claims_export.read_bytes()
    assert not store._redaction_recovery_intent_path(event_id).exists()


@pytest.mark.parametrize(
    ("directory_name", "recover"),
    [
        (
            "pending-redaction-recoveries",
            lambda store: store._pending_redaction_recovery_paths(),
        ),
        (
            "pending-blob-deletions",
            lambda store: store.recover_pending_blob_cleanups(),
        ),
    ],
)
def test_concurrent_recovery_directory_removal_is_an_empty_snapshot(
    store: TruthStore,
    monkeypatch: pytest.MonkeyPatch,
    directory_name: str,
    recover,
) -> None:
    directory = store.paths.sidecar / directory_name
    directory.mkdir()
    path_type = type(directory)
    iterdir = path_type.iterdir

    def remove_before_enumeration(path: Path):
        if path == directory:
            directory.rmdir()
            raise FileNotFoundError(directory)
        return iterdir(path)

    monkeypatch.setattr(path_type, "iterdir", remove_before_enumeration)

    assert recover(store) == ()


def test_commit_boundary_crash_recovers_inline_evidence_export_on_open(
    store: TruthStore,
    redactor: TruthRedactor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "INL7x9"
    locator = "file:///inline-commit-boundary.txt"
    evidence = store.capture_evidence(
        kind="document",
        source_locator=locator,
        actor=HUMAN,
        acquisition_method="paste",
        content=secret,
    )
    assert evidence.content == secret
    assert evidence.content_path is None
    gesture = _gesture(
        store,
        subject_ref=evidence.id,
        payload_sha256=evidence.content_sha256,
    )
    event_id = new_id()
    assert secret.encode() in store.paths.claims_export.read_bytes()

    monkeypatch.setattr(
        store,
        "_run_on_commit",
        lambda **_kwargs: (_ for _ in ()).throw(_SimulatedCrash()),
    )
    with pytest.raises(_SimulatedCrash):
        redactor.redact(
            subject_kind="evidence",
            subject_ref=evidence.id,
            actor=HUMAN,
            reason="privacy",
            basis_kind="gesture",
            basis_ref=gesture.id,
            event_id=event_id,
        )

    recovery = store._redaction_recovery_intent_path(event_id)
    current = store.get_evidence(evidence.id)
    assert current is not None and current.redacted_at is not None
    assert current.content is None
    assert recovery.read_bytes() == b""
    assert secret not in str(recovery)
    assert locator not in str(recovery)
    assert not store.paths.claims_export.exists()

    reopened = TruthStore.open(store.paths.sidecar, inline_content_bytes=8)

    assert not recovery.exists()
    assert secret.encode() not in reopened.paths.claims_export.read_bytes()


def test_precommit_rollback_leaves_safe_intent_that_open_cancels(
    store: TruthStore,
    redactor: TruthRedactor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = b"ROLLBACK-PRIVATE-BLOB-4af951"
    locator = "file:///rollback-before-commit-private.bin"
    evidence = store.capture_evidence(
        kind="document",
        source_locator=locator,
        actor=HUMAN,
        acquisition_method="paste",
        content=secret,
    )
    blob = store.resolve_blob_path(evidence.content_path or "")
    gesture = _gesture(
        store,
        subject_ref=evidence.id,
        payload_sha256=evidence.content_sha256,
    )
    queue = store._queue_blob_cleanup_locked

    def queue_then_abort(conn, digest: str) -> Path:
        intent = queue(conn, digest)
        assert intent.name == evidence.content_sha256
        assert intent.read_bytes() == b""
        assert secret.decode() not in str(intent)
        assert locator not in str(intent)
        raise RuntimeError("fail before database commit")

    monkeypatch.setattr(store, "_queue_blob_cleanup_locked", queue_then_abort)
    with pytest.raises(RuntimeError, match="before database commit"):
        redactor.redact(
            subject_kind="evidence",
            subject_ref=evidence.id,
            actor=HUMAN,
            reason="privacy",
            basis_kind="gesture",
            basis_ref=gesture.id,
        )

    intent = store._blob_cleanup_intent_path(evidence.content_sha256)
    current = store.get_evidence(evidence.id)
    assert current is not None and current.redacted_at is None
    assert current.content_path == evidence.content_path
    assert intent.is_file()
    assert blob.is_file()

    reopened = TruthStore.open(store.paths.sidecar, inline_content_bytes=8)

    assert reopened.blob_reference_count(evidence.content_sha256) == 1
    assert blob.is_file()
    assert not intent.exists()


def test_next_open_recovers_crash_after_redaction_commit_before_unlink(
    store: TruthStore,
    redactor: TruthRedactor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = b"COMMITTED-PRIVATE-BLOB-9c150e"
    locator = "file:///commit-before-unlink-private.bin"
    evidence = store.capture_evidence(
        kind="document",
        source_locator=locator,
        actor=HUMAN,
        acquisition_method="paste",
        content=secret,
    )
    blob = store.resolve_blob_path(evidence.content_path or "")
    gesture = _gesture(
        store,
        subject_ref=evidence.id,
        payload_sha256=evidence.content_sha256,
    )

    def crash_before_unlink(_digest: str) -> bool:
        raise _SimulatedCrash("after commit, before unlink")

    monkeypatch.setattr(store, "_finish_blob_cleanup", crash_before_unlink)
    with pytest.raises(_SimulatedCrash, match="before unlink"):
        redactor.redact(
            subject_kind="evidence",
            subject_ref=evidence.id,
            actor=HUMAN,
            reason="privacy",
            basis_kind="gesture",
            basis_ref=gesture.id,
        )

    intent = store._blob_cleanup_intent_path(evidence.content_sha256)
    current = store.get_evidence(evidence.id)
    assert current is not None and current.redacted_at is not None
    assert current.content_path is None
    assert intent.name == evidence.content_sha256
    assert intent.read_bytes() == b""
    assert secret.decode() not in str(intent)
    assert locator not in str(intent)
    assert blob.is_file()

    reopened = TruthStore.open(store.paths.sidecar, inline_content_bytes=8)

    assert not blob.exists()
    assert not intent.exists()
    assert reopened.recover_pending_blob_cleanups() == ()


def test_explicit_deferred_cleanup_finishes_and_clears_durable_intent(
    store: TruthStore,
    redactor: TruthRedactor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///caller-finished-cleanup.bin",
        actor=HUMAN,
        acquisition_method="paste",
        content=b"CALLER-FINISHED-PRIVATE-BLOB-52c3d6",
    )
    blob = store.resolve_blob_path(evidence.content_path or "")
    gesture = _gesture(
        store,
        subject_ref=evidence.id,
        payload_sha256=evidence.content_sha256,
    )

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(
            store,
            "_finish_blob_cleanup",
            lambda _digest: (_ for _ in ()).throw(_SimulatedCrash()),
        )
        with pytest.raises(_SimulatedCrash):
            redactor.redact(
                subject_kind="evidence",
                subject_ref=evidence.id,
                actor=HUMAN,
                reason="privacy",
                basis_kind="gesture",
                basis_ref=gesture.id,
            )

    intent = store._blob_cleanup_intent_path(evidence.content_sha256)
    assert intent.is_file()

    assert redactor.cleanup_redacted_blob(evidence.content_sha256) is True
    assert not blob.exists()
    assert not intent.exists()


def test_next_open_recovers_crash_after_unlink_before_intent_removal(
    store: TruthStore,
    redactor: TruthRedactor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///unlink-before-intent-removal.bin",
        actor=HUMAN,
        acquisition_method="paste",
        content=b"UNLINKED-PRIVATE-BLOB-01b6c3",
    )
    blob = store.resolve_blob_path(evidence.content_path or "")
    gesture = _gesture(
        store,
        subject_ref=evidence.id,
        payload_sha256=evidence.content_sha256,
    )

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(
            store,
            "_finish_blob_cleanup",
            lambda _digest: (_ for _ in ()).throw(_SimulatedCrash()),
        )
        with pytest.raises(_SimulatedCrash):
            redactor.redact(
                subject_kind="evidence",
                subject_ref=evidence.id,
                actor=HUMAN,
                reason="privacy",
                basis_kind="gesture",
                basis_ref=gesture.id,
            )

    intent = store._blob_cleanup_intent_path(evidence.content_sha256)
    path_type = type(intent)
    unlink = path_type.unlink

    def crash_removing_intent(path: Path, *args, **kwargs) -> None:
        if path == intent:
            raise _SimulatedCrash("after unlink, before intent removal")
        unlink(path, *args, **kwargs)

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(path_type, "unlink", crash_removing_intent)
        with pytest.raises(_SimulatedCrash, match="intent removal"):
            store.recover_pending_blob_cleanups()

    assert not blob.exists()
    assert intent.is_file()

    reopened = TruthStore.open(store.paths.sidecar, inline_content_bytes=8)

    assert not blob.exists()
    assert not intent.exists()
    assert reopened.recover_pending_blob_cleanups() == ()


def test_reintroduced_reference_cancels_pending_cleanup_on_open(
    store: TruthStore,
    redactor: TruthRedactor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"REINTRODUCED-SHARED-BLOB-d74432"
    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///original-private.bin",
        actor=HUMAN,
        acquisition_method="paste",
        content=content,
    )
    blob = store.resolve_blob_path(evidence.content_path or "")
    gesture = _gesture(
        store,
        subject_ref=evidence.id,
        payload_sha256=evidence.content_sha256,
    )

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(
            store,
            "_finish_blob_cleanup",
            lambda _digest: (_ for _ in ()).throw(_SimulatedCrash()),
        )
        with pytest.raises(_SimulatedCrash):
            redactor.redact(
                subject_kind="evidence",
                subject_ref=evidence.id,
                actor=HUMAN,
                reason="privacy",
                basis_kind="gesture",
                basis_ref=gesture.id,
            )

    intent = store._blob_cleanup_intent_path(evidence.content_sha256)
    replacement = store.capture_evidence(
        kind="document",
        source_locator="file:///new-live-reference.bin",
        actor=HUMAN,
        acquisition_method="paste",
        content=content,
    )
    assert replacement.content_sha256 == evidence.content_sha256
    assert intent.is_file()

    reopened = TruthStore.open(store.paths.sidecar, inline_content_bytes=8)

    assert reopened.blob_reference_count(evidence.content_sha256) == 1
    assert reopened.read_evidence_bytes(replacement.id) == content
    assert blob.is_file()
    assert not intent.exists()


def test_failed_recovery_export_cannot_leave_pre_redaction_plaintext(
    store: TruthStore,
    redactor: TruthRedactor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "STALE-EXPORT-PRIVATE-8d3f24"
    claim = _claim(store, secret)
    gesture = _gesture(
        store,
        subject_ref=claim.id,
        payload_sha256=claim.canonical_sha256,
    )
    assert secret.encode() in store.paths.claims_export.read_bytes()

    def fail_export(_store: TruthStore, *_args, **_kwargs) -> None:
        raise RuntimeError("forced recovery export failure")

    monkeypatch.setattr("work_buddy.truth.export.export_store", fail_export)
    with pytest.raises(PostCommitHookError, match="post-commit hook failed"):
        redactor.redact(
            subject_kind="claim",
            subject_ref=claim.id,
            actor=HUMAN,
            reason="privacy",
            basis_kind="gesture",
            basis_ref=gesture.id,
        )

    assert store.get_claim(claim.id).proposition == "[redacted]"
    assert not store.paths.claims_export.exists()


def test_shared_blob_survives_until_last_reference_is_redacted(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    content = b"one shared private blob"
    rows = [
        store.capture_evidence(
            kind="document",
            source_locator=f"file:///source-{index}.bin",
            actor=HUMAN,
            acquisition_method="paste",
            content=content,
        )
        for index in (1, 2)
    ]
    path = store.resolve_blob_path(rows[0].content_path or "")

    first = redactor.redact(
        subject_kind="evidence",
        subject_ref=rows[0].id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=_gesture(
            store,
            subject_ref=rows[0].id,
            payload_sha256=rows[0].content_sha256,
        ).id,
    )
    assert first.blob_deleted is False
    assert path.exists()

    second = redactor.redact(
        subject_kind="evidence",
        subject_ref=rows[1].id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=_gesture(
            store,
            subject_ref=rows[1].id,
            payload_sha256=rows[1].content_sha256,
        ).id,
    )
    assert second.blob_deleted is True
    assert not path.exists()


def test_caller_transaction_refuses_blob_redaction_without_side_effects(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///rollback.bin",
        actor=HUMAN,
        acquisition_method="paste",
        content=b"rollback private bytes",
    )
    path = store.resolve_blob_path(evidence.content_path or "")
    gesture = _gesture(
        store,
        subject_ref=evidence.id,
        payload_sha256=evidence.content_sha256,
    )

    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(InvariantViolation, match="caller-owned transaction"):
            redactor.redact(
                subject_kind="evidence",
                subject_ref=evidence.id,
                actor=HUMAN,
                reason="privacy",
                basis_kind="gesture",
                basis_ref=gesture.id,
                conn=conn,
            )
        assert path.exists()
        conn.rollback()
    finally:
        conn.close()

    assert store.get_evidence(evidence.id).redacted_at is None
    assert path.exists()
    with store.connect() as verify:
        assert store._get_gesture_locked(verify, gesture.id).consumed_at is None
    assert redactor.cleanup_redacted_blob(evidence.content_sha256) is False


def test_direct_span_redaction_scrubs_selector_and_only_bound_gestures(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    private_exact = "PRIVATE-SPAN-8d951f"
    source = f"public prefix {private_exact} public middle SAFE-SPAN public suffix"
    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///direct-span.txt",
        actor=HUMAN,
        acquisition_method="paste",
        content=source,
    )
    private_span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(
            exact=private_exact,
            prefix="public prefix ",
            suffix=" public middle",
        ),
        actor=HUMAN,
    )
    sibling_span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact="SAFE-SPAN"),
        actor=HUMAN,
    )
    private_gesture = _gesture(
        store,
        subject_ref=private_span.id,
        payload_sha256=private_span.span_sha256,
        payload_excerpt=f"reviewed {private_exact}",
    )
    sibling_gesture = _gesture(
        store,
        subject_ref=sibling_span.id,
        payload_sha256=sibling_span.span_sha256,
        payload_excerpt="reviewed SAFE-SPAN",
    )
    sibling_selector = sibling_span.selector_json

    result = redactor.redact(
        subject_kind="span",
        subject_ref=private_span.id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=private_gesture.id,
    )

    assert result.event.subject_ref == private_span.id
    redacted = store.get_span(private_span.id)
    assert redacted.id == private_span.id
    assert redacted.span_sha256 == private_span.span_sha256
    assert redacted.quote_exact is None
    assert redacted.selector_json == REDACTED_SELECTOR_JSON
    assert store.get_evidence(evidence.id).redacted_at is None
    assert store.get_span(sibling_span.id).selector_json == sibling_selector
    with store.connect() as conn:
        stored_private = store._get_gesture_locked(conn, private_gesture.id)
        stored_sibling = store._get_gesture_locked(conn, sibling_gesture.id)
    assert stored_private.payload_excerpt == "[redacted]"
    assert stored_private.payload_sha256 == private_span.span_sha256
    assert stored_private.consumed_at is not None
    assert stored_sibling.payload_excerpt == "reviewed SAFE-SPAN"
    assert stored_sibling.consumed_at is None


def test_redaction_removes_secrets_from_database_and_recovery_export(
    store: TruthStore,
) -> None:
    lifecycle = TruthLifecycle(store)
    redactor = TruthRedactor(store, lifecycle=lifecycle)
    claim_secret = "PRIVATE-CLAIM-c1358a"
    structured_secret = "PRIVATE-STRUCTURED-47f31b"
    exact_secret = "PRIVATE-EXACT-671ea4"
    prefix_secret = "PRIVATE-PREFIX-5be37d "
    suffix_secret = " PRIVATE-SUFFIX-a326e9"
    evidence_secret = f"{prefix_secret}{exact_secret}{suffix_secret}"
    secrets = (
        claim_secret,
        structured_secret,
        exact_secret,
        prefix_secret.strip(),
        suffix_secret.strip(),
    )

    claim = store.propose_claim(
        proposition=claim_secret,
        claim_kind="fact",
        structured={"private": structured_secret},
        actor=HUMAN,
    ).claim
    claim_gesture = _gesture(
        store,
        subject_ref=claim.id,
        payload_sha256=claim.canonical_sha256,
        payload_excerpt=f"{claim_secret} {structured_secret}",
    )
    claim_result = redactor.redact(
        subject_kind="claim",
        subject_ref=claim.id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=claim_gesture.id,
    )

    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///privacy-export.txt",
        actor=HUMAN,
        acquisition_method="paste",
        content=evidence_secret,
    )
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(
            exact=exact_secret,
            prefix=prefix_secret,
            suffix=suffix_secret,
        ),
        actor=HUMAN,
    )
    evidence_gesture = _gesture(
        store,
        subject_ref=evidence.id,
        payload_sha256=evidence.content_sha256,
        payload_excerpt=evidence_secret,
    )
    span_gesture = _gesture(
        store,
        subject_ref=span.id,
        payload_sha256=span.span_sha256,
        payload_excerpt=f"{prefix_secret}{exact_secret}{suffix_secret}",
    )

    public_evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///public-export.txt",
        actor=HUMAN,
        acquisition_method="paste",
        content="PUBLIC-PREFIX SAFE-EXACT PUBLIC-SUFFIX",
    )
    public_span = store.mark_span(
        evidence_id=public_evidence.id,
        selector=CompositeSelector(
            exact="SAFE-EXACT",
            prefix="PUBLIC-PREFIX ",
            suffix=" PUBLIC-SUFFIX",
        ),
        actor=HUMAN,
    )
    public_gesture = _gesture(
        store,
        subject_ref=public_span.id,
        payload_sha256=public_span.span_sha256,
        payload_excerpt="PUBLIC-PREFIX SAFE-EXACT PUBLIC-SUFFIX",
    )

    evidence_result = redactor.redact(
        subject_kind="evidence",
        subject_ref=evidence.id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=evidence_gesture.id,
    )

    redacted_claim = store.get_claim(claim.id)
    redacted_evidence = store.get_evidence(evidence.id)
    redacted_span = store.get_span(span.id)
    assert (redacted_claim.id, redacted_claim.canonical_sha256) == (
        claim.id,
        claim.canonical_sha256,
    )
    assert (redacted_evidence.id, redacted_evidence.content_sha256) == (
        evidence.id,
        evidence.content_sha256,
    )
    assert (redacted_span.id, redacted_span.span_sha256) == (
        span.id,
        span.span_sha256,
    )
    assert redacted_span.selector_json == REDACTED_SELECTOR_JSON
    assert evidence_result.cascade_events[0].subject_ref == span.id
    assert claim_result.event.subject_ref == claim.id

    with store.connect() as conn:
        event_subjects = {
            (row["subject_kind"], row["subject_ref"])
            for row in conn.execute(
                "SELECT subject_kind, subject_ref FROM redaction_events"
            )
        }
        gestures = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, payload_sha256, payload_excerpt FROM gestures"
            )
        }
        logical_rows = b"\n".join(
            repr(tuple(row)).encode("utf-8")
            for table in ("claims", "evidence", "evidence_spans", "gestures")
            for row in conn.execute(f"SELECT * FROM {table}")
        )
    assert {("claim", claim.id), ("evidence", evidence.id), ("span", span.id)} <= (
        event_subjects
    )
    assert gestures[claim_gesture.id]["payload_excerpt"] == "[redacted]"
    assert gestures[evidence_gesture.id]["payload_excerpt"] == "[redacted]"
    assert gestures[span_gesture.id]["payload_excerpt"] == "[redacted]"
    assert gestures[claim_gesture.id]["payload_sha256"] == claim.canonical_sha256
    assert gestures[evidence_gesture.id]["payload_sha256"] == evidence.content_sha256
    assert gestures[span_gesture.id]["payload_sha256"] == span.span_sha256
    assert gestures[public_gesture.id]["payload_excerpt"] == (
        "PUBLIC-PREFIX SAFE-EXACT PUBLIC-SUFFIX"
    )
    assert store.get_span(public_span.id).selector_json == public_span.selector_json

    export_path = store.paths.export_dir / "privacy-check.jsonl"
    export_store(store, export_path)
    exported = export_path.read_bytes()
    with store.connect() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database_files = (
        store.paths.db,
        Path(f"{store.paths.db}-wal"),
    )
    database_bytes = b"".join(
        path.read_bytes() for path in database_files if path.exists()
    )
    for secret in secrets:
        assert secret.encode() not in logical_rows
        assert secret.encode() not in database_bytes
        assert secret.encode() not in exported


def test_cascade_failure_rolls_back_all_content_receipts_events_and_consumption(
    store: TruthStore,
    redactor: TruthRedactor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "ROLLBACK-PRIVATE-2c68a1"
    source = f"prefix {secret} suffix"
    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///redaction-rollback.txt",
        actor=HUMAN,
        acquisition_method="paste",
        content=source,
    )
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(
            exact=secret,
            prefix="prefix ",
            suffix=" suffix",
        ),
        actor=HUMAN,
    )
    evidence_gesture = _gesture(
        store,
        subject_ref=evidence.id,
        payload_sha256=evidence.content_sha256,
        payload_excerpt=source,
    )
    span_gesture = _gesture(
        store,
        subject_ref=span.id,
        payload_sha256=span.span_sha256,
        payload_excerpt=secret,
    )
    original_insert = redactor._insert_event_locked

    def fail_on_cascade(conn, **kwargs):
        if kwargs["subject_kind"] == "span":
            raise RuntimeError("forced cascade audit failure")
        return original_insert(conn, **kwargs)

    monkeypatch.setattr(redactor, "_insert_event_locked", fail_on_cascade)
    with pytest.raises(RuntimeError, match="forced cascade audit failure"):
        redactor.redact(
            subject_kind="evidence",
            subject_ref=evidence.id,
            actor=HUMAN,
            reason="privacy",
            basis_kind="gesture",
            basis_ref=evidence_gesture.id,
        )

    restored_evidence = store.get_evidence(evidence.id)
    restored_span = store.get_span(span.id)
    assert restored_evidence.content_path == evidence.content_path
    assert restored_evidence.redacted_at is None
    assert restored_span.quote_exact == secret
    assert restored_span.selector_json == span.selector_json
    assert restored_span.redacted_at is None
    assert store.resolve_blob_path(evidence.content_path or "").exists()
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM redaction_events").fetchone()[0] == 0
        restored_evidence_gesture = store._get_gesture_locked(conn, evidence_gesture.id)
        restored_span_gesture = store._get_gesture_locked(conn, span_gesture.id)
    assert restored_evidence_gesture.payload_excerpt == source
    assert restored_evidence_gesture.consumed_at is None
    assert restored_span_gesture.payload_excerpt == secret
    assert restored_span_gesture.consumed_at is None


def test_redaction_is_idempotent_without_consuming_a_second_gesture(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    claim = _claim(store)
    first_gesture = _gesture(
        store,
        subject_ref=claim.id,
        payload_sha256=claim.canonical_sha256,
    )
    first = redactor.redact(
        subject_kind="claim",
        subject_ref=claim.id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=first_gesture.id,
    )
    second_gesture = _gesture(
        store,
        subject_ref=claim.id,
        payload_sha256=claim.canonical_sha256,
    )

    second = redactor.redact(
        subject_kind="claim",
        subject_ref=claim.id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=second_gesture.id,
    )

    assert second.created is False
    assert second.event == first.event
    with store.connect() as conn:
        assert store._get_gesture_locked(conn, second_gesture.id).consumed_at is None
        assert (
            store._get_gesture_locked(conn, second_gesture.id).payload_excerpt
            == "[redacted]"
        )

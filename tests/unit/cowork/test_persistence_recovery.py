"""Crash-boundary and policy tests for Co-work persistence operations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

import work_buddy.truth.export as export_module
from work_buddy.cowork import (
    bootstrap,
    feedback,
    materialization,
    readiness,
    reimport,
    retirement,
    sitting_lifecycle,
)
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.export import UncompactedDocumentError, export_store
from work_buddy.truth.identity import canonical_json, new_id, sha256_bytes
from work_buddy.truth.profiles import DocumentSurfacePolicy, dump_profile
from work_buddy.truth.store import TruthStore

from .conftest import HUMAN


def _ready(store_ctx, *, path: str, key: str):
    source = f"# {key}\n\nOriginal body.\n".encode()
    target = store_ctx["root"] / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source)
    intent, _ = bootstrap.prepare_bootstrap(
        store_ctx["store"],
        metadata={
            "mode": "import",
            "path": path,
            "idempotency_key": key,
            "expected_file_sha256": sha256_bytes(source),
        },
        source=None,
        actor=HUMAN,
    )
    snapshot = b"YDOC:" + source
    receipt = bootstrap.commit_bootstrap(
        store_ctx["store"],
        bootstrap_id=intent.id,
        snapshot=snapshot,
        source_sha256=sha256_bytes(source),
        snapshot_sha256=sha256_bytes(snapshot),
        ydoc_schema=bootstrap.YDOC_SCHEMA,
        actor=HUMAN,
    )
    return documents.get_document(store_ctx["store"], intent.document_id), source, receipt


def test_snapshot_replacement_recovers_both_pointer_boundaries(
    store_ctx, monkeypatch
):
    store = store_ctx["store"]
    document, _source, ready = _ready(
        store_ctx, path="docs/recovery-head.md", key="recovery-head-0001"
    )
    old_snapshot = document.ydoc_snapshot_sha256
    assert old_snapshot is not None
    _, live_head = ydoc_store.append_update_cas(
        store,
        document_id=document.id,
        update=b"pending-edit",
        snapshot_sha256=old_snapshot,
        expected_structured_head_sha256=ready["structured_head_sha256"],
    )
    assert not store.paths.claims_export.exists()
    replacement_bytes = b"YDOC:replacement-complete"
    replacement_sha = sha256_bytes(replacement_bytes)

    with ydoc_store.document_lock(store, document.id):
        ydoc_store.prepare_snapshot_replacement_locked(
            store,
            document_id=document.id,
            snapshot=replacement_bytes,
            expected_new_snapshot_sha256=replacement_sha,
            expected_current_snapshot_sha256=old_snapshot,
            expected_current_structured_head_sha256=live_head,
        )
    assert documents.get_document(store, document.id).ydoc_snapshot_sha256 == old_snapshot
    assert ydoc_store.update_tail_present(store, document_id=document.id)
    assert ydoc_store.recover_compaction(store, document_id=document.id) is True
    assert documents.get_document(store, document.id).ydoc_snapshot_sha256 == old_snapshot
    assert ydoc_store.update_tail_present(store, document_id=document.id)

    with ydoc_store.document_lock(store, document.id):
        replacement = ydoc_store.prepare_snapshot_replacement_locked(
            store,
            document_id=document.id,
            snapshot=replacement_bytes,
            expected_new_snapshot_sha256=replacement_sha,
            expected_current_snapshot_sha256=old_snapshot,
            expected_current_structured_head_sha256=live_head,
        )
        # Model a process death immediately after the ledger pointer commits:
        # the normal post-commit export cannot run while the old update tail is
        # intentionally still present at this crash boundary.
        monkeypatch.setattr(store, "_run_on_commit", lambda *_args, **_kwargs: None)
        documents.commit_document_version(
            store,
            document_id=document.id,
            kind="snapshot_compacted",
            projection_sha256=document.content_sha256,
            ydoc_snapshot_sha256=replacement.snapshot_sha256,
            structured_head_sha256=replacement.structured_head_sha256,
            actor=HUMAN,
        )
    reopened = TruthStore.open(store.paths.sidecar)
    assert ydoc_store.recover_compaction(reopened, document_id=document.id) is True
    refreshed = documents.get_document(reopened, document.id)
    assert refreshed.ydoc_snapshot_sha256 == replacement_sha
    assert not ydoc_store.update_tail_present(reopened, document_id=document.id)


def test_store_open_finishes_committed_reimport_after_pointer_commit(
    store_ctx, monkeypatch
):
    store = store_ctx["store"]
    document, _source, _ready_receipt = _ready(
        store_ctx,
        path="docs/reimport-open-recovery.md",
        key="reimport-open-recovery-bootstrap",
    )
    external = b"# External replacement\n\nCrash-safe body.\n"
    (store_ctx["root"] / document.path).write_bytes(external)
    intent, _created = reimport.prepare_reimport(
        store,
        document_id=document.id,
        actor=HUMAN,
        idempotency_key="reimport-open-recovery",
    )
    staged = Path(intent.staged_path)
    replacement = b"YDOC:" + external
    original_finish = ydoc_store.finish_snapshot_replacement_locked

    class SimulatedProcessDeath(BaseException):
        pass

    def die_after_pointer_commit(*_args, **_kwargs):
        raise SimulatedProcessDeath

    monkeypatch.setattr(
        ydoc_store,
        "finish_snapshot_replacement_locked",
        die_after_pointer_commit,
    )
    with pytest.raises(SimulatedProcessDeath):
        reimport.commit_reimport(
            store,
            document_id=document.id,
            intent_id=intent.id,
            actor=HUMAN,
            replacement_snapshot=replacement,
            replacement_snapshot_sha256=sha256_bytes(replacement),
        )

    assert staged.is_file()
    assert ydoc_store.compaction_recovery_pending(
        store, document_id=document.id
    )
    with store._read_connection() as conn:
        committed = conn.execute(
            "SELECT state, receipt_json FROM cowork_reimport_intents WHERE id = ?",
            (intent.id,),
        ).fetchone()
    assert committed["state"] == "committed"
    assert committed["receipt_json"] is not None

    monkeypatch.setattr(
        ydoc_store,
        "finish_snapshot_replacement_locked",
        original_finish,
    )
    reopened = store_ctx["registry"].open_store(store.store_id)

    assert not staged.exists()
    assert not ydoc_store.compaction_recovery_pending(
        reopened, document_id=document.id
    )
    assert not ydoc_store.update_tail_present(reopened, document_id=document.id)
    assert (
        documents.get_document(reopened, document.id).ydoc_snapshot_sha256
        == sha256_bytes(replacement)
    )
    with reopened._read_connection() as conn:
        recovered = conn.execute(
            "SELECT state, receipt_json, recovery_detail "
            "FROM cowork_reimport_intents WHERE id = ?",
            (intent.id,),
        ).fetchone()
    assert recovered["state"] == "committed"
    assert recovered["receipt_json"] == committed["receipt_json"]
    assert recovered["recovery_detail"] is None


def test_store_open_expires_prepared_reimport_after_short_lock_probe(
    store_ctx, monkeypatch
):
    store = store_ctx["store"]
    document, _source, _ready_receipt = _ready(
        store_ctx,
        path="docs/reimport-expiry-recovery.md",
        key="reimport-expiry-recovery-bootstrap",
    )
    external = b"# External replacement\n\nAbandoned body.\n"
    (store_ctx["root"] / document.path).write_bytes(external)
    intent, _created = reimport.prepare_reimport(
        store,
        document_id=document.id,
        actor=HUMAN,
        idempotency_key="reimport-expiry-recovery",
    )
    staged = Path(intent.staged_path)
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE cowork_reimport_intents SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000000Z", intent.id),
        )

    observed_timeouts: list[float] = []
    original_lock = ydoc_store.document_lock

    @contextmanager
    def recording_lock(*args, **kwargs):
        observed_timeouts.append(float(kwargs.get("timeout", 10.0)))
        with original_lock(*args, **kwargs):
            yield

    monkeypatch.setattr(ydoc_store, "document_lock", recording_lock)
    reopened = store_ctx["registry"].open_store(store.store_id)

    assert 0.01 in observed_timeouts
    assert not staged.exists()
    with reopened._read_connection() as conn:
        recovered = conn.execute(
            "SELECT state, recovery_detail FROM cowork_reimport_intents WHERE id = ?",
            (intent.id,),
        ).fetchone()
    assert recovered["state"] == "cancelled"
    assert recovered["recovery_detail"] is None


def test_uncompacted_tail_defers_only_automatic_recovery_export(store_ctx):
    store = store_ctx["store"]
    document, _source, ready = _ready(
        store_ctx,
        path="docs/export-tail.md",
        key="export-tail-bootstrap-0001",
    )
    assert store.paths.claims_export.is_file()
    _, live_head = ydoc_store.append_update_cas(
        store,
        document_id=document.id,
        update=b"opaque-structural-update",
        snapshot_sha256=document.ydoc_snapshot_sha256,
        expected_structured_head_sha256=ready["structured_head_sha256"],
    )
    assert not store.paths.claims_export.exists()

    # A second, unrelated bootstrap commits its prepared intent even though the
    # first document is temporarily not exportable. The recovery projection is
    # already absent, and the transient condition does not become a false write
    # failure.
    second_source = b"# Existing Markdown to register\n"
    second_target = store_ctx["root"] / "docs" / "second-import.md"
    second_target.write_bytes(second_source)
    second_intent, created = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "import",
            "path": "docs/second-import.md",
            "idempotency_key": "second-import-0001",
            "expected_file_sha256": sha256_bytes(second_source),
        },
        source=None,
        actor=HUMAN,
    )
    assert created and second_intent.state == "prepared"
    assert not store.paths.claims_export.exists()

    # Explicit export remains strict: callers asking for a lossless artifact
    # must compact the structured tail first.
    with pytest.raises(UncompactedDocumentError) as uncompacted:
        export_store(store)
    assert uncompacted.value.document_id == document.id

    compacted_snapshot = b"YDOC:compacted-structural-update"
    ydoc_store.compact_and_advance(
        store,
        document_id=document.id,
        snapshot=compacted_snapshot,
        expected_snapshot_sha256=sha256_bytes(compacted_snapshot),
        expected_structured_head_sha256=live_head,
        actor=HUMAN,
    )
    assert not ydoc_store.update_tail_present(store, document_id=document.id)
    assert store.paths.claims_export.is_file()


def test_explicit_export_and_ydoc_append_have_safe_publication_order(
    store_ctx, monkeypatch
):
    store = store_ctx["store"]
    document, _source, ready = _ready(
        store_ctx,
        path="docs/export-append-race.md",
        key="export-append-race-bootstrap-0001",
    )
    publication_entered = Event()
    release_publication = Event()
    append_started = Event()
    original_write = export_module.atomic_write_bytes

    def pause_export(path, payload):
        if Path(path).resolve() == store.paths.claims_export.resolve():
            publication_entered.set()
            assert release_publication.wait(10)
        return original_write(path, payload)

    monkeypatch.setattr(export_module, "atomic_write_bytes", pause_export)

    def append_after_export_check():
        append_started.set()
        return ydoc_store.append_update_cas(
            store,
            document_id=document.id,
            update=b"racing-structured-update",
            snapshot_sha256=document.ydoc_snapshot_sha256,
            expected_structured_head_sha256=ready["structured_head_sha256"],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        exporting = executor.submit(export_store, store)
        assert publication_entered.wait(10)
        appending = executor.submit(append_after_export_check)
        assert append_started.wait(5)
        assert not appending.done()
        release_publication.set()
        exporting.result(timeout=15)
        _cursor, live_head = appending.result(timeout=15)

    # Export won the order, then append invalidated it before publishing the
    # tail. The inverse order is rejected by explicit export's tail check.
    assert ydoc_store.update_tail_present(store, document_id=document.id)
    assert not store.paths.claims_export.exists()
    with pytest.raises(UncompactedDocumentError):
        export_store(store)

    compacted_snapshot = b"YDOC:racing-structured-update"
    ydoc_store.compact_and_advance(
        store,
        document_id=document.id,
        snapshot=compacted_snapshot,
        expected_snapshot_sha256=sha256_bytes(compacted_snapshot),
        expected_structured_head_sha256=live_head,
        actor=HUMAN,
    )
    assert store.paths.claims_export.is_file()


def test_bootstrap_recovery_resets_safe_publish_and_cleans_committed_stage(
    store_ctx, monkeypatch
):
    store = store_ctx["store"]
    source = b"# Recover create\n"
    intent, _ = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "create",
            "path": "docs/recover-create.md",
            "idempotency_key": "recover-create-0001",
            "initial_source_sha256": sha256_bytes(source),
        },
        source=source,
        actor=HUMAN,
    )
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE cowork_bootstrap_intents SET state = 'publishing' WHERE id = ?",
            (intent.id,),
        )
    assert bootstrap.recover_bootstrap_intent(store, intent.id).state == "prepared"

    target = store_ctx["root"] / intent.normalized_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source)
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE cowork_bootstrap_intents SET state = 'publishing' WHERE id = ?",
            (intent.id,),
        )
    assert bootstrap.recover_bootstrap_intent(store, intent.id).state == "prepared"
    assert not target.exists()

    snapshot = b"YDOC:" + source
    remove_stage = bootstrap._remove_stage
    monkeypatch.setattr(bootstrap, "_remove_stage", lambda *_args: None)
    receipt = bootstrap.commit_bootstrap(
        store,
        bootstrap_id=intent.id,
        snapshot=snapshot,
        source_sha256=sha256_bytes(source),
        snapshot_sha256=sha256_bytes(snapshot),
        ydoc_schema=bootstrap.YDOC_SCHEMA,
        actor=HUMAN,
    )
    stage = bootstrap._stage_path(store, intent.id)
    assert stage.is_file()
    monkeypatch.setattr(bootstrap, "_remove_stage", remove_stage)
    recovered = bootstrap.recover_bootstrap_intent(store, intent.id)
    assert canonical_json(recovered.receipt) == canonical_json(receipt)
    assert recovered.receipt["document_version_id"]
    assert not stage.exists()

    # Ordinary committed history and an unexpired prepared request are not
    # recovery candidates, so routine store opens do not lock either one.
    pending, _ = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "create",
            "path": "docs/still-being-prepared.md",
            "idempotency_key": "still-being-prepared-0001",
            "initial_source_sha256": sha256_bytes(b"# Pending\n"),
        },
        source=b"# Pending\n",
        actor=HUMAN,
    )
    calls: list[float] = []

    @contextmanager
    def unexpected_lock(*_args, timeout=10.0, **_kwargs):
        calls.append(timeout)
        yield

    monkeypatch.setattr(bootstrap.ydoc_store, "document_lock", unexpected_lock)
    assert bootstrap.recover_bootstrap_intents(store) == {
        "cancelled": 0,
        "committed": 0,
        "recovery_required": 0,
    }
    assert calls == []

    # A publishing row is plausible crash residue, but the global scan probes
    # its normal lock without making an unrelated request wait for a live writer.
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE cowork_bootstrap_intents SET state = 'publishing' WHERE id = ?",
            (pending.id,),
        )

    @contextmanager
    def busy_lock(*_args, timeout=10.0, **_kwargs):
        calls.append(timeout)
        raise TimeoutError("live publisher")
        yield  # pragma: no cover - makes this a context manager

    monkeypatch.setattr(bootstrap.ydoc_store, "document_lock", busy_lock)
    bootstrap.recover_bootstrap_intents(store)
    assert calls == [0.01]


def _insert_materialization_intent(
    store,
    *,
    intent_id: str,
    key: str,
    document,
    head: str,
    rendered_sha: str,
    staged: Path,
    quarantine: Path,
    state: str = "publishing",
    receipt: dict | None = None,
):
    now = "2026-07-22T20:00:00.000+00:00"
    with store.write_transaction() as conn:
        conn.execute(
            "INSERT INTO cowork_materialization_intents (id, idempotency_key, "
            "actor_ref, document_id, state, expected_file_sha256, "
            "expected_structured_head_sha256, snapshot_sha256, rendered_sha256, "
            "staged_path, quarantine_path, document_version_id, created_at, "
            "updated_at, committed_at, receipt_json, recovery_detail) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                intent_id,
                key,
                HUMAN.ref,
                document.id,
                state,
                document.content_sha256,
                head,
                document.ydoc_snapshot_sha256,
                rendered_sha,
                str(staged),
                str(quarantine),
                new_id(),
                now,
                now,
                now if state == "committed" else None,
                None if receipt is None else canonical_json(receipt),
            ),
        )


def test_materialization_stable_retry_restore_and_committed_cleanup(
    store_ctx, monkeypatch
):
    store = store_ctx["store"]
    document, source, ready = _ready(
        store_ctx, path="docs/save-recovery.md", key="save-recovery-0001"
    )
    rendered = b"# Saved after recovery\n"
    rendered_sha = sha256_bytes(rendered)
    intent_id = new_id()
    target = store_ctx["root"] / document.path
    staged, quarantine = materialization._paths(target, intent_id)
    staged.write_bytes(rendered)
    _insert_materialization_intent(
        store,
        intent_id=intent_id,
        key="save-retry-0001",
        document=document,
        head=ready["structured_head_sha256"],
        rendered_sha=rendered_sha,
        staged=staged,
        quarantine=quarantine,
    )
    receipt = materialization.publish_projection(
        store,
        document_id=document.id,
        rendered_markdown=rendered.decode(),
        rendered_sha256=rendered_sha,
        expected_file_sha256=document.content_sha256,
        expected_structured_head_sha256=ready["structured_head_sha256"],
        snapshot_sha256=ready["snapshot_sha256"],
        actor=HUMAN,
        idempotency_key="save-retry-0001",
    )
    assert receipt["materialization_intent_id"] == intent_id
    assert target.read_bytes() == rendered

    # Recreate the exact crash-after-DB-before-cleanup artifacts. A retry must
    # keep the committed receipt and clean only the byte-for-byte known files.
    staged.write_bytes(rendered)
    quarantine.write_bytes(source)
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE cowork_materialization_intents SET staged_path = ?, "
            "quarantine_path = ?, recovery_detail = "
            "'recovery_required:post_commit_cleanup' WHERE id = ?",
            (str(staged), str(quarantine), intent_id),
        )
    recovered = materialization.recover_materialization_intent(store, intent_id)
    assert canonical_json(recovered.receipt) == canonical_json(receipt)
    assert not staged.exists() and not quarantine.exists()

    materialization_lock_calls: list[float] = []

    @contextmanager
    def unexpected_materialization_lock(*_args, timeout=10.0, **_kwargs):
        materialization_lock_calls.append(timeout)
        yield

    monkeypatch.setattr(
        materialization.ydoc_store,
        "document_lock",
        unexpected_materialization_lock,
    )
    assert materialization.recover_materializations(store) == {
        "restored": 0,
        "committed": 0,
        "recovery_required": 0,
    }
    assert materialization_lock_calls == []

    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE cowork_materialization_intents SET state = 'publishing' "
            "WHERE id = ?",
            (intent_id,),
        )

    @contextmanager
    def busy_materialization_lock(*_args, timeout=10.0, **_kwargs):
        materialization_lock_calls.append(timeout)
        raise TimeoutError("live Save")
        yield  # pragma: no cover - makes this a context manager

    monkeypatch.setattr(
        materialization.ydoc_store,
        "document_lock",
        busy_materialization_lock,
    )
    materialization.recover_materializations(store)
    assert materialization_lock_calls == [0.01]


def test_document_class_policy_denies_every_mutation_before_blob_write(
    store_ctx, client
):
    store = store_ctx["store"]
    document, _source, ready = _ready(
        store_ctx, path="docs/policy.md", key="policy-ready-0001"
    )
    profile = replace(
        store.profile,
        document_surface=DocumentSurfacePolicy(
            enabled=True,
            allowed_document_classes=("generated",),
            feedback_capture=True,
        ),
    )
    dump_profile(profile, store.paths.sidecar)
    classified = readiness.classify_document(store, document)
    assert classified.disabled_reason == "document_class_not_allowed"
    assert not any(classified.permissions.values())
    assert not ydoc_store.update_tail_present(store, document_id=document.id)
    push = client.post(
        f"/api/truth/doc/{document.id}/ydoc?store_id={store.store_id}",
        data=b"forbidden-update",
        headers={
            "X-WB-Base-Ydoc-Sha256": ready["structured_head_sha256"],
            "X-WB-Base-Ydoc-Generation": documents.current_ydoc_generation(
                store, document.id
            ),
        },
    )
    assert push.status_code == 403
    assert push.get_json()["error"]["code"] == "policy_forbidden"
    assert not ydoc_store.update_tail_present(store, document_id=document.id)

    posted: list[str] = []
    with store._read_connection() as conn:
        before_spans = conn.execute("SELECT COUNT(*) FROM document_spans").fetchone()[0]
        before_evidence = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    with pytest.raises(InvariantViolation):
        feedback.capture_feedback(
            store,
            document_id=document.id,
            span={"exact": "Original body.", "prefix": "", "suffix": ""},
            verbatim_text="Forbidden feedback",
            actor=HUMAN,
            post_message=lambda text: posted.append(text),
        )
    with store._read_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM document_spans").fetchone()[0] == before_spans
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == before_evidence
    assert posted == []

    forbidden_snapshot = b"YDOC:forbidden-policy-write"
    forbidden_sha = sha256_bytes(forbidden_snapshot)
    forbidden_blob = store.resolve_blob_path(f"blobs/{forbidden_sha}")
    with pytest.raises(materialization.MaterializationError) as denied:
        materialization.publish_projection(
            store,
            document_id=document.id,
            rendered_markdown="# Forbidden\n",
            rendered_sha256=sha256_bytes(b"# Forbidden\n"),
            expected_file_sha256=document.content_sha256,
            expected_structured_head_sha256=ready["structured_head_sha256"],
            snapshot_sha256=ready["snapshot_sha256"],
            replacement_snapshot=forbidden_snapshot,
            replacement_snapshot_sha256=forbidden_sha,
            actor=HUMAN,
        )
    assert denied.value.code == "policy_forbidden"
    assert not forbidden_blob.exists()

    with pytest.raises(sitting_lifecycle.SittingError) as sitting_denied:
        sitting_lifecycle.prepare_sitting(
            store,
            document_id=document.id,
            actor=HUMAN,
            items=[{"proposal_id": "missing", "verb": "defer", "canonical_sha256": "0" * 64}],
            expected_file_sha256=document.content_sha256,
            expected_structured_head_sha256=ready["structured_head_sha256"],
            idempotency_key="policy-sitting-0001",
        )
    assert sitting_denied.value.code == "policy_forbidden"
    with pytest.raises(retirement.RetirementError) as retire_denied:
        retirement.prepare_retirement(
            store,
            document_id=document.id,
            actor=HUMAN,
            idempotency_key="policy-retire-0001",
        )
    assert retire_denied.value.code == "policy_forbidden"
    (store_ctx["root"] / document.path).write_bytes(b"# External drift\n")
    with pytest.raises(reimport.ReimportError) as reimport_denied:
        reimport.prepare_reimport(
            store,
            document_id=document.id,
            actor=HUMAN,
            idempotency_key="policy-reimport-0001",
        )
    assert reimport_denied.value.code == "policy_forbidden"

    response = client.post(
        f"/api/truth/doc/{document.id}/materialize?store_id={store.store_id}",
        json={
            "rendered_markdown": "# Forbidden\n",
            "rendered_sha256": sha256_bytes(b"# Forbidden\n"),
            "expected_file_sha256": document.content_sha256,
            "expected_structured_head_sha256": ready["structured_head_sha256"],
            "snapshot_sha256": ready["snapshot_sha256"],
        },
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "policy_forbidden"

    disabled = replace(
        profile,
        document_surface=DocumentSurfacePolicy(
            enabled=False,
            allowed_document_classes=("co_authored",),
            feedback_capture=True,
        ),
    )
    dump_profile(disabled, store.paths.sidecar)
    disabled_readiness = readiness.classify_document(store, document)
    assert disabled_readiness.disabled_reason == "document_surface_disabled"
    assert not any(disabled_readiness.permissions.values())
    with pytest.raises(materialization.MaterializationError) as disabled_save:
        materialization.publish_projection(
            store,
            document_id=document.id,
            rendered_markdown="# Still forbidden\n",
            rendered_sha256=sha256_bytes(b"# Still forbidden\n"),
            expected_file_sha256=sha256_bytes(b"# External drift\n"),
            expected_structured_head_sha256=ready["structured_head_sha256"],
            snapshot_sha256=ready["snapshot_sha256"],
            actor=HUMAN,
        )
    assert disabled_save.value.code == "policy_forbidden"

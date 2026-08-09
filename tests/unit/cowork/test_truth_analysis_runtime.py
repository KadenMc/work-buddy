from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from work_buddy.cowork import truth_analysis_runtime as runtime
from work_buddy.truth.identity import sha256_text


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_DB_PATH", tmp_path / "truth-analysis.db")


def _create(
    *,
    run_id: str = "a" * 32,
    document_id: str = "d" * 32,
    execution_deadline_at: str | None = None,
):
    return runtime.create_run(
        run_id=run_id,
        store_id="store-alpha",
        document_id=document_id,
        action_snapshot_id="b" * 32,
        selection={
            "provider_id": "claude-code",
            "model_id": "sonnet",
            "provider_label": "Claude Code",
            "model_label": "Sonnet",
        },
        authorization_receipt_id="c" * 32,
        context_sha256="e" * 64,
        request={"schema": "wb.cowork.truth-analysis-request/v1"},
        session_id=f"{run_id}-cowork-truth-analysis",
        at="2026-08-09T12:00:00+00:00",
        execution_deadline_at=execution_deadline_at,
    )


def _output(*, candidate_hash: str | None = None):
    digest = candidate_hash or sha256_text("candidate-one")
    return {
        "schema": "wb.cowork.truth-analysis-output/v1",
        "candidates": [
            {
                "candidate_id": "f" * 32,
                "canonical_sha256": digest,
                "proposition": "A bounded proposition.",
                "claim_kind": "factual",
                "expression": {"role": "paraphrase"},
            }
        ],
        "source_coverage": [],
    }


def test_create_and_read_preserves_exact_binding_without_truth_tables():
    created = _create()

    assert created.status == "prepared"
    assert created.selection["provider_id"] == "claude-code"
    assert created.request["schema"] == "wb.cowork.truth-analysis-request/v1"
    assert runtime.get_run(created.run_id) == created

    with sqlite3.connect(runtime._DB_PATH) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert tables == {
        "cowork_truth_analysis_runs",
        "cowork_truth_analysis_candidate_decisions",
        "cowork_truth_analysis_decision_intents",
        "cowork_truth_analysis_searches",
        "cowork_truth_analysis_fetches",
    }
    assert "claims" not in tables
    assert "evidence" not in tables


def test_launch_claim_is_single_owner_and_fenced():
    run = _create()
    deadline = (
        datetime(2026, 8, 9, 12, 5, tzinfo=timezone.utc)
    ).isoformat()

    launching, claimed = runtime.claim_run_launch(
        run.run_id,
        launch_owner="queue-lease-1",
        lease_expires_at=deadline,
        at="2026-08-09T12:00:01+00:00",
    )
    duplicate, claimed_again = runtime.claim_run_launch(
        run.run_id,
        launch_owner="queue-lease-2",
        lease_expires_at=deadline,
        at="2026-08-09T12:00:02+00:00",
    )

    assert claimed is True
    assert launching.status == "launching"
    assert launching.launch_owner == "queue-lease-1"
    assert claimed_again is False
    assert duplicate.launch_owner == "queue-lease-1"
    with pytest.raises(ValueError, match="no longer holds"):
        runtime.update_run(
            run.run_id,
            status="running",
            expected_launch_owner="queue-lease-2",
        )
    running = runtime.update_run(
        run.run_id,
        status="running",
        pid=1234,
        expected_launch_owner="queue-lease-1",
    )
    assert running.pid == 1234
    assert running.launch_lease_expires_at is None


def test_overdue_live_run_is_fenced_and_late_runtime_work_is_rejected():
    now = datetime.now(timezone.utc)
    run = _create(
        execution_deadline_at=(now + timedelta(minutes=1)).isoformat()
    )
    runtime.update_run(run.run_id, status="running", pid=1234)

    expired, changed = runtime.expire_run_if_overdue(
        run.run_id,
        at=(now + timedelta(minutes=2)).isoformat(),
    )

    assert changed is True
    assert expired is not None
    assert expired.status == "failed"
    assert expired.error_code == "execution_deadline_exceeded"
    assert runtime.reconcilable_runs() == ()
    with pytest.raises(ValueError, match="transition"):
        runtime.update_run(run.run_id, status="completed", output=_output())


def test_completed_output_is_hashed_and_immutable():
    run = _create()
    payload = _output()

    completed = runtime.update_run(run.run_id, status="completed", output=payload)
    replay = runtime.update_run(
        run.run_id,
        status="completed",
        output=payload,
        output_sha256=completed.output_sha256,
    )

    assert completed.output == payload
    assert completed.output_sha256 is not None
    assert replay.output_sha256 == completed.output_sha256
    with pytest.raises(ValueError, match="immutable"):
        runtime.update_run(
            run.run_id,
            status="completed",
            output={**payload, "source_coverage": [{"source": "web"}]},
        )


def test_output_cannot_be_attached_to_nonterminal_or_terminal_failure():
    run = _create()

    with pytest.raises(ValueError, match="only be stored as completed"):
        runtime.update_run(run.run_id, status="running", output=_output())
    failed = runtime.update_run(
        run.run_id,
        status="failed",
        error_code="invalid_output",
        error="The typed output was invalid.",
    )
    assert failed.error_code == "invalid_output"
    with pytest.raises(ValueError, match="transition"):
        runtime.update_run(failed.run_id, status="running")


def test_candidate_decision_is_hash_bound_and_idempotent():
    run = _create()
    candidate_hash = sha256_text("candidate-one")
    runtime.update_run(
        run.run_id,
        status="completed",
        output=_output(candidate_hash=candidate_hash),
    )

    decision, replayed = runtime.record_candidate_decision(
        run_id=run.run_id,
        candidate_id="f" * 32,
        candidate_canonical_sha256=candidate_hash,
        decision="save_as_proposed",
        edits={"proposition": "A bounded proposition."},
        result={"claim_id": "1" * 32, "expression_id": "2" * 32},
        decided_by_ref="dashboard-user",
        at="2026-08-09T12:10:00+00:00",
    )
    same, replayed_again = runtime.record_candidate_decision(
        run_id=run.run_id,
        candidate_id="f" * 32,
        candidate_canonical_sha256=candidate_hash,
        decision="save_as_proposed",
        edits={"proposition": "A bounded proposition."},
        result={"claim_id": "1" * 32, "expression_id": "2" * 32},
        decided_by_ref="dashboard-user",
        at="2026-08-09T12:11:00+00:00",
    )

    assert replayed is False
    assert replayed_again is True
    assert same == decision
    assert runtime.candidate_decisions_for_run(run.run_id) == (decision,)
    with pytest.raises(ValueError, match="changed after it was shown"):
        runtime.record_candidate_decision(
            run_id=run.run_id,
            candidate_id="f" * 32,
            candidate_canonical_sha256="0" * 64,
            decision="dismiss",
            edits=None,
            result={"status": "dismissed"},
            decided_by_ref="dashboard-user",
        )
    with pytest.raises(ValueError, match="already has another decision"):
        runtime.record_candidate_decision(
            run_id=run.run_id,
            candidate_id="f" * 32,
            candidate_canonical_sha256=candidate_hash,
            decision="dismiss",
            edits=None,
            result={"status": "dismissed"},
            decided_by_ref="dashboard-user",
        )


def test_failed_prepared_decision_intent_can_be_released_exactly():
    run = _create()
    candidate_hash = sha256_text("candidate-one")
    runtime.update_run(
        run.run_id,
        status="completed",
        output=_output(candidate_hash=candidate_hash),
    )
    prepared = {
        "run_id": run.run_id,
        "candidate_id": "f" * 32,
        "candidate_canonical_sha256": candidate_hash,
        "decision": "save_as_proposed",
        "edits": {"evidence_candidate_ids": []},
        "decided_by_ref": "dashboard-user",
    }
    assert runtime.prepare_candidate_decision(**prepared) is False
    assert runtime.clear_candidate_decision_intent(**prepared) is True
    dismissed = {**prepared, "decision": "dismiss", "edits": {}}
    assert runtime.prepare_candidate_decision(**dismissed) is False
    assert runtime.clear_candidate_decision_intent(**prepared) is False
    assert runtime.clear_candidate_decision_intent(**dismissed) is True


def test_document_and_recovery_queries_exclude_terminal_runs():
    first = _create(run_id="a" * 32)
    runtime.update_run(first.run_id, status="completed", output=_output())
    runtime.record_candidate_decision(
        run_id=first.run_id,
        candidate_id="f" * 32,
        candidate_canonical_sha256=sha256_text("candidate-one"),
        decision="dismiss",
        edits=None,
        result={"status": "dismissed"},
        decided_by_ref="dashboard-user",
    )
    second = _create(run_id="b" * 32)
    _create(run_id="c" * 32, document_id="9" * 32)
    runtime.update_run(second.run_id, status="running", pid=44)

    assert [item.run_id for item in runtime.runs_for_document(
        "store-alpha", "d" * 32
    )] == [first.run_id, second.run_id]
    assert [item.run_id for item in runtime.reconcilable_runs()] == [second.run_id, "c" * 32]


def test_document_runs_preserve_insertion_order_when_timestamps_match():
    first = _create(run_id="f" * 32)
    runtime.update_run(first.run_id, status="failed", error_code="fixture_complete")
    second = _create(run_id="0" * 32)

    assert [
        item.run_id
        for item in runtime.runs_for_document("store-alpha", "d" * 32)
    ] == [first.run_id, second.run_id]


def test_create_run_atomically_serializes_distinct_document_targets():
    barrier = threading.Barrier(2)

    def create(run_id: str):
        barrier.wait()
        try:
            return _create(run_id=run_id)
        except runtime.TruthAnalysisRunConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ("1" * 32, "2" * 32)))

    created = [item for item in results if isinstance(item, runtime.TruthAnalysisRuntimeRun)]
    blocked = [item for item in results if isinstance(item, runtime.TruthAnalysisRunConflict)]
    assert len(created) == 1
    assert len(blocked) == 1
    assert blocked[0].run_id == created[0].run_id
    assert blocked[0].status == "prepared"
    assert blocked[0].pending_candidates is None
    assert runtime.runs_for_document("store-alpha", "d" * 32) == (created[0],)


def test_launch_lease_must_expire_after_claim_time():
    run = _create()
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="expire after"):
        runtime.claim_run_launch(
            run.run_id,
            lease_expires_at=(now - timedelta(seconds=1)).isoformat(),
            at=now.isoformat(),
        )


def test_search_and_fetch_receipts_are_run_scoped_bounded_and_replayable():
    run = _create()
    hit = {
        "hit_id": "1" * 32,
        "title": "Bounded source",
        "url": "https://example.test/source",
        "snippet": "A bounded result.",
        "provider": "fake",
        "raw_text": "Captured source text.",
    }
    search, replayed = runtime.record_search_receipt(
        run_id=run.run_id,
        query="bounded query",
        status="completed",
        hits=[hit],
        external_egress=True,
        max_searches=1,
        at="2026-08-09T12:01:00+00:00",
    )
    same, replayed_again = runtime.record_search_receipt(
        run_id=run.run_id,
        query="bounded query",
        status="completed",
        hits=[hit],
        external_egress=True,
        max_searches=1,
        at="2026-08-09T12:02:00+00:00",
    )

    assert replayed is False
    assert replayed_again is True
    assert same == search
    assert runtime.search_hit_for_run(run.run_id, hit["hit_id"]) == hit
    with pytest.raises(ValueError, match="limit reached"):
        runtime.record_search_receipt(
            run_id=run.run_id,
            query="second query",
            status="completed",
            hits=[],
            external_egress=True,
            max_searches=1,
        )

    fetch, fetch_replayed = runtime.record_fetch_receipt(
        run_id=run.run_id,
        hit_id=hit["hit_id"],
        status="completed",
        url=hit["url"],
        canonical_url=hit["url"],
        title=hit["title"],
        text=hit["raw_text"],
        content_sha256=sha256_text(hit["raw_text"]),
        extractor="search_provider_inline",
        external_egress=False,
        max_fetches=1,
        at="2026-08-09T12:03:00+00:00",
    )
    same_fetch, replayed_fetch = runtime.record_fetch_receipt(
        run_id=run.run_id,
        hit_id=hit["hit_id"],
        status="completed",
        url=hit["url"],
        canonical_url=hit["url"],
        title=hit["title"],
        text=hit["raw_text"],
        content_sha256=sha256_text(hit["raw_text"]),
        extractor="search_provider_inline",
        external_egress=False,
        max_fetches=1,
    )
    assert fetch_replayed is False
    assert replayed_fetch is True
    assert same_fetch == fetch
    assert runtime.get_fetch_receipt(run.run_id, fetch.fetch_id) == fetch
    with pytest.raises(ValueError, match="admitted search hit"):
        runtime.record_fetch_receipt(
            run_id=run.run_id,
            hit_id="2" * 32,
            status="unavailable",
            url="https://not-admitted.test/",
            canonical_url="https://not-admitted.test/",
            title="Not admitted",
            text="",
            content_sha256="",
            extractor="none",
            external_egress=False,
            max_fetches=2,
        )

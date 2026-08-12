from __future__ import annotations

import json
from pathlib import Path

import pytest

from work_buddy.security.actors import ActorRef
from work_buddy.sources.models import SourceRef
from work_buddy.truth import documents
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.document_redaction import (
    get_document_content_redaction,
    scrub_exact_managed_document_content,
)
from work_buddy.truth.export import export_store, import_store
from work_buddy.truth.identity import canonical_json, new_id, sha256_bytes
from work_buddy.truth.migrations import (
    REDACTED_ACTION_CONTEXT_JSON,
    REDACTED_SELECTOR_JSON,
)
from work_buddy.truth.queries import integrity_findings
from work_buddy.truth.store import PostCommitHookError, TruthStore


NOW = "2026-08-10T00:00:00.000+00:00"
DOC_ID = "d1" * 16
OLD_VERSION_ID = "d2" * 16
REPLACEMENT_VERSION_ID = "d3" * 16
SPAN_ID = "d4" * 16
ACTION_ID = "d5" * 16
PROPOSAL_ID = "d6" * 16
STATUS_ID = "d7" * 16


def _profile(store_id: str) -> dict[str, object]:
    return {
        "store_id": store_id,
        "profile": "document-redaction-test",
        "title": "Document redaction",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": "redact",
            "confirmation_surfaces": ["dashboard"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": True,
        "document_surface": {"enabled": True},
    }


class _Registry:
    def paths_for_store_id(self, _store_id: str):
        return ()


def _seed(tmp_path: Path) -> tuple[TruthStore, dict[str, str]]:
    store = TruthStore.create(tmp_path / "truth", _profile("e1" * 16))
    actor = Actor("system", "fixture")
    old_projection = b"Sensitive copied source text.\n"
    old_snapshot = b"sensitive opaque ydoc snapshot"
    replacement_projection = b"[redacted]\n"
    replacement_snapshot = b"redacted opaque ydoc snapshot"
    target = b"Sensitive copied source text."
    digests = {
        "old_projection": sha256_bytes(old_projection),
        "old_snapshot": sha256_bytes(old_snapshot),
        "replacement_projection": sha256_bytes(replacement_projection),
        "replacement_snapshot": sha256_bytes(replacement_snapshot),
        "target": sha256_bytes(target),
        "head_old": sha256_bytes(b"old structured head"),
        "head_replacement": sha256_bytes(b"replacement structured head"),
    }
    for digest, value in (
        (digests["old_projection"], old_projection),
        (digests["old_snapshot"], old_snapshot),
        (digests["replacement_projection"], replacement_projection),
        (digests["replacement_snapshot"], replacement_snapshot),
        (digests["target"], target),
    ):
        store._store_blob_bytes(digest, value)

    documents.register_document(
        store,
        path="notes/source-copy.md",
        title="Source copy",
        document_class="co_authored",
        content_sha256=digests["old_projection"],
        ydoc_snapshot_sha256=digests["old_snapshot"],
        actor=actor,
        document_id=DOC_ID,
        at=NOW,
    )
    with store.write_transaction() as conn:
        conn.execute(
            "INSERT INTO document_versions "
            "(id, document_id, kind, projection_sha256, ydoc_snapshot_sha256, "
            "structured_head_sha256, created_at, actor_kind, actor_ref, detail) "
            "VALUES (?, ?, 'initial_import', ?, ?, ?, ?, 'system', 'fixture', 'import')",
            (
                OLD_VERSION_ID,
                DOC_ID,
                digests["old_projection"],
                digests["old_snapshot"],
                digests["head_old"],
                NOW,
            ),
        )
        store._insert_ledger_record_locked(conn, "document_version", OLD_VERSION_ID)
        conn.execute(
            "INSERT INTO document_spans "
            "(id, document_id, selector_json, quote_exact, span_sha256, "
            "author_kind, author_ref, created_at, created_by_kind, created_by_ref, "
            "redacted_at) VALUES (?, ?, ?, 'Sensitive copied source text.', ?, "
            "'unknown', NULL, ?, 'system', 'fixture', NULL)",
            (
                SPAN_ID,
                DOC_ID,
                canonical_json(
                    {
                        "exact": "Sensitive copied source text.",
                        "prefix": "",
                        "suffix": "",
                    }
                ),
                digests["target"],
                NOW,
            ),
        )
        store._insert_ledger_record_locked(conn, "document_span", SPAN_ID)
        conn.execute(
            "INSERT INTO action_snapshots "
            "(id, document_id, document_version_id, ydoc_snapshot_sha256, "
            "structured_head_sha256, ydoc_generation_sha256, "
            "baseline_projection_sha256, projection_sha256, projection_blob_sha256, "
            "target_kind, target_selector_json, target_text_sha256, target_blob_sha256, "
            "context_boundary_json, allowed_change_ranges_json, egress_boundary_json, "
            "canonical_sha256, created_at, created_by_kind, created_by_ref, "
            "created_by_meta_json, redacted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'text_quote', ?, ?, ?, ?, ?, ?, ?, ?, "
            "'system', 'fixture', NULL, NULL)",
            (
                ACTION_ID,
                DOC_ID,
                OLD_VERSION_ID,
                digests["old_snapshot"],
                digests["head_old"],
                sha256_bytes(b"generation"),
                digests["old_projection"],
                digests["old_projection"],
                digests["old_projection"],
                canonical_json(
                    {
                        "kind": "text_quote",
                        "selector": {"exact": "Sensitive copied source text."},
                        "resolved": {
                            "start": 0,
                            "end": len("Sensitive copied source text."),
                            "exact": "Sensitive copied source text.",
                        },
                    }
                ),
                digests["target"],
                digests["target"],
                canonical_json({"kind": "action_target"}),
                canonical_json(
                    [{"start": 0, "end": len("Sensitive copied source text.")}]
                ),
                canonical_json({"class": "local_only"}),
                sha256_bytes(b"action canonical"),
                NOW,
            ),
        )
        store._insert_ledger_record_locked(conn, "action_snapshot", ACTION_ID)
        conn.execute(
            "INSERT INTO proposals "
            "(id, document_id, base_content_sha256, base_structured_head_sha256, "
            "selector_json, quote_exact, span_sha256, replacement, rationale, tldr, "
            "claim_refs_json, canonical_sha256, dedup_key, expires_at, created_at, "
            "created_by_kind, created_by_ref, meta_json, redacted_at) "
            "VALUES (?, ?, ?, ?, ?, 'Sensitive copied source text.', ?, 'Rewrite', "
            "'Derived rationale', 'Derived summary', '[]', ?, ?, NULL, ?, 'agent_run', "
            "'fixture-run', '{}', NULL)",
            (
                PROPOSAL_ID,
                DOC_ID,
                digests["old_projection"],
                digests["head_old"],
                canonical_json({"exact": "Sensitive copied source text."}),
                digests["target"],
                sha256_bytes(b"proposal canonical"),
                sha256_bytes(b"proposal dedup"),
                NOW,
            ),
        )
        store._insert_ledger_record_locked(conn, "proposal", PROPOSAL_ID)
        conn.execute(
            "INSERT INTO proposal_status_events "
            "(id, proposal_id, status, decision, at, actor_kind, actor_ref, "
            "basis_kind, basis_ref, note) VALUES (?, ?, 'open', NULL, ?, 'system', "
            "'fixture', 'rule', 'created', NULL)",
            (STATUS_ID, PROPOSAL_ID, NOW),
        )
        store._insert_ledger_record_locked(
            conn, "proposal_status_event", STATUS_ID
        )

    _, replacement, _ = documents.commit_document_version(
        store,
        document_id=DOC_ID,
        kind="materialized",
        projection_sha256=digests["replacement_projection"],
        ydoc_snapshot_sha256=digests["replacement_snapshot"],
        structured_head_sha256=digests["head_replacement"],
        actor=Actor("system", "sources-redaction"),
        detail="source-redaction:source-event-1",
        version_id=REPLACEMENT_VERSION_ID,
        at="2026-08-10T00:01:00.000+00:00",
    )
    assert replacement.id == REPLACEMENT_VERSION_ID
    return store, digests


def _source_ref() -> SourceRef:
    return SourceRef(
        "authority-1",
        "item-0001",
    )


def _actor_ref() -> ActorRef:
    return ActorRef("authority-1", "sources-redaction", "service", "tenant-1")


def test_exact_managed_document_redaction_scrubs_and_round_trips(
    tmp_path: Path,
) -> None:
    store, digests = _seed(tmp_path)

    result = scrub_exact_managed_document_content(
        store,
        document_id=DOC_ID,
        replacement_document_version_id=REPLACEMENT_VERSION_ID,
        source_usage_id="usage-1",
        source_ref=_source_ref().to_dict(),
        source_redaction_event_id="source-event-1",
        actor_ref=_actor_ref().to_dict(),
        content_class="exact_copy",
        redaction_policy="scrub",
        at="2026-08-10T00:02:00.000+00:00",
    )

    assert result.complete
    assert result.status.status == "cleanup_complete"
    assert {
        digests["old_projection"],
        digests["old_snapshot"],
        digests["target"],
    } <= set(result.deleted_blob_sha256s)
    assert store.resolve_blob_path(
        f"blobs/{digests['replacement_projection']}"
    ).exists()
    assert store.resolve_blob_path(
        f"blobs/{digests['replacement_snapshot']}"
    ).exists()

    with store._read_connection() as conn:
        span = conn.execute(
            "SELECT * FROM document_spans WHERE id = ?", (SPAN_ID,)
        ).fetchone()
        action = conn.execute(
            "SELECT * FROM action_snapshots WHERE id = ?", (ACTION_ID,)
        ).fetchone()
        proposal = conn.execute(
            "SELECT * FROM proposals WHERE id = ?", (PROPOSAL_ID,)
        ).fetchone()
    assert span["quote_exact"] is None
    assert span["selector_json"] == REDACTED_SELECTOR_JSON
    assert span["redacted_at"] is not None
    assert action["target_selector_json"] == REDACTED_ACTION_CONTEXT_JSON
    assert action["context_boundary_json"] == REDACTED_ACTION_CONTEXT_JSON
    assert action["allowed_change_ranges_json"] == "[]"
    assert action["redacted_at"] is not None
    assert proposal["quote_exact"] is None
    assert proposal["replacement"] is None
    assert proposal["rationale"] is None
    assert proposal["tldr"] is None
    assert proposal["redacted_at"] is not None
    assert not [
        finding
        for finding in integrity_findings(store)
        if finding.severity == "error"
    ]

    replay = scrub_exact_managed_document_content(
        store,
        document_id=DOC_ID,
        replacement_document_version_id=REPLACEMENT_VERSION_ID,
        source_usage_id="usage-1",
        source_ref=_source_ref().to_dict(),
        source_redaction_event_id="source-event-1",
        actor_ref=_actor_ref().to_dict(),
        content_class="exact_copy",
        redaction_policy="scrub",
    )
    assert replay.receipt.id == result.receipt.id
    assert replay.complete
    assert get_document_content_redaction(store, result.receipt.id).complete

    exported = export_store(store)
    payload = exported.path.read_bytes()
    for digest in (
        digests["old_projection"],
        digests["old_snapshot"],
        digests["target"],
    ):
        assert digest.encode() in payload  # retained as a content-free tombstone hash
        assert not store.resolve_blob_path(f"blobs/{digest}").exists()

    destination = tmp_path / "restored"
    destination.mkdir()
    restored = import_store(exported.path, destination, registry=_Registry()).store
    restored_result = get_document_content_redaction(restored, result.receipt.id)
    assert restored_result.complete
    assert restored_result.receipt.coverage_sha256 == result.receipt.coverage_sha256
    assert not [
        finding
        for finding in integrity_findings(restored)
        if finding.severity == "error"
    ]


def test_mixed_derivative_is_rejected_before_any_redaction(tmp_path: Path) -> None:
    store, digests = _seed(tmp_path)
    with pytest.raises(InvariantViolation, match="content_class='exact_copy'"):
        scrub_exact_managed_document_content(
            store,
            document_id=DOC_ID,
            replacement_document_version_id=REPLACEMENT_VERSION_ID,
            source_usage_id="usage-1",
            source_ref=_source_ref().to_dict(),
            source_redaction_event_id="source-event-1",
            actor_ref=_actor_ref().to_dict(),
            content_class="mixed_derivative",
            redaction_policy="scrub",
        )
    assert store.resolve_blob_path(f"blobs/{digests['old_projection']}").exists()
    with store._read_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM document_content_redactions"
        ).fetchone()[0] == 0


def test_committed_redaction_resumes_blob_cleanup_after_a_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, digests = _seed(tmp_path)
    original_cleanup = TruthStore._finish_blob_cleanup
    calls = 0

    def _interrupt_cleanup(self: TruthStore, digest: str) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated cleanup interruption")
        return original_cleanup(self, digest)

    monkeypatch.setattr(TruthStore, "_finish_blob_cleanup", _interrupt_cleanup)
    with pytest.raises(PostCommitHookError, match="blob cleanup failed"):
        scrub_exact_managed_document_content(
            store,
            document_id=DOC_ID,
            replacement_document_version_id=REPLACEMENT_VERSION_ID,
            source_usage_id="usage-1",
            source_ref=_source_ref().to_dict(),
            source_redaction_event_id="source-event-1",
            actor_ref=_actor_ref().to_dict(),
            content_class="exact_copy",
            redaction_policy="scrub",
        )

    with store._read_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM document_content_redactions"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT quote_exact FROM document_spans WHERE id = ?", (SPAN_ID,)
        ).fetchone()[0] is None

    monkeypatch.setattr(TruthStore, "_finish_blob_cleanup", original_cleanup)
    resumed = scrub_exact_managed_document_content(
        store,
        document_id=DOC_ID,
        replacement_document_version_id=REPLACEMENT_VERSION_ID,
        source_usage_id="usage-1",
        source_ref=_source_ref().to_dict(),
        source_redaction_event_id="source-event-1",
        actor_ref=_actor_ref().to_dict(),
        content_class="exact_copy",
        redaction_policy="scrub",
    )
    assert resumed.complete
    assert not store.resolve_blob_path(f"blobs/{digests['old_projection']}").exists()


def test_integrity_reports_an_unreferenced_covered_blob_restored_after_completion(
    tmp_path: Path,
) -> None:
    store, digests = _seed(tmp_path)
    result = scrub_exact_managed_document_content(
        store,
        document_id=DOC_ID,
        replacement_document_version_id=REPLACEMENT_VERSION_ID,
        source_usage_id="usage-1",
        source_ref=_source_ref().to_dict(),
        source_redaction_event_id="source-event-1",
        actor_ref=_actor_ref().to_dict(),
        content_class="exact_copy",
        redaction_policy="scrub",
    )
    assert result.complete

    store._store_blob_bytes(
        digests["old_projection"], b"Sensitive copied source text.\n"
    )
    findings = integrity_findings(store)
    assert any(
        finding.code == "document-content-redaction-blob-retained"
        and finding.severity == "error"
        for finding in findings
    )


def test_semantic_derivative_keeps_managed_copy_completion_fail_closed(
    tmp_path: Path,
) -> None:
    store, _digests = _seed(tmp_path)
    claim = store.propose_claim(
        proposition="The copied source says something consequential.",
        claim_kind="fact",
        actor=Actor("human", "fixture-user"),
    ).claim
    expression_id = new_id()
    with store.write_transaction() as conn:
        conn.execute(
            "INSERT INTO expressions "
            "(id, document_span_id, claim_ref_kind, claim_ref, role, "
            "claim_canonical_sha256, span_sha256, created_at, created_by_kind, "
            "created_by_ref) VALUES (?, ?, 'local', ?, 'instantiation', ?, ?, ?, "
            "'human', 'fixture-user')",
            (
                expression_id,
                SPAN_ID,
                claim.id,
                claim.canonical_sha256,
                sha256_bytes(b"Sensitive copied source text."),
                NOW,
            ),
        )
        store._insert_ledger_record_locked(conn, "expression", expression_id)

    result = scrub_exact_managed_document_content(
        store,
        document_id=DOC_ID,
        replacement_document_version_id=REPLACEMENT_VERSION_ID,
        source_usage_id="usage-1",
        source_ref=_source_ref().to_dict(),
        source_redaction_event_id="source-event-1",
        actor_ref=_actor_ref().to_dict(),
        content_class="exact_copy",
        redaction_policy="scrub",
    )

    assert not result.complete
    assert result.status.status == "cleanup_incomplete"
    assert claim.id in result.review_target_refs

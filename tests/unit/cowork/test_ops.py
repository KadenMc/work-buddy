"""Unit tests for the cowork_doc_* capabilities.

The ops call the real document engine against a real registered v2 store, so
these tests exercise parameter validation, the producer-identity refusal paths,
the {claim, role} shape enforcement, a proposal reaching the proposals table, a
comment producing a flag, and an expression minted with its role.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from work_buddy.conversations import store as conversation_store
from work_buddy.cowork import conversations as cowork_conversations
from work_buddy.cowork import feedback as cowork_feedback
from work_buddy.mcp_server.op_registry import load_builtin_ops

# Register the built-in ops first. The cowork surface reuses the truth-ops
# producer-identity plumbing lazily, so loading the built-ins here keeps the
# registry consistent regardless of test collection order.
load_builtin_ops()

import work_buddy.cowork.ops as cowork_ops  # noqa: E402
import work_buddy.mcp_server.ops.truth_ops as truth_ops  # noqa: E402
from work_buddy.cowork.file_importers import MARKDOWN_MAX_SOURCE_BYTES  # noqa: E402
from work_buddy.truth import documents, expressions, proposals, ydoc_store  # noqa: E402
from work_buddy.truth.contracts import Actor, InvariantViolation  # noqa: E402
from work_buddy.truth.events import TruthEventEmission  # noqa: E402
from work_buddy.truth.identity import new_id, sha256_bytes  # noqa: E402
from work_buddy.truth.registry import TruthStoreRegistry  # noqa: E402
from work_buddy.truth.store import TruthStore  # noqa: E402


SESSION_ID = "session-cowork-ops"
MODEL = "cowork-test-model"
HUMAN = Actor("human", "reviewer")
NOW = "2026-07-17T12:00:00.000+00:00"
BODY = "# Fixture\n\nOriginal sentence for cowork ops tests.\n"
QUOTE = "Original sentence"


def _profile(store_id: str, *, document_surface: bool = True) -> dict[str, object]:
    profile: dict[str, object] = {
        "store_id": store_id,
        "profile": "cowork-doc",
        "title": "Cowork document store",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": "retain",
            "confirmation_surfaces": ["dashboard", "cli", "chat_consent"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": True,
    }
    if document_surface:
        profile["document_surface"] = {
            "enabled": True,
            "allowed_document_classes": ["co_authored", "generated"],
            "feedback_capture": True,
        }
    return profile


def _make_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    document_surface: bool = True,
) -> dict[str, object]:
    conversations_db = tmp_path / "throwaway-conversations.db"
    monkeypatch.setattr(conversation_store, "_DB_PATH", conversations_db)
    conversations_conn = conversation_store.get_connection()
    try:
        conversation_store._ensure_schema(conversations_conn)
    finally:
        conversations_conn.close()
    registry = TruthStoreRegistry(tmp_path / "registry.db")
    monkeypatch.setattr(cowork_ops, "_registry", lambda: registry)
    monkeypatch.setattr(
        truth_ops,
        "_session_manifest",
        lambda session_id: {"session_id": session_id, "harness_id": "codex"},
    )
    emitted: list[tuple[str, dict[str, object]]] = []

    def emit(event_type: str, **kwargs: object) -> TruthEventEmission:
        emitted.append((event_type, kwargs))
        return TruthEventEmission(f"event-{len(emitted)}", True)

    monkeypatch.setattr(cowork_ops, "emit_truth_event", emit)
    disclosures: list[tuple[str, object, dict[str, object]]] = []

    class RecordingDisclosureBoundary:
        def account_payload(self, run, **kwargs):
            disclosures.append(("input", run, dict(kwargs)))
            return None

        def bind_output(self, run, **kwargs):
            disclosures.append(("output", run, dict(kwargs)))
            return SimpleNamespace(
                manifest_sha256="d" * 64,
                entry_count=1,
                through_sequence=1,
            )

    from work_buddy.cowork import worker_disclosure

    monkeypatch.setattr(
        worker_disclosure,
        "get_cowork_worker_disclosure",
        lambda: RecordingDisclosureBoundary(),
    )
    store_id = new_id()
    root = tmp_path / "scope"
    root.mkdir()
    store = TruthStore.create(root, _profile(store_id, document_surface=document_surface))
    registry.register(store)
    return {
        "store": store,
        "store_id": store_id,
        "registry": registry,
        "root": root,
        "emitted": emitted,
        "disclosures": disclosures,
    }


@pytest.fixture
def cowork(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    return _make_store(tmp_path, monkeypatch)


def _register_doc(
    store: object,
    *,
    path: str = "docs/fixture.md",
    body: str = BODY,
    document_class: str = "co_authored",
    write_file: bool = True,
) -> tuple[str, str]:
    content_sha256 = sha256_bytes(body.encode("utf-8"))
    record = documents.register_document(
        store,
        path=path,
        title="Fixture",
        document_class=document_class,
        content_sha256=content_sha256,
        actor=HUMAN,
        at=NOW,
    )
    if write_file:
        target = store.paths.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write raw bytes so the on-disk hash matches content_sha256 exactly.
        # Text mode would translate newlines on Windows and drift the hash.
        target.write_bytes(body.encode("utf-8"))
    return record.id, content_sha256


def _seed_claim(store: object, proposition: str = "The value is one.") -> object:
    return store.propose_claim(
        proposition=proposition,
        claim_kind="fact",
        actor=HUMAN,
    ).claim


def _one_hunk(replacement: str = "Revised sentence") -> list[dict[str, object]]:
    return [{"quote_anchor": {"exact": QUOTE}, "replacement": replacement}]


def _activate_document_lease(store_id: str, document_id: str, generation: str):
    binding = cowork_conversations.ensure_document_conversation(
        document_id=document_id,
        store_id=store_id,
    )
    consumer = f"cowork-document:{store_id}:{document_id}"
    claim = conversation_store.claim_agent_lease(
        binding.conversation_id,
        consumer,
        generation,
        execution={
            "provider_id": "test-provider",
            "model_id": "test-model",
        },
    )
    assert claim is not None and claim["claimed"] is True
    assert conversation_store.activate_agent_lease(
        binding.conversation_id,
        consumer,
        generation,
        92001,
    )
    return binding, consumer


# --------------------------------------------------------------------------
# Registration.
# --------------------------------------------------------------------------


def test_register_ops_binds_all_five_idempotently() -> None:
    from work_buddy.mcp_server import op_registry

    cowork_ops.register_ops()
    for name in (
        "cowork_doc_list",
        "cowork_doc_get",
        "cowork_doc_propose_edit",
        "cowork_doc_comment",
        "cowork_doc_expression_mark",
    ):
        assert op_registry.get_op(f"op.wb.{name}") is not None


# --------------------------------------------------------------------------
# Read capabilities.
# --------------------------------------------------------------------------


def test_list_and_get_report_document_and_open_layer(cowork: dict[str, object]) -> None:
    store_id = str(cowork["store_id"])
    doc_id, content_sha = _register_doc(cowork["store"])

    listed = cowork_ops.cowork_doc_list(store_id)
    assert listed["ok"] is True
    assert listed["count"] == 1
    entry = listed["docs"][0]
    assert entry["document_id"] == doc_id
    assert entry["document_class"] == "co_authored"
    assert entry["drift_state"] == "clean"
    assert entry["current_file_sha256"] == content_sha
    assert entry["import_source_sha256"] is None
    assert entry["observed_source_file_sha256"] == content_sha
    assert entry["source_file_sha256"] == content_sha
    assert entry["last_materialized_sha256"] == content_sha
    assert entry["open_proposal_count"] == 0
    assert entry["open_flag_count"] == 0

    got = cowork_ops.cowork_doc_get(store_id, doc_id)
    assert got["document_id"] == doc_id
    assert got["drift"]["state"] == "clean"
    assert got["hashes"]["current_file_sha256"] == content_sha
    assert got["import_source_sha256"] is None
    assert got["observed_source_file_sha256"] == content_sha
    assert got["hashes"]["import_source_sha256"] is None
    assert got["hashes"]["observed_source_file_sha256"] == content_sha
    assert got["hashes"]["source_file_sha256"] == content_sha
    assert got["open_proposals"] == []
    assert got["expressions"] == []
    assert got["feedback"] == []


def test_list_and_get_distinguish_recorded_import_from_observed_source(
    cowork: dict[str, object],
) -> None:
    store = cowork["store"]
    store_id = str(cowork["store_id"])
    source_path = store.paths.root / "docs/fixture.md"
    source_path.parent.mkdir(parents=True)
    imported_source = b"# Imported source\n"
    source_path.write_bytes(imported_source)
    imported_source_sha256 = sha256_bytes(imported_source)
    snapshot = b"YDOC-OPS-DETACHED-IMPORT"
    snapshot_sha256 = ydoc_store.write_snapshot(store, snapshot=snapshot)
    structured_head_sha256 = ydoc_store.structured_head_from_segments(
        snapshot,
        (),
    )
    document, _, _ = documents.register_ready_document(
        store,
        path="docs/fixture.md",
        title="Fixture",
        document_class="co_authored",
        projection_bytes=imported_source,
        ydoc_snapshot_sha256=snapshot_sha256,
        structured_head_sha256=structured_head_sha256,
        actor=HUMAN,
        mode="import",
        document_meta={
            "source": {
                "kind": "file_import",
                "writeback_policy": "never",
                "sha256": imported_source_sha256,
                "importer_id": "markdown/v1",
                "media_type": "text/markdown",
            }
        },
        at=NOW,
    )
    changed_source = b"# Source changed after import\n"
    observed_source_sha256 = sha256_bytes(changed_source)
    source_path.write_bytes(changed_source)

    listed = cowork_ops.cowork_doc_list(store_id)["docs"][0]
    assert listed["source_writeback"] == "never"
    assert listed["current_file_sha256"] == imported_source_sha256
    assert listed["import_source_sha256"] == imported_source_sha256
    assert listed["observed_source_file_sha256"] == observed_source_sha256
    assert listed["source_file_sha256"] == observed_source_sha256
    assert listed["drift_state"] == "clean"

    got = cowork_ops.cowork_doc_get(store_id, document.id)
    assert got["source_writeback"] == "never"
    assert got["import_source_sha256"] == imported_source_sha256
    assert got["observed_source_file_sha256"] == observed_source_sha256
    assert got["hashes"]["current_file_sha256"] == imported_source_sha256
    assert got["hashes"]["import_source_sha256"] == imported_source_sha256
    assert (
        got["hashes"]["observed_source_file_sha256"]
        == observed_source_sha256
    )
    assert got["hashes"]["source_file_sha256"] == observed_source_sha256
    assert got["drift"]["state"] == "clean"

    with source_path.open("wb") as stream:
        stream.truncate(MARKDOWN_MAX_SOURCE_BYTES + 1)
    oversized_listed = cowork_ops.cowork_doc_list(store_id)["docs"][0]
    oversized_got = cowork_ops.cowork_doc_get(store_id, document.id)
    assert oversized_listed["current_file_sha256"] == imported_source_sha256
    assert oversized_listed["observed_source_file_sha256"] is None
    assert oversized_listed["drift_state"] == "clean"
    assert oversized_got["hashes"]["current_file_sha256"] == imported_source_sha256
    assert oversized_got["observed_source_file_sha256"] is None
    assert oversized_got["drift"]["state"] == "clean"


def test_cowork_execution_session_cannot_read_another_document(
    cowork: dict[str, object],
) -> None:
    store_id = str(cowork["store_id"])
    own_doc_id, _ = _register_doc(cowork["store"])
    other_doc_id, _ = _register_doc(
        cowork["store"],
        path="docs/other.md",
        body="# Other\n\nOther document.\n",
    )
    own_generation = "generation-own"
    other_generation = "generation-other"
    _activate_document_lease(store_id, own_doc_id, own_generation)
    _activate_document_lease(store_id, other_doc_id, other_generation)

    own = cowork_ops.cowork_doc_get(
        store_id,
        own_doc_id,
        agent_session_id=f"{own_generation}-cowork",
    )
    other = cowork_ops.cowork_doc_get(
        store_id,
        other_doc_id,
        agent_session_id=f"{own_generation}-cowork",
    )

    assert own["ok"] is True
    disclosures = cowork["disclosures"]
    assert len(disclosures) == 1
    assert disclosures[0][0] == "input"
    assert disclosures[0][2]["payload"]["document_id"] == own_doc_id
    assert other == {
        "ok": False,
        "status": "lease_lost",
        "document_id": other_doc_id,
        "store_id": store_id,
    }


def test_get_exposes_truth_backed_feedback_by_exact_message_id(
    cowork: dict[str, object],
) -> None:
    store = cowork["store"]
    store_id = str(cowork["store_id"])
    doc_id, _ = _register_doc(store)
    poster = cowork_conversations.feedback_poster(
        document_id=doc_id,
        store_id=store_id,
    )
    captured = cowork_feedback.capture_feedback(
        store,
        document_id=doc_id,
        span=cowork_feedback.FeedbackSpan(
            exact=QUOTE,
            prefix="",
            suffix=" for cowork ops tests.",
            node_id_hint="throwaway-node",
        ),
        verbatim_text="Please tighten this.",
        actor=HUMAN,
        post_message=poster,
        at=NOW,
        emit_event=lambda *_args, **_kwargs: TruthEventEmission(
            "feedback-event",
            True,
        ),
    )

    got = cowork_ops.cowork_doc_get(store_id, doc_id)
    assert got["feedback"] == [
        {
            "evidence_id": captured.evidence_id,
            "span_id": captured.document_span_id,
            "conversation_id": captured.conversation_id,
            "message_id": captured.message_id,
            "text": "Please tighten this.",
            "anchor": {
                "exact": QUOTE,
                "prefix": "",
                "suffix": " for cowork ops tests.",
                "node_id_hint": "throwaway-node",
            },
        }
    ]


def test_list_profile_filter_is_store_scoped(cowork: dict[str, object]) -> None:
    store_id = str(cowork["store_id"])
    _register_doc(cowork["store"])
    assert cowork_ops.cowork_doc_list(store_id, profile="cowork-doc")["count"] == 1
    assert cowork_ops.cowork_doc_list(store_id, profile="other")["count"] == 0


# --------------------------------------------------------------------------
# Propose edit.
# --------------------------------------------------------------------------


def test_propose_edit_opens_a_proposal_and_emits(cowork: dict[str, object]) -> None:
    store_id = str(cowork["store_id"])
    doc_id, _ = _register_doc(cowork["store"])

    result = cowork_ops.cowork_doc_propose_edit(
        store_id,
        doc_id,
        _one_hunk(),
        "The sentence reads better revised.",
        "tighten the sentence",
        MODEL,
        agent_session_id=SESSION_ID,
    )
    assert result["ok"] is True
    assert result["created_count"] == 1
    proposal_id = result["proposals"][0]["id"]

    store = cowork["registry"].open_store(store_id)
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 1
    open_props = proposals.open_proposals(store, document_id=doc_id)
    assert [item.id for item in open_props] == [proposal_id]
    assert open_props[0].replacement == "Revised sentence"

    got = cowork_ops.cowork_doc_get(store_id, doc_id)
    view = got["open_proposals"][0]
    assert view["kind"] == "edit"
    assert view["base_ok"] is True
    assert view["applicability"]["status"] == "applicable"
    assert view["quote_anchor"]["exact"] == QUOTE
    assert view["producer"]["session_id"] == SESSION_ID

    assert cowork["emitted"][-1][0] == "truth.doc_proposed"
    assert cowork["emitted"][-1][1]["data"]["kind"] == "edit"


def test_propose_edit_opens_deletion_as_an_edit(cowork: dict[str, object]) -> None:
    store_id = str(cowork["store_id"])
    doc_id, _ = _register_doc(cowork["store"])

    result = cowork_ops.cowork_doc_propose_edit(
        store_id,
        doc_id,
        _one_hunk(""),
        "This sentence should be removed.",
        "Remove the sentence",
        MODEL,
        agent_session_id=SESSION_ID,
    )

    assert result["created_count"] == 1
    assert result["proposals"][0]["replacement"] == ""
    open_proposal = cowork_ops.cowork_doc_get(store_id, doc_id)["open_proposals"][0]
    assert open_proposal["kind"] == "edit"
    assert open_proposal["replacement"] == ""
    assert cowork["emitted"][-1][1]["data"]["kind"] == "edit"


def test_propose_edit_preserves_meaningful_edge_whitespace(
    cowork: dict[str, object],
) -> None:
    store_id = str(cowork["store_id"])
    doc_id, _ = _register_doc(cowork["store"])
    replacement = " \nRevised sentence.\n "

    result = cowork_ops.cowork_doc_propose_edit(
        store_id,
        doc_id,
        _one_hunk(replacement),
        "Preserve intentional spacing.",
        "Revise with spacing",
        MODEL,
        agent_session_id=SESSION_ID,
    )

    assert result["proposals"][0]["replacement"] == replacement
    assert proposals.open_proposals(
        cowork["store"], document_id=doc_id
    )[0].replacement == replacement


def test_propose_edit_defaults_base_to_current_content(cowork: dict[str, object]) -> None:
    store_id = str(cowork["store_id"])
    doc_id, content_sha = _register_doc(cowork["store"])
    cowork_ops.cowork_doc_propose_edit(
        store_id, doc_id, _one_hunk(), "reason", "tldr", MODEL,
        agent_session_id=SESSION_ID,
    )
    got = cowork_ops.cowork_doc_get(store_id, doc_id)
    assert got["open_proposals"][0]["base_doc_sha256"] == content_sha
    assert got["open_proposals"][0]["base_ok"] is True


def test_generation_rotation_between_receive_and_proposal_fences_stale_write(
    cowork: dict[str, object],
) -> None:
    store_id = str(cowork["store_id"])
    doc_id, _ = _register_doc(cowork["store"])
    old_generation = "cowork-generation-old"
    binding, consumer = _activate_document_lease(
        store_id,
        doc_id,
        old_generation,
    )
    turn = conversation_store.post_user_message(
        binding.conversation_id,
        "Please revise this sentence.",
    )
    assert turn is not None
    received = conversation_store.receive_user_message(
        binding.conversation_id,
        consumer,
        old_generation,
    )
    assert received["message"]["message_id"] == turn.message_id

    assert conversation_store.stop_agent_lease(
        binding.conversation_id,
        consumer,
        old_generation,
    )
    new_generation = "cowork-generation-new"
    claim = conversation_store.claim_agent_lease(
        binding.conversation_id,
        consumer,
        new_generation,
    )
    assert claim is not None and claim["claimed"] is True
    assert conversation_store.activate_agent_lease(
        binding.conversation_id,
        consumer,
        new_generation,
        92002,
    )

    stale = cowork_ops.cowork_doc_propose_edit(
        store_id,
        doc_id,
        _one_hunk(),
        "Stale generation must not write.",
        "stale",
        MODEL,
        agent_session_id=SESSION_ID,
        conversation_id=binding.conversation_id,
        consumer=consumer,
        generation=old_generation,
    )
    assert stale == {"ok": False, "status": "lease_lost"}
    assert proposals.open_proposals(cowork["store"], document_id=doc_id) == ()

    current = cowork_ops.cowork_doc_propose_edit(
        store_id,
        doc_id,
        _one_hunk(),
        "Current generation may propose.",
        "current",
        MODEL,
        agent_session_id=SESSION_ID,
        conversation_id=binding.conversation_id,
        consumer=consumer,
        generation=new_generation,
    )
    assert current["ok"] is True
    assert current["created_count"] == 1


def test_document_write_lease_cannot_be_reused_for_another_document(
    cowork: dict[str, object],
) -> None:
    store_id = str(cowork["store_id"])
    first_doc, _ = _register_doc(
        cowork["store"],
        path="docs/first-fenced.md",
    )
    second_doc, _ = _register_doc(
        cowork["store"],
        path="docs/second-fenced.md",
    )
    generation = "cowork-generation-bound"
    binding, consumer = _activate_document_lease(
        store_id,
        first_doc,
        generation,
    )
    result = cowork_ops.cowork_doc_comment(
        store_id,
        second_doc,
        {"exact": QUOTE},
        "Must not cross the document boundary.",
        "wrong document",
        MODEL,
        agent_session_id=SESSION_ID,
        conversation_id=binding.conversation_id,
        consumer=consumer,
        generation=generation,
    )
    assert result == {"ok": False, "status": "lease_lost"}
    assert proposals.open_proposals(
        cowork["store"],
        document_id=second_doc,
    ) == ()


def test_propose_and_comment_reject_retired_document(
    cowork: dict[str, object],
) -> None:
    store = cowork["store"]
    store_id = str(cowork["store_id"])
    doc_id, _ = _register_doc(store, path="docs/retired-ops.md")
    documents.retire_document(
        store,
        document_id=doc_id,
        actor=HUMAN,
    )

    with pytest.raises(InvariantViolation, match="retired documents"):
        cowork_ops.cowork_doc_propose_edit(
            store_id,
            doc_id,
            _one_hunk(),
            "No longer active.",
            "retired",
            MODEL,
            agent_session_id=SESSION_ID,
        )
    with pytest.raises(InvariantViolation, match="retired documents"):
        cowork_ops.cowork_doc_comment(
            store_id,
            doc_id,
            {"exact": QUOTE},
            "No longer active.",
            "retired",
            MODEL,
            agent_session_id=SESSION_ID,
        )


def test_propose_edit_enforces_claim_ref_role(cowork: dict[str, object]) -> None:
    store_id = str(cowork["store_id"])
    doc_id, _ = _register_doc(cowork["store"])
    claim = _seed_claim(cowork["store"])

    with pytest.raises(InvariantViolation, match="role must be one of"):
        cowork_ops.cowork_doc_propose_edit(
            store_id, doc_id, _one_hunk(), "reason", "tldr", MODEL,
            claim_refs=[{"claim": claim.id, "role": "bogus"}],
            agent_session_id=SESSION_ID,
        )

    cowork_ops.cowork_doc_propose_edit(
        store_id, doc_id, _one_hunk(), "reason", "tldr", MODEL,
        claim_refs=[{"claim": claim.id, "role": "summary"}, claim.id],
        agent_session_id=SESSION_ID,
    )
    got = cowork_ops.cowork_doc_get(store_id, doc_id)
    refs = got["open_proposals"][0]["claim_refs"]
    assert {"claim": claim.id, "role": "summary"} in refs
    assert {"claim": claim.id, "role": "instantiation"} in refs


def test_propose_edit_validates_hunks_and_anchor(cowork: dict[str, object]) -> None:
    store_id = str(cowork["store_id"])
    doc_id, _ = _register_doc(cowork["store"])

    with pytest.raises(InvariantViolation, match="at least one edit"):
        cowork_ops.cowork_doc_propose_edit(
            store_id, doc_id, [], "r", "t", MODEL, agent_session_id=SESSION_ID
        )
    with pytest.raises(InvariantViolation, match="replacement must be a string"):
        cowork_ops.cowork_doc_propose_edit(
            store_id, doc_id, [{"quote_anchor": {"exact": QUOTE}}], "r", "t", MODEL,
            agent_session_id=SESSION_ID,
        )
    with pytest.raises(InvariantViolation, match="replacement must be a string"):
        cowork_ops.cowork_doc_propose_edit(
            store_id,
            doc_id,
            [{"quote_anchor": {"exact": QUOTE}, "replacement": 42}],
            "r",
            "t",
            MODEL,
            agent_session_id=SESSION_ID,
        )
    with pytest.raises(InvariantViolation, match="whitespace-only"):
        cowork_ops.cowork_doc_propose_edit(
            store_id,
            doc_id,
            [{"quote_anchor": {"exact": QUOTE}, "replacement": " \n\t"}],
            "r",
            "t",
            MODEL,
            agent_session_id=SESSION_ID,
        )
    with pytest.raises(InvariantViolation, match="exact quote"):
        cowork_ops.cowork_doc_propose_edit(
            store_id, doc_id, [{"quote_anchor": {"prefix": "x"}, "replacement": "y"}],
            "r", "t", MODEL, agent_session_id=SESSION_ID,
        )

    with pytest.raises(InvariantViolation, match="whitespace-only"):
        cowork_ops.cowork_doc_propose_edit(
            store_id,
            doc_id,
            [
                {
                    "quote_anchor": {"exact": QUOTE},
                    "replacement": "Valid first replacement",
                },
                {
                    "quote_anchor": {"exact": QUOTE},
                    "replacement": "   ",
                },
            ],
            "must be atomic",
            "no partial proposal",
            MODEL,
            agent_session_id=SESSION_ID,
        )
    assert proposals.open_proposals(cowork["store"], document_id=doc_id) == ()


def test_propose_edit_rejects_claim_refs_on_deletion_atomically(
    cowork: dict[str, object],
) -> None:
    store_id = str(cowork["store_id"])
    doc_id, _ = _register_doc(cowork["store"])
    claim = _seed_claim(cowork["store"])

    with pytest.raises(
        InvariantViolation,
        match="deletion proposals cannot carry claim_refs",
    ):
        cowork_ops.cowork_doc_propose_edit(
            store_id,
            doc_id,
            [
                {
                    "quote_anchor": {"exact": QUOTE},
                    "replacement": "Valid first replacement",
                },
                {
                    "quote_anchor": {"exact": QUOTE},
                    "replacement": "",
                },
            ],
            "must be atomic",
            "no partial proposal",
            MODEL,
            claim_refs=[{"claim": claim.id, "role": "summary"}],
            agent_session_id=SESSION_ID,
        )

    assert proposals.open_proposals(cowork["store"], document_id=doc_id) == ()


def test_cowork_execution_session_cannot_omit_its_document_write_fence(
    cowork: dict[str, object],
) -> None:
    store_id = str(cowork["store_id"])
    doc_id, _ = _register_doc(cowork["store"])
    generation = "generation-bound"
    _binding, _consumer = _activate_document_lease(
        store_id,
        doc_id,
        generation,
    )

    result = cowork_ops.cowork_doc_propose_edit(
        store_id,
        doc_id,
        _one_hunk(),
        "reason",
        "tldr",
        MODEL,
        agent_session_id=f"{generation}-cowork",
    )

    assert result == {"ok": False, "status": "lease_lost"}
    assert proposals.open_proposals(
        cowork["store"],
        document_id=doc_id,
    ) == ()


def test_bound_document_output_manifest_is_written_before_truth_proposal(
    cowork: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_id = str(cowork["store_id"])
    store = cowork["store"]
    doc_id, _ = _register_doc(store)
    generation = "generation-write-ahead"
    binding, consumer = _activate_document_lease(store_id, doc_id, generation)
    calls: list[str] = []

    def bind_before_truth(**kwargs):
        assert proposals.open_proposals(store, document_id=doc_id) == ()
        calls.append(str(kwargs["output_ref"]))
        return {
            "manifest_sha256": "d" * 64,
            "entry_count": 1,
            "through_sequence": 1,
        }

    monkeypatch.setattr(cowork_ops, "_bind_document_worker_output", bind_before_truth)
    result = cowork_ops.cowork_doc_propose_edit(
        store_id,
        doc_id,
        _one_hunk(),
        "reason",
        "tldr",
        MODEL,
        agent_session_id=f"{generation}-cowork",
        conversation_id=binding.conversation_id,
        consumer=consumer,
        generation=generation,
    )

    assert result["created_count"] == 1
    assert result["input_manifest"]["manifest_sha256"] == "d" * 64
    assert len(calls) == 1
    assert calls[0].startswith("cowork-document-proposals-request:")


# --------------------------------------------------------------------------
# Producer identity.
# --------------------------------------------------------------------------


def test_propose_requires_gateway_session_identity(cowork: dict[str, object]) -> None:
    store_id = str(cowork["store_id"])
    doc_id, _ = _register_doc(cowork["store"])
    with pytest.raises(InvariantViolation, match="gateway session"):
        cowork_ops.cowork_doc_propose_edit(
            store_id, doc_id, _one_hunk(), "reason", "tldr", MODEL
        )


def test_propose_rejects_model_mismatch(
    cowork: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    store_id = str(cowork["store_id"])
    doc_id, _ = _register_doc(cowork["store"])
    monkeypatch.setattr(
        truth_ops,
        "_session_manifest",
        lambda session_id: {
            "session_id": session_id,
            "harness_id": "codex",
            "model": "manifest-model",
        },
    )
    with pytest.raises(InvariantViolation, match="does not match"):
        cowork_ops.cowork_doc_propose_edit(
            store_id, doc_id, _one_hunk(), "reason", "tldr", "caller-model",
            agent_session_id=SESSION_ID,
        )


# --------------------------------------------------------------------------
# Comment (flag) and expression mark.
# --------------------------------------------------------------------------


def test_comment_opens_a_flag(cowork: dict[str, object]) -> None:
    store_id = str(cowork["store_id"])
    doc_id, _ = _register_doc(cowork["store"])

    result = cowork_ops.cowork_doc_comment(
        store_id,
        doc_id,
        {"exact": QUOTE},
        "This claim is unsupported.",
        "unsupported claim",
        MODEL,
        agent_session_id=SESSION_ID,
    )
    assert result["created"] is True
    assert result["proposal"]["replacement"] is None
    assert result["proposal"]["rationale"] == "This claim is unsupported."

    store = cowork["registry"].open_store(store_id)
    open_props = proposals.open_proposals(store, document_id=doc_id)
    assert len(open_props) == 1
    assert open_props[0].replacement is None

    got = cowork_ops.cowork_doc_get(store_id, doc_id)
    assert got["open_proposals"][0]["kind"] == "flag"
    assert cowork["emitted"][-1][0] == "truth.doc_proposed"
    assert cowork["emitted"][-1][1]["data"]["kind"] == "flag"


def test_comment_requires_a_nonempty_body(cowork: dict[str, object]) -> None:
    store_id = str(cowork["store_id"])
    doc_id, _ = _register_doc(cowork["store"])
    with pytest.raises(InvariantViolation, match="comment body"):
        cowork_ops.cowork_doc_comment(
            store_id, doc_id, {"exact": QUOTE}, "   ", "tldr", MODEL,
            agent_session_id=SESSION_ID,
        )


def test_expression_mark_mints_expression_with_role(cowork: dict[str, object]) -> None:
    store_id = str(cowork["store_id"])
    doc_id, _ = _register_doc(cowork["store"])
    claim = _seed_claim(cowork["store"])

    result = cowork_ops.cowork_doc_expression_mark(
        store_id,
        doc_id,
        {"exact": QUOTE},
        claim.id,
        "paraphrase",
        MODEL,
        agent_session_id=SESSION_ID,
    )
    assert result["ok"] is True
    assert result["expression"]["role"] == "paraphrase"
    assert result["document_span"]["author_kind"] == "unknown"
    assert result["document_span"]["author_ref"] is None

    store = cowork["registry"].open_store(store_id)
    expr_rows = expressions.expressions_for_document(store, doc_id)
    assert [item.role for item in expr_rows] == ["paraphrase"]

    got = cowork_ops.cowork_doc_get(store_id, doc_id)
    assert got["expressions"][0]["role"] == "paraphrase"
    assert got["expressions"][0]["claim_ref"] == claim.id
    assert cowork["emitted"][-1][0] == "truth.doc_expression_marked"


def test_expression_mark_requires_a_valid_role(cowork: dict[str, object]) -> None:
    store_id = str(cowork["store_id"])
    doc_id, _ = _register_doc(cowork["store"])
    claim = _seed_claim(cowork["store"])
    with pytest.raises(InvariantViolation, match="role must be one of"):
        cowork_ops.cowork_doc_expression_mark(
            store_id, doc_id, {"exact": QUOTE}, claim.id, "bogus", MODEL,
            agent_session_id=SESSION_ID,
        )


# --------------------------------------------------------------------------
# document_surface gate.
# --------------------------------------------------------------------------


def test_ops_refuse_when_document_surface_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_store(tmp_path, monkeypatch, document_surface=False)
    store_id = str(ctx["store_id"])
    with pytest.raises(InvariantViolation, match="document_surface"):
        cowork_ops.cowork_doc_list(store_id)


# --------------------------------------------------------------------------
# Exact conversation reads through the shared worker disclosure boundary.
# --------------------------------------------------------------------------


@pytest.fixture
def accounted_conversation(cowork, tmp_path, monkeypatch):
    from work_buddy.agent_execution.disclosure import (
        DisclosureGateway,
        DisclosureManifestStore,
    )
    from work_buddy.cowork import conversation_source_dependencies, worker_disclosure
    from work_buddy.sources import ActorRef, SourceStore
    from work_buddy.sources.disclosure import SourcesDisclosureService

    source_store = SourceStore.create(tmp_path / "conversation-sources")
    issuer = ActorRef(source_store.authority_id, "conversation-test", "service", "tenant-test")
    sources = SourcesDisclosureService(source_store, tenant_scope_id="tenant-test", issuer=issuer)
    gateway = DisclosureGateway(DisclosureManifestStore(tmp_path / "disclosures.db"), sources)
    boundary = worker_disclosure.CoworkWorkerDisclosureBoundary(gateway, sources)
    monkeypatch.setattr(worker_disclosure, "get_cowork_worker_disclosure", lambda: boundary)
    # Output dependency recording is not the read-accounting seam under test.
    monkeypatch.setattr(
        conversation_source_dependencies, "record_conversation_source_dependency",
        lambda **kwargs: None,
    )
    document_id, _ = _register_doc(cowork["store"])
    generation = "conversation-read-generation"
    binding = cowork_conversations.ensure_document_conversation(
        document_id=document_id, store_id=str(cowork["store_id"]),
    )
    consumer = f"cowork-document:{cowork['store_id']}:{document_id}"
    execution = {
        "schema_version": 1, "provider_id": "test-provider", "model_id": "test-model",
        "provider_label": "Test provider", "model_label": "Test model",
    }
    assert conversation_store.claim_agent_lease(
        binding.conversation_id, consumer, generation, execution=execution,
    )["claimed"] is True
    assert conversation_store.activate_agent_lease(binding.conversation_id, consumer, generation, 92001)
    run = worker_disclosure.CoworkWorkerRun(
        run_id=f"{generation}-cowork", worker_session_id=f"{generation}-cowork",
        provider_id="test-provider", model_id="test-model",
        authorization_ref=f"cowork-document-agent:{binding.conversation_id}:{generation}",
        purpose="cowork_document_agent",
    )
    boundary.account_payload(
        run, payload={"text": "Fixture initial context"}, source_role="human_input",
        tool_call_id="fixture-context", idempotency_key="fixture-context",
    )
    question = conversation_store.add_message(
        binding.conversation_id, "agent", "Continue with this document?",
        message_id="exact-cowork-question", message_type="question", response_type="boolean",
    )
    answer = conversation_store.respond_to_message_with_user_message(
        binding.conversation_id, question.message_id, "true",
        user_message_id="exact-cowork-answer", context={"in_reply_to": question.message_id},
    )
    assert answer is not None
    return SimpleNamespace(
        boundary=boundary, source_store=source_store, gateway=gateway, run=run,
        conversation_id=binding.conversation_id, consumer=consumer, generation=generation,
        question=question, answer=answer, execution=execution,
    )


def _read_accounted_conversation(context, operation_name):
    from work_buddy.mcp_server.op_registry import get_op

    params = {
        "conversation_id": context.conversation_id,
        "consumer": context.consumer,
        "generation": context.generation,
        "agent_session_id": context.run.worker_session_id,
        "timeout_seconds": 0,
    }
    if operation_name != "conversation_receive":
        params["message_id"] = context.question.message_id
    if operation_name == "conversation_ask":
        params.update(question=context.question.content, response_type="boolean")
    return get_op(f"op.wb.{operation_name}")(**params)


@pytest.mark.parametrize("operation_name", ["conversation_ask", "conversation_poll", "conversation_receive"])
@pytest.mark.parametrize("change", ["stop", "rotate", "producer", "binding"])
def test_conversation_read_rechecks_exact_authority_after_accounting(
    accounted_conversation, monkeypatch, operation_name, change,
):
    from work_buddy.agent_execution.disclosure import DisclosureState

    context = accounted_conversation
    original = context.boundary.account_payload

    def account_then_change(run, **kwargs):
        result = original(run, **kwargs)
        # A separate writer must be possible while Sources work is running.
        if change in {"stop", "rotate"}:
            assert conversation_store.stop_agent_lease(
                context.conversation_id, context.consumer, context.generation,
            )
            if change == "rotate":
                claimed = conversation_store.claim_agent_lease(
                    context.conversation_id, context.consumer, "replacement-generation",
                    execution=context.execution,
                )
                assert claimed["claimed"] is True
                assert conversation_store.activate_agent_lease(
                    context.conversation_id, context.consumer, "replacement-generation", 92002,
                )
        else:
            with conversation_store.get_connection() as conn:
                if change == "producer":
                    conn.execute(
                        "UPDATE conversation_agent_leases SET execution_json = "
                        "json_set(execution_json, '$.model_id', 'another-model') "
                        "WHERE conversation_id = ? AND consumer = ?",
                        (context.conversation_id, context.consumer),
                    )
                else:
                    conn.execute(
                        "UPDATE conversations SET metadata = "
                        "json_set(metadata, '$.cowork_document_id', 'another-document') "
                        "WHERE conversation_id = ?", (context.conversation_id,),
                    )
        return result

    monkeypatch.setattr(context.boundary, "account_payload", account_then_change)
    result = _read_accounted_conversation(context, operation_name)
    assert result == {"status": "lease_lost", "conversation_id": context.conversation_id}
    entry = next(
        entry for entry in context.gateway.store.list_entries(context.run.run_id)
        if entry.tool_call_id == operation_name
    )
    assert entry.state is DisclosureState.POSSIBLY_SENT


@pytest.mark.parametrize("operation_name", ["conversation_ask", "conversation_poll"])
def test_question_answer_lineage_redaction_blocks_replay_and_output(
    accounted_conversation, operation_name,
):
    from work_buddy.agent_execution.disclosure import DisclosureSourceError
    from work_buddy.sources.models import SourceRef
    from work_buddy.sources.redact import redact_source

    context = accounted_conversation
    result = _read_accounted_conversation(context, operation_name)
    assert result["status"] == "answered"
    assert result["message_id"] == context.question.message_id
    assert result["response"] == context.answer.content
    entry = next(
        entry for entry in context.gateway.store.list_entries(context.run.run_id)
        if entry.tool_call_id == operation_name
    )
    derived = SourceRef.parse(entry.source_ref)
    with context.source_store.connect() as conn:
        refs = conn.execute(
            "SELECT input_authority_id, input_item_id FROM source_derivations "
            "WHERE derived_authority_id = ? AND derived_item_id = ?",
            (derived.authority_id, derived.item_id),
        ).fetchall()
    native = {
        context.source_store.get_item(ref).origin_ref.native_item_id: ref
        for row in refs
        for ref in [SourceRef(row["input_authority_id"], row["input_item_id"])]
    }
    assert set(native) == {context.question.message_id, context.answer.message_id}
    # A causal output proves delivery; the exact read is replayable until one
    # of its native inputs is redacted, rather than merely ambiguous.
    context.boundary.bind_output(context.run, output_ref="before-redaction", idempotency_key="before-redaction")
    assert _read_accounted_conversation(context, operation_name)["response"] == "true"
    answer_ref = native[context.answer.message_id]
    context.source_store.grant_access(
        source_ref=answer_ref, principal=context.boundary.sources.issuer,
        purpose="redaction", access_mode="metadata", authorization_fingerprint="f" * 64,
    )
    redact_source(
        context.source_store, source_ref=answer_ref, actor=context.boundary.sources.issuer,
        authorization_fingerprint="f" * 64, reason_code="user_requested",
    )
    assert context.source_store.get_item(derived).lifecycle_state == "redacted"
    with pytest.raises(DisclosureSourceError):
        context.boundary.bind_output(context.run, output_ref="after-redaction", idempotency_key="after-redaction")
    with pytest.raises(DisclosureSourceError):
        _read_accounted_conversation(context, operation_name)


@pytest.mark.parametrize("operation_name", ["conversation_ask", "conversation_poll"])
@pytest.mark.parametrize("invalid_link", ["legacy_unlinked", "mismatched_text", "duplicate_link"])
def test_question_answer_requires_one_exact_native_answer(
    accounted_conversation, operation_name, invalid_link,
):
    context = accounted_conversation
    with conversation_store.get_connection() as conn:
        if invalid_link == "legacy_unlinked":
            conn.execute("UPDATE messages SET context_json = NULL WHERE message_id = ?", (context.answer.message_id,))
        elif invalid_link == "mismatched_text":
            conn.execute("UPDATE messages SET content = 'different answer' WHERE message_id = ?", (context.answer.message_id,))
        else:
            conversation_store.post_user_message(
                context.conversation_id, context.answer.content,
                message_id="duplicate-linked-answer", context={"in_reply_to": context.question.message_id}, conn=conn,
            )
    result = _read_accounted_conversation(context, operation_name)
    assert result["status"] == "invalid_request"
    assert result["error"] == "The exact native conversation answer is unavailable"
    assert "response" not in result
    assert "question" not in result
    assert not any(
        entry.tool_call_id == operation_name
        for entry in context.gateway.store.list_entries(context.run.run_id)
    )


def test_pending_question_read_captures_its_exact_native_origin(
    accounted_conversation, monkeypatch,
):
    from work_buddy.mcp_server.op_registry import get_op
    from work_buddy.sources.models import SourceRef

    context = accounted_conversation
    pending = conversation_store.add_message(
        context.conversation_id, "agent", "A different pending question?",
        message_id="pending-native-question", message_type="question", response_type="boolean",
    )
    captured = []
    original = context.boundary.account_payload

    def account(run, **kwargs):
        captured.extend(kwargs["derivation_refs"])
        return original(run, **kwargs)

    monkeypatch.setattr(context.boundary, "account_payload", account)
    result = get_op("op.wb.conversation_poll")(
        conversation_id=context.conversation_id, message_id=pending.message_id,
        consumer=context.consumer, generation=context.generation,
        agent_session_id=context.run.worker_session_id,
    )
    assert result == {
        "status": "pending", "message_id": pending.message_id, "question": pending.content,
    }
    assert len(captured) == 1
    native = context.source_store.get_item(SourceRef.parse(captured[0]))
    assert native.origin_ref.container_id == context.conversation_id
    assert native.origin_ref.native_item_id == pending.message_id

from __future__ import annotations

import errno
import importlib
import json
from pathlib import Path

import pytest

from work_buddy.consent import (
    ConsentRequired,
    per_invocation_authorization,
)
from work_buddy.cowork import project_store
from work_buddy.cowork.project_store import (
    FolderLifecycleError,
    ProjectStoreManager,
)
from work_buddy.mcp_server.op_registry import load_builtin_ops
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.events import TruthEventEmission
from work_buddy.truth.lifecycle import ConfirmationResult, TruthLifecycle
from work_buddy.truth.registry import TruthStoreRegistry


load_builtin_ops()
truth_ops = importlib.import_module("work_buddy.mcp_server.ops.truth_ops")


SESSION_ID = "session-truth-ops"
MODEL = "gpt-test"


def _profile(store_id: str | None = None) -> dict[str, object]:
    profile: dict[str, object] = {
        "profile": "ops-test",
        "title": "Truth operation test",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": "retain",
            "confirmation_surfaces": ["chat_consent", "cli"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": True,
    }
    if store_id is not None:
        profile["store_id"] = store_id
    return profile


@pytest.fixture
def operation_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    registry = TruthStoreRegistry(tmp_path / "truth-registry.db")
    monkeypatch.setattr(truth_ops, "_registry", lambda: registry)
    monkeypatch.setattr(
        truth_ops,
        "_session_manifest",
        lambda session_id: {
            "session_id": session_id,
            "harness_id": "codex",
        },
    )
    emitted: list[tuple[str, dict[str, object]]] = []

    def emit(event_type: str, **kwargs):
        emitted.append((event_type, kwargs))
        return TruthEventEmission(f"event-{len(emitted)}", True)

    monkeypatch.setattr(truth_ops, "emit_truth_event", emit)
    (tmp_path / "scope").mkdir()
    created = truth_ops.truth_store_create.__wrapped__(
        str(tmp_path / "scope"),
        _profile(),
    )
    store_id = created["store"]["store_id"]
    return {
        "registry": registry,
        "store_id": store_id,
        "emitted": emitted,
        "root": tmp_path / "scope",
    }


def _capture_and_span(
    operation_store: dict[str, object],
    tmp_path: Path,
    *,
    text: str,
    filename: str,
) -> tuple[str, str]:
    store_id = str(operation_store["store_id"])
    captured = truth_ops.truth_evidence_capture(
        store_id,
        "document",
        (tmp_path / filename).resolve().as_uri(),
        "file_read",
        MODEL,
        content=text,
        origin="preexisting",
        producer_call_id="call-1",
        agent_session_id=SESSION_ID,
    )
    marked = truth_ops.truth_span_mark(
        store_id,
        captured["evidence"]["id"],
        {"exact": text},
        MODEL,
        agent_session_id=SESSION_ID,
    )
    return captured["evidence"]["id"], marked["span"]["id"]


def _propose_supported(
    operation_store: dict[str, object],
    tmp_path: Path,
    proposition: str,
    *,
    filename: str,
    claim_kind: str = "fact",
) -> tuple[str, str]:
    _, span_id = _capture_and_span(
        operation_store,
        tmp_path,
        text=proposition,
        filename=filename,
    )
    proposed = truth_ops.truth_claim_propose(
        str(operation_store["store_id"]),
        proposition,
        claim_kind,
        MODEL,
        support_span_ids=[span_id],
        agent_session_id=SESSION_ID,
    )
    return proposed["claim"]["id"], span_id


def _authorize(callable_, operation: str, *args, **kwargs):
    with pytest.raises(ConsentRequired) as blocked:
        callable_(*args, **kwargs)
    required = blocked.value
    with per_invocation_authorization(
        operation,
        required.fingerprint,
        request_id="request-1",
        response_surface="dashboard",
        context=required.context,
    ):
        result = callable_(*args, **kwargs)
    return required, result


def test_store_create_mints_registers_lists_and_emits(
    operation_store: dict[str, object],
) -> None:
    store_id = str(operation_store["store_id"])
    assert len(store_id) == 32
    listed = truth_ops.truth_store_list()
    assert listed["count"] == 1
    assert listed["stores"][0]["store_id"] == store_id
    assert operation_store["emitted"][0][0] == "truth.store_created"


def test_store_create_registration_failure_rolls_back_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = TruthStoreRegistry(tmp_path / "truth-registry.db")
    monkeypatch.setattr(truth_ops, "_registry", lambda: registry)
    emitted: list[str] = []
    monkeypatch.setattr(
        truth_ops,
        "emit_truth_event",
        lambda event_type, **kwargs: emitted.append(event_type),
    )
    original_register = registry.register

    def register_then_fail(store):
        original_register(store)
        raise RuntimeError("forced registry failure")

    monkeypatch.setattr(registry, "register", register_then_fail)
    root = tmp_path / "failed-scope"
    root.mkdir()
    store_id = "1" * 32
    with pytest.raises(RuntimeError, match="forced registry failure"):
        truth_ops.truth_store_create.__wrapped__(
            str(root),
            _profile(store_id),
    )

    assert not (root / ".wbuddy").exists()
    assert registry.list_stores(refresh=False) == ()
    assert emitted == []

    monkeypatch.setattr(registry, "register", original_register)
    retried = truth_ops.truth_store_create.__wrapped__(
        str(root),
        _profile(store_id),
    )
    assert retried["store"]["store_id"] == store_id
    assert (root / ".wbuddy" / "cowork" / "store.db").is_file()
    assert (root / ".wbuddy" / "manifest.yaml").is_file()
    assert [row.store_id for row in registry.list_stores(refresh=False)] == [store_id]
    assert emitted == ["truth.store_created"]


def test_generic_store_create_rejects_partial_cowork_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = TruthStoreRegistry(tmp_path / "truth-registry.db")
    monkeypatch.setattr(truth_ops, "_registry", lambda: registry)
    monkeypatch.setattr(
        truth_ops,
        "emit_truth_event",
        lambda *args, **kwargs: TruthEventEmission("event-create", True),
    )
    root = tmp_path / "ordinary-root"
    partial_canonical = root / ".wbuddy" / "cowork"
    partial_canonical.mkdir(parents=True)
    sentinel = partial_canonical / "interrupted-setup"
    sentinel.write_text("preserve", encoding="utf-8")

    # The folder is a collision, and the refusal says so: it holds Work Buddy
    # data that does not form a complete Co-work folder.
    with pytest.raises(FolderLifecycleError) as failure:
        truth_ops.truth_store_create.__wrapped__(
            str(root),
            _profile("3" * 32),
        )

    assert failure.value.code == "folder_layout_incomplete"
    assert "does not form a complete Co-work folder" in str(failure.value)
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not (partial_canonical / "store.db").exists()
    assert not (partial_canonical / "store.yaml").exists()


def test_store_create_walks_the_folder_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup pays for one descendant walk, not two.

    The walk that authorizes the write runs inside the folder operation locks
    and re-classifies the folder from the filesystem. A second walk ahead of
    it would prove nothing the locked one does not re-prove, and on a large
    folder it is the dominant cost of the operation.
    """

    registry = TruthStoreRegistry(tmp_path / "truth-registry.db")
    monkeypatch.setattr(truth_ops, "_registry", lambda: registry)
    monkeypatch.setattr(
        truth_ops,
        "emit_truth_event",
        lambda *args, **kwargs: TruthEventEmission("event-create", True),
    )
    root = tmp_path / "walked-once"
    (root / "nested" / "deeper").mkdir(parents=True)
    (root / "nested" / "note.txt").write_text("content", encoding="utf-8")
    walks: list[Path] = []
    real_scan = ProjectStoreManager._scan_descendants

    def record(self: ProjectStoreManager, scanned: Path, **kwargs: object):
        walks.append(scanned)
        return real_scan(self, scanned, **kwargs)

    monkeypatch.setattr(ProjectStoreManager, "_scan_descendants", record)

    created = truth_ops.truth_store_create.__wrapped__(
        str(root),
        _profile("4" * 32),
    )

    assert created["store"]["store_id"] == "4" * 32
    assert [item.resolve() for item in walks] == [root.resolve()]


def test_store_create_refuses_an_oversized_folder_with_an_actionable_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized folder is named as oversized, not as a race.

    Setup here holds no earlier observation of the folder, so the refusal
    carries the classification's own code and prose. The agent reads the
    exception text and nothing else, so that text names both the folder's
    state and the action that answers it.
    """

    registry = TruthStoreRegistry(tmp_path / "truth-registry.db")
    monkeypatch.setattr(truth_ops, "_registry", lambda: registry)
    monkeypatch.setattr(
        truth_ops,
        "emit_truth_event",
        lambda *args, **kwargs: TruthEventEmission("event-create", True),
    )
    # The manager is built inside the operation with no scan arguments, and it
    # resolves the module defaults in its body, so lowering the threshold here
    # reaches it. A refusal proves the threshold moved: at the shipped value
    # this folder sets up cleanly.
    monkeypatch.setattr(
        project_store,
        "DEFAULT_SCAN_WORK_LIMIT",
        project_store._SCAN_DIRECTORY_WEIGHT,
    )
    root = tmp_path / "too-wide"
    root.mkdir()
    for index in range(4):
        (root / f"item-{index}").write_text("x", encoding="utf-8")

    with pytest.raises(FolderLifecycleError) as failure:
        truth_ops.truth_store_create.__wrapped__(
            str(root),
            _profile("5" * 32),
        )

    assert failure.value.code == "folder_too_large_for_safe_setup"
    message = str(failure.value)
    assert "too many items" in message
    assert "narrower folder" in message
    assert not (root / ".wbuddy").exists()
    assert registry.list_stores(refresh=False) == ()


def test_store_create_hands_back_an_unread_folder_as_worth_retrying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A folder Co-work could not read is refused as unread, not as damaged.

    The agent surface renders the exception type and its text and nothing
    else, so a healthy folder whose manifest another process held open for a
    moment has to come back saying exactly that. Reporting it as incomplete
    Work Buddy data would send an autonomous caller to repair a store the very
    next call would have read without trouble.
    """

    registry = TruthStoreRegistry(tmp_path / "truth-registry.db")
    monkeypatch.setattr(truth_ops, "_registry", lambda: registry)
    monkeypatch.setattr(
        truth_ops,
        "emit_truth_event",
        lambda *args, **kwargs: TruthEventEmission("event-create", True),
    )
    root = tmp_path / "held-open"
    (root / ".wbuddy").mkdir(parents=True)
    (root / ".wbuddy" / "manifest.yaml").write_text(
        "format: wbuddy-folder/v1\ncomponents:\n  cowork: {path: cowork}\n",
        encoding="utf-8",
    )
    real_read_bytes = Path.read_bytes

    def refuse_the_manifest(self: Path) -> bytes:
        if self.name == "manifest.yaml":
            raise OSError(errno.EACCES, "held open by another process")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", refuse_the_manifest)

    with pytest.raises(FolderLifecycleError) as failure:
        truth_ops.truth_store_create.__wrapped__(
            str(root),
            _profile("6" * 32),
        )

    assert failure.value.code == "folder_unreachable"
    assert failure.value.retryable is True
    assert failure.value.status == 503
    message = str(failure.value)
    assert "temporarily unavailable" in message
    assert "again in a moment" in message
    assert "Repair" not in message
    assert not (root / ".wbuddy" / "cowork").exists()
    assert registry.list_stores(refresh=False) == ()


def test_agent_capture_validates_locator_and_forces_producer_identity(
    operation_store: dict[str, object],
    tmp_path: Path,
) -> None:
    captured = truth_ops.truth_evidence_capture(
        str(operation_store["store_id"]),
        "document",
        (tmp_path / "source.txt").resolve().as_uri(),
        "file_read",
        MODEL,
        content="A durable source.",
        meta={"topic": "test"},
        producer_call_id="call-42",
        agent_session_id=SESSION_ID,
    )
    meta = json.loads(captured["evidence"]["meta_json"])
    assert meta | {
        "model": MODEL,
        "model_source": "caller_asserted",
        "harness": "codex",
        "surface": "mcp",
        "session_id": SESSION_ID,
        "call_id": "call-42",
    } == meta
    assert meta["verifiability_class"] == "A"
    assert captured["locator"]["locator_scheme"] == "file"

    with pytest.raises(InvariantViolation, match="authoritative actor field"):
        truth_ops.truth_evidence_capture(
            str(operation_store["store_id"]),
            "document",
            (tmp_path / "spoof.txt").resolve().as_uri(),
            "file_read",
            MODEL,
            content="Spoof attempt.",
            meta={"model": "not-the-gateway-model"},
            agent_session_id=SESSION_ID,
        )


def test_agent_model_uses_manifest_value_and_records_verified_source(
    operation_store: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        truth_ops,
        "_session_manifest",
        lambda session_id: {
            "session_id": session_id,
            "harness_id": "codex",
            "model": MODEL,
        },
    )
    captured = truth_ops.truth_evidence_capture(
        str(operation_store["store_id"]),
        "document",
        (tmp_path / "manifest-model.txt").resolve().as_uri(),
        "file_read",
        MODEL,
        content="Manifest-backed model identity.",
        agent_session_id=SESSION_ID,
    )
    meta = json.loads(captured["evidence"]["meta_json"])
    assert meta["model"] == MODEL
    assert meta["model_source"] == "session_manifest"


def test_placeholder_manifest_model_falls_back_to_labeled_caller_claim(
    operation_store: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        truth_ops,
        "_session_manifest",
        lambda session_id: {
            "session_id": session_id,
            "harness_id": "codex",
            "model": " UNKNOWN ",
        },
    )
    captured = truth_ops.truth_evidence_capture(
        str(operation_store["store_id"]),
        "document",
        (tmp_path / "placeholder-model.txt").resolve().as_uri(),
        "file_read",
        MODEL,
        content="Explicitly labeled fallback identity.",
        agent_session_id=SESSION_ID,
    )
    meta = json.loads(captured["evidence"]["meta_json"])
    assert meta["model"] == MODEL
    assert meta["model_source"] == "caller_asserted"


def test_agent_model_must_match_nonplaceholder_manifest_model(
    operation_store: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        truth_ops.truth_evidence_capture(
            str(operation_store["store_id"]),
            "document",
            (tmp_path / "model-mismatch.txt").resolve().as_uri(),
            "file_read",
            "caller-model",
            content="Contradictory model identity.",
            agent_session_id=SESSION_ID,
        )


def test_agent_write_fails_closed_without_session_harness_or_model(
    operation_store: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator = (tmp_path / "missing.txt").resolve().as_uri()
    with pytest.raises(InvariantViolation, match="gateway session"):
        truth_ops.truth_evidence_capture(
            str(operation_store["store_id"]),
            "document",
            locator,
            "file_read",
            MODEL,
            content="Missing session.",
        )
    monkeypatch.setattr(truth_ops, "_session_manifest", lambda session_id: {})
    with pytest.raises(InvariantViolation, match="harness"):
        truth_ops.truth_evidence_capture(
            str(operation_store["store_id"]),
            "document",
            locator,
            "file_read",
            MODEL,
            content="Missing harness.",
            agent_session_id=SESSION_ID,
        )
    monkeypatch.setattr(
        truth_ops,
        "_session_manifest",
        lambda session_id: {"session_id": session_id, "harness_id": "unknown"},
    )
    with pytest.raises(InvariantViolation, match="harness"):
        truth_ops.truth_evidence_capture(
            str(operation_store["store_id"]),
            "document",
            locator,
            "file_read",
            MODEL,
            content="Placeholder harness.",
            agent_session_id=SESSION_ID,
        )
    monkeypatch.setattr(
        truth_ops,
        "_session_manifest",
        lambda session_id: {"session_id": session_id, "harness_id": "codex"},
    )
    with pytest.raises(InvariantViolation, match="model"):
        truth_ops.truth_evidence_capture(
            str(operation_store["store_id"]),
            "document",
            locator,
            "file_read",
            " null ",
            content="Placeholder model.",
            agent_session_id=SESSION_ID,
        )


def test_propose_attaches_support_and_derivation_in_one_operation(
    operation_store: dict[str, object],
    tmp_path: Path,
) -> None:
    _, first_span = _capture_and_span(
        operation_store,
        tmp_path,
        text="The first premise.",
        filename="premise.txt",
    )
    premise = truth_ops.truth_claim_propose(
        str(operation_store["store_id"]),
        "The first premise.",
        "fact",
        MODEL,
        support_span_ids=[first_span],
        agent_session_id=SESSION_ID,
    )
    _, conclusion_span = _capture_and_span(
        operation_store,
        tmp_path,
        text="The conclusion.",
        filename="conclusion.txt",
    )
    conclusion = truth_ops.truth_claim_propose(
        str(operation_store["store_id"]),
        "The conclusion.",
        "fact",
        MODEL,
        support_span_ids=[conclusion_span],
        derivation={
            "method": "deduction",
            "premises": [premise["claim"]["id"]],
            "confidence": 0.9,
        },
        agent_session_id=SESSION_ID,
    )
    assert len(conclusion["support_links"]) == 1
    assert conclusion["derivation"]["premises"][0]["ref"] == premise["claim"]["id"]


def test_deduplicated_proposal_does_not_emit_a_second_lifecycle_event(
    operation_store: dict[str, object],
    tmp_path: Path,
) -> None:
    _, span_id = _capture_and_span(
        operation_store,
        tmp_path,
        text="A canonical proposal.",
        filename="canonical.txt",
    )
    first = truth_ops.truth_claim_propose(
        str(operation_store["store_id"]),
        "A canonical proposal.",
        "fact",
        MODEL,
        support_span_ids=[span_id],
        agent_session_id=SESSION_ID,
    )
    count_after_first = len(operation_store["emitted"])
    repeated = truth_ops.truth_claim_propose(
        str(operation_store["store_id"]),
        "A canonical proposal.",
        "fact",
        MODEL,
        support_span_ids=[span_id],
        agent_session_id=SESSION_ID,
    )
    assert first["created"] is True
    assert repeated["created"] is False
    assert repeated["event"] is None
    assert len(operation_store["emitted"]) == count_after_first


def test_confirm_is_server_composed_and_per_invocation_only(
    operation_store: dict[str, object],
    tmp_path: Path,
) -> None:
    claim_id, _ = _propose_supported(
        operation_store,
        tmp_path,
        "The reviewed claim.",
        filename="review.txt",
    )
    required, result = _authorize(
        truth_ops.truth_claim_confirm,
        "truth.claim_confirm",
        str(operation_store["store_id"]),
        claim_id,
    )
    assert "The reviewed claim." in required.body
    assert required.context["claim_payload"]["proposition"] == "The reviewed claim."
    assert required.context["agent_authored_only"] is False
    assert required.grant_policy == "per_invocation"
    assert result["result"]["event"]["status"] == "confirmed"
    assert result["authorization"] == {
        "request_id": "request-1",
        "response_surface": "dashboard",
    }

    with pytest.raises(ConsentRequired) as second:
        truth_ops.truth_claim_confirm(str(operation_store["store_id"]), claim_id)
    assert second.value.grant_policy == "per_invocation"


def test_confirm_does_not_emit_confirmed_event_for_needs_review_result(
    operation_store: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_id, _ = _propose_supported(
        operation_store,
        tmp_path,
        "A successor race needs review.",
        filename="race.txt",
    )

    def conflict_result(self, **kwargs):
        conn = kwargs["conn"]
        gesture = self.store._get_gesture_locked(conn, kwargs["gesture_id"])
        status = self.store._latest_status_locked(
            conn,
            kwargs["claim_id"],
            include_overlay=False,
        )
        assert gesture is not None and status is not None
        return ConfirmationResult(
            event=None,
            created=False,
            gesture=gesture,
            superseded_events=(),
            needs_review_event=status,
        )

    monkeypatch.setattr(TruthLifecycle, "confirm_claim", conflict_result)
    event_count = len(operation_store["emitted"])
    _, result = _authorize(
        truth_ops.truth_claim_confirm,
        "truth.claim_confirm",
        str(operation_store["store_id"]),
        claim_id,
    )
    assert result["event"] is None
    assert len(operation_store["emitted"]) == event_count


def test_reasoned_rejection_creates_result_only_after_exact_approval(
    operation_store: dict[str, object],
    tmp_path: Path,
) -> None:
    source_id, _ = _propose_supported(
        operation_store,
        tmp_path,
        "The deployment passed.",
        filename="source-claim.txt",
    )
    _, negative_span = _capture_and_span(
        operation_store,
        tmp_path,
        text="It is not the case that: The deployment passed.",
        filename="negative.txt",
    )
    with pytest.raises(ConsentRequired) as blocked:
        truth_ops.truth_claim_reject(
            str(operation_store["store_id"]),
            source_id,
            "reject_as_false",
            support_span_ids=[negative_span],
        )
    registry = operation_store["registry"]
    store = registry.open_store(str(operation_store["store_id"]))
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1

    with per_invocation_authorization(
        "truth.claim_reject",
        blocked.value.fingerprint,
        request_id="request-reject",
        response_surface="obsidian",
        context=blocked.value.context,
    ):
        rejected = truth_ops.truth_claim_reject(
            str(operation_store["store_id"]),
            source_id,
            "reject_as_false",
            support_span_ids=[negative_span],
        )
    assert rejected["result"]["source_event"]["status"] == "rejected"
    assert rejected["result"]["result_event"]["status"] == "confirmed"
    assert rejected["result"]["result_claim"]["proposition"].startswith(
        "It is not the case that "
    )


def test_reasoned_rejection_prompt_warns_for_agent_only_result_support(
    operation_store: dict[str, object],
    tmp_path: Path,
) -> None:
    source_id, _ = _propose_supported(
        operation_store,
        tmp_path,
        "The deployment passed.",
        filename="agent-warning-source.txt",
    )
    result_text = "It is not the case that: The deployment passed."
    captured = truth_ops.truth_evidence_capture(
        str(operation_store["store_id"]),
        "document",
        (tmp_path / "agent-warning-result.txt").resolve().as_uri(),
        "paste",
        MODEL,
        content=result_text,
        origin="agent_generated",
        agent_session_id=SESSION_ID,
    )
    marked = truth_ops.truth_span_mark(
        str(operation_store["store_id"]),
        captured["evidence"]["id"],
        {"exact": result_text},
        MODEL,
        agent_session_id=SESSION_ID,
    )

    with pytest.raises(ConsentRequired) as blocked:
        truth_ops.truth_claim_reject(
            str(operation_store["store_id"]),
            source_id,
            "reject_as_false",
            support_span_ids=[marked["span"]["id"]],
        )

    result_spec = blocked.value.context["decision"]["result"]
    receipt = result_spec["support_receipts"][0]
    assert receipt["author_kind"] == "agent_run"
    assert receipt["author_ref"] == SESSION_ID
    assert receipt["derived_from_store"] is None
    assert result_spec["agent_authored_only"] is True
    assert "supported only by agent-authored evidence" in result_spec["support_warning"]
    assert result_spec["support_warning"] in blocked.value.body


def test_redaction_is_exact_claim_only_and_per_invocation(
    operation_store: dict[str, object],
    tmp_path: Path,
) -> None:
    claim_id, _ = _propose_supported(
        operation_store,
        tmp_path,
        "This content is private.",
        filename="private.txt",
    )
    required, redacted = _authorize(
        truth_ops.truth_claim_redact,
        "truth.claim_redact",
        str(operation_store["store_id"]),
        claim_id,
    )
    assert "This content is private." in required.body
    assert redacted["result"]["event"]["subject_kind"] == "claim"
    store = operation_store["registry"].open_store(str(operation_store["store_id"]))
    assert store.get_claim(claim_id).proposition == "[redacted]"
    with pytest.raises(InvariantViolation, match="already redacted"):
        truth_ops.truth_claim_redact(str(operation_store["store_id"]), claim_id)


def test_false_rejection_preserves_required_structured_fields(
    operation_store: dict[str, object],
    tmp_path: Path,
) -> None:
    required_profile = _profile()
    required_profile["required_fields"] = {"fact": ["subject"]}
    (tmp_path / "structured-scope").mkdir()
    created = truth_ops.truth_store_create.__wrapped__(
        str(tmp_path / "structured-scope"),
        required_profile,
    )
    scoped = {**operation_store, "store_id": created["store"]["store_id"]}
    _, source_span = _capture_and_span(
        scoped,
        tmp_path,
        text="The structured deployment passed.",
        filename="structured-source.txt",
    )
    proposed = truth_ops.truth_claim_propose(
        str(scoped["store_id"]),
        "The structured deployment passed.",
        "fact",
        MODEL,
        structured={"subject": "deployment"},
        support_span_ids=[source_span],
        agent_session_id=SESSION_ID,
    )
    _, negative_span = _capture_and_span(
        scoped,
        tmp_path,
        text="It is not the case that The structured deployment passed.",
        filename="structured-negative.txt",
    )
    _, rejected = _authorize(
        truth_ops.truth_claim_reject,
        "truth.claim_reject",
        str(scoped["store_id"]),
        proposed["claim"]["id"],
        "reject_as_false",
        support_span_ids=[negative_span],
    )
    structured = json.loads(rejected["result"]["result_claim"]["structured_json"])
    assert structured == {"subject": "deployment"}


def test_query_supersede_and_integrity_sweep_use_registered_store(
    operation_store: dict[str, object],
    tmp_path: Path,
) -> None:
    first_id, _ = _propose_supported(
        operation_store,
        tmp_path,
        "The value is one.",
        filename="one.txt",
    )
    _, confirmed = _authorize(
        truth_ops.truth_claim_confirm,
        "truth.claim_confirm",
        str(operation_store["store_id"]),
        first_id,
    )
    assert confirmed["ok"] is True

    _, second_span = _capture_and_span(
        operation_store,
        tmp_path,
        text="The value is two.",
        filename="two.txt",
    )
    superseded = truth_ops.truth_claim_supersede(
        str(operation_store["store_id"]),
        first_id,
        "updated",
        MODEL,
        proposition="The value is two.",
        claim_kind="fact",
        valid_from="2026-07-16T20:00:00+00:00",
        support_span_ids=[second_span],
        agent_session_id=SESSION_ID,
    )
    assert superseded["supersedes_link"]["to_ref"] == first_id
    event_count = len(operation_store["emitted"])
    repeated = truth_ops.truth_claim_supersede(
        str(operation_store["store_id"]),
        first_id,
        "updated",
        MODEL,
        successor_claim_id=superseded["successor"]["id"],
        agent_session_id=SESSION_ID,
    )
    assert repeated["link_created"] is False
    assert repeated["event"] is None
    assert len(operation_store["emitted"]) == event_count

    current = truth_ops.truth_query(str(operation_store["store_id"]))
    assert current["count"] == 1
    assert current["items"][0]["claim"]["id"] == first_id
    swept = truth_ops.truth_sweep(str(operation_store["store_id"]), "integrity")
    assert swept["sweep"]["kind"] == "integrity"
    assert operation_store["emitted"][-1][0] == "truth.sweep_completed"


def test_existing_successor_rejects_every_new_successor_parameter_without_writes(
    operation_store: dict[str, object],
    tmp_path: Path,
) -> None:
    predecessor_id, _ = _propose_supported(
        operation_store,
        tmp_path,
        "The predecessor assertion.",
        filename="supersede-predecessor.txt",
    )
    successor_id, _ = _propose_supported(
        operation_store,
        tmp_path,
        "The existing successor assertion.",
        filename="supersede-successor.txt",
    )
    registry = operation_store["registry"]
    assert isinstance(registry, TruthStoreRegistry)
    store = registry.open_store(str(operation_store["store_id"]))

    def counts() -> tuple[int, ...]:
        with store.connect() as conn:
            return tuple(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "claims",
                    "claim_links",
                    "derivations",
                    "derivation_premises",
                )
            )

    before_counts = counts()
    event_count = len(operation_store["emitted"])
    invalid_parameters = [
        ("proposition", {"proposition": "A replacement payload."}),
        ("claim_kind", {"claim_kind": "fact"}),
        ("structured", {"structured": {"subject": "replacement"}}),
        ("scope", {"scope": "store"}),
        ("valid_from", {"valid_from": "2026-07-16T20:00:00+00:00"}),
        ("valid_to", {"valid_to": "2026-07-17T20:00:00+00:00"}),
        ("confidence_extraction", {"confidence_extraction": 0.8}),
        ("meta", {"meta": {"topic": "replacement"}}),
    ]
    for parameter, kwargs in invalid_parameters:
        with pytest.raises(InvariantViolation, match=parameter):
            truth_ops.truth_claim_supersede(
                str(operation_store["store_id"]),
                predecessor_id,
                "updated",
                MODEL,
                successor_claim_id=successor_id,
                agent_session_id=SESSION_ID,
                **kwargs,
            )
        assert counts() == before_counts
        assert len(operation_store["emitted"]) == event_count


def test_challenge_emits_only_for_the_created_transition(
    operation_store: dict[str, object],
    tmp_path: Path,
) -> None:
    target_id, _ = _propose_supported(
        operation_store,
        tmp_path,
        "The target assertion.",
        filename="target.txt",
    )
    _authorize(
        truth_ops.truth_claim_confirm,
        "truth.claim_confirm",
        str(operation_store["store_id"]),
        target_id,
    )
    challenger_id, _ = _propose_supported(
        operation_store,
        tmp_path,
        "The conflicting assertion.",
        filename="challenger.txt",
    )
    first = truth_ops.truth_claim_challenge(
        str(operation_store["store_id"]),
        target_id,
        challenger_id,
        MODEL,
        note="The evidence conflicts.",
        agent_session_id=SESSION_ID,
    )
    assert first["result"]["created"] is True
    event_count = len(operation_store["emitted"])
    repeated = truth_ops.truth_claim_challenge(
        str(operation_store["store_id"]),
        target_id,
        challenger_id,
        MODEL,
        note="The evidence conflicts.",
        agent_session_id=SESSION_ID,
    )
    assert repeated["result"]["created"] is False
    assert repeated["event"] is None
    assert len(operation_store["emitted"]) == event_count


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"view": "as-of"}, "requires belief_at"),
        (
            {"view": "current", "belief_at": "2026-07-16T20:00:00+00:00"},
            "only valid for historical views",
        ),
        ({"view": "current", "claim_id": "a" * 32}, "only valid"),
        ({"view": "conflicts", "scope": "store"}, "not valid"),
        ({"view": "needs-review", "valid_at": "2026-07-16"}, "not valid"),
    ],
)
def test_query_rejects_incompatible_view_arguments(
    operation_store: dict[str, object],
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(InvariantViolation, match=message):
        truth_ops.truth_query(str(operation_store["store_id"]), **kwargs)


@pytest.mark.parametrize(
    ("kind", "kwargs", "message"),
    [
        ("bogus", {}, "sweep kind must be"),
        ("integrity", {"claim_id": "a" * 32}, "does not accept claim_id"),
        ("integrity", {"evidence_id": "b" * 32}, "does not accept claim_id"),
        ("integrity", {"span_id": "c" * 32}, "does not accept claim_id"),
        ("supersession", {}, "requires claim_id"),
        (
            "supersession",
            {"claim_id": "a" * 32, "evidence_id": "b" * 32},
            "does not accept evidence_id",
        ),
        (
            "supersession",
            {"claim_id": "a" * 32, "span_id": "c" * 32},
            "does not accept evidence_id",
        ),
        ("source", {}, "requires exactly one"),
        (
            "source",
            {"evidence_id": "b" * 32, "span_id": "c" * 32},
            "requires exactly one",
        ),
        (
            "source",
            {"claim_id": "a" * 32, "evidence_id": "b" * 32},
            "does not accept claim_id",
        ),
    ],
)
def test_sweep_rejects_invalid_parameter_matrix_before_recording(
    operation_store: dict[str, object],
    kind: str,
    kwargs: dict[str, object],
    message: str,
) -> None:
    registry = operation_store["registry"]
    assert isinstance(registry, TruthStoreRegistry)
    store = registry.open_store(str(operation_store["store_id"]))
    with store.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM sweeps").fetchone()[0]
    event_count = len(operation_store["emitted"])

    with pytest.raises(InvariantViolation, match=message):
        truth_ops.truth_sweep(
            str(operation_store["store_id"]),
            kind,
            **kwargs,
        )

    with store.connect() as conn:
        after = conn.execute("SELECT COUNT(*) FROM sweeps").fetchone()[0]
    assert after == before
    assert len(operation_store["emitted"]) == event_count


def test_event_publication_failure_never_changes_committed_success(
    operation_store: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, span_id = _capture_and_span(
        operation_store,
        tmp_path,
        text="The event spine may be unavailable.",
        filename="event-failure.txt",
    )
    monkeypatch.setattr(
        truth_ops,
        "emit_truth_event",
        lambda *args, **kwargs: TruthEventEmission(None, False, "spine unavailable"),
    )
    proposed = truth_ops.truth_claim_propose(
        str(operation_store["store_id"]),
        "The event spine may be unavailable.",
        "fact",
        MODEL,
        support_span_ids=[span_id],
        agent_session_id=SESSION_ID,
    )
    assert proposed["ok"] is True
    assert proposed["created"] is True
    assert proposed["event"] == {
        "event_id": None,
        "published": False,
        "error": "spine unavailable",
    }
    queried = truth_ops.truth_query(
        str(operation_store["store_id"]),
        view="needs-review",
    )
    assert queried["ok"] is True

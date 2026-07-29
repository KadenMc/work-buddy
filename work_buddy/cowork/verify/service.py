"""Domain services for the first Co-work Verify/Co-think vertical foundation."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from work_buddy.cowork.readiness import classify_document
from work_buddy.truth import documents, proposals, ydoc_store
from work_buddy.truth.anchors import CompositeSelector, reanchor
from work_buddy.truth.contracts import (
    Actor,
    InvariantViolation,
    validate_agent_producer_meta,
)
from work_buddy.truth.identity import (
    canonical_json,
    new_id,
    sha256_bytes,
    sha256_text,
    utc_now,
)
from work_buddy.truth.store import TruthStore

from . import store as verify_store
from .contracts import (
    ActionSnapshot,
    ActionTarget,
    CheckDefinitionVersion,
    CheckExecution,
    CothinkItem,
    CothinkItemStatusEvent,
    CriterionActivation,
    CriterionCheckBinding,
    CriterionDefinitionVersion,
    DeterministicEvaluation,
    EvaluationPlanSnapshot,
    EvaluationResult,
    EvaluationRun,
    ModelCallAuthorizationReceipt,
    ResultRelation,
    RoutingDisposition,
    SeededTerminologyExactMatch,
    VerifyInvariantViolation,
)


TERMINOLOGY_EXACT_MATCH_KEY = "terminology_exact_match"
TERMINOLOGY_EXACT_MATCH_VERSION = 1
TERMINOLOGY_EXACT_MATCH_EXECUTOR = (
    "work_buddy.cowork.verify.service:run_terminology_exact_match"
)
SURFACING_DECISIONS = frozenset({"surface", "route_to_correction"})
ROUTING_DECISIONS = SURFACING_DECISIONS | frozenset(
    {"suppress", "defer", "supersede"}
)
RESULT_RELATION_KINDS = frozenset(
    {"addresses", "rechecks", "supersedes", "derived_from", "related"}
)
RESULT_RELATION_TARGET_KINDS = frozenset(
    {"evaluation_result", "evaluation_run", "proposal", "cothink_item", "external"}
)
COTHINK_ITEM_STATUSES = frozenset({"open", "parked", "dismissed"})
COTHINK_ITEM_TRANSITIONS = {
    "open": frozenset({"parked", "dismissed"}),
    "parked": frozenset({"dismissed"}),
    "dismissed": frozenset(),
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECORD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SYSTEM_ACTOR = Actor("system", "cowork-verify")
_SEED_DOMAIN = b"work-buddy:cowork-verify:seed:v1\0"
_COTHINK_STATUS_DOMAIN = b"work-buddy:cothink-item-status:v1\0"


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VerifyInvariantViolation(f"{label} must be a nonempty string")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise VerifyInvariantViolation(
            f"{label} must be a lowercase SHA-256 digest"
        )
    return value


def _id(value: str | None, label: str) -> str:
    candidate = new_id() if value is None else value
    if _RECORD_ID_RE.fullmatch(candidate) is None:
        raise VerifyInvariantViolation(f"{label} must be a lowercase 32-hex id")
    return candidate


def _timestamp(value: str | None, label: str) -> str:
    candidate = utc_now() if value is None else _text(value, label)
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerifyInvariantViolation(
            f"{label} must be an ISO 8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VerifyInvariantViolation(f"{label} must carry a UTC offset")
    return candidate


def _mapping(value: Mapping[str, Any] | None, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise VerifyInvariantViolation(f"{label} must be an object")
    return dict(value)


def _sequence(value: Sequence[Mapping[str, Any]] | None, label: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise VerifyInvariantViolation(f"{label} must be a list")
    items: list[Any] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise VerifyInvariantViolation(f"{label} entries must be objects")
        items.append(dict(item))
    return items


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json(value))


def _actor_fields(actor: Actor) -> tuple[str, str | None, str | None]:
    if not isinstance(actor, Actor):
        raise TypeError("actor must be an Actor")
    if actor.kind == "agent_run":
        validate_agent_producer_meta(actor.meta)
    meta = canonical_json(dict(actor.meta)) if actor.meta else None
    return actor.kind, actor.ref, meta


def _seed_id(label: str) -> str:
    return sha256_bytes(_SEED_DOMAIN + label.encode("utf-8"))[:32]


def _require_record(
    store: TruthStore,
    record_type: type[Any],
    record_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> Any:
    record = verify_store.get_record(store, record_type, record_id, conn=conn)
    if record is None:
        raise VerifyInvariantViolation(
            f"{record_type.__name__} does not exist: {record_id}"
        )
    return record


def _insert_seed(
    store: TruthStore,
    record: Any,
    *,
    conn: sqlite3.Connection,
) -> Any:
    existing = verify_store.get_record(store, type(record), record.id, conn=conn)
    if existing is not None:
        if existing.canonical_sha256 != record.canonical_sha256:
            raise VerifyInvariantViolation(
                f"seeded {type(record).__name__} identity conflicts with store state"
            )
        return existing
    return verify_store.insert_record(store, record, conn=conn)


def terminology_exact_match_defaults(
    *,
    actor: Actor = _SYSTEM_ACTOR,
    at: str | None = None,
) -> SeededTerminologyExactMatch:
    """Build the immutable built-in preferred-term records without writing."""

    created_at = _timestamp(at, "seed timestamp")
    actor_kind, actor_ref, actor_meta = _actor_fields(actor)

    criterion_payload = {
        "stable_key": TERMINOLOGY_EXACT_MATCH_KEY,
        "version": TERMINOLOGY_EXACT_MATCH_VERSION,
        "title": "Preferred terminology",
        "description": (
            "Find an exact non-preferred label and identify its configured "
            "preferred established term."
        ),
        "criterion_kind": "terminology",
        "origin": "system",
        "configuration_schema": {
            "type": "object",
            "required": ["terms"],
            "properties": {
                "terms": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["non_preferred", "preferred"],
                    },
                }
            },
        },
    }
    criterion = CriterionDefinitionVersion(
        id=_seed_id("criterion:terminology_exact_match:v1"),
        stable_key=TERMINOLOGY_EXACT_MATCH_KEY,
        version=TERMINOLOGY_EXACT_MATCH_VERSION,
        title=criterion_payload["title"],
        description=criterion_payload["description"],
        criterion_kind=criterion_payload["criterion_kind"],
        origin=criterion_payload["origin"],
        configuration_schema_json=canonical_json(
            criterion_payload["configuration_schema"]
        ),
        canonical_sha256=_canonical_hash(criterion_payload),
        created_at=created_at,
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )

    check_payload = {
        "stable_key": TERMINOLOGY_EXACT_MATCH_KEY,
        "version": TERMINOLOGY_EXACT_MATCH_VERSION,
        "title": "Terminology exact-match check",
        "mechanism": "deterministic",
        "executor_ref": TERMINOLOGY_EXACT_MATCH_EXECUTOR,
        "supported_criterion_kinds": ["terminology"],
        "input_schema": {
            "type": "object",
            "required": ["target_text_sha256", "configuration"],
        },
        "output_schema": {
            "type": "object",
            "required": ["matches"],
            "properties": {"matches": {"type": "array"}},
        },
        "limitations": [
            "Exact, case-sensitive matching only.",
            "A match reports terminology use; it does not judge contextual intent.",
        ],
        "origin": "system",
    }
    check = CheckDefinitionVersion(
        id=_seed_id("check:terminology_exact_match:v1"),
        stable_key=TERMINOLOGY_EXACT_MATCH_KEY,
        version=TERMINOLOGY_EXACT_MATCH_VERSION,
        title=check_payload["title"],
        mechanism=check_payload["mechanism"],
        executor_ref=check_payload["executor_ref"],
        supported_criterion_kinds_json=canonical_json(
            check_payload["supported_criterion_kinds"]
        ),
        input_schema_json=canonical_json(check_payload["input_schema"]),
        output_schema_json=canonical_json(check_payload["output_schema"]),
        limitations_json=canonical_json(check_payload["limitations"]),
        origin=check_payload["origin"],
        canonical_sha256=_canonical_hash(check_payload),
        created_at=created_at,
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )

    binding_configuration = {
        "terms": [
            {
                "non_preferred": "Co-work scope",
                "preferred": "document target",
            }
        ]
    }
    binding_payload = {
        "criterion_definition_version_id": criterion.id,
        "check_definition_version_id": check.id,
        "configuration": binding_configuration,
    }
    binding = CriterionCheckBinding(
        id=_seed_id("binding:terminology_exact_match:v1"),
        criterion_definition_version_id=criterion.id,
        check_definition_version_id=check.id,
        configuration_json=canonical_json(binding_configuration),
        canonical_sha256=_canonical_hash(binding_payload),
        created_at=created_at,
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )

    activation_payload = {
        "criterion_definition_version_id": criterion.id,
        "criterion_check_binding_id": binding.id,
        "scope": {"kind": "document"},
        "is_enabled": True,
        "is_required": False,
        "origin": "system",
    }
    activation = CriterionActivation(
        id=_seed_id("activation:terminology_exact_match:v1"),
        criterion_definition_version_id=criterion.id,
        criterion_check_binding_id=binding.id,
        scope_json=canonical_json(activation_payload["scope"]),
        is_enabled=1,
        is_required=0,
        origin="system",
        canonical_sha256=_canonical_hash(activation_payload),
        created_at=created_at,
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )

    return SeededTerminologyExactMatch(
        criterion=criterion,
        check=check,
        binding=binding,
        activation=activation,
    )


def seed_terminology_exact_match(
    store: TruthStore,
    *,
    actor: Actor = _SYSTEM_ACTOR,
    at: str | None = None,
) -> SeededTerminologyExactMatch:
    """Idempotently persist the optional built-in preferred-term check."""

    defaults = terminology_exact_match_defaults(actor=actor, at=at)
    with store.write_transaction() as conn:
        criterion = _insert_seed(store, defaults.criterion, conn=conn)
        check = _insert_seed(store, defaults.check, conn=conn)
        binding = _insert_seed(store, defaults.binding, conn=conn)
        activation = _insert_seed(store, defaults.activation, conn=conn)
    return SeededTerminologyExactMatch(
        criterion=criterion,
        check=check,
        binding=binding,
        activation=activation,
    )


def _normalize_target(
    target: ActionTarget | Mapping[str, Any] | None,
) -> ActionTarget:
    if target is None:
        return ActionTarget.document()
    if isinstance(target, ActionTarget):
        return target
    if not isinstance(target, Mapping):
        raise VerifyInvariantViolation("target must be an ActionTarget or object")
    kind = target.get("kind")
    if kind == "document":
        return ActionTarget.document()
    if kind != "text_quote":
        raise VerifyInvariantViolation(
            "target.kind must be 'document' or 'text_quote'"
        )
    if "selector" in target:
        selector = CompositeSelector.from_web_annotation(target["selector"])
        return ActionTarget.text_quote(
            selector.exact,
            prefix=selector.prefix,
            suffix=selector.suffix,
            start=selector.start,
            end=selector.end,
        )
    return ActionTarget.text_quote(
        target.get("exact"),
        prefix=target.get("prefix", ""),
        suffix=target.get("suffix", ""),
        start=target.get("start"),
        end=target.get("end"),
    )


def _resolve_target(
    projection: str,
    target: ActionTarget,
    projection_sha256: str,
) -> tuple[str, dict[str, Any], int, int]:
    if target.kind == "document":
        return (
            projection,
            {"kind": "document", "start": 0, "end": len(projection)},
            0,
            len(projection),
        )
    if target.kind != "text_quote" or target.exact is None:
        raise VerifyInvariantViolation("unsupported action target")
    selector = CompositeSelector(
        exact=target.exact,
        prefix=target.prefix,
        suffix=target.suffix,
        start=target.start,
        end=target.end,
    )
    try:
        resolved = reanchor(
            projection,
            selector,
            expected_snapshot_sha256=projection_sha256,
        )
    except Exception as exc:
        raise VerifyInvariantViolation(
            f"action target does not locate exactly in the frozen projection: {exc}"
        ) from exc
    payload = {
        "kind": "text_quote",
        "selector": selector.to_web_annotation(),
        "resolved": {
            "start": resolved.start,
            "end": resolved.end,
            "exact": resolved.exact,
        },
    }
    return resolved.exact, payload, resolved.start, resolved.end


def create_action_snapshot(
    store: TruthStore,
    *,
    document_id: str,
    projection: str | bytes | bytearray | memoryview,
    expected_snapshot_sha256: str,
    expected_structured_head_sha256: str,
    expected_ydoc_generation_sha256: str,
    expected_projection_sha256: str,
    actor: Actor,
    target: ActionTarget | Mapping[str, Any] | None = None,
    context_boundary: Mapping[str, Any] | None = None,
    allowed_change_ranges: Sequence[Mapping[str, Any]] | None = None,
    egress_boundary: Mapping[str, Any] | None = None,
    at: str | None = None,
    snapshot_id: str | None = None,
) -> ActionSnapshot:
    """Freeze one exact, server-validated action input without materializing."""

    document_ref = _id(document_id, "document_id")
    expected_snapshot = _digest(
        expected_snapshot_sha256, "expected_snapshot_sha256"
    )
    expected_head = _digest(
        expected_structured_head_sha256,
        "expected_structured_head_sha256",
    )
    expected_generation = _digest(
        expected_ydoc_generation_sha256,
        "expected_ydoc_generation_sha256",
    )
    expected_projection = _digest(
        expected_projection_sha256,
        "expected_projection_sha256",
    )
    if isinstance(projection, str):
        projection_text = projection
        projection_bytes = projection.encode("utf-8")
    elif isinstance(projection, (bytes, bytearray, memoryview)):
        projection_bytes = bytes(projection)
        try:
            projection_text = projection_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise VerifyInvariantViolation(
                "canonical Markdown projection must be UTF-8"
            ) from exc
    else:
        raise VerifyInvariantViolation("projection must be text or bytes")
    projection_digest = sha256_bytes(projection_bytes)
    if projection_digest != expected_projection:
        raise VerifyInvariantViolation(
            "projection bytes do not match expected_projection_sha256"
        )

    normalized_target = _normalize_target(target)
    target_text, selector_payload, target_start, target_end = _resolve_target(
        projection_text,
        normalized_target,
        projection_digest,
    )
    target_bytes = target_text.encode("utf-8")
    target_digest = sha256_bytes(target_bytes)
    context_value = _mapping(
        context_boundary
        if context_boundary is not None
        else {"kind": "action_target"},
        "context_boundary",
    )
    allowed_value = _sequence(allowed_change_ranges, "allowed_change_ranges")
    if not allowed_value:
        allowed_value = [{"start": target_start, "end": target_end}]
    for item in allowed_value:
        start = item.get("start")
        end = item.get("end")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < target_start
            or end > target_end
            or end < start
        ):
            raise VerifyInvariantViolation(
                "allowed change ranges must be contained in the action target"
            )
    egress_value = _mapping(
        egress_boundary
        if egress_boundary is not None
        else {"class": "local_only"},
        "egress_boundary",
    )
    created_at = _timestamp(at, "action snapshot timestamp")
    actor_kind, actor_ref, actor_meta = _actor_fields(actor)

    with ydoc_store.document_lock(store, document_ref):
        document = documents.get_document(store, document_ref)
        readiness = classify_document(store, document)
        if readiness.initialization_state != "ready":
            raise VerifyInvariantViolation(
                "document is not ready for an exact action snapshot"
            )
        if readiness.snapshot_sha256 != expected_snapshot:
            raise VerifyInvariantViolation("document snapshot changed before capture")
        if readiness.structured_head_sha256 != expected_head:
            raise VerifyInvariantViolation(
                "document structured head changed before capture"
            )
        if not readiness.permissions["open"]:
            raise VerifyInvariantViolation(
                f"document cannot be opened for capture: {readiness.disabled_reason}"
            )
        generation = documents.current_ydoc_generation(store, document_ref)
        if generation != expected_generation:
            raise VerifyInvariantViolation(
                "document Y.Doc generation changed before capture"
            )
        if (
            not ydoc_store.update_tail_present(store, document_id=document_ref)
            and projection_digest != document.content_sha256
        ):
            raise VerifyInvariantViolation(
                "projection differs from the durable document without a "
                "structured update tail"
            )
        current_version = documents.current_document_version(store, document_ref)
        canonical_payload = {
            "document_id": document_ref,
            "document_version_id": (
                None if current_version is None else current_version.id
            ),
            "ydoc_snapshot_sha256": expected_snapshot,
            "structured_head_sha256": expected_head,
            "ydoc_generation_sha256": expected_generation,
            "baseline_projection_sha256": document.content_sha256,
            "projection_sha256": projection_digest,
            "target_kind": normalized_target.kind,
            "target_selector": selector_payload,
            "target_text_sha256": target_digest,
            "context_boundary": context_value,
            "allowed_change_ranges": allowed_value,
            "egress_boundary": egress_value,
        }
        canonical_sha256 = _canonical_hash(canonical_payload)
        store._store_blob_bytes(projection_digest, projection_bytes)
        store._store_blob_bytes(target_digest, target_bytes)
        with store.write_transaction() as conn:
            existing = verify_store.get_by_canonical_sha256(
                store,
                ActionSnapshot,
                canonical_sha256,
                conn=conn,
            )
            if existing is not None:
                return existing
            record = ActionSnapshot(
                id=_id(snapshot_id, "action snapshot id"),
                document_id=document_ref,
                document_version_id=(
                    None if current_version is None else current_version.id
                ),
                ydoc_snapshot_sha256=expected_snapshot,
                structured_head_sha256=expected_head,
                ydoc_generation_sha256=expected_generation,
                baseline_projection_sha256=document.content_sha256,
                projection_sha256=projection_digest,
                projection_blob_sha256=projection_digest,
                target_kind=normalized_target.kind,
                target_selector_json=canonical_json(selector_payload),
                target_text_sha256=target_digest,
                target_blob_sha256=target_digest,
                context_boundary_json=canonical_json(context_value),
                allowed_change_ranges_json=canonical_json(allowed_value),
                egress_boundary_json=canonical_json(egress_value),
                canonical_sha256=canonical_sha256,
                created_at=created_at,
                created_by_kind=actor_kind,
                created_by_ref=actor_ref,
                created_by_meta_json=actor_meta,
            )
            return verify_store.insert_record(store, record, conn=conn)


def create_terminology_plan(
    store: TruthStore,
    *,
    action_snapshot_id: str,
    criterion_activation_id: str | None = None,
    actor: Actor = _SYSTEM_ACTOR,
    at: str | None = None,
    plan_id: str | None = None,
) -> EvaluationPlanSnapshot:
    seeded = seed_terminology_exact_match(store, actor=_SYSTEM_ACTOR, at=at)
    action = _require_record(store, ActionSnapshot, action_snapshot_id)
    activation = (
        seeded.activation
        if criterion_activation_id is None
        else _require_record(
            store,
            CriterionActivation,
            criterion_activation_id,
        )
    )
    if (
        not activation.is_enabled
        or activation.criterion_definition_version_id != seeded.criterion.id
        or activation.criterion_check_binding_id != seeded.binding.id
    ):
        raise VerifyInvariantViolation(
            "terminology plan activation does not select the admitted check"
        )
    plan_payload = {
        "schema": "work-buddy.cowork-evaluation-plan/v1",
        "action_snapshot_id": action.id,
        "checks": [
            {
                "criterion_definition_version_id": seeded.criterion.id,
                "check_definition_version_id": seeded.check.id,
                "criterion_check_binding_id": seeded.binding.id,
                "criterion_activation_id": activation.id,
                "configuration_sha256": sha256_text(
                    seeded.binding.configuration_json
                ),
            }
        ],
    }
    canonical_sha256 = _canonical_hash(plan_payload)
    existing = verify_store.get_by_canonical_sha256(
        store,
        EvaluationPlanSnapshot,
        canonical_sha256,
    )
    if existing is not None:
        return existing
    actor_kind, actor_ref, actor_meta = _actor_fields(actor)
    record = EvaluationPlanSnapshot(
        id=_id(plan_id, "evaluation plan id"),
        action_snapshot_id=action.id,
        plan_json=canonical_json(plan_payload),
        canonical_sha256=canonical_sha256,
        created_at=_timestamp(at, "evaluation plan timestamp"),
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    try:
        return verify_store.insert_record(store, record)
    except sqlite3.IntegrityError:
        concurrent = verify_store.get_by_canonical_sha256(
            store,
            EvaluationPlanSnapshot,
            canonical_sha256,
        )
        if concurrent is None:
            raise
        return concurrent


def _read_blob(store: TruthStore, digest: str, label: str) -> bytes:
    path = store.resolve_blob_path(f"blobs/{digest}")
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise VerifyInvariantViolation(f"{label} blob is unavailable") from exc
    if sha256_bytes(value) != digest:
        raise VerifyInvariantViolation(f"{label} blob failed integrity validation")
    return value


def _term_matches(
    text: str,
    configuration: Mapping[str, Any],
) -> list[dict[str, Any]]:
    terms = configuration.get("terms")
    if not isinstance(terms, list):
        raise VerifyInvariantViolation(
            "terminology exact-match configuration requires a terms list"
        )
    matches: list[dict[str, Any]] = []
    for term in terms:
        if not isinstance(term, Mapping):
            raise VerifyInvariantViolation("terminology term must be an object")
        non_preferred = _text(term.get("non_preferred"), "non_preferred term")
        preferred = _text(term.get("preferred"), "preferred term")
        offset = text.find(non_preferred)
        while offset >= 0:
            matches.append(
                {
                    "non_preferred": non_preferred,
                    "preferred": preferred,
                    "start": offset,
                    "end": offset + len(non_preferred),
                }
            )
            offset = text.find(non_preferred, offset + len(non_preferred))
    return sorted(
        matches,
        key=lambda item: (
            item["start"],
            item["end"],
            item["non_preferred"],
            item["preferred"],
        ),
    )


def terminology_exact_matches(
    text: str,
    configuration: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Evaluate the admitted exact-match check without writing ledger state."""

    return tuple(_term_matches(text, configuration))


def run_terminology_exact_match(
    store: TruthStore,
    *,
    action_snapshot_id: str,
    criterion_activation_id: str | None = None,
    actor: Actor = _SYSTEM_ACTOR,
    at: str | None = None,
) -> DeterministicEvaluation:
    """Run and persist the seeded deterministic terminology check."""

    seeded = seed_terminology_exact_match(store, actor=_SYSTEM_ACTOR, at=at)
    action = _require_record(store, ActionSnapshot, action_snapshot_id)
    plan = create_terminology_plan(
        store,
        action_snapshot_id=action.id,
        criterion_activation_id=criterion_activation_id,
        actor=actor,
        at=at,
    )
    target_bytes = _read_blob(
        store,
        action.target_blob_sha256,
        "action target",
    )
    try:
        target_text = target_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerifyInvariantViolation("action target is not UTF-8 text") from exc
    configuration = json.loads(seeded.binding.configuration_json)
    matches = _term_matches(target_text, configuration)
    selector_payload = json.loads(action.target_selector_json)
    target_start = int(selector_payload.get("start", 0))
    if action.target_kind == "text_quote":
        target_start = int(selector_payload["resolved"]["start"])
    completed_at = _timestamp(at, "evaluation timestamp")
    actor_kind, actor_ref, actor_meta = _actor_fields(actor)
    run_payload = {
        "action_snapshot_id": action.id,
        "plan_snapshot_id": plan.id,
        "run_kind": "verify",
        "executor": TERMINOLOGY_EXACT_MATCH_EXECUTOR,
        "input_sha256": action.target_text_sha256,
    }
    run_sha256 = _canonical_hash(run_payload)
    existing_run = verify_store.get_by_canonical_sha256(
        store,
        EvaluationRun,
        run_sha256,
    )
    if existing_run is not None:
        executions = verify_store.list_records(
            store,
            CheckExecution,
            where="source.evaluation_run_id = ?",
            params=(existing_run.id,),
        )
        results = verify_store.list_records(
            store,
            EvaluationResult,
            where="source.evaluation_run_id = ?",
            params=(existing_run.id,),
        )
        if len(executions) != 1:
            raise VerifyInvariantViolation(
                "deterministic evaluation has an incomplete execution ledger"
            )
        return DeterministicEvaluation(
            plan=plan,
            run=existing_run,
            execution=executions[0],
            results=results,
        )

    output_payload = {"matches": matches}
    output_sha256 = _canonical_hash(output_payload)
    run = EvaluationRun(
        id=new_id(),
        action_snapshot_id=action.id,
        plan_snapshot_id=plan.id,
        run_kind="verify",
        status="completed",
        canonical_sha256=run_sha256,
        started_at=completed_at,
        completed_at=completed_at,
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    execution_payload = {
        "evaluation_run_id": run.id,
        "check_definition_version_id": seeded.check.id,
        "criterion_check_binding_id": seeded.binding.id,
        "mechanism": "deterministic",
        "status": "succeeded",
        "input_sha256": action.target_text_sha256,
        "output_sha256": output_sha256,
    }
    execution = CheckExecution(
        id=new_id(),
        evaluation_run_id=run.id,
        check_definition_version_id=seeded.check.id,
        criterion_check_binding_id=seeded.binding.id,
        mechanism="deterministic",
        status="succeeded",
        input_sha256=action.target_text_sha256,
        output_sha256=output_sha256,
        diagnostics_json=canonical_json(
            {"match_count": len(matches), "output_sha256": output_sha256}
        ),
        producer_json=canonical_json(
            {
                "kind": "deterministic",
                "executor_ref": TERMINOLOGY_EXACT_MATCH_EXECUTOR,
                "version": TERMINOLOGY_EXACT_MATCH_VERSION,
            }
        ),
        canonical_sha256=_canonical_hash(execution_payload),
        started_at=completed_at,
        completed_at=completed_at,
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    results: list[EvaluationResult] = []
    if not matches:
        message = (
            "No configured non-preferred term was found in the frozen "
            "evaluation target."
        )
        payload = {
            "match_count": 0,
            "coverage": "complete_exact_string",
        }
        result_payload = {
            "evaluation_run_id": run.id,
            "check_execution_id": execution.id,
            "criterion_definition_version_id": seeded.criterion.id,
            "result_kind": "conforming",
            "severity": "info",
            "message": message,
            "evidence_selector": None,
            "payload": payload,
        }
        results.append(
            EvaluationResult(
                id=new_id(),
                evaluation_run_id=run.id,
                check_execution_id=execution.id,
                criterion_definition_version_id=seeded.criterion.id,
                result_kind="conforming",
                severity="info",
                message=message,
                evidence_selector_json=None,
                payload_json=canonical_json(payload),
                canonical_sha256=_canonical_hash(result_payload),
                created_at=completed_at,
                created_by_kind=actor_kind,
                created_by_ref=actor_ref,
                created_by_meta_json=actor_meta,
            )
        )
    for match in matches:
        projection_start = target_start + int(match["start"])
        projection_end = target_start + int(match["end"])
        evidence_selector = {
            "kind": "text_quote",
            "selector": CompositeSelector(
                exact=str(match["non_preferred"]),
                start=projection_start,
                end=projection_end,
            ).to_web_annotation(),
        }
        message = (
            f"Use the preferred term “{match['preferred']}” instead of "
            f"“{match['non_preferred']}”."
        )
        payload = {
            "non_preferred": match["non_preferred"],
            "preferred": match["preferred"],
            "target_relative_start": match["start"],
            "target_relative_end": match["end"],
        }
        result_payload = {
            "evaluation_run_id": run.id,
            "check_execution_id": execution.id,
            "criterion_definition_version_id": seeded.criterion.id,
            "result_kind": "finding",
            "severity": "warning",
            "message": message,
            "evidence_selector": evidence_selector,
            "payload": payload,
        }
        results.append(
            EvaluationResult(
                id=new_id(),
                evaluation_run_id=run.id,
                check_execution_id=execution.id,
                criterion_definition_version_id=seeded.criterion.id,
                result_kind="finding",
                severity="warning",
                message=message,
                evidence_selector_json=canonical_json(evidence_selector),
                payload_json=canonical_json(payload),
                canonical_sha256=_canonical_hash(result_payload),
                created_at=completed_at,
                created_by_kind=actor_kind,
                created_by_ref=actor_ref,
                created_by_meta_json=actor_meta,
            )
        )

    with store.write_transaction() as conn:
        verify_store.insert_record(store, run, conn=conn)
        verify_store.insert_record(store, execution, conn=conn)
        for result in results:
            verify_store.insert_record(store, result, conn=conn)
    return DeterministicEvaluation(
        plan=plan,
        run=run,
        execution=execution,
        results=tuple(results),
    )


def record_routing_disposition(
    store: TruthStore,
    *,
    evaluation_result_id: str,
    decision: str,
    rationale: str,
    actor: Actor,
    policy_snapshot_sha256: str | None = None,
    at: str | None = None,
    disposition_id: str | None = None,
) -> RoutingDisposition:
    result = _require_record(store, EvaluationResult, evaluation_result_id)
    decision_value = _text(decision, "routing decision")
    if decision_value not in ROUTING_DECISIONS:
        raise VerifyInvariantViolation(
            f"routing decision must be one of {sorted(ROUTING_DECISIONS)}"
        )
    rationale_value = _text(rationale, "routing rationale")
    policy = (
        None
        if policy_snapshot_sha256 is None
        else _digest(policy_snapshot_sha256, "policy_snapshot_sha256")
    )
    actor_kind, actor_ref, actor_meta = _actor_fields(actor)
    payload = {
        "evaluation_result_id": result.id,
        "decision": decision_value,
        "rationale": rationale_value,
        "policy_snapshot_sha256": policy,
    }
    canonical_sha256 = _canonical_hash(payload)
    existing = verify_store.get_by_canonical_sha256(
        store,
        RoutingDisposition,
        canonical_sha256,
    )
    if existing is not None:
        return existing
    record = RoutingDisposition(
        id=_id(disposition_id, "routing disposition id"),
        evaluation_result_id=result.id,
        decision=decision_value,
        rationale=rationale_value,
        policy_snapshot_sha256=policy,
        canonical_sha256=canonical_sha256,
        created_at=_timestamp(at, "routing disposition timestamp"),
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    return verify_store.insert_record(store, record)


def surfaced_results(
    store: TruthStore,
    *,
    document_id: str | None = None,
    action_snapshot_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any], ...]:
    """Project only coordinator-routed results; raw results are excluded."""

    rows = verify_store.surfaced_result_rows(
        store,
        document_id=document_id,
        action_snapshot_id=action_snapshot_id,
        conn=conn,
    )
    return tuple(
        {
            "id": result.id,
            "evaluation_run_id": result.evaluation_run_id,
            "action_snapshot_id": snapshot.id,
            "document_id": snapshot.document_id,
            "result_kind": result.result_kind,
            "severity": result.severity,
            "message": result.message,
            "evidence_selector": (
                None
                if result.evidence_selector_json is None
                else json.loads(result.evidence_selector_json)
            ),
            "payload": json.loads(result.payload_json),
            "canonical_sha256": result.canonical_sha256,
            "disposition": {
                "id": disposition.id,
                "decision": disposition.decision,
                "rationale": disposition.rationale,
                "canonical_sha256": disposition.canonical_sha256,
                "created_at": disposition.created_at,
            },
            "created_at": result.created_at,
        }
        for result, disposition, snapshot in rows
    )


def record_result_relation(
    store: TruthStore,
    *,
    evaluation_result_id: str,
    relation_kind: str,
    target_kind: str,
    target_ref: str,
    actor: Actor,
    at: str | None = None,
    relation_id: str | None = None,
) -> ResultRelation:
    result = _require_record(store, EvaluationResult, evaluation_result_id)
    relation = _text(relation_kind, "relation_kind")
    if relation not in RESULT_RELATION_KINDS:
        raise VerifyInvariantViolation(
            f"relation_kind must be one of {sorted(RESULT_RELATION_KINDS)}"
        )
    target_type = _text(target_kind, "target_kind")
    if target_type not in RESULT_RELATION_TARGET_KINDS:
        raise VerifyInvariantViolation(
            f"target_kind must be one of {sorted(RESULT_RELATION_TARGET_KINDS)}"
        )
    target = _text(target_ref, "target_ref")
    if target_type == "evaluation_result":
        _require_record(store, EvaluationResult, target)
    elif target_type == "evaluation_run":
        _require_record(store, EvaluationRun, target)
    elif target_type == "proposal":
        try:
            proposals.get_proposal(store, target)
        except InvariantViolation as exc:
            raise VerifyInvariantViolation(
                f"ProposalRecord does not exist: {target}"
            ) from exc
    elif target_type == "cothink_item":
        _require_record(store, CothinkItem, target)
    actor_kind, actor_ref, actor_meta = _actor_fields(actor)
    payload = {
        "evaluation_result_id": result.id,
        "relation_kind": relation,
        "target_kind": target_type,
        "target_ref": target,
    }
    canonical_sha256 = _canonical_hash(payload)
    existing = verify_store.get_by_canonical_sha256(
        store,
        ResultRelation,
        canonical_sha256,
    )
    if existing is not None:
        return existing
    record = ResultRelation(
        id=_id(relation_id, "result relation id"),
        evaluation_result_id=result.id,
        relation_kind=relation,
        target_kind=target_type,
        target_ref=target,
        canonical_sha256=canonical_sha256,
        created_at=_timestamp(at, "result relation timestamp"),
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    return verify_store.insert_record(store, record)


def record_model_call_authorization(
    store: TruthStore,
    *,
    action_snapshot_id: str,
    provider: str,
    model: str,
    context_sha256: str,
    content_boundary: Mapping[str, Any],
    egress_class: str,
    cost_ceiling_usd: float,
    retry_limit: int,
    expires_at: str,
    actor: Actor,
    plan_snapshot_id: str | None = None,
    at: str | None = None,
    receipt_id: str | None = None,
) -> ModelCallAuthorizationReceipt:
    action = _require_record(store, ActionSnapshot, action_snapshot_id)
    plan: EvaluationPlanSnapshot | None = None
    if plan_snapshot_id is not None:
        plan = _require_record(
            store,
            EvaluationPlanSnapshot,
            plan_snapshot_id,
        )
        if plan.action_snapshot_id != action.id:
            raise VerifyInvariantViolation(
                "model authorization plan belongs to another action snapshot"
            )
    provider_value = _text(provider, "provider")
    model_value = _text(model, "model")
    context = _digest(context_sha256, "context_sha256")
    boundary = _mapping(content_boundary, "content_boundary")
    egress = _text(egress_class, "egress_class")
    if (
        isinstance(cost_ceiling_usd, bool)
        or not isinstance(cost_ceiling_usd, (int, float))
        or not math.isfinite(float(cost_ceiling_usd))
        or float(cost_ceiling_usd) < 0
    ):
        raise VerifyInvariantViolation(
            "cost_ceiling_usd must be a finite nonnegative number"
        )
    if (
        isinstance(retry_limit, bool)
        or not isinstance(retry_limit, int)
        or retry_limit < 0
    ):
        raise VerifyInvariantViolation("retry_limit must be a nonnegative integer")
    expiry = _timestamp(expires_at, "authorization expiry")
    created_at = _timestamp(at, "authorization timestamp")
    if datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= datetime.fromisoformat(
        created_at.replace("Z", "+00:00")
    ):
        raise VerifyInvariantViolation("authorization expiry must be after creation")
    actor_kind, actor_ref, actor_meta = _actor_fields(actor)
    payload = {
        "action_snapshot_id": action.id,
        "plan_snapshot_id": None if plan is None else plan.id,
        "provider": provider_value,
        "model": model_value,
        "context_sha256": context,
        "content_boundary": boundary,
        "egress_class": egress,
        "cost_ceiling_usd": float(cost_ceiling_usd),
        "retry_limit": retry_limit,
        "expires_at": expiry,
    }
    canonical_sha256 = _canonical_hash(payload)
    existing = verify_store.get_by_canonical_sha256(
        store,
        ModelCallAuthorizationReceipt,
        canonical_sha256,
    )
    if existing is not None:
        return existing
    record = ModelCallAuthorizationReceipt(
        id=_id(receipt_id, "model authorization receipt id"),
        action_snapshot_id=action.id,
        plan_snapshot_id=None if plan is None else plan.id,
        provider=provider_value,
        model=model_value,
        context_sha256=context,
        content_boundary_json=canonical_json(boundary),
        egress_class=egress,
        cost_ceiling_usd=float(cost_ceiling_usd),
        retry_limit=retry_limit,
        expires_at=expiry,
        canonical_sha256=canonical_sha256,
        created_at=created_at,
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    return verify_store.insert_record(store, record)


def record_cothink_item(
    store: TruthStore,
    *,
    action_snapshot_id: str,
    subtype: str,
    purpose: str,
    payload: Mapping[str, Any],
    rationale: str,
    provenance: Mapping[str, Any],
    actor: Actor,
    delivery_state: str = "delivered",
    at: str | None = None,
    item_id: str | None = None,
) -> CothinkItem:
    action = _require_record(store, ActionSnapshot, action_snapshot_id)
    subtype_value = _text(subtype, "Co-think subtype")
    purpose_value = _text(purpose, "Co-think purpose")
    payload_value = _mapping(payload, "Co-think payload")
    rationale_value = _text(rationale, "Co-think rationale")
    provenance_value = _mapping(provenance, "Co-think provenance")
    delivery = _text(delivery_state, "Co-think delivery state")
    if delivery not in {"queued", "delivered", "unavailable"}:
        raise VerifyInvariantViolation(
            "Co-think delivery state must be queued, delivered, or unavailable"
        )
    actor_kind, actor_ref, actor_meta = _actor_fields(actor)
    canonical_payload = {
        "action_snapshot_id": action.id,
        "subtype": subtype_value,
        "purpose": purpose_value,
        "payload": payload_value,
        "rationale": rationale_value,
        "delivery_state": delivery,
        "provenance": provenance_value,
    }
    canonical_sha256 = _canonical_hash(canonical_payload)
    existing = verify_store.get_by_canonical_sha256(
        store,
        CothinkItem,
        canonical_sha256,
    )
    if existing is not None:
        if verify_store.latest_cothink_status(store, existing.id) is None:
            raise VerifyInvariantViolation(
                "Co-think item has no lifecycle status event"
            )
        return existing
    record = CothinkItem(
        id=_id(item_id, "Co-think item id"),
        action_snapshot_id=action.id,
        subtype=subtype_value,
        purpose=purpose_value,
        payload_json=canonical_json(payload_value),
        rationale=rationale_value,
        delivery_state=delivery,
        provenance_json=canonical_json(provenance_value),
        canonical_sha256=canonical_sha256,
        created_at=_timestamp(at, "Co-think item timestamp"),
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    status_payload = {
        "cothink_item_id": record.id,
        "status": "open",
        "reason": None,
    }
    initial_status = CothinkItemStatusEvent(
        id=sha256_bytes(_COTHINK_STATUS_DOMAIN + record.id.encode("utf-8"))[:32],
        cothink_item_id=record.id,
        status="open",
        reason=None,
        canonical_sha256=_canonical_hash(status_payload),
        created_at=record.created_at,
        created_by_kind=record.created_by_kind,
        created_by_ref=record.created_by_ref,
        created_by_meta_json=record.created_by_meta_json,
    )
    with store.write_transaction() as conn:
        concurrent = verify_store.get_by_canonical_sha256(
            store,
            CothinkItem,
            canonical_sha256,
            conn=conn,
        )
        if concurrent is not None:
            if (
                verify_store.latest_cothink_status(
                    store,
                    concurrent.id,
                    conn=conn,
                )
                is None
            ):
                raise VerifyInvariantViolation(
                    "Co-think item has no lifecycle status event"
                )
            return concurrent
        verify_store.insert_record(store, record, conn=conn)
        verify_store.insert_record(store, initial_status, conn=conn)
    return record


def current_cothink_item_status(
    store: TruthStore,
    *,
    cothink_item_id: str,
) -> CothinkItemStatusEvent:
    """Return the current lifecycle status by immutable ledger order."""

    item = _require_record(store, CothinkItem, cothink_item_id)
    current = verify_store.latest_cothink_status(store, item.id)
    if current is None:
        raise VerifyInvariantViolation(
            "Co-think item has no lifecycle status event"
        )
    return current


def record_cothink_item_status(
    store: TruthStore,
    *,
    cothink_item_id: str,
    status: str,
    actor: Actor,
    reason: str | None = None,
    at: str | None = None,
    event_id: str | None = None,
) -> CothinkItemStatusEvent:
    """Append one valid lifecycle transition, idempotent at current status."""

    status_value = _text(status, "Co-think item status")
    if status_value not in COTHINK_ITEM_STATUSES:
        raise VerifyInvariantViolation(
            f"Co-think item status must be one of {sorted(COTHINK_ITEM_STATUSES)}"
        )
    if reason is not None:
        reason_value: str | None = _text(reason, "Co-think status reason")
    else:
        reason_value = None
    actor_kind, actor_ref, actor_meta = _actor_fields(actor)
    with store.write_transaction() as conn:
        item = _require_record(
            store,
            CothinkItem,
            cothink_item_id,
            conn=conn,
        )
        current = verify_store.latest_cothink_status(
            store,
            item.id,
            conn=conn,
        )
        if current is None:
            raise VerifyInvariantViolation(
                "Co-think item has no lifecycle status event"
            )
        if status_value == current.status:
            return current
        allowed = COTHINK_ITEM_TRANSITIONS.get(current.status)
        if allowed is None or status_value not in allowed:
            raise VerifyInvariantViolation(
                f"invalid Co-think item status transition: "
                f"{current.status} -> {status_value}"
            )
        payload = {
            "cothink_item_id": item.id,
            "status": status_value,
            "reason": reason_value,
        }
        record = CothinkItemStatusEvent(
            id=_id(event_id, "Co-think status event id"),
            cothink_item_id=item.id,
            status=status_value,
            reason=reason_value,
            canonical_sha256=_canonical_hash(payload),
            created_at=_timestamp(at, "Co-think status timestamp"),
            created_by_kind=actor_kind,
            created_by_ref=actor_ref,
            created_by_meta_json=actor_meta,
        )
        return verify_store.insert_record(store, record, conn=conn)


def cothink_items(
    store: TruthStore,
    *,
    action_snapshot_id: str | None = None,
    document_id: str | None = None,
    delivered_only: bool = True,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any], ...]:
    if action_snapshot_id is not None and document_id is not None:
        raise VerifyInvariantViolation(
            "Co-think projection accepts action_snapshot_id or document_id, not both"
        )
    where: list[str] = []
    params: list[Any] = []
    if action_snapshot_id is not None:
        where.append("source.action_snapshot_id = ?")
        params.append(action_snapshot_id)
    if delivered_only:
        where.append("source.delivery_state = 'delivered'")
    def _read(
        read_conn: sqlite3.Connection,
    ) -> tuple[
        tuple[CothinkItem, ...],
        dict[str, CothinkItemStatusEvent | None],
    ]:
        records = verify_store.list_records(
            store,
            CothinkItem,
            where=" AND ".join(where),
            params=tuple(params),
            conn=read_conn,
        )
        if document_id is not None:
            action_ids = {
                snapshot.id
                for snapshot in verify_store.list_records(
                    store,
                    ActionSnapshot,
                    where="source.document_id = ?",
                    params=(document_id,),
                    conn=read_conn,
                )
            }
            records = tuple(
                record
                for record in records
                if record.action_snapshot_id in action_ids
            )
        statuses = {
            record.id: verify_store.latest_cothink_status(
                store,
                record.id,
                conn=read_conn,
            )
            for record in records
        }
        return records, statuses

    if conn is None:
        with store._read_connection() as read_conn:
            read_conn.execute("BEGIN")
            records, statuses = _read(read_conn)
    else:
        records, statuses = _read(conn)
    projected: list[dict[str, Any]] = []
    for record in records:
        status = statuses[record.id]
        if status is None:
            raise VerifyInvariantViolation(
                "Co-think item has no lifecycle status event"
            )
        projected.append(
            {
                "id": record.id,
                "action_snapshot_id": record.action_snapshot_id,
                "subtype": record.subtype,
                "purpose": record.purpose,
                "payload": json.loads(record.payload_json),
                "rationale": record.rationale,
                "delivery_state": record.delivery_state,
                "provenance": json.loads(record.provenance_json),
                "canonical_sha256": record.canonical_sha256,
                "created_at": record.created_at,
                "lifecycle": {
                    "status": status.status,
                    "event_id": status.id,
                    "reason": status.reason,
                    "created_at": status.created_at,
                    "actor": {
                        "kind": status.created_by_kind,
                        "ref": status.created_by_ref,
                        "meta": (
                            None
                            if status.created_by_meta_json is None
                            else json.loads(status.created_by_meta_json)
                        ),
                    },
                },
            }
        )
    return tuple(projected)


__all__ = [
    "RESULT_RELATION_KINDS",
    "ROUTING_DECISIONS",
    "SURFACING_DECISIONS",
    "COTHINK_ITEM_STATUSES",
    "COTHINK_ITEM_TRANSITIONS",
    "TERMINOLOGY_EXACT_MATCH_EXECUTOR",
    "TERMINOLOGY_EXACT_MATCH_KEY",
    "TERMINOLOGY_EXACT_MATCH_VERSION",
    "cothink_items",
    "current_cothink_item_status",
    "create_action_snapshot",
    "create_terminology_plan",
    "record_cothink_item",
    "record_cothink_item_status",
    "record_model_call_authorization",
    "record_result_relation",
    "record_routing_disposition",
    "run_terminology_exact_match",
    "seed_terminology_exact_match",
    "surfaced_results",
    "terminology_exact_matches",
    "terminology_exact_match_defaults",
]

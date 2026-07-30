"""Durable, capture-gated recheck intents derived from committed sittings.

A sitting receipt is already the durable fact that exact proposals were
applied. Verify result relations already identify which evaluation run
produced each proposal and whether a later run rechecked it. Combining those
records yields a restart-safe pending intent without adding a second mutable
queue or treating the sitting itself as model-call authorization.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from work_buddy.cowork.verify import (
    ActionSnapshot,
    EvaluationResult,
    EvaluationRun,
    ResultRelation,
)
from work_buddy.cowork.verify import store as verify_store
from work_buddy.cowork.verify_coordination import (
    review_applications,
    root_coordination_for_run,
)
from work_buddy.truth.identity import canonical_json, sha256_text
from work_buddy.truth.store import TruthStore


class VerifyRecheckIntentError(ValueError):
    """A derived recheck intent is missing, fulfilled, or mismatched."""


@dataclass(frozen=True, slots=True)
class VerifyRecheckIntent:
    id: str
    sitting_id: str
    document_id: str
    source_run_id: str
    proposal_ids: tuple[str, ...]
    pending_proposal_ids: tuple[str, ...]
    fulfilled_by_run_ids: tuple[str, ...]
    committed_at: str
    source_action_snapshot_id: str
    original_target_source: str | None
    original_target_label: str | None
    original_target_kind: str
    original_target_selector_json: str
    original_target_text_sha256: str
    original_target_reference_json: str | None
    original_target_reference_sha256: str | None
    provider_id: str
    model_id: str
    provider_label: str
    model_label: str
    original_request_summary_json: str

    @property
    def status(self) -> str:
        if not self.pending_proposal_ids:
            return "fulfilled"
        if self.original_target_source is None or (
            self.original_target_kind != "document"
            and self.original_target_reference_sha256 is None
        ):
            return "user_action_required"
        return "pending_capture"

    def to_dict(self) -> dict[str, Any]:
        original_request = json.loads(self.original_request_summary_json)
        return {
            "id": self.id,
            "sitting_id": self.sitting_id,
            "document_id": self.document_id,
            "source_run_id": self.source_run_id,
            "proposal_ids": list(self.proposal_ids),
            "pending_proposal_ids": list(self.pending_proposal_ids),
            "fulfilled_by_run_ids": list(self.fulfilled_by_run_ids),
            "committed_at": self.committed_at,
            "status": self.status,
            # These immutable request fields are projected at the level the
            # R2/UI contract consumes. Keep the complete original request
            # below for audit and forward-compatible inspection.
            "user_goal": str(original_request.get("user_goal") or ""),
            "protected_intent": str(
                original_request.get("protected_intent") or ""
            ),
            "original_action_target": {
                "action_snapshot_id": self.source_action_snapshot_id,
                "source": self.original_target_source,
                "label": self.original_target_label,
                "kind": self.original_target_kind,
                "selector": json.loads(self.original_target_selector_json),
                "target_text_sha256": self.original_target_text_sha256,
                "target_reference": (
                    None
                    if self.original_target_reference_json is None
                    else json.loads(self.original_target_reference_json)
                ),
                "target_reference_sha256": (
                    self.original_target_reference_sha256
                ),
            },
            "execution": {
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "provider_label": self.provider_label,
                "model_label": self.model_label,
            },
            "original_request": json.loads(
                self.original_request_summary_json
            ),
            "requires": {
                "fresh_action_snapshot": True,
                "fresh_model_call_authorization": True,
                "same_target_source": self.status != "user_action_required",
                "same_target_reference": (
                    self.original_target_kind == "document"
                    or self.original_target_reference_sha256 is not None
                ),
                "exact_target_resolution": (
                    self.original_target_kind == "document"
                    or self.original_target_reference_sha256 is not None
                ),
                "user_affirmed_exact_target_required": (
                    self.status == "user_action_required"
                ),
                "on_unresolved": "user_action_required",
                "allow_widen_to_whole_document": False,
            },
        }


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise VerifyRecheckIntentError(
            "committed sitting has an invalid timestamp"
        ) from exc
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def _applied_proposal_ids(receipt: Mapping[str, Any]) -> tuple[str, ...]:
    results = receipt.get("results")
    if not isinstance(results, list):
        return ()
    values: list[str] = []
    for result in results:
        if (
            not isinstance(result, Mapping)
            or result.get("result") != "applied"
        ):
            continue
        proposal_id = result.get("proposal_id")
        if isinstance(proposal_id, str) and proposal_id and proposal_id not in values:
            values.append(proposal_id)
    return tuple(values)


def _execution_for_run(
    store: TruthStore,
    run_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, str], dict[str, Any]] | None:
    root = root_coordination_for_run(store, run_id, conn=conn)
    if root is None:
        return None
    selection = root["selection"]
    if (
        not str(selection.get("provider_id") or "")
        or not str(selection.get("model_id") or "")
    ):
        return None
    return (
        {
            "provider_id": str(selection.get("provider_id") or ""),
            "model_id": str(selection.get("model_id") or ""),
            "provider_label": str(selection.get("provider_label") or ""),
            "model_label": str(selection.get("model_label") or ""),
        },
        dict(root["request_summary"]),
    )


def _action_target_for_run(
    store: TruthStore,
    run: EvaluationRun,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, str | None] | None:
    action = verify_store.get_record(
        store,
        ActionSnapshot,
        run.action_snapshot_id,
        conn=conn,
    )
    if action is None:
        return None
    try:
        context = json.loads(action.context_boundary_json)
        selector = json.loads(action.target_selector_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(context, Mapping) or not isinstance(selector, Mapping):
        return None
    source = context.get("target_source")
    if not isinstance(source, str) or not source:
        # A legacy whole-document action is unambiguous. A legacy text target
        # is not: selection, section, Working on, and custom range share one
        # selector shape, so inferring any of them would redirect user intent.
        source = "whole_document" if action.target_kind == "document" else None
    label = context.get("target_label")
    target_reference = context.get("target_reference")
    target_reference_sha256 = context.get("target_reference_sha256")
    if not isinstance(target_reference, Mapping):
        target_reference = None
        target_reference_sha256 = None
    elif not isinstance(target_reference_sha256, str):
        target_reference = None
        target_reference_sha256 = None
    return {
        "source_action_snapshot_id": action.id,
        "original_target_source": source,
        "original_target_label": (
            label if isinstance(label, str) and label else None
        ),
        "original_target_kind": action.target_kind,
        "original_target_selector_json": canonical_json(selector),
        "original_target_text_sha256": action.target_text_sha256,
        "original_target_reference_json": (
            None
            if target_reference is None
            else canonical_json(target_reference)
        ),
        "original_target_reference_sha256": target_reference_sha256,
    }


def _run_is_bound_fulfillment(
    store: TruthStore,
    *,
    run: EvaluationRun,
    source_run_id: str,
    intent_id: str,
    proposal_ids: tuple[str, ...],
    source_target: Mapping[str, str | None],
    source_execution: Mapping[str, str],
    source_request: Mapping[str, Any],
    committed_at: str,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Require the later run to carry the exact derived recheck binding."""

    root = root_coordination_for_run(store, run.id, conn=conn)
    if root is None:
        return False
    request = root["request_summary"]
    recheck_proposals = request.get("recheck_of_proposal_ids")
    if not isinstance(recheck_proposals, list):
        return False
    if (
        request.get("recheck_intent_id") != intent_id
        or request.get("recheck_of_run_id") != source_run_id
        or tuple(recheck_proposals) != proposal_ids
        or root["selection"].get("provider_id")
        != source_execution["provider_id"]
        or root["selection"].get("model_id")
        != source_execution["model_id"]
        or request.get("user_goal")
        != source_request.get("user_goal")
        or request.get("protected_intent")
        != source_request.get("protected_intent")
    ):
        return False
    target = _action_target_for_run(store, run, conn=conn)
    if target is None:
        return False
    if (
        target["source_action_snapshot_id"]
        == source_target["source_action_snapshot_id"]
    ):
        return False
    source_requires_confirmation = (
        source_target["original_target_source"] is None
        or (
            source_target["original_target_kind"] != "document"
            and source_target["original_target_reference_sha256"] is None
        )
    )
    if source_requires_confirmation:
        action = verify_store.get_record(
            store,
            ActionSnapshot,
            run.action_snapshot_id,
            conn=conn,
        )
        if action is None:
            return False
        try:
            context = json.loads(action.context_boundary_json)
            if not isinstance(context, Mapping):
                return False
            _validate_user_affirmed_recheck_target(
                store=store,
                action_snapshot=action,
                context=context,
                target_confirmation=(
                    request.get("recheck_target_confirmation")
                    if isinstance(
                        request.get("recheck_target_confirmation"),
                        Mapping,
                    )
                    else None
                ),
                recheck_intent_id=intent_id,
                source_run_id=source_run_id,
                proposal_ids=proposal_ids,
                user_goal=str(source_request.get("user_goal") or ""),
                protected_intent=str(
                    source_request.get("protected_intent") or ""
                ),
                committed_at=committed_at,
                conn=conn,
            )
        except (
            json.JSONDecodeError,
            TypeError,
            VerifyRecheckIntentError,
        ):
            return False
        return True
    if (
        target["original_target_source"]
        != source_target["original_target_source"]
        or target["original_target_kind"]
        != source_target["original_target_kind"]
        or target["original_target_reference_sha256"]
        != source_target["original_target_reference_sha256"]
    ):
        return False
    return True


def verification_recheck_intents(
    store: TruthStore,
    *,
    document_id: str,
    conn: sqlite3.Connection | None = None,
) -> tuple[VerifyRecheckIntent, ...]:
    """Derive applied-proposal rechecks from R5 and Verify append-only facts."""

    applications = list(
        review_applications(store, document_id=document_id, conn=conn)
    )
    if not applications:
        # Compatibility fallback for a live pre-v7 sitting that has not yet
        # been projected into the portable ledger.
        if conn is None:
            with store._read_connection() as read_conn:
                rows = read_conn.execute(
                    """
                    SELECT id, committed_at, receipt_json
                    FROM cowork_sitting_intents
                    WHERE document_id = ?
                      AND state = 'committed'
                      AND receipt_json IS NOT NULL
                    ORDER BY committed_at, id
                    """,
                    (document_id,),
                ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, committed_at, receipt_json
                FROM cowork_sitting_intents
                WHERE document_id = ?
                  AND state = 'committed'
                  AND receipt_json IS NOT NULL
                ORDER BY committed_at, id
                """,
                (document_id,),
            ).fetchall()
        for row in rows:
            try:
                receipt = json.loads(str(row["receipt_json"]))
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(receipt, Mapping):
                continue
            applied = _applied_proposal_ids(receipt)
            if applied:
                applications.append(
                    {
                        "id": str(row["id"]),
                        "committed_at": str(row["committed_at"] or ""),
                        "applied_proposal_ids": list(applied),
                    }
                )
    results = verify_store.list_records(
        store,
        EvaluationResult,
        conn=conn,
    )
    result_run = {
        result.id: result.evaluation_run_id
        for result in results
    }
    runs = verify_store.list_records(store, EvaluationRun, conn=conn)
    run_by_id = {run.id: run for run in runs}
    run_started_at = {run.id: _utc(run.started_at) for run in runs}
    relations = verify_store.list_records(store, ResultRelation, conn=conn)
    source_runs_by_proposal: dict[str, set[str]] = {}
    for relation in relations:
        if (
            relation.relation_kind == "addresses"
            and relation.target_kind == "proposal"
        ):
            run_id = result_run.get(relation.evaluation_result_id)
            if run_id is not None:
                source_runs_by_proposal.setdefault(
                    relation.target_ref,
                    set(),
                ).add(run_id)

    intents: list[VerifyRecheckIntent] = []
    for row in applications:
        applied = tuple(row["applied_proposal_ids"])
        if not applied:
            continue
        committed_at = str(row["committed_at"] or "")
        committed_time = _utc(committed_at)
        grouped: dict[str, list[str]] = {}
        for proposal_id in applied:
            for run_id in sorted(source_runs_by_proposal.get(proposal_id, ())):
                grouped.setdefault(run_id, []).append(proposal_id)
        for source_run_id, proposal_values in grouped.items():
            source_run = run_by_id.get(source_run_id)
            if source_run is None:
                continue
            source_binding = _execution_for_run(
                store,
                source_run_id,
                conn=conn,
            )
            target = _action_target_for_run(store, source_run, conn=conn)
            if source_binding is None or target is None:
                continue
            execution, original_request = source_binding
            proposal_ids = tuple(dict.fromkeys(proposal_values))
            intent_id = sha256_text(
                canonical_json(
                    {
                        "domain": "work-buddy.cowork-verify-recheck-intent/v1",
                        "sitting_id": str(row["id"]),
                        "source_run_id": source_run_id,
                        "proposal_ids": list(proposal_ids),
                    }
                )
            )
            fulfilled_by_proposal: dict[str, set[str]] = {
                proposal_id: set() for proposal_id in proposal_ids
            }
            for relation in relations:
                if (
                    relation.relation_kind != "rechecks"
                    or relation.target_kind != "proposal"
                    or relation.target_ref not in fulfilled_by_proposal
                    or _utc(relation.created_at) <= committed_time
                ):
                    continue
                run_id = result_run.get(relation.evaluation_result_id)
                if (
                    run_id is not None
                    and run_id != source_run_id
                    and run_started_at.get(run_id, committed_time)
                    > committed_time
                    and run_id in run_by_id
                    and _run_is_bound_fulfillment(
                        store,
                        run=run_by_id[run_id],
                        source_run_id=source_run_id,
                        intent_id=intent_id,
                        proposal_ids=proposal_ids,
                        source_target=target,
                        source_execution=execution,
                        source_request=original_request,
                        committed_at=committed_at,
                        conn=conn,
                    )
                ):
                    fulfilled_by_proposal[relation.target_ref].add(run_id)
            pending = tuple(
                proposal_id
                for proposal_id in proposal_ids
                if not fulfilled_by_proposal[proposal_id]
            )
            fulfilled_runs = tuple(
                sorted(
                    {
                        run_id
                        for run_ids in fulfilled_by_proposal.values()
                        for run_id in run_ids
                    }
                )
            )
            intents.append(
                VerifyRecheckIntent(
                    id=intent_id,
                    sitting_id=str(row["id"]),
                    document_id=document_id,
                    source_run_id=source_run_id,
                    proposal_ids=proposal_ids,
                    pending_proposal_ids=pending,
                    fulfilled_by_run_ids=fulfilled_runs,
                    committed_at=committed_at,
                    **target,
                    **execution,
                    original_request_summary_json=canonical_json(
                        original_request
                    ),
                )
            )
    return tuple(intents)


def validate_recheck_intent(
    store: TruthStore,
    *,
    document_id: str,
    intent_id: str,
    source_run_id: str,
    proposal_ids: Sequence[str],
    user_goal: str,
    protected_intent: str,
    action_snapshot: ActionSnapshot | None = None,
    target_confirmation: Mapping[str, Any] | None = None,
    affirmed_action_snapshot: ActionSnapshot | None = None,
) -> VerifyRecheckIntent:
    """Bind a fresh Verify start to exactly one still-pending sitting intent."""

    intent = next(
        (
            candidate
            for candidate in verification_recheck_intents(
                store,
                document_id=document_id,
            )
            if candidate.id == intent_id
        ),
        None,
    )
    if intent is None:
        raise VerifyRecheckIntentError("recheck intent does not exist")
    if intent.status == "fulfilled":
        raise VerifyRecheckIntentError("recheck intent is already fulfilled")
    if intent.source_run_id != source_run_id:
        raise VerifyRecheckIntentError(
            "recheck intent belongs to another source run"
        )
    requested = tuple(proposal_ids)
    if requested != intent.pending_proposal_ids:
        raise VerifyRecheckIntentError(
            "recheck intent proposal binding changed"
        )
    try:
        original_request = json.loads(
            intent.original_request_summary_json
        )
    except (json.JSONDecodeError, TypeError) as exc:
        raise VerifyRecheckIntentError(
            "recheck intent has an invalid original request"
        ) from exc
    if not isinstance(original_request, Mapping):
        raise VerifyRecheckIntentError(
            "recheck intent has an invalid original request"
        )
    if (
        user_goal != str(original_request.get("user_goal") or "")
        or protected_intent
        != str(original_request.get("protected_intent") or "")
    ):
        raise VerifyRecheckIntentError(
            "recheck must preserve the original user goal and protected intent"
        )
    if action_snapshot is not None:
        if action_snapshot.document_id != document_id:
            raise VerifyRecheckIntentError(
                "fresh recheck capture belongs to another document"
            )
        if action_snapshot.id == intent.source_action_snapshot_id:
            raise VerifyRecheckIntentError(
                "recheck requires a fresh exact action snapshot"
            )
        if _utc(action_snapshot.created_at) <= _utc(intent.committed_at):
            raise VerifyRecheckIntentError(
                "recheck capture predates the committed sitting"
            )
        try:
            context = json.loads(action_snapshot.context_boundary_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise VerifyRecheckIntentError(
                "fresh recheck capture has invalid target context"
            ) from exc
        if not isinstance(context, Mapping):
            raise VerifyRecheckIntentError(
                "fresh recheck capture has invalid target context"
            )
        if intent.status == "user_action_required":
            _validate_user_affirmed_recheck_target(
                store=store,
                action_snapshot=action_snapshot,
                context=context,
                target_confirmation=target_confirmation,
                affirmed_action_snapshot=affirmed_action_snapshot,
                committed_at=intent.committed_at,
                recheck_intent_id=intent.id,
                source_run_id=intent.source_run_id,
                proposal_ids=intent.pending_proposal_ids,
                user_goal=user_goal,
                protected_intent=protected_intent,
            )
        else:
            if (
                target_confirmation is not None
                or affirmed_action_snapshot is not None
            ):
                raise VerifyRecheckIntentError(
                    "an exact durable recheck target cannot be replaced by "
                    "user target confirmation"
                )
            if context.get("target_source") != intent.original_target_source:
                raise VerifyRecheckIntentError(
                    "fresh recheck capture must resolve the original target "
                    "source; widening to another target is not allowed"
                )
            if action_snapshot.target_kind != intent.original_target_kind:
                raise VerifyRecheckIntentError(
                    "fresh recheck capture changed the original target kind"
                )
            if (
                intent.original_target_reference_sha256 is not None
                and context.get("target_reference_sha256")
                != intent.original_target_reference_sha256
            ):
                raise VerifyRecheckIntentError(
                    "fresh recheck capture did not resolve the original target "
                    "reference"
                )
    elif intent.status == "user_action_required":
        raise VerifyRecheckIntentError(
            "the unresolved original target requires a fresh explicitly "
            "affirmed Working on capture"
        )
    elif (
        target_confirmation is not None
        or affirmed_action_snapshot is not None
    ):
        raise VerifyRecheckIntentError(
            "target confirmation requires a fresh recheck capture"
        )
    return intent


def _validate_user_affirmed_recheck_target(
    *,
    store: TruthStore,
    action_snapshot: ActionSnapshot,
    context: Mapping[str, Any],
    target_confirmation: Mapping[str, Any] | None,
    affirmed_action_snapshot: ActionSnapshot | None = None,
    committed_at: str | None = None,
    recheck_intent_id: str,
    source_run_id: str,
    proposal_ids: Sequence[str],
    user_goal: str,
    protected_intent: str,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Validate server-attested replacement-target evidence for a legacy intent."""

    if action_snapshot.created_by_kind != "human":
        raise VerifyRecheckIntentError(
            "recheck target confirmation requires a fresh human capture"
        )
    if (
        action_snapshot.target_kind != "text_quote"
        or context.get("target_source") != "working_target"
    ):
        raise VerifyRecheckIntentError(
            "the unresolved original target requires an exact Working on "
            "passage; widening is not allowed"
        )
    target_reference = context.get("target_reference")
    target_reference_sha256 = context.get("target_reference_sha256")
    if (
        not isinstance(target_reference, Mapping)
        or target_reference.get("kind") != "text_range"
        or target_reference.get("granularity") != "character"
        or not isinstance(target_reference_sha256, str)
        or not target_reference_sha256
    ):
        raise VerifyRecheckIntentError(
            "the affirmed Working on passage requires a durable target "
            "reference"
        )
    if target_confirmation is None:
        raise VerifyRecheckIntentError(
            "the unresolved original target requires explicit user "
            "affirmation"
        )
    required_keys = {
        "schema",
        "method",
        "affirmed_capture_id",
        "affirmed_action_snapshot_id",
        "run_capture_id",
        "target_reference_sha256",
        "target_text_sha256",
    }
    if set(target_confirmation) != required_keys:
        raise VerifyRecheckIntentError(
            "recheck target confirmation has an invalid shape"
        )
    if (
        target_confirmation.get("schema")
        != "work-buddy.cowork-recheck-target-confirmation/v1"
        or target_confirmation.get("method")
        != "user_affirmed_working_target"
    ):
        raise VerifyRecheckIntentError(
            "recheck target confirmation has an unsupported method"
        )
    affirmed_capture_id = target_confirmation.get("affirmed_capture_id")
    affirmed_action_snapshot_id = target_confirmation.get(
        "affirmed_action_snapshot_id"
    )
    run_capture_id = target_confirmation.get("run_capture_id")
    if (
        not isinstance(affirmed_capture_id, str)
        or not affirmed_capture_id.strip()
        or not isinstance(affirmed_action_snapshot_id, str)
        or not affirmed_action_snapshot_id.strip()
        or not isinstance(run_capture_id, str)
        or not run_capture_id.strip()
    ):
        raise VerifyRecheckIntentError(
            "recheck target confirmation requires exact capture ids"
        )
    if affirmed_capture_id == run_capture_id:
        raise VerifyRecheckIntentError(
            "recheck target confirmation requires a separate completed "
            "affirmation capture"
        )
    affirmed_action = affirmed_action_snapshot
    if affirmed_action is None:
        affirmed_action = verify_store.get_record(
            store,
            ActionSnapshot,
            affirmed_action_snapshot_id,
            conn=conn,
        )
    if (
        affirmed_action is None
        or affirmed_action.id != affirmed_action_snapshot_id
    ):
        raise VerifyRecheckIntentError(
            "recheck target confirmation has no attested affirmation snapshot"
        )
    if affirmed_action.id == action_snapshot.id:
        raise VerifyRecheckIntentError(
            "recheck target confirmation requires separate action snapshots"
        )
    if (
        affirmed_action.document_id != action_snapshot.document_id
        or affirmed_action.created_by_kind != "human"
        or affirmed_action.created_by_ref != action_snapshot.created_by_ref
    ):
        raise VerifyRecheckIntentError(
            "recheck target affirmation belongs to another human or document"
        )
    if committed_at is not None and (
        _utc(affirmed_action.created_at) <= _utc(committed_at)
        or _utc(affirmed_action.created_at)
        > _utc(action_snapshot.created_at)
    ):
        raise VerifyRecheckIntentError(
            "recheck target affirmation is not ordered between the committed "
            "sitting and the Run capture"
        )
    try:
        affirmed_context = json.loads(affirmed_action.context_boundary_json)
        affirmed_egress = json.loads(affirmed_action.egress_boundary_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise VerifyRecheckIntentError(
            "recheck target affirmation has invalid attested context"
        ) from exc
    if not isinstance(affirmed_context, Mapping) or not isinstance(
        affirmed_egress,
        Mapping,
    ):
        raise VerifyRecheckIntentError(
            "recheck target affirmation has invalid attested context"
        )
    if (
        affirmed_action.target_kind != "text_quote"
        or affirmed_context.get("target_source") != "working_target"
        or affirmed_context.get("purpose")
        != "user_affirmed_exact_recheck_target"
        or affirmed_egress.get("class") != "no_external_egress"
        or affirmed_egress.get("content") != "none"
    ):
        raise VerifyRecheckIntentError(
            "recheck target affirmation is not an attested exact Working on "
            "capture"
        )
    affirmed_reference = affirmed_context.get("target_reference")
    affirmed_reference_sha256 = affirmed_context.get(
        "target_reference_sha256"
    )
    if (
        not isinstance(affirmed_reference, Mapping)
        or affirmed_reference.get("kind") != "text_range"
        or affirmed_reference.get("granularity") != "character"
        or not isinstance(affirmed_reference_sha256, str)
        or not affirmed_reference_sha256
    ):
        raise VerifyRecheckIntentError(
            "recheck target affirmation has no durable character reference"
        )
    affirmed_authority = affirmed_context.get("authority_context")
    if (
        not isinstance(affirmed_authority, Mapping)
        or affirmed_authority.get("recheck_intent_id") != recheck_intent_id
        or affirmed_authority.get("source_run_id") != source_run_id
        or affirmed_authority.get("pending_proposal_ids")
        != list(proposal_ids)
        or affirmed_authority.get("user_goal_sha256")
        != sha256_text(user_goal)
        or affirmed_authority.get("protected_intent_sha256")
        != sha256_text(protected_intent)
    ):
        raise VerifyRecheckIntentError(
            "recheck target affirmation is bound to another recheck intent"
        )
    if affirmed_context.get("capture_id") != affirmed_capture_id:
        raise VerifyRecheckIntentError(
            "recheck target affirmation capture id is not attested"
        )
    if run_capture_id != context.get("capture_id"):
        raise VerifyRecheckIntentError(
            "recheck target confirmation belongs to another run capture"
        )
    if (
        target_confirmation.get("target_reference_sha256")
        != target_reference_sha256
    ):
        raise VerifyRecheckIntentError(
            "recheck target confirmation changed the affirmed target "
            "reference"
        )
    if affirmed_reference_sha256 != target_reference_sha256:
        raise VerifyRecheckIntentError(
            "Run target reference does not match the attested affirmation"
        )
    if (
        target_confirmation.get("target_text_sha256")
        != action_snapshot.target_text_sha256
    ):
        raise VerifyRecheckIntentError(
            "recheck target confirmation changed the affirmed target text"
        )
    if affirmed_action.target_text_sha256 != action_snapshot.target_text_sha256:
        raise VerifyRecheckIntentError(
            "Run target text does not match the attested affirmation"
        )


__all__ = [
    "VerifyRecheckIntent",
    "VerifyRecheckIntentError",
    "validate_recheck_intent",
    "verification_recheck_intents",
]

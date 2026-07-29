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
                "same_target_source": True,
                "same_target_reference": (
                    self.original_target_kind == "document"
                    or self.original_target_reference_sha256 is not None
                ),
                "exact_target_resolution": (
                    self.original_target_kind == "document"
                    or self.original_target_reference_sha256 is not None
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
    ):
        return False
    target = _action_target_for_run(store, run, conn=conn)
    if target is None:
        return False
    if (
        target["source_action_snapshot_id"]
        == source_target["source_action_snapshot_id"]
        or target["original_target_source"]
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
    action_snapshot: ActionSnapshot | None = None,
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
    if intent.status == "user_action_required":
        raise VerifyRecheckIntentError(
            "the original scoped target source is unavailable; "
            "the user must choose a new target explicitly"
        )
    if intent.source_run_id != source_run_id:
        raise VerifyRecheckIntentError(
            "recheck intent belongs to another source run"
        )
    requested = tuple(proposal_ids)
    if requested != intent.pending_proposal_ids:
        raise VerifyRecheckIntentError(
            "recheck intent proposal binding changed"
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
    return intent


__all__ = [
    "VerifyRecheckIntent",
    "VerifyRecheckIntentError",
    "validate_recheck_intent",
    "verification_recheck_intents",
]

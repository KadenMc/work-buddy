"""Criterion-first configuration for Co-work Verify.

This module projects immutable Verify definitions, bindings, and activation
events into one effective per-document configuration. It deliberately keeps
criterion authorship, activation authorization, executor admission, and data
sharing as separate facts.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from work_buddy.agent_execution.models import AgentExecutionSelection
from work_buddy.cowork.verify import (
    CheckDefinitionVersion,
    CriterionActivation,
    CriterionCheckBinding,
    CriterionDefinitionVersion,
    SeededTerminologyExactMatch,
    VerifyInvariantViolation,
    seed_instruction_model_check,
    seed_terminology_exact_match,
    terminology_exact_match_defaults,
)
from work_buddy.cowork.verify import store as verify_store
from work_buddy.cowork.verify.service import admitted_check_executor
from work_buddy.cowork.verify_execution import (
    verify_execution_disclosure_plan,
)
from work_buddy.cowork.verify_jobs import MAX_VERIFY_JOB_BUDGET_USD
from work_buddy.truth import documents
from work_buddy.truth.contracts import Actor, validate_agent_producer_meta
from work_buddy.truth.identity import canonical_json, new_id, sha256_text, utc_now
from work_buddy.truth.store import DocumentRecord, TruthStore


CONFIGURATION_SCHEMA = "work-buddy.cowork-verify-configuration/v1"
MAX_USER_CHECK_EVALUATION_INSTRUCTIONS_CHARS = 8_000
_SYSTEM_ORIGIN = "system"
_USER_ORIGIN = "user"
_EXPECTED_ACTIVATION_UNSET = object()


def _timestamp(value: str | None) -> str:
    candidate = utc_now() if value is None else value
    if not isinstance(candidate, str) or not candidate.strip():
        raise VerifyInvariantViolation("activation timestamp must be nonempty")
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerifyInvariantViolation(
            "activation timestamp must be ISO 8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VerifyInvariantViolation(
            "activation timestamp must carry a UTC offset"
        )
    return candidate


def _actor_fields(actor: Actor) -> tuple[str, str | None, str | None]:
    if not isinstance(actor, Actor):
        raise TypeError("actor must be an Actor")
    if actor.kind == "agent_run":
        validate_agent_producer_meta(actor.meta)
    return (
        actor.kind,
        actor.ref,
        canonical_json(dict(actor.meta)) if actor.meta else None,
    )


def _json_object(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise VerifyInvariantViolation(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise VerifyInvariantViolation(f"{label} must be a JSON object")
    return parsed


def _json_list(value: str, label: str) -> list[Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise VerifyInvariantViolation(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, list):
        raise VerifyInvariantViolation(f"{label} must be a JSON list")
    return parsed


def _actor_projection(
    *,
    kind: str,
    ref: str | None,
    meta_json: str | None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "ref": ref,
        "meta": None if meta_json is None else _json_object(meta_json, "actor meta"),
    }


def _scope_specificity(scope: Mapping[str, Any], document_id: str) -> int | None:
    """Return deterministic activation specificity or None when inapplicable."""

    if scope.get("kind") != "document":
        return None
    scoped_document = scope.get("document_id")
    if scoped_document is None:
        return 0
    return 1 if scoped_document == document_id else None


def _check_availability(
    check: CheckDefinitionVersion,
    *,
    criterion_kind: str,
) -> dict[str, Any]:
    """Project executor admission independently from the stored definition."""

    executor = admitted_check_executor(
        check,
        criterion_kind=criterion_kind,
    )
    if executor is not None:
        return {
            "state": "available",
            "reason": None,
            "execution_location": (
                "local"
                if executor.execution_mode == "in_process"
                else "account_backed_agent"
            ),
        }
    return {
        "state": "unavailable",
        "reason": "executor_not_admitted",
        "execution_location": None,
    }


def _data_sharing(
    check: CheckDefinitionVersion,
    *,
    criterion_kind: str,
) -> dict[str, Any]:
    executor = admitted_check_executor(
        check,
        criterion_kind=criterion_kind,
    )
    if executor is not None and executor.execution_mode == "in_process":
        return {
            "class": "local_only",
            "external_egress": False,
            "basis": "admitted_deterministic_executor",
        }
    if (
        executor is not None
        and executor.execution_mode == "account_backed_specialist"
    ):
        return {
            "class": "account_backed_agent",
            "external_egress": True,
            "basis": "explicit_verify_run_selection",
        }
    return {
        "class": "not_authorized",
        "external_egress": None,
        "basis": "no_admitted_executor",
    }


def _latest_definitions(
    records: tuple[CriterionDefinitionVersion, ...],
) -> tuple[CriterionDefinitionVersion, ...]:
    by_key: dict[str, CriterionDefinitionVersion] = {}
    for record in records:
        current = by_key.get(record.stable_key)
        if current is None or record.version > current.version:
            by_key[record.stable_key] = record
    return tuple(
        sorted(by_key.values(), key=lambda item: (item.stable_key, item.version))
    )


def _applicable_activations(
    activations: tuple[CriterionActivation, ...],
    *,
    criterion_id: str,
    document_id: str,
) -> tuple[
    list[tuple[int, int, CriterionActivation, dict[str, Any]]],
    list[dict[str, Any]],
]:
    applicable: list[tuple[int, int, CriterionActivation, dict[str, Any]]] = []
    issues: list[dict[str, Any]] = []
    for sequence, activation in enumerate(activations, start=1):
        if activation.criterion_definition_version_id != criterion_id:
            continue
        scope = _json_object(activation.scope_json, "criterion activation scope")
        specificity = _scope_specificity(scope, document_id)
        if specificity is None:
            continue
        if (
            activation.origin == _USER_ORIGIN
            and activation.created_by_kind != "human"
        ):
            issues.append(
                {
                    "code": "unauthorized_user_activation",
                    "activation_id": activation.id,
                }
            )
            continue
        applicable.append((specificity, sequence, activation, scope))
    return applicable, issues


def _effective_activation(
    activations: tuple[CriterionActivation, ...],
    *,
    criterion_id: str,
    document_id: str,
) -> tuple[CriterionActivation | None, dict[str, Any] | None, bool, list[dict[str, Any]]]:
    applicable, issues = _applicable_activations(
        activations,
        criterion_id=criterion_id,
        document_id=document_id,
    )
    policy = [
        item for item in applicable if item[2].origin != _USER_ORIGIN
    ]
    user = [
        item
        for item in applicable
        if item[2].origin == _USER_ORIGIN and item[0] == 1
    ]
    latest_policy = max(policy, key=lambda item: (item[0], item[1]), default=None)
    latest_user = max(user, key=lambda item: item[1], default=None)
    required = bool(latest_policy and latest_policy[2].is_required)
    if required:
        selected = latest_policy
        if selected is not None and not selected[2].is_enabled:
            issues.append(
                {
                    "code": "required_activation_disabled",
                    "activation_id": selected[2].id,
                }
            )
    elif latest_user is not None:
        selected = latest_user
    else:
        selected = latest_policy
    if selected is None:
        return None, None, False, issues
    return selected[2], selected[3], required, issues


def _criterion_applies_to_document(
    criterion: CriterionDefinitionVersion,
    *,
    bindings: tuple[CriterionCheckBinding, ...],
    activations: tuple[CriterionActivation, ...],
    document_id: str,
) -> bool:
    """Keep personal criteria inside the document that owns their activation.

    System definitions may intentionally carry document-class defaults. A
    user-authored criterion, however, is personal document configuration: it
    is visible only when an authorized activation names this exact document
    and selects a binding that belongs to the same criterion. A generic
    ``{"kind": "document"}`` activation must not silently turn a personal
    check into a reusable cross-document definition.
    """

    if criterion.origin != _USER_ORIGIN:
        return True
    criterion_binding_ids = {
        binding.id
        for binding in bindings
        if binding.criterion_definition_version_id == criterion.id
    }
    if not criterion_binding_ids:
        return False
    applicable, _issues = _applicable_activations(
        activations,
        criterion_id=criterion.id,
        document_id=document_id,
    )
    return any(
        specificity == 1
        and activation.criterion_check_binding_id in criterion_binding_ids
        for specificity, _sequence, activation, _scope in applicable
    )


def _configuration_projection(
    store: TruthStore,
    document_id: str,
    *,
    conn: sqlite3.Connection,
    system_defaults: SeededTerminologyExactMatch | None = None,
    execution_selection: AgentExecutionSelection | None = None,
) -> dict[str, Any]:
    criterion_records = list(
        verify_store.list_records(
            store,
            CriterionDefinitionVersion,
            conn=conn,
        )
    )
    check_records = list(
        verify_store.list_records(
            store,
            CheckDefinitionVersion,
            conn=conn,
        )
    )
    bindings = list(
        verify_store.list_records(
            store,
            CriterionCheckBinding,
            conn=conn,
        )
    )
    activations = list(
        verify_store.list_records(
            store,
            CriterionActivation,
            conn=conn,
        )
    )
    if system_defaults is not None:
        if all(
            item.id != system_defaults.criterion.id
            for item in criterion_records
        ):
            criterion_records.append(system_defaults.criterion)
        if all(item.id != system_defaults.check.id for item in check_records):
            check_records.append(system_defaults.check)
        if all(
            item.id != system_defaults.binding.id for item in bindings
        ):
            bindings.append(system_defaults.binding)
        if all(
            item.id != system_defaults.activation.id
            for item in activations
        ):
            activations.append(system_defaults.activation)

    binding_records = tuple(bindings)
    activation_records = tuple(activations)
    criteria = _latest_definitions(
        tuple(
            criterion
            for criterion in criterion_records
            if _criterion_applies_to_document(
                criterion,
                bindings=binding_records,
                activations=activation_records,
                document_id=document_id,
            )
        )
    )
    checks = {
        item.id: item
        for item in check_records
    }
    projected: list[dict[str, Any]] = []
    for criterion in criteria:
        criterion_bindings = tuple(
            item
            for item in bindings
            if item.criterion_definition_version_id == criterion.id
        )
        effective, effective_scope, required, issues = _effective_activation(
            activation_records,
            criterion_id=criterion.id,
            document_id=document_id,
        )
        projected_checks: list[dict[str, Any]] = []
        for binding in criterion_bindings:
            check = checks.get(binding.check_definition_version_id)
            if check is None:
                issues.append(
                    {
                        "code": "missing_check_definition",
                        "binding_id": binding.id,
                    }
                )
                continue
            availability = _check_availability(
                check,
                criterion_kind=criterion.criterion_kind,
            )
            projected_checks.append(
                {
                    "id": check.id,
                    "stable_key": check.stable_key,
                    "version": check.version,
                    "title": check.title,
                    "method": {
                        "mechanism": check.mechanism,
                        "executor_ref": check.executor_ref,
                    },
                    "limitations": _json_list(
                        check.limitations_json,
                        "check limitations",
                    ),
                    "origin": {
                        "definition_origin": check.origin,
                        "author": _actor_projection(
                            kind=check.created_by_kind,
                            ref=check.created_by_ref,
                            meta_json=check.created_by_meta_json,
                        ),
                    },
                    "data_sharing": _data_sharing(
                        check,
                        criterion_kind=criterion.criterion_kind,
                    ),
                    "availability": availability,
                    "binding": {
                        "id": binding.id,
                        "selected": bool(
                            effective
                            and effective.criterion_check_binding_id == binding.id
                        ),
                        "configuration": _json_object(
                            binding.configuration_json,
                            "criterion-check binding configuration",
                        ),
                    },
                }
            )
        projected_checks.sort(
            key=lambda item: (
                item["stable_key"],
                item["version"],
                item["binding"]["id"],
            )
        )
        available = any(
            item["availability"]["state"] == "available"
            for item in projected_checks
        )
        selected_available = any(
            item["binding"]["selected"]
            and item["availability"]["state"] == "available"
            for item in projected_checks
        )
        enabled = bool(effective and effective.is_enabled)
        if enabled and not selected_available:
            issues.append(
                {
                    "code": "selected_check_unavailable",
                    "activation_id": None if effective is None else effective.id,
                }
            )
        if required and (not enabled or not selected_available):
            operational_state = "blocked_required_check"
        elif enabled and selected_available:
            operational_state = "active"
        elif enabled:
            operational_state = "unavailable"
        else:
            operational_state = "inactive"
        activation_projection: dict[str, Any] = {
            "id": None if effective is None else effective.id,
            "enabled": enabled,
            "required": required,
            "locked": required,
            "scope": effective_scope,
            "origin": None if effective is None else effective.origin,
            "criterion_check_binding_id": (
                None
                if effective is None
                else effective.criterion_check_binding_id
            ),
            "selected_check_available": selected_available,
            "authorized_by": (
                None
                if effective is None
                else _actor_projection(
                    kind=effective.created_by_kind,
                    ref=effective.created_by_ref,
                    meta_json=effective.created_by_meta_json,
                )
            ),
        }
        projected.append(
            {
                "id": criterion.id,
                "stable_key": criterion.stable_key,
                "version": criterion.version,
                "title": criterion.title,
                "description": criterion.description,
                "kind": criterion.criterion_kind,
                "author_origin": {
                    "definition_origin": criterion.origin,
                    "author": _actor_projection(
                        kind=criterion.created_by_kind,
                        ref=criterion.created_by_ref,
                        meta_json=criterion.created_by_meta_json,
                    ),
                },
                "effective_activation": activation_projection,
                "mechanism_availability": {
                    "state": "available" if available else "unavailable",
                    "available_check_count": sum(
                        item["availability"]["state"] == "available"
                        for item in projected_checks
                    ),
                    "total_check_count": len(projected_checks),
                },
                "operational_state": operational_state,
                "checks": projected_checks,
                "issues": issues,
            }
        )
    return {
        "schema": CONFIGURATION_SCHEMA,
        "document_id": document_id,
        "execution_plan": verify_execution_disclosure_plan(
            execution_selection
        ),
        "coordination": {
            "deprecated": True,
            "authoritative_projection": "execution_plan",
            "required": True,
            "selection": "explicit_provider_and_model_at_run_start",
            # Preserve the v1 compatibility token. The authoritative
            # execution_plan above carries the newer normalized boundary.
            "content_boundary": "complete_permitted_frozen_document",
            "egress_class": "account_backed_agent",
            "external_egress": True,
            # Compatibility only. This is the bounded launch-budget request,
            # not a provider-independent guarantee. Consumers must use the
            # authoritative execution_plan cost-control projection above.
            "cost_ceiling_usd_per_worker": MAX_VERIFY_JOB_BUDGET_USD,
            "cost_ceiling_semantics": (
                "requested_launch_budget_not_provider_guarantee"
            ),
            "separate_reviser_for_findings": True,
            "pattern": "coordinator_then_optional_reviser_then_coordinator",
            "base_worker_calls": 1,
            "maximum_worker_calls": 3,
        },
        "criteria": projected,
    }


def list_effective_verification_configuration(
    store: TruthStore,
    *,
    document_id: str,
    ensure_system_defaults: bool = True,
    document: DocumentRecord | None = None,
    execution_selection: AgentExecutionSelection | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Return the wire-ready effective criterion/check configuration."""

    current_document = document or documents.get_document(
        store,
        document_id,
        conn=conn,
    )
    if current_document.id != document_id:
        raise VerifyInvariantViolation(
            "verification configuration document binding is invalid"
        )
    defaults = None
    if ensure_system_defaults:
        seed_terminology_exact_match(store)
    else:
        defaults = terminology_exact_match_defaults()
    if conn is not None:
        return _configuration_projection(
            store,
            current_document.id,
            conn=conn,
            system_defaults=defaults,
            execution_selection=execution_selection,
        )
    with store._read_connection() as read_conn:
        return _configuration_projection(
            store,
            current_document.id,
            conn=read_conn,
            system_defaults=defaults,
            execution_selection=execution_selection,
        )


def set_document_criterion_enabled(
    store: TruthStore,
    *,
    document_id: str,
    criterion_key: str,
    enabled: bool,
    actor: Actor,
    expected_activation_id: str | None | object = _EXPECTED_ACTIVATION_UNSET,
    at: str | None = None,
) -> dict[str, Any]:
    """Append a human-authorized document override unless state is unchanged."""

    if actor.kind != "human":
        raise VerifyInvariantViolation(
            "document criterion overrides require a human actor"
        )
    if not isinstance(criterion_key, str) or not criterion_key.strip():
        raise VerifyInvariantViolation("criterion_key must be nonempty")
    if not isinstance(enabled, bool):
        raise VerifyInvariantViolation("enabled must be a boolean")
    documents.get_document(store, document_id)
    seed_terminology_exact_match(store)
    created_at = _timestamp(at)
    actor_kind, actor_ref, actor_meta = _actor_fields(actor)

    with store.write_transaction() as conn:
        document = documents.get_document(store, document_id, conn=conn)
        before = _configuration_projection(store, document.id, conn=conn)
        criterion = next(
            (
                item
                for item in before["criteria"]
                if item["stable_key"] == criterion_key
            ),
            None,
        )
        if criterion is None:
            raise VerifyInvariantViolation(
                f"criterion does not exist: {criterion_key}"
            )
        activation = criterion["effective_activation"]
        if (
            expected_activation_id is not _EXPECTED_ACTIVATION_UNSET
            and activation["id"] != expected_activation_id
        ):
            raise VerifyInvariantViolation(
                "verification configuration changed; reload before trying again"
            )
        if not enabled and activation["required"]:
            raise VerifyInvariantViolation(
                "required criterion cannot be disabled"
            )
        if (
            enabled
            and criterion["mechanism_availability"]["state"] != "available"
        ):
            raise VerifyInvariantViolation(
                "criterion has no admitted available check"
            )
        if activation["enabled"] is enabled:
            return {
                "changed": False,
                "activation_id": activation["id"],
                "configuration": before,
            }
        available_binding = next(
            (
                item["binding"]["id"]
                for item in criterion["checks"]
                if item["availability"]["state"] == "available"
            ),
            None,
        )
        if available_binding is None:
            available_binding = next(
                (
                    item["binding"]["id"]
                    for item in criterion["checks"]
                ),
                None,
            )
        if available_binding is None:
            raise VerifyInvariantViolation(
                "criterion has no admitted criterion-check binding"
            )
        scope = {"kind": "document", "document_id": document.id}
        payload = {
            "criterion_definition_version_id": criterion["id"],
            "criterion_check_binding_id": available_binding,
            "scope": scope,
            "is_enabled": enabled,
            "is_required": False,
            "origin": _USER_ORIGIN,
        }
        record = CriterionActivation(
            id=new_id(),
            criterion_definition_version_id=criterion["id"],
            criterion_check_binding_id=available_binding,
            scope_json=canonical_json(scope),
            is_enabled=int(enabled),
            is_required=0,
            origin=_USER_ORIGIN,
            canonical_sha256=sha256_text(canonical_json(payload)),
            created_at=created_at,
            created_by_kind=actor_kind,
            created_by_ref=actor_ref,
            created_by_meta_json=actor_meta,
        )
        verify_store.insert_record(store, record, conn=conn)
        after = _configuration_projection(store, document.id, conn=conn)
        return {
            "changed": True,
            "activation_id": record.id,
            "configuration": after,
        }


def create_user_criterion_draft(
    store: TruthStore,
    *,
    document_id: str,
    title: str,
    description: str,
    evaluation_instructions: str,
    limitations: list[str] | tuple[str, ...] = (),
    actor: Actor,
    at: str | None = None,
) -> dict[str, Any]:
    """Persist an honest user-authored criterion with an unadmitted checker.

    Saving the draft records authorship and proposed evaluation semantics. It
    does not grant model-call authority or pretend the generated checker is
    admitted; the criterion therefore remains visibly unavailable until a
    separately reviewed executor version is installed.
    """

    if actor.kind != "human":
        raise VerifyInvariantViolation(
            "user criterion drafts require a human actor"
        )
    title_value = title.strip() if isinstance(title, str) else ""
    description_value = (
        description.strip() if isinstance(description, str) else ""
    )
    instructions_value = (
        evaluation_instructions.strip()
        if isinstance(evaluation_instructions, str)
        else ""
    )
    if not title_value:
        raise VerifyInvariantViolation("criterion title must be nonempty")
    if not description_value:
        raise VerifyInvariantViolation(
            "criterion description must be nonempty"
        )
    if not instructions_value:
        raise VerifyInvariantViolation(
            "evaluation instructions must be nonempty"
        )
    if (
        len(instructions_value)
        > MAX_USER_CHECK_EVALUATION_INSTRUCTIONS_CHARS
    ):
        raise VerifyInvariantViolation(
            "evaluation instructions exceed the supported "
            f"{MAX_USER_CHECK_EVALUATION_INSTRUCTIONS_CHARS}-character boundary"
        )
    if len(title_value) > 160 or len(description_value) > 2000:
        raise VerifyInvariantViolation("criterion draft is too long")
    normalized_limitations = [
        item.strip()
        for item in limitations
        if isinstance(item, str) and item.strip()
    ]
    if len(normalized_limitations) > 20 or any(
        len(item) > 500 for item in normalized_limitations
    ):
        raise VerifyInvariantViolation(
            "criterion limitations exceed the supported draft boundary"
        )
    document = documents.get_document(store, document_id)
    seed_terminology_exact_match(store)
    created_at = _timestamp(at)
    actor_kind, actor_ref, actor_meta = _actor_fields(actor)
    identity_payload = {
        "document_id": document.id,
        "title": title_value,
        "description": description_value,
        "evaluation_instructions": instructions_value,
        "limitations": normalized_limitations,
        "author_ref": actor.ref,
    }
    identity = sha256_text(canonical_json(identity_payload))
    stable_key = f"user_criterion_{identity[:16]}"
    criterion_payload = {
        "stable_key": stable_key,
        "version": 1,
        "title": title_value,
        "description": description_value,
        "criterion_kind": "user_authored",
        "origin": _USER_ORIGIN,
        "configuration_schema": {
            "type": "object",
            "required": ["evaluation_instructions"],
        },
    }
    criterion = CriterionDefinitionVersion(
        id=sha256_text(f"criterion:{identity}")[:32],
        stable_key=stable_key,
        version=1,
        title=title_value,
        description=description_value,
        criterion_kind="user_authored",
        origin=_USER_ORIGIN,
        configuration_schema_json=canonical_json(
            criterion_payload["configuration_schema"]
        ),
        canonical_sha256=sha256_text(canonical_json(criterion_payload)),
        created_at=created_at,
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    check_payload = {
        "stable_key": f"{stable_key}_proposed_checker",
        "version": 1,
        "title": f"Proposed checker for {title_value}",
        "mechanism": "model_judge_draft",
        "executor_ref": "unadmitted:user-authored-checker",
        "supported_criterion_kinds": ["user_authored"],
        "input_schema": {
            "type": "object",
            "required": ["frozen_target", "evaluation_instructions"],
        },
        "output_schema": {
            "type": "object",
            "required": ["outcome", "rationale"],
        },
        "limitations": normalized_limitations
        or [
            "This is a proposed checker definition, not an admitted executor.",
        ],
        "origin": _USER_ORIGIN,
    }
    check = CheckDefinitionVersion(
        id=sha256_text(f"check:{identity}")[:32],
        stable_key=check_payload["stable_key"],
        version=1,
        title=check_payload["title"],
        mechanism=check_payload["mechanism"],
        executor_ref=check_payload["executor_ref"],
        supported_criterion_kinds_json=canonical_json(
            check_payload["supported_criterion_kinds"]
        ),
        input_schema_json=canonical_json(check_payload["input_schema"]),
        output_schema_json=canonical_json(check_payload["output_schema"]),
        limitations_json=canonical_json(check_payload["limitations"]),
        origin=_USER_ORIGIN,
        canonical_sha256=sha256_text(canonical_json(check_payload)),
        created_at=created_at,
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    binding_payload = {
        "criterion_definition_version_id": criterion.id,
        "check_definition_version_id": check.id,
        "configuration": {
            "evaluation_instructions": instructions_value,
            "status": "draft_unadmitted",
        },
    }
    binding = CriterionCheckBinding(
        id=sha256_text(f"binding:{identity}")[:32],
        criterion_definition_version_id=criterion.id,
        check_definition_version_id=check.id,
        configuration_json=canonical_json(binding_payload["configuration"]),
        canonical_sha256=sha256_text(canonical_json(binding_payload)),
        created_at=created_at,
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    scope = {"kind": "document", "document_id": document.id}
    activation_payload = {
        "criterion_definition_version_id": criterion.id,
        "criterion_check_binding_id": binding.id,
        "scope": scope,
        "is_enabled": False,
        "is_required": False,
        "origin": _USER_ORIGIN,
    }
    activation = CriterionActivation(
        id=sha256_text(f"activation:{identity}")[:32],
        criterion_definition_version_id=criterion.id,
        criterion_check_binding_id=binding.id,
        scope_json=canonical_json(scope),
        is_enabled=0,
        is_required=0,
        origin=_USER_ORIGIN,
        canonical_sha256=sha256_text(canonical_json(activation_payload)),
        created_at=created_at,
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    with store.write_transaction() as conn:
        for record in (criterion, check, binding, activation):
            existing = verify_store.get_by_canonical_sha256(
                store,
                type(record),
                record.canonical_sha256,
                conn=conn,
            )
            if existing is None:
                verify_store.insert_record(store, record, conn=conn)
        configuration = _configuration_projection(
            store,
            document.id,
            conn=conn,
        )
    return {
        "criterion_key": stable_key,
        "status": "draft_unadmitted",
        "configuration": configuration,
    }


def create_user_verification_check(
    store: TruthStore,
    *,
    document_id: str,
    title: str,
    description: str,
    evaluation_instructions: str,
    limitations: list[str] | tuple[str, ...] = (),
    actor: Actor,
    at: str | None = None,
) -> dict[str, Any]:
    """Create one runnable user-authored criterion from admitted building blocks.

    The user authors declarative evaluation semantics only. Execution remains
    bound to the immutable, system-owned instruction-model check, so creating a
    criterion cannot introduce or admit executable code.
    """

    if actor.kind != "human":
        raise VerifyInvariantViolation(
            "user verification checks require a human actor"
        )
    title_value = title.strip() if isinstance(title, str) else ""
    description_value = (
        description.strip() if isinstance(description, str) else ""
    )
    instructions_value = (
        evaluation_instructions.strip()
        if isinstance(evaluation_instructions, str)
        else ""
    )
    if not title_value:
        raise VerifyInvariantViolation("criterion title must be nonempty")
    if not description_value:
        raise VerifyInvariantViolation(
            "criterion description must be nonempty"
        )
    if not instructions_value:
        raise VerifyInvariantViolation(
            "evaluation instructions must be nonempty"
        )
    if (
        len(instructions_value)
        > MAX_USER_CHECK_EVALUATION_INSTRUCTIONS_CHARS
    ):
        raise VerifyInvariantViolation(
            "evaluation instructions exceed the supported "
            f"{MAX_USER_CHECK_EVALUATION_INSTRUCTIONS_CHARS}-character boundary"
        )
    if len(title_value) > 160 or len(description_value) > 2000:
        raise VerifyInvariantViolation("criterion draft is too long")
    normalized_limitations = [
        item.strip()
        for item in limitations
        if isinstance(item, str) and item.strip()
    ]
    if len(normalized_limitations) > 20 or any(
        len(item) > 500 for item in normalized_limitations
    ):
        raise VerifyInvariantViolation(
            "criterion limitations exceed the supported draft boundary"
        )

    document = documents.get_document(store, document_id)
    created_at = _timestamp(at)
    seed_terminology_exact_match(store)
    admitted_check = seed_instruction_model_check(store, at=created_at)
    actor_kind, actor_ref, actor_meta = _actor_fields(actor)
    identity_payload = {
        "document_id": document.id,
        "title": title_value,
        "description": description_value,
        "evaluation_instructions": instructions_value,
        "limitations": normalized_limitations,
        "author_ref": actor.ref,
    }
    identity = sha256_text(canonical_json(identity_payload))
    # Keep runnable criteria in a distinct stable-key namespace so an
    # identical legacy unadmitted draft cannot shadow this admitted version.
    stable_key = f"user_check_{identity[:16]}"
    criterion_payload = {
        "stable_key": stable_key,
        "version": 1,
        "title": title_value,
        "description": description_value,
        "criterion_kind": "user_authored",
        "origin": _USER_ORIGIN,
        "configuration_schema": {
            "type": "object",
            "required": ["evaluation_instructions"],
            "properties": {
                "evaluation_instructions": {"type": "string"},
                "limitations": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
    }
    criterion = CriterionDefinitionVersion(
        id=sha256_text(f"criterion:runnable:{identity}")[:32],
        stable_key=stable_key,
        version=1,
        title=title_value,
        description=description_value,
        criterion_kind="user_authored",
        origin=_USER_ORIGIN,
        configuration_schema_json=canonical_json(
            criterion_payload["configuration_schema"]
        ),
        canonical_sha256=sha256_text(canonical_json(criterion_payload)),
        created_at=created_at,
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    binding_configuration = {
        "evaluation_instructions": instructions_value,
        "limitations": normalized_limitations,
    }
    binding_payload = {
        "criterion_definition_version_id": criterion.id,
        "check_definition_version_id": admitted_check.id,
        "configuration": binding_configuration,
    }
    binding = CriterionCheckBinding(
        id=sha256_text(f"binding:runnable:{identity}")[:32],
        criterion_definition_version_id=criterion.id,
        check_definition_version_id=admitted_check.id,
        configuration_json=canonical_json(binding_configuration),
        canonical_sha256=sha256_text(canonical_json(binding_payload)),
        created_at=created_at,
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    scope = {"kind": "document", "document_id": document.id}
    activation_payload = {
        "criterion_definition_version_id": criterion.id,
        "criterion_check_binding_id": binding.id,
        "scope": scope,
        "is_enabled": True,
        "is_required": False,
        "origin": _USER_ORIGIN,
    }
    activation = CriterionActivation(
        id=sha256_text(f"activation:runnable:{identity}")[:32],
        criterion_definition_version_id=criterion.id,
        criterion_check_binding_id=binding.id,
        scope_json=canonical_json(scope),
        is_enabled=1,
        is_required=0,
        origin=_USER_ORIGIN,
        canonical_sha256=sha256_text(canonical_json(activation_payload)),
        created_at=created_at,
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    created = False
    with store.write_transaction() as conn:
        for record in (criterion, binding, activation):
            existing = verify_store.get_by_canonical_sha256(
                store,
                type(record),
                record.canonical_sha256,
                conn=conn,
            )
            if existing is None:
                verify_store.insert_record(store, record, conn=conn)
                created = True
        configuration = _configuration_projection(
            store,
            document.id,
            conn=conn,
        )
    return {
        "criterion_key": stable_key,
        "status": "active",
        "created": created,
        "configuration": configuration,
    }


__all__ = [
    "CONFIGURATION_SCHEMA",
    "MAX_USER_CHECK_EVALUATION_INSTRUCTIONS_CHARS",
    "create_user_criterion_draft",
    "create_user_verification_check",
    "list_effective_verification_configuration",
    "set_document_criterion_enabled",
]

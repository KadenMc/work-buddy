"""Per-document Truth eligibility, activation, and admission authority.

Co-work document durability and provenance exist independently from Truth.  This
module is the narrow policy seam that decides whether one document may observe
or mutate the scoped claim/evidence ledger.  Eligibility is frozen in an
immutable interaction-contract assignment; activation is append-only and the
current rows are rebuildable projections.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.identity import canonical_json, sha256_text, utc_now

if TYPE_CHECKING:  # pragma: no cover
    from work_buddy.truth.store import TruthStore


ELIGIBILITIES = frozenset({"unsupported", "allowed", "required"})
ACTIVATION_STATES = frozenset({"disabled", "enabled", "paused"})
SEAL_STATES = frozenset({"pending", "committed", "aborted"})

WORKING_DOCUMENT_CONTRACT = "working_document"
TRUTH_DOCUMENT_CONTRACT = "truth_document"
PROVENANCE_DOCUMENT_CONTRACT = "provenance_document"
LEGACY_FULL_COWORK_CONTRACT = "legacy_full_cowork"
CONTRACT_VERSION = 1


class TruthActivationError(InvariantViolation):
    """A document Truth policy or transition failed closed."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 409,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.details = dict(details or {})


def _document_definition(
    *,
    behavior_id: str,
    purpose: str,
    authorship: str,
    truth_eligibility: str,
    activation_policy: str,
) -> dict[str, Any]:
    return {
        "schema": "wb.content-interaction-contract/v1",
        "behavior_id": f"{behavior_id}/v1",
        "definition_version": CONTRACT_VERSION,
        "purpose": purpose,
        "value_shape": "rich_document",
        "authority": "cowork_document",
        "authorship": authorship,
        "ai": {
            "read_policy": "workflow_disclosure",
            "contribution": "coedit_document",
            "trigger_policy": "manual_only",
            "disclosure_policy_id": "cowork_document/v1",
            "context_budget_id": "cowork_document/v1",
            "provider_entitlement_id": "cowork_document/v1",
        },
        "persistence": "document_head",
        "lineage": "document_spans",
        "truth": {
            "eligibility": truth_eligibility,
            "activation_policy": activation_policy,
        },
        "search": "content",
        "review": "review_proposals",
        "retention_policy_id": "cowork_document/v1",
        "privacy_class": "domain_inherited",
    }


_CONTRACT_DEFINITIONS: dict[tuple[str, int], dict[str, Any]] = {
    (WORKING_DOCUMENT_CONTRACT, CONTRACT_VERSION): _document_definition(
        behavior_id=WORKING_DOCUMENT_CONTRACT,
        purpose="An evolving, provenance-aware working document.",
        authorship="mixed",
        truth_eligibility="allowed",
        activation_policy="explicit_opt_in",
    ),
    (TRUTH_DOCUMENT_CONTRACT, CONTRACT_VERSION): _document_definition(
        behavior_id=TRUTH_DOCUMENT_CONTRACT,
        purpose="An evidence-backed claim and decision document.",
        authorship="mixed",
        truth_eligibility="required",
        activation_policy="required_explicit_create",
    ),
    (PROVENANCE_DOCUMENT_CONTRACT, CONTRACT_VERSION): _document_definition(
        behavior_id=PROVENANCE_DOCUMENT_CONTRACT,
        purpose="A rich document with provenance and no Truth capability.",
        authorship="mixed",
        truth_eligibility="unsupported",
        activation_policy="not_applicable",
    ),
    (LEGACY_FULL_COWORK_CONTRACT, CONTRACT_VERSION): _document_definition(
        behavior_id=LEGACY_FULL_COWORK_CONTRACT,
        purpose="Compatibility contract for documents created before per-document Truth policy.",
        authorship="mixed",
        truth_eligibility="allowed",
        activation_policy="explicit_opt_in",
    ),
}


@dataclass(frozen=True, slots=True)
class InteractionContract:
    contract_id: str
    version: int
    definition: Mapping[str, Any]
    definition_sha256: str

    @property
    def eligibility(self) -> str:
        return str(self.definition["truth"]["eligibility"])

    @property
    def wire_id(self) -> str:
        return f"{self.contract_id}/v{self.version}"


@dataclass(frozen=True, slots=True)
class DocumentTruthPolicy:
    store_id: str
    document_id: str
    binding_id: str | None
    interaction_contract_id: str
    interaction_contract_version: int
    interaction_contract_sha256: str
    cowork_document_class: str
    eligibility: str
    activation_state: str | None
    activation_revision: int | None
    admission_state: str | None
    admission_seal_revision: int | None
    coordinator_decision_id: str | None
    coordinator_decision_sha256: str | None
    policy_fingerprint: str
    provenance_enabled: bool
    truth_observable: bool
    truth_mutable: bool
    truth_analysis_available: bool
    ledger_present: bool
    recovery_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "wb.document-truth-policy/v1",
            "store_id": self.store_id,
            "document_id": self.document_id,
            "binding_id": self.binding_id,
            "interaction_contract": {
                "id": self.interaction_contract_id,
                "version": self.interaction_contract_version,
                "definition_sha256": self.interaction_contract_sha256,
            },
            "cowork_document_class": self.cowork_document_class,
            "eligibility": self.eligibility,
            "activation": (
                None
                if self.activation_state is None
                else {
                    "state": self.activation_state,
                    "revision": self.activation_revision,
                }
            ),
            "admission": (
                None
                if self.admission_state is None
                else {
                    "state": self.admission_state,
                    "seal_revision": self.admission_seal_revision,
                    "coordinator_decision_id": self.coordinator_decision_id,
                    "coordinator_decision_sha256": self.coordinator_decision_sha256,
                }
            ),
            "policy_fingerprint": self.policy_fingerprint,
            "capabilities": {
                "provenance": self.provenance_enabled,
                "truth_observe": self.truth_observable,
                "truth_mutate": self.truth_mutable,
                "truth_analysis": self.truth_analysis_available,
            },
            "ledger_present": self.ledger_present,
            "recovery_reason": self.recovery_reason,
        }

    def capability_envelope(self) -> dict[str, Any]:
        """Return the compact server-authoritative editor composition."""

        return {
            "schema": "wb.cowork-document-capabilities/v1",
            "interaction_contract": {
                "contract_id": self.interaction_contract_id,
                "version": self.interaction_contract_version,
                "digest": self.interaction_contract_sha256 or None,
            },
            "modules": {
                "review": True,
                "provenance": self.provenance_enabled,
                "chat": True,
                "truth": self.truth_observable,
            },
            "truth": {
                "eligibility": self.eligibility,
                "activation": self.activation_state,
                "activation_revision": self.activation_revision,
                "policy_fingerprint": self.policy_fingerprint,
                "ledger_present": self.ledger_present,
                "unavailable_reason": self.recovery_reason,
            },
        }


def _stable_id(domain: str, value: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json({"domain": domain, **dict(value)}))[:32]


def interaction_contract(contract_id: str, version: int = CONTRACT_VERSION) -> InteractionContract:
    key = (str(contract_id).strip(), int(version))
    definition = _CONTRACT_DEFINITIONS.get(key)
    if definition is None:
        raise TruthActivationError(
            "unknown_interaction_contract",
            f"Unknown interaction contract {key[0]!r} version {key[1]}.",
            status=400,
        )
    normalized = json.loads(canonical_json(definition))
    digest = sha256_text(canonical_json(normalized))
    return InteractionContract(key[0], key[1], normalized, digest)


def registered_interaction_contracts() -> tuple[InteractionContract, ...]:
    return tuple(interaction_contract(*key) for key in sorted(_CONTRACT_DEFINITIONS))


def _actor_ref(actor: Actor | str) -> str:
    if isinstance(actor, Actor):
        value = actor.ref or actor.kind
    else:
        value = str(actor)
    normalized = value.strip()
    if not normalized:
        raise TruthActivationError("actor_required", "A policy actor is required.", status=400)
    return normalized


def _insert_ledger(conn: sqlite3.Connection, record_type: str, record_key: str) -> None:
    conn.execute(
        "INSERT INTO ledger_records(record_type, record_key) VALUES (?, ?)",
        (record_type, record_key),
    )


def register_contract_definitions_locked(
    conn: sqlite3.Connection, *, at: str | None = None
) -> None:
    timestamp = at or utc_now()
    for contract in registered_interaction_contracts():
        payload = {
            "contract_id": contract.contract_id,
            "definition_version": contract.version,
            "definition_sha256": contract.definition_sha256,
        }
        record_id = _stable_id("work-buddy.interaction-contract-definition/v1", payload)
        existing = conn.execute(
            "SELECT * FROM interaction_contract_definitions "
            "WHERE contract_id = ? AND definition_version = ?",
            (contract.contract_id, contract.version),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["definition_sha256"]) != contract.definition_sha256
                or str(existing["definition_json"]) != canonical_json(contract.definition)
            ):
                raise TruthActivationError(
                    "interaction_contract_mismatch",
                    f"Stored interaction contract {contract.wire_id} does not match this runtime.",
                )
            continue
        conn.execute(
            "INSERT INTO interaction_contract_definitions "
            "(id, contract_id, definition_version, definition_json, "
            "definition_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                record_id,
                contract.contract_id,
                contract.version,
                canonical_json(contract.definition),
                contract.definition_sha256,
                timestamp,
            ),
        )
        _insert_ledger(conn, "interaction_contract_definition", record_id)


def _definition_from_assignment(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> InteractionContract:
    contract = interaction_contract(
        str(row["interaction_contract_id"]),
        int(row["interaction_contract_version"]),
    )
    stored = conn.execute(
        "SELECT definition_json, definition_sha256 FROM interaction_contract_definitions "
        "WHERE contract_id = ? AND definition_version = ?",
        (contract.contract_id, contract.version),
    ).fetchone()
    if (
        stored is None
        or str(stored["definition_sha256"]) != contract.definition_sha256
        or str(row["interaction_contract_sha256"]) != contract.definition_sha256
    ):
        raise TruthActivationError(
            "interaction_contract_mismatch",
            "The document's frozen interaction contract cannot be verified.",
        )
    return contract


def _ledger_summary_locked(conn: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    expression_rows = conn.execute(
        "SELECT e.id, e.claim_ref_kind, e.claim_ref, e.document_span_id "
        "FROM expressions e JOIN document_spans s ON s.id = e.document_span_id "
        "WHERE s.document_id = ? ORDER BY e.id",
        (document_id,),
    ).fetchall()
    local_claims = sorted(
        {str(row["claim_ref"]) for row in expression_rows if row["claim_ref_kind"] == "local"}
    )
    identities: set[tuple[str, str]] = {
        ("expression", str(row["id"])) for row in expression_rows
    }
    identities.update(
        ("document_span", str(row["document_span_id"])) for row in expression_rows
    )
    if local_claims:
        marks = ",".join("?" for _ in local_claims)
        for row in conn.execute(
            f"SELECT id FROM claims WHERE id IN ({marks})", tuple(local_claims)
        ):
            identities.add(("claim", str(row["id"])))
        for row in conn.execute(
            f"SELECT id FROM claim_status_events WHERE claim_id IN ({marks})",
            tuple(local_claims),
        ):
            identities.add(("claim_status_event", str(row["id"])))
        link_rows = conn.execute(
            f"SELECT id, to_kind, to_ref FROM claim_links "
            f"WHERE from_claim_id IN ({marks}) ORDER BY id",
            tuple(local_claims),
        ).fetchall()
        for row in link_rows:
            identities.add(("claim_link", str(row["id"])))
            if row["to_kind"] == "evidence_span":
                span = conn.execute(
                    "SELECT id, evidence_id FROM evidence_spans WHERE id = ?",
                    (row["to_ref"],),
                ).fetchone()
                if span is not None:
                    identities.add(("evidence_span", str(span["id"])))
                    identities.add(("evidence", str(span["evidence_id"])))
            elif row["to_kind"] == "evidence":
                identities.add(("evidence", str(row["to_ref"])))
    high_water = 0
    ordered: list[dict[str, Any]] = []
    for record_type, record_key in sorted(identities):
        ledger = conn.execute(
            "SELECT seq FROM ledger_records WHERE record_type = ? AND record_key = ?",
            (record_type, record_key),
        ).fetchone()
        seq = 0 if ledger is None else int(ledger["seq"])
        high_water = max(high_water, seq)
        ordered.append({"record_type": record_type, "record_key": record_key, "seq": seq})
    return {
        "has_ledger": bool(expression_rows),
        "expression_count": len(expression_rows),
        "claim_count": len(local_claims),
        "ledger_high_water_seq": high_water,
        "ledger_digest": sha256_text(canonical_json(ordered)),
    }


def _policy_fingerprint(value: Mapping[str, Any]) -> str:
    return sha256_text(
        canonical_json({"domain": "work-buddy.document-truth-policy/v1", **dict(value)})
    )


def _document_row_locked(
    conn: sqlite3.Connection, document_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT d.id, d.document_class, d.created_at, d.ydoc_snapshot_sha256, "
        "d.meta_json, "
        "(SELECT v.structured_head_sha256 FROM document_versions v "
        "WHERE v.document_id = d.id ORDER BY v.created_at DESC, v.id DESC LIMIT 1) "
        "AS structured_head_sha256 "
        "FROM documents d WHERE d.id = ?",
        (document_id,),
    ).fetchone()


def _task_aggregate_recovery_reason(
    store: "TruthStore",
    *,
    document_id: str,
    binding_id: str | None,
    coordinator_decision_id: str | None,
    coordinator_decision_sha256: str | None,
    meta_json: str | None,
) -> str | None:
    """Fail closed for documents created by the task aggregate coordinator.

    Ordinary and legacy task documents intentionally have no ``task_admission``
    marker and retain the local Truth admission behavior.  A marker is an
    explicit cross-store claim, so malformed metadata, causality drift, or an
    unpublished TaskStore decision makes Truth unavailable until recovery.
    """

    try:
        metadata = json.loads(meta_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return (
            "task_admission_metadata_invalid"
            if "task_admission" in str(meta_json or "")
            else None
        )
    if not isinstance(metadata, dict) or "task_admission" not in metadata:
        return None
    admission = metadata.get("task_admission")
    if not isinstance(admission, dict):
        return "task_admission_metadata_invalid"
    schema = admission.get("schema")
    kind = admission.get("kind")
    task_id = admission.get("task_id")
    if (
        schema != "wb.task-document-admission/v1"
        or kind not in {"aggregate_creation/v2", "document_attachment/v1"}
        or not isinstance(task_id, str)
        or not task_id.strip()
        or metadata.get("domain_content") is not True
        or metadata.get("domain_namespace") != "tasks"
        or metadata.get("domain_entity_id") != task_id
        or not binding_id
    ):
        return "task_admission_metadata_mismatch"

    try:
        from work_buddy.document_kernel.causality import DocumentCausalityStore

        causality_path = store.paths.sidecar / "document-causality.db"
        if not causality_path.is_file():
            return "task_admission_binding_unavailable"
        binding = DocumentCausalityStore(store.paths.sidecar).get_binding(binding_id)
    except Exception:
        return "task_admission_binding_unavailable"
    if (
        binding is None
        or binding.binding_id != binding_id
        or binding.domain_namespace != "tasks"
        or binding.domain_kind != "task_knowledge"
        or binding.domain_entity_id != task_id
        or binding.role != "task_knowledge"
        or binding.store_id != store.store_id
        or binding.document_id != document_id
        or binding.lifecycle != "current"
    ):
        return "task_admission_binding_mismatch"

    # Existing-task attachments use their own admission protocol.  Their exact
    # metadata and current task binding were checked above, but they are not a
    # task-creation publication and must not be looked up in that coordinator.
    if kind == "document_attachment/v1":
        return None
    if not coordinator_decision_id or not coordinator_decision_sha256:
        return "task_creation_decision_unavailable"
    try:
        from work_buddy.tasks import (
            TaskStore,
            verify_published_task_creation_decision,
        )

        verify_published_task_creation_decision(
            TaskStore(),
            task_id=task_id,
            store_id=store.store_id,
            document_id=document_id,
            binding_id=binding_id,
            coordinator_decision_id=coordinator_decision_id,
            coordinator_decision_sha256=coordinator_decision_sha256,
        )
    except Exception:
        return "task_creation_decision_unavailable"
    return None


def _live_document_head(store: "TruthStore", document: sqlite3.Row) -> str | None:
    """Resolve the current structured head while the caller holds document locks."""

    snapshot_sha256 = document["ydoc_snapshot_sha256"]
    if snapshot_sha256 is not None:
        from work_buddy.truth import ydoc_store

        return ydoc_store.current_structured_head(
            store,
            document_id=str(document["id"]),
            snapshot_sha256=str(snapshot_sha256),
        )
    value = document["structured_head_sha256"]
    return None if value is None else str(value)


def _envelope_locked(
    store: "TruthStore", conn: sqlite3.Connection, document_id: str
) -> DocumentTruthPolicy:
    assignment = conn.execute(
        "SELECT * FROM document_interaction_contract_assignments WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    if assignment is None:
        fingerprint = _policy_fingerprint(
            {"store_id": store.store_id, "document_id": document_id, "state": "missing"}
        )
        return DocumentTruthPolicy(
            store_id=store.store_id,
            document_id=document_id,
            binding_id=None,
            interaction_contract_id="",
            interaction_contract_version=0,
            interaction_contract_sha256="",
            cowork_document_class="",
            eligibility="unsupported",
            activation_state=None,
            activation_revision=None,
            admission_state=None,
            admission_seal_revision=None,
            coordinator_decision_id=None,
            coordinator_decision_sha256=None,
            policy_fingerprint=fingerprint,
            provenance_enabled=True,
            truth_observable=False,
            truth_mutable=False,
            truth_analysis_available=False,
            ledger_present=False,
            recovery_reason="truth_policy_missing",
        )
    try:
        contract = _definition_from_assignment(conn, assignment)
    except TruthActivationError as exc:
        fingerprint = _policy_fingerprint(
            {"store_id": store.store_id, "document_id": document_id, "state": exc.code}
        )
        return DocumentTruthPolicy(
            store_id=store.store_id,
            document_id=document_id,
            binding_id=assignment["binding_id"],
            interaction_contract_id=str(assignment["interaction_contract_id"]),
            interaction_contract_version=int(assignment["interaction_contract_version"]),
            interaction_contract_sha256=str(assignment["interaction_contract_sha256"]),
            cowork_document_class=str(assignment["cowork_document_class"]),
            eligibility="unsupported",
            activation_state=None,
            activation_revision=None,
            admission_state=None,
            admission_seal_revision=None,
            coordinator_decision_id=None,
            coordinator_decision_sha256=None,
            policy_fingerprint=fingerprint,
            provenance_enabled=True,
            truth_observable=False,
            truth_mutable=False,
            truth_analysis_available=False,
            ledger_present=False,
            recovery_reason=exc.code,
        )
    activation = conn.execute(
        "SELECT * FROM document_truth_activation_current WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    seal = conn.execute(
        "SELECT * FROM document_truth_admission_seals_current WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    receipt = conn.execute(
        "SELECT * FROM document_truth_policy_receipts WHERE document_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (document_id,),
    ).fetchone()
    recovery_reason: str | None = None
    ledger = _ledger_summary_locked(conn, document_id)
    state = None if activation is None else str(activation["state"])
    revision = None if activation is None else int(activation["activation_revision"])
    seal_state = None if seal is None else str(seal["state"])
    if contract.eligibility == "unsupported":
        if activation is not None:
            recovery_reason = "unsupported_with_activation"
        elif ledger["has_ledger"]:
            recovery_reason = "unsupported_with_ledger"
        elif receipt is None or str(receipt["outcome"]) != "not_applicable":
            recovery_reason = "not_applicable_receipt_missing"
        elif str(receipt["interaction_contract_sha256"]) != contract.definition_sha256:
            recovery_reason = "not_applicable_receipt_mismatch"
    else:
        if activation is None:
            recovery_reason = "activation_missing"
        elif seal is None or seal_state != "committed":
            recovery_reason = "admission_seal_missing"
        elif state == "disabled" and ledger["has_ledger"]:
            recovery_reason = "disabled_with_ledger"
        if recovery_reason is None:
            document = _document_row_locked(conn, document_id)
            if document is None:
                recovery_reason = "document_not_found"
            else:
                recovery_reason = _task_aggregate_recovery_reason(
                    store,
                    document_id=document_id,
                    binding_id=(
                        None
                        if assignment["binding_id"] is None
                        else str(assignment["binding_id"])
                    ),
                    coordinator_decision_id=(
                        None
                        if seal is None
                        else str(seal["coordinator_decision_id"])
                    ),
                    coordinator_decision_sha256=(
                        None
                        if seal is None
                        else str(seal["coordinator_decision_sha256"])
                    ),
                    meta_json=document["meta_json"],
                )
    admitted = recovery_reason is None and seal_state == "committed"
    observable = admitted and state in {"enabled", "paused"}
    mutable = admitted and state == "enabled"
    fingerprint = _policy_fingerprint(
        {
            "store_id": store.store_id,
            "document_id": document_id,
            "contract_sha256": contract.definition_sha256,
            "activation_state": state,
            "activation_revision": revision,
            "admission_state": seal_state,
            "admission_seal_revision": None if seal is None else int(seal["seal_revision"]),
            "recovery_reason": recovery_reason,
        }
    )
    return DocumentTruthPolicy(
        store_id=store.store_id,
        document_id=document_id,
        binding_id=None if assignment["binding_id"] is None else str(assignment["binding_id"]),
        interaction_contract_id=contract.contract_id,
        interaction_contract_version=contract.version,
        interaction_contract_sha256=contract.definition_sha256,
        cowork_document_class=str(assignment["cowork_document_class"]),
        eligibility=contract.eligibility,
        activation_state=state,
        activation_revision=revision,
        admission_state=seal_state,
        admission_seal_revision=None if seal is None else int(seal["seal_revision"]),
        coordinator_decision_id=(
            None if seal is None else str(seal["coordinator_decision_id"])
        ),
        coordinator_decision_sha256=(
            None if seal is None else str(seal["coordinator_decision_sha256"])
        ),
        policy_fingerprint=fingerprint,
        provenance_enabled=True,
        truth_observable=observable,
        truth_mutable=mutable,
        truth_analysis_available=mutable,
        ledger_present=bool(ledger["has_ledger"]),
        recovery_reason=recovery_reason,
    )


def resolve_document_truth_policy(
    store: "TruthStore",
    document_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> DocumentTruthPolicy:
    if conn is not None:
        return _envelope_locked(store, conn, str(document_id))
    with store._read_connection() as read_conn:
        read_conn.execute("BEGIN")
        try:
            return _envelope_locked(store, read_conn, str(document_id))
        finally:
            if read_conn.in_transaction:
                read_conn.execute("ROLLBACK")


def resolve_document_truth_policy_snapshot(
    store: "TruthStore", document_id: str
) -> tuple[DocumentTruthPolicy, str | None]:
    """Read the policy and current document head under one document barrier."""

    from work_buddy.cowork import lifecycle_lock
    from work_buddy.truth import ydoc_store

    with lifecycle_lock.document_lifecycle_lock(store.store_id, document_id):
        with ydoc_store.document_lock(store, document_id):
            with store._read_connection() as read_conn:
                read_conn.execute("BEGIN")
                try:
                    document = _document_row_locked(read_conn, document_id)
                    if document is None:
                        raise TruthActivationError(
                            "document_not_found", "Document does not exist.", status=404
                        )
                    return (
                        _envelope_locked(store, read_conn, document_id),
                        _live_document_head(store, document),
                    )
                finally:
                    if read_conn.in_transaction:
                        read_conn.execute("ROLLBACK")


def _append_transition_locked(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    previous_state: str | None,
    next_state: str,
    activation_revision: int,
    observed_head: str | None,
    actor_ref: str,
    intent_id: str,
    reason: str | None,
) -> None:
    ledger = _ledger_summary_locked(conn, document_id)
    request = {
        "document_id": document_id,
        "previous_state": previous_state,
        "next_state": next_state,
        "activation_revision": activation_revision,
        "observed_head": observed_head,
        "actor_ref": actor_ref,
        "intent_id": intent_id,
        "reason": reason,
    }
    request_sha256 = sha256_text(canonical_json(request))
    record_id = _stable_id("work-buddy.document-truth-activation/v1", request)
    existing = conn.execute(
        "SELECT request_sha256 FROM document_truth_activation_transitions "
        "WHERE document_id = ? AND intent_id = ?",
        (document_id, intent_id),
    ).fetchone()
    if existing is not None:
        if str(existing["request_sha256"]) != request_sha256:
            raise TruthActivationError(
                "activation_idempotency_conflict",
                "This activation intent was already used for another request.",
            )
        return
    timestamp = utc_now()
    conn.execute(
        "INSERT INTO document_truth_activation_transitions "
        "(id, document_id, activation_revision, previous_state, next_state, "
        "observed_head_sha256, ledger_high_water_seq, ledger_digest, actor_ref, "
        "intent_id, reason, request_sha256, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record_id,
            document_id,
            activation_revision,
            previous_state,
            next_state,
            observed_head,
            ledger["ledger_high_water_seq"],
            ledger["ledger_digest"],
            actor_ref,
            intent_id,
            reason,
            request_sha256,
            timestamp,
        ),
    )
    _insert_ledger(conn, "document_truth_activation_transition", record_id)
    if previous_state is None:
        conn.execute(
            "INSERT INTO document_truth_activation_current "
            "(document_id, activation_revision, state, transition_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (document_id, activation_revision, next_state, record_id, timestamp),
        )
    else:
        cursor = conn.execute(
            "UPDATE document_truth_activation_current SET activation_revision = ?, "
            "state = ?, transition_id = ?, updated_at = ? "
            "WHERE document_id = ? AND activation_revision = ? AND state = ?",
            (
                activation_revision,
                next_state,
                record_id,
                timestamp,
                document_id,
                activation_revision - 1,
                previous_state,
            ),
        )
        if cursor.rowcount != 1:
            raise TruthActivationError(
                "activation_revision_conflict",
                "Truth activation changed before this request committed.",
                retryable=True,
            )


def _append_seal_locked(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    intent_id: str,
    activation_revision: int,
    state: str,
    seal_revision: int,
    coordinator_decision_id: str,
    coordinator_decision_sha256: str,
    actor_ref: str,
) -> None:
    payload = {
        "document_id": document_id,
        "intent_id": intent_id,
        "activation_revision": activation_revision,
        "state": state,
        "seal_revision": seal_revision,
        "coordinator_decision_id": coordinator_decision_id,
        "coordinator_decision_sha256": coordinator_decision_sha256,
        "actor_ref": actor_ref,
    }
    canonical_sha256 = sha256_text(canonical_json(payload))
    record_id = _stable_id("work-buddy.document-truth-admission-seal/v1", payload)
    timestamp = utc_now()
    existing = conn.execute(
        "SELECT canonical_sha256 FROM document_truth_admission_seal_events WHERE id = ?",
        (record_id,),
    ).fetchone()
    if existing is not None:
        if str(existing["canonical_sha256"]) != canonical_sha256:
            raise TruthActivationError("admission_seal_conflict", "Admission seal replay changed.")
        return
    conn.execute(
        "INSERT INTO document_truth_admission_seal_events "
        "(id, document_id, intent_id, activation_revision, state, seal_revision, "
        "coordinator_decision_id, coordinator_decision_sha256, actor_ref, "
        "canonical_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record_id,
            document_id,
            intent_id,
            activation_revision,
            state,
            seal_revision,
            coordinator_decision_id,
            coordinator_decision_sha256,
            actor_ref,
            canonical_sha256,
            timestamp,
        ),
    )
    _insert_ledger(conn, "document_truth_admission_seal_event", record_id)
    current = conn.execute(
        "SELECT seal_revision FROM document_truth_admission_seals_current "
        "WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    if current is None:
        conn.execute(
            "INSERT INTO document_truth_admission_seals_current "
            "(document_id, intent_id, activation_revision, state, seal_revision, "
            "coordinator_decision_id, coordinator_decision_sha256, event_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                document_id,
                intent_id,
                activation_revision,
                state,
                seal_revision,
                coordinator_decision_id,
                coordinator_decision_sha256,
                record_id,
                timestamp,
            ),
        )
    else:
        cursor = conn.execute(
            "UPDATE document_truth_admission_seals_current SET state = ?, "
            "seal_revision = ?, event_id = ?, updated_at = ? "
            "WHERE document_id = ? AND seal_revision = ? "
            "AND intent_id = ? AND coordinator_decision_id = ? "
            "AND coordinator_decision_sha256 = ?",
            (
                state,
                seal_revision,
                record_id,
                timestamp,
                document_id,
                seal_revision - 1,
                intent_id,
                coordinator_decision_id,
                coordinator_decision_sha256,
            ),
        )
        if cursor.rowcount != 1:
            raise TruthActivationError(
                "admission_seal_revision_conflict",
                "Document admission changed before this request committed.",
                retryable=True,
            )


def _append_pending_seal_decision_binding_locked(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    intent_id: str,
    activation_revision: int,
    seal_revision: int,
    provisional_coordinator_decision_id: str,
    provisional_coordinator_decision_sha256: str,
    coordinator_decision_id: str,
    coordinator_decision_sha256: str,
    actor_ref: str,
) -> None:
    payload = {
        "document_id": document_id,
        "intent_id": intent_id,
        "activation_revision": activation_revision,
        "state": "pending",
        "seal_revision": seal_revision,
        "coordinator_decision_id": coordinator_decision_id,
        "coordinator_decision_sha256": coordinator_decision_sha256,
        "actor_ref": actor_ref,
    }
    canonical_sha256 = sha256_text(canonical_json(payload))
    record_id = _stable_id("work-buddy.document-truth-admission-seal/v1", payload)
    timestamp = utc_now()
    existing = conn.execute(
        "SELECT canonical_sha256 FROM document_truth_admission_seal_events WHERE id = ?",
        (record_id,),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO document_truth_admission_seal_events "
            "(id, document_id, intent_id, activation_revision, state, seal_revision, "
            "coordinator_decision_id, coordinator_decision_sha256, actor_ref, "
            "canonical_sha256, created_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                document_id,
                intent_id,
                activation_revision,
                seal_revision,
                coordinator_decision_id,
                coordinator_decision_sha256,
                actor_ref,
                canonical_sha256,
                timestamp,
            ),
        )
        _insert_ledger(conn, "document_truth_admission_seal_event", record_id)
    elif str(existing["canonical_sha256"]) != canonical_sha256:
        raise TruthActivationError(
            "admission_seal_conflict", "Admission decision binding replay changed."
        )
    cursor = conn.execute(
        "UPDATE document_truth_admission_seals_current SET "
        "coordinator_decision_id=?, coordinator_decision_sha256=?, "
        "seal_revision=?, event_id=?, updated_at=? "
        "WHERE document_id=? AND intent_id=? AND activation_revision=? "
        "AND state='pending' AND seal_revision=? "
        "AND coordinator_decision_id=? AND coordinator_decision_sha256=?",
        (
            coordinator_decision_id,
            coordinator_decision_sha256,
            seal_revision,
            record_id,
            timestamp,
            document_id,
            intent_id,
            activation_revision,
            seal_revision - 1,
            provisional_coordinator_decision_id,
            provisional_coordinator_decision_sha256,
        ),
    )
    if cursor.rowcount != 1:
        raise TruthActivationError(
            "admission_seal_revision_conflict",
            "Document admission changed before its final decision was bound.",
            retryable=True,
        )


def provision_document_policy(
    store: "TruthStore",
    *,
    document_id: str,
    interaction_contract_id: str,
    interaction_contract_version: int = CONTRACT_VERSION,
    binding_id: str | None = None,
    initial_activation: str | None = None,
    explicit_truth_acknowledged: bool = False,
    actor: Actor | str,
    intent_id: str,
    coordinator_decision_id: str | None = None,
    coordinator_decision_sha256: str | None = None,
    commit_admission: bool = True,
    conn: sqlite3.Connection | None = None,
) -> DocumentTruthPolicy:
    """Freeze a document contract and stage/commit its initial Truth policy."""

    contract = interaction_contract(interaction_contract_id, interaction_contract_version)
    actor_ref = _actor_ref(actor)
    normalized_intent = str(intent_id).strip()
    if not normalized_intent:
        raise TruthActivationError("intent_required", "A policy intent_id is required.", status=400)
    context = store.write_transaction(conn) if conn is not None else store.write_transaction()
    with context as tx:
        register_contract_definitions_locked(tx)
        document = _document_row_locked(tx, document_id)
        if document is None:
            raise TruthActivationError("document_not_found", "Document does not exist.", status=404)
        assignment_payload = {
            "document_id": document_id,
            "binding_id": binding_id,
            "interaction_contract_id": contract.contract_id,
            "interaction_contract_version": contract.version,
            "interaction_contract_sha256": contract.definition_sha256,
            "cowork_document_class": str(document["document_class"]),
            "intent_id": normalized_intent,
        }
        assignment_id = _stable_id(
            "work-buddy.document-interaction-contract-assignment/v1", assignment_payload
        )
        existing = tx.execute(
            "SELECT * FROM document_interaction_contract_assignments WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        if existing is None:
            tx.execute(
                "INSERT INTO document_interaction_contract_assignments "
                "(id, document_id, binding_id, interaction_contract_id, "
                "interaction_contract_version, interaction_contract_sha256, "
                "cowork_document_class, actor_ref, intent_id, assigned_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    assignment_id,
                    document_id,
                    binding_id,
                    contract.contract_id,
                    contract.version,
                    contract.definition_sha256,
                    document["document_class"],
                    actor_ref,
                    normalized_intent,
                    utc_now(),
                ),
            )
            _insert_ledger(tx, "document_interaction_contract_assignment", assignment_id)
        elif any(
            (
                str(existing["interaction_contract_id"]) != contract.contract_id,
                int(existing["interaction_contract_version"]) != contract.version,
                str(existing["interaction_contract_sha256"]) != contract.definition_sha256,
                str(existing["cowork_document_class"]) != str(document["document_class"]),
                (None if existing["binding_id"] is None else str(existing["binding_id"]))
                != binding_id,
            )
        ):
            raise TruthActivationError(
                "interaction_contract_assignment_conflict",
                "This document already has a different frozen interaction contract.",
            )

        if contract.eligibility == "unsupported":
            if initial_activation is not None:
                raise TruthActivationError(
                    "truth_unsupported",
                    "This interaction contract does not support Truth activation.",
                    status=400,
                )
            receipt_payload = {
                "document_id": document_id,
                "binding_id": binding_id,
                "interaction_contract_id": contract.contract_id,
                "interaction_contract_version": contract.version,
                "interaction_contract_sha256": contract.definition_sha256,
                "outcome": "not_applicable",
                "intent_id": normalized_intent,
                "actor_ref": actor_ref,
            }
            receipt_id = _stable_id("work-buddy.document-truth-policy-receipt/v1", receipt_payload)
            existing_receipt = tx.execute(
                "SELECT request_sha256 FROM document_truth_policy_receipts "
                "WHERE document_id = ? AND intent_id = ?",
                (document_id, normalized_intent),
            ).fetchone()
            request_sha256 = sha256_text(canonical_json(receipt_payload))
            if existing_receipt is None:
                tx.execute(
                    "INSERT INTO document_truth_policy_receipts "
                    "(id, document_id, binding_id, interaction_contract_id, "
                    "interaction_contract_version, interaction_contract_sha256, "
                    "outcome, intent_id, actor_ref, request_sha256, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'not_applicable', ?, ?, ?, ?)",
                    (
                        receipt_id,
                        document_id,
                        binding_id,
                        contract.contract_id,
                        contract.version,
                        contract.definition_sha256,
                        normalized_intent,
                        actor_ref,
                        request_sha256,
                        utc_now(),
                    ),
                )
                _insert_ledger(tx, "document_truth_policy_receipt", receipt_id)
            elif str(existing_receipt["request_sha256"]) != request_sha256:
                raise TruthActivationError(
                    "policy_idempotency_conflict",
                    "This policy intent was already used for another request.",
                )
            return _envelope_locked(store, tx, document_id)

        state = initial_activation or ("disabled" if contract.eligibility == "allowed" else None)
        if state not in ACTIVATION_STATES:
            raise TruthActivationError(
                "initial_activation_required",
                "An eligible document requires a valid initial Truth activation.",
                status=400,
            )
        if contract.eligibility == "required" and (
            state != "enabled" or not explicit_truth_acknowledged
        ):
            raise TruthActivationError(
                "truth_acknowledgement_required",
                "A Truth-centered document must be explicitly created with Truth enabled.",
                status=400,
            )
        if contract.eligibility == "allowed" and state == "paused":
            raise TruthActivationError(
                "invalid_initial_activation",
                "An allowed document cannot start paused without a retained ledger.",
                status=400,
            )
        current = tx.execute(
            "SELECT * FROM document_truth_activation_current WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        if current is None:
            _append_transition_locked(
                tx,
                document_id=document_id,
                previous_state=None,
                next_state=state,
                activation_revision=1,
                observed_head=_live_document_head(store, document),
                actor_ref=actor_ref,
                intent_id=normalized_intent,
                reason="document_provisioning",
            )
        elif str(current["state"]) != state:
            raise TruthActivationError(
                "activation_provisioning_conflict",
                "This document was already provisioned with another Truth state.",
            )
        decision_payload = {
            "document_id": document_id,
            "intent_id": normalized_intent,
            "contract_sha256": contract.definition_sha256,
            "activation_state": state,
            "activation_revision": 1,
        }
        decision_id = coordinator_decision_id or _stable_id(
            "work-buddy.document-provisioning-decision/v1", decision_payload
        )
        decision_sha = coordinator_decision_sha256 or sha256_text(
            canonical_json(decision_payload)
        )
        seal = tx.execute(
            "SELECT * FROM document_truth_admission_seals_current WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        if seal is None:
            _append_seal_locked(
                tx,
                document_id=document_id,
                intent_id=normalized_intent,
                activation_revision=1,
                state="pending",
                seal_revision=1,
                coordinator_decision_id=decision_id,
                coordinator_decision_sha256=decision_sha,
                actor_ref=actor_ref,
            )
            seal_revision = 1
        else:
            seal_revision = int(seal["seal_revision"])
            if (
                str(seal["intent_id"]) != normalized_intent
                or str(seal["coordinator_decision_id"]) != decision_id
                or str(seal["coordinator_decision_sha256"]) != decision_sha
            ):
                raise TruthActivationError(
                    "admission_seal_conflict",
                    "This document has another provisioning decision.",
                )
        if commit_admission and str(
            tx.execute(
                "SELECT state FROM document_truth_admission_seals_current WHERE document_id = ?",
                (document_id,),
            ).fetchone()["state"]
        ) == "pending":
            _append_seal_locked(
                tx,
                document_id=document_id,
                intent_id=normalized_intent,
                activation_revision=1,
                state="committed",
                seal_revision=seal_revision + 1,
                coordinator_decision_id=decision_id,
                coordinator_decision_sha256=decision_sha,
                actor_ref=actor_ref,
            )
        return _envelope_locked(store, tx, document_id)


def bind_pending_document_admission_decision(
    store: "TruthStore",
    *,
    document_id: str,
    intent_id: str,
    expected_seal_revision: int,
    provisional_coordinator_decision_id: str,
    provisional_coordinator_decision_sha256: str,
    coordinator_decision_id: str,
    coordinator_decision_sha256: str,
    actor: Actor | str,
    conn: sqlite3.Connection | None = None,
) -> DocumentTruthPolicy:
    """Append-bind one staged seal to the coordinator's final aggregate decision."""

    values = {
        "intent_id": str(intent_id).strip(),
        "provisional_coordinator_decision_id": str(
            provisional_coordinator_decision_id
        ).strip(),
        "coordinator_decision_id": str(coordinator_decision_id).strip(),
    }
    if not all(values.values()):
        raise TruthActivationError(
            "admission_decision_invalid",
            "Admission decision identities must not be empty.",
            status=400,
        )
    for digest in (
        provisional_coordinator_decision_sha256,
        coordinator_decision_sha256,
    ):
        if len(str(digest)) != 64 or any(
            character not in "0123456789abcdef" for character in str(digest)
        ):
            raise TruthActivationError(
                "admission_decision_invalid",
                "Admission decision digests must be lowercase SHA-256 values.",
                status=400,
            )
    if isinstance(expected_seal_revision, bool) or int(expected_seal_revision) < 1:
        raise TruthActivationError(
            "admission_seal_revision_conflict",
            "A positive expected seal revision is required.",
            status=400,
        )
    actor_ref = _actor_ref(actor)
    context = store.write_transaction(conn) if conn is not None else store.write_transaction()
    with context as tx:
        seal = tx.execute(
            "SELECT * FROM document_truth_admission_seals_current WHERE document_id=?",
            (document_id,),
        ).fetchone()
        if seal is None or str(seal["intent_id"]) != values["intent_id"]:
            raise TruthActivationError(
                "admission_seal_revision_conflict",
                "The staged admission seal is unavailable or belongs to another intent.",
                retryable=True,
            )
        expected = int(expected_seal_revision)
        current_revision = int(seal["seal_revision"])
        final_matches = (
            str(seal["coordinator_decision_id"])
            == values["coordinator_decision_id"]
            and str(seal["coordinator_decision_sha256"])
            == coordinator_decision_sha256
        )
        if current_revision == expected + 1 and final_matches:
            prior = tx.execute(
                "SELECT * FROM document_truth_admission_seal_events "
                "WHERE document_id=? AND seal_revision=?",
                (document_id, expected),
            ).fetchone()
            if (
                prior is None
                or str(prior["state"]) != "pending"
                or str(prior["intent_id"]) != values["intent_id"]
                or str(prior["coordinator_decision_id"])
                != values["provisional_coordinator_decision_id"]
                or str(prior["coordinator_decision_sha256"])
                != provisional_coordinator_decision_sha256
            ):
                raise TruthActivationError(
                    "admission_seal_conflict",
                    "Admission decision binding replay does not match its prior seal.",
                )
            return _envelope_locked(store, tx, document_id)
        if current_revision != expected or str(seal["state"]) != "pending":
            raise TruthActivationError(
                "admission_seal_revision_conflict",
                "Document admission changed before its final decision was bound.",
                retryable=True,
            )
        if (
            str(seal["coordinator_decision_id"])
            != values["provisional_coordinator_decision_id"]
            or str(seal["coordinator_decision_sha256"])
            != provisional_coordinator_decision_sha256
        ):
            raise TruthActivationError(
                "coordinator_decision_mismatch",
                "The provisional coordinator decision does not match the staged admission.",
            )
        if final_matches:
            return _envelope_locked(store, tx, document_id)
        _append_pending_seal_decision_binding_locked(
            tx,
            document_id=document_id,
            intent_id=values["intent_id"],
            activation_revision=int(seal["activation_revision"]),
            seal_revision=current_revision + 1,
            provisional_coordinator_decision_id=values[
                "provisional_coordinator_decision_id"
            ],
            provisional_coordinator_decision_sha256=(
                provisional_coordinator_decision_sha256
            ),
            coordinator_decision_id=values["coordinator_decision_id"],
            coordinator_decision_sha256=coordinator_decision_sha256,
            actor_ref=actor_ref,
        )
        return _envelope_locked(store, tx, document_id)


def commit_document_admission(
    store: "TruthStore",
    *,
    document_id: str,
    expected_seal_revision: int,
    coordinator_decision_id: str,
    coordinator_decision_sha256: str,
    actor: Actor | str,
    conn: sqlite3.Connection | None = None,
) -> DocumentTruthPolicy:
    actor_ref = _actor_ref(actor)
    context = store.write_transaction(conn) if conn is not None else store.write_transaction()
    with context as tx:
        seal = tx.execute(
            "SELECT * FROM document_truth_admission_seals_current WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        if seal is None:
            raise TruthActivationError(
                "admission_seal_revision_conflict",
                "Document admission changed before this request committed.",
                retryable=True,
            )
        if (
            str(seal["coordinator_decision_id"]) != coordinator_decision_id
            or str(seal["coordinator_decision_sha256"]) != coordinator_decision_sha256
        ):
            raise TruthActivationError(
                "coordinator_decision_mismatch",
                "The coordinator decision does not match the staged admission.",
            )
        if str(seal["state"]) == "committed":
            return _envelope_locked(store, tx, document_id)
        if int(seal["seal_revision"]) != int(expected_seal_revision):
            raise TruthActivationError(
                "admission_seal_revision_conflict",
                "Document admission changed before this request committed.",
                retryable=True,
            )
        if str(seal["state"]) != "pending":
            raise TruthActivationError("admission_aborted", "Document admission was aborted.")
        _append_seal_locked(
            tx,
            document_id=document_id,
            intent_id=str(seal["intent_id"]),
            activation_revision=int(seal["activation_revision"]),
            state="committed",
            seal_revision=int(seal["seal_revision"]) + 1,
            coordinator_decision_id=coordinator_decision_id,
            coordinator_decision_sha256=coordinator_decision_sha256,
            actor_ref=actor_ref,
        )
        return _envelope_locked(store, tx, document_id)


def abort_document_admission(
    store: "TruthStore",
    *,
    document_id: str,
    expected_seal_revision: int,
    coordinator_decision_id: str,
    coordinator_decision_sha256: str,
    actor: Actor | str,
    conn: sqlite3.Connection | None = None,
) -> DocumentTruthPolicy:
    """Fence a staged document when its cross-store coordinator aborts."""

    actor_ref = _actor_ref(actor)
    context = store.write_transaction(conn) if conn is not None else store.write_transaction()
    with context as tx:
        seal = tx.execute(
            "SELECT * FROM document_truth_admission_seals_current WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        if seal is None:
            raise TruthActivationError(
                "admission_seal_revision_conflict",
                "Document admission changed before this request committed.",
                retryable=True,
            )
        if (
            str(seal["coordinator_decision_id"]) != coordinator_decision_id
            or str(seal["coordinator_decision_sha256"])
            != coordinator_decision_sha256
        ):
            raise TruthActivationError(
                "coordinator_decision_mismatch",
                "The coordinator decision does not match the staged admission.",
            )
        if str(seal["state"]) == "aborted":
            return _envelope_locked(store, tx, document_id)
        if int(seal["seal_revision"]) != int(expected_seal_revision):
            raise TruthActivationError(
                "admission_seal_revision_conflict",
                "Document admission changed before this request committed.",
                retryable=True,
            )
        if str(seal["state"]) != "pending":
            raise TruthActivationError(
                "admission_already_committed",
                "A committed document admission cannot be aborted.",
            )
        _append_seal_locked(
            tx,
            document_id=document_id,
            intent_id=str(seal["intent_id"]),
            activation_revision=int(seal["activation_revision"]),
            state="aborted",
            seal_revision=int(seal["seal_revision"]) + 1,
            coordinator_decision_id=coordinator_decision_id,
            coordinator_decision_sha256=coordinator_decision_sha256,
            actor_ref=actor_ref,
        )
        return _envelope_locked(store, tx, document_id)


def transition_document_truth_activation(
    store: "TruthStore",
    *,
    document_id: str,
    next_state: str,
    expected_activation_revision: int,
    actor: Actor | str,
    intent_id: str,
    reason: str | None = None,
    expected_interaction_contract_sha256: str | None = None,
    expected_document_head_sha256: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> DocumentTruthPolicy:
    """Apply one explicit, revision-bound activation transition.

    Standalone calls take the document lifecycle and Y.Doc locks before the
    SQLite transaction so the observed structured head and policy transition
    form one admission barrier. A caller supplying ``conn`` already owns that
    larger lock/transaction boundary.
    """

    state = str(next_state).strip().lower()
    if state not in ACTIVATION_STATES:
        raise TruthActivationError("invalid_activation", "Truth activation state is invalid.", status=400)
    if isinstance(expected_activation_revision, bool):
        raise TruthActivationError(
            "invalid_activation_revision",
            "expected_activation_revision must be a positive integer.",
            status=400,
        )
    try:
        expected_revision = int(expected_activation_revision)
    except (TypeError, ValueError) as exc:
        raise TruthActivationError(
            "invalid_activation_revision",
            "expected_activation_revision must be a positive integer.",
            status=400,
        ) from exc
    if expected_revision <= 0:
        raise TruthActivationError(
            "invalid_activation_revision",
            "expected_activation_revision must be a positive integer.",
            status=400,
        )
    actor_ref = _actor_ref(actor)
    normalized_intent = str(intent_id).strip()
    if not normalized_intent:
        raise TruthActivationError("intent_required", "An activation intent_id is required.", status=400)
    if reason is not None and not isinstance(reason, str):
        raise TruthActivationError(
            "invalid_activation_reason",
            "An activation reason must be text when supplied.",
            status=400,
        )

    from contextlib import ExitStack

    from work_buddy.cowork import lifecycle_lock
    from work_buddy.truth import ydoc_store

    with ExitStack() as stack:
        if conn is None:
            stack.enter_context(
                lifecycle_lock.document_lifecycle_lock(store.store_id, document_id)
            )
            stack.enter_context(ydoc_store.document_lock(store, document_id))
        context = (
            store.write_transaction(conn) if conn is not None else store.write_transaction()
        )
        tx = stack.enter_context(context)
        policy = _envelope_locked(store, tx, document_id)
        if policy.recovery_reason is not None:
            raise TruthActivationError(
                policy.recovery_reason,
                "This document's Truth policy must be recovered before it can change.",
            )
        if policy.eligibility == "unsupported":
            raise TruthActivationError("truth_unsupported", "This document does not support Truth.")
        if expected_interaction_contract_sha256 is not None and (
            expected_interaction_contract_sha256 != policy.interaction_contract_sha256
        ):
            raise TruthActivationError(
                "interaction_contract_revision_conflict",
                "The interaction contract changed after it was shown.",
                retryable=True,
            )

        # An exact retry must succeed even though its first commit advanced the
        # current revision. Reusing the intent for any other request fails
        # closed before ordinary CAS handling.
        existing = tx.execute(
            "SELECT * FROM document_truth_activation_transitions "
            "WHERE document_id = ? AND intent_id = ?",
            (document_id, normalized_intent),
        ).fetchone()
        if existing is not None:
            replay_head = (
                existing["observed_head_sha256"]
                if expected_document_head_sha256 is None
                else expected_document_head_sha256
            )
            replay_request = {
                "document_id": document_id,
                "previous_state": existing["previous_state"],
                "next_state": state,
                "activation_revision": expected_revision + 1,
                "observed_head": replay_head,
                "actor_ref": actor_ref,
                "intent_id": normalized_intent,
                "reason": reason,
            }
            if sha256_text(canonical_json(replay_request)) != str(
                existing["request_sha256"]
            ):
                raise TruthActivationError(
                    "activation_idempotency_conflict",
                    "This activation intent was already used for another request.",
                )
            return policy

        if policy.activation_revision != expected_revision:
            raise TruthActivationError(
                "activation_revision_conflict",
                "Truth activation changed after it was shown.",
                retryable=True,
                details={"current_activation_revision": policy.activation_revision},
            )
        document = _document_row_locked(tx, document_id)
        if document is None:
            raise TruthActivationError("document_not_found", "Document does not exist.", status=404)
        observed_head = _live_document_head(store, document)
        if (
            expected_document_head_sha256 is not None
            and observed_head != expected_document_head_sha256
        ):
            raise TruthActivationError(
                "document_head_conflict",
                "The document changed after its Truth settings were shown.",
                retryable=True,
            )
        previous = str(policy.activation_state)
        if previous == state:
            return policy
        ledger = _ledger_summary_locked(tx, document_id)
        valid = False
        if policy.eligibility == "allowed":
            valid = (
                (previous == "disabled" and state == "enabled")
                or (previous == "enabled" and state == "disabled" and not ledger["has_ledger"])
                or (previous == "enabled" and state == "paused" and ledger["has_ledger"])
                or (previous == "paused" and state == "enabled")
            )
        elif policy.eligibility == "required":
            valid = (previous, state) in {("enabled", "paused"), ("paused", "enabled")}
        if not valid:
            if previous == "enabled" and state == "disabled" and ledger["has_ledger"]:
                message = "This document has Truth history; pause it instead of disabling it."
            elif previous == "enabled" and state == "paused" and not ledger["has_ledger"]:
                message = "An allowed document without Truth history must be disabled, not paused."
            else:
                message = f"Truth activation cannot change from {previous} to {state}."
            raise TruthActivationError("invalid_activation_transition", message)
        _append_transition_locked(
            tx,
            document_id=document_id,
            previous_state=previous,
            next_state=state,
            activation_revision=int(policy.activation_revision or 0) + 1,
            observed_head=observed_head,
            actor_ref=actor_ref,
            intent_id=normalized_intent,
            reason=reason,
        )
        transitioned = _envelope_locked(store, tx, document_id)

    if conn is None and previous == "enabled" and state != "enabled":
        # Runtime analysis state lives outside the scoped Truth database. Run
        # this only after the store-owned transaction commits; late worker
        # submissions remain fenced independently by their activation revision.
        from work_buddy.cowork.truth_analysis_dispatch import (
            cancel_truth_analysis_runs_for_activation,
        )

        cancel_truth_analysis_runs_for_activation(
            store_id=store.store_id,
            document_id=document_id,
            valid_activation_revision=None,
        )
    return transitioned


def require_truth_access(
    store: "TruthStore",
    document_id: str,
    *,
    mutation: bool,
    expected_activation_revision: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> DocumentTruthPolicy:
    policy = resolve_document_truth_policy(store, document_id, conn=conn)
    if policy.recovery_reason is not None:
        raise TruthActivationError(
            policy.recovery_reason,
            "This document's Truth policy is unavailable pending recovery.",
            status=409,
        )
    if expected_activation_revision is not None and (
        policy.activation_revision != int(expected_activation_revision)
    ):
        raise TruthActivationError(
            "activation_revision_conflict",
            "Truth activation changed after this work was prepared.",
            retryable=True,
            details={"current_activation_revision": policy.activation_revision},
        )
    if mutation and not policy.truth_mutable:
        code = "truth_paused" if policy.activation_state == "paused" else "truth_disabled"
        raise TruthActivationError(
            code,
            "Truth tools are paused for this document."
            if policy.activation_state == "paused"
            else "Truth tools are not enabled for this document.",
            status=409 if policy.activation_state == "paused" else 403,
        )
    if not mutation and not policy.truth_observable:
        raise TruthActivationError(
            "truth_disabled",
            "Truth tools are not available for this document.",
            status=403,
        )
    return policy


def rebuild_policy_projections_locked(conn: sqlite3.Connection) -> None:
    """Rebuild current activation/seal projections from portable history."""

    conn.execute("DELETE FROM document_truth_activation_current")
    rows = conn.execute(
        "SELECT t.* FROM document_truth_activation_transitions t "
        "JOIN (SELECT document_id, MAX(activation_revision) AS revision "
        "FROM document_truth_activation_transitions GROUP BY document_id) latest "
        "ON latest.document_id = t.document_id "
        "AND latest.revision = t.activation_revision"
    ).fetchall()
    for row in rows:
        conn.execute(
            "INSERT INTO document_truth_activation_current "
            "(document_id, activation_revision, state, transition_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                row["document_id"],
                row["activation_revision"],
                row["next_state"],
                row["id"],
                row["created_at"],
            ),
        )
    conn.execute("DELETE FROM document_truth_admission_seals_current")
    seals = conn.execute(
        "SELECT e.* FROM document_truth_admission_seal_events e "
        "JOIN (SELECT document_id, MAX(seal_revision) AS revision "
        "FROM document_truth_admission_seal_events GROUP BY document_id) latest "
        "ON latest.document_id = e.document_id AND latest.revision = e.seal_revision"
    ).fetchall()
    for row in seals:
        conn.execute(
            "INSERT INTO document_truth_admission_seals_current "
            "(document_id, intent_id, activation_revision, state, seal_revision, "
            "coordinator_decision_id, coordinator_decision_sha256, event_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["document_id"],
                row["intent_id"],
                row["activation_revision"],
                row["state"],
                row["seal_revision"],
                row["coordinator_decision_id"],
                row["coordinator_decision_sha256"],
                row["id"],
                row["created_at"],
            ),
        )


def backfill_legacy_document_policies(conn: sqlite3.Connection) -> int:
    """Preserve pre-v11 full Co-work behavior with explicit enabled seals."""

    rows = conn.execute(
        "SELECT d.id, d.document_class, d.created_at, "
        "(SELECT v.structured_head_sha256 FROM document_versions v "
        "WHERE v.document_id = d.id ORDER BY v.created_at DESC, v.id DESC LIMIT 1) "
        "AS structured_head_sha256 FROM documents d ORDER BY d.created_at, d.id"
    ).fetchall()
    if not rows:
        return 0
    register_contract_definitions_locked(conn)
    contract = interaction_contract(LEGACY_FULL_COWORK_CONTRACT)
    count = 0
    for row in rows:
        document_id = str(row["id"])
        if conn.execute(
            "SELECT 1 FROM document_interaction_contract_assignments WHERE document_id = ?",
            (document_id,),
        ).fetchone() is not None:
            continue
        actor_ref = "truth-v11-compatibility"
        intent_id = f"truth-v11-compat:{document_id}"
        assignment_payload = {
            "document_id": document_id,
            "binding_id": None,
            "interaction_contract_id": contract.contract_id,
            "interaction_contract_version": contract.version,
            "interaction_contract_sha256": contract.definition_sha256,
            "cowork_document_class": str(row["document_class"]),
            "intent_id": intent_id,
        }
        assignment_id = _stable_id(
            "work-buddy.document-interaction-contract-assignment/v1", assignment_payload
        )
        conn.execute(
            "INSERT INTO document_interaction_contract_assignments "
            "(id, document_id, binding_id, interaction_contract_id, "
            "interaction_contract_version, interaction_contract_sha256, "
            "cowork_document_class, actor_ref, intent_id, assigned_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
            (
                assignment_id,
                document_id,
                contract.contract_id,
                contract.version,
                contract.definition_sha256,
                row["document_class"],
                actor_ref,
                intent_id,
                row["created_at"],
            ),
        )
        _insert_ledger(conn, "document_interaction_contract_assignment", assignment_id)
        _append_transition_locked(
            conn,
            document_id=document_id,
            previous_state=None,
            next_state="enabled",
            activation_revision=1,
            observed_head=row["structured_head_sha256"],
            actor_ref=actor_ref,
            intent_id=intent_id,
            reason="v11_compatibility_backfill",
        )
        decision_payload = {
            "document_id": document_id,
            "intent_id": intent_id,
            "contract_sha256": contract.definition_sha256,
            "activation_state": "enabled",
            "activation_revision": 1,
        }
        decision_id = _stable_id(
            "work-buddy.document-provisioning-decision/v1", decision_payload
        )
        decision_sha = sha256_text(canonical_json(decision_payload))
        _append_seal_locked(
            conn,
            document_id=document_id,
            intent_id=intent_id,
            activation_revision=1,
            state="pending",
            seal_revision=1,
            coordinator_decision_id=decision_id,
            coordinator_decision_sha256=decision_sha,
            actor_ref=actor_ref,
        )
        _append_seal_locked(
            conn,
            document_id=document_id,
            intent_id=intent_id,
            activation_revision=1,
            state="committed",
            seal_revision=2,
            coordinator_decision_id=decision_id,
            coordinator_decision_sha256=decision_sha,
            actor_ref=actor_ref,
        )
        count += 1
    return count


__all__ = [
    "CONTRACT_VERSION",
    "LEGACY_FULL_COWORK_CONTRACT",
    "PROVENANCE_DOCUMENT_CONTRACT",
    "TRUTH_DOCUMENT_CONTRACT",
    "WORKING_DOCUMENT_CONTRACT",
    "DocumentTruthPolicy",
    "InteractionContract",
    "TruthActivationError",
    "abort_document_admission",
    "backfill_legacy_document_policies",
    "bind_pending_document_admission_decision",
    "commit_document_admission",
    "interaction_contract",
    "provision_document_policy",
    "rebuild_policy_projections_locked",
    "registered_interaction_contracts",
    "require_truth_access",
    "resolve_document_truth_policy",
    "resolve_document_truth_policy_snapshot",
    "transition_document_truth_activation",
]

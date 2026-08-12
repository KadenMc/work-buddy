"""Authoritative Truth desired-state reader for Hindsight projection.

The reader reconstructs projection eligibility from one consistent Truth
snapshot.  It never asks Hindsight what Truth should mean, and it never reads
source bytes: source resolution receipts are used only as portable provenance
and redaction fences.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Iterable, Iterator, Mapping, Sequence

from work_buddy.hindsight_projection.contracts import (
    DesiredProjectionState,
    ProjectionClaimSnapshot,
    ProjectionIneligible,
    ProjectionIntentSpec,
    ProjectionNotFound,
    ProjectionValidationError,
    SourceDependency,
    canonical_sha256,
    projection_generation_sha256,
)
from work_buddy.hindsight_projection.store import TruthHindsightProjectionStore
from work_buddy.sources.models import SourceRef
from work_buddy.security.actors import ActorRef
from work_buddy.truth.queries import (
    ClaimState,
    claim_evidence_relations,
    resolve_claim_states,
)
from work_buddy.truth.store import TruthStore


DEFAULT_POLICY_ID = "confirmed_current_v1"
DEFAULT_PROJECTION_METHOD = "hindsight_llm_retain_v1"


@dataclass(frozen=True, slots=True)
class TruthHindsightProjectionPolicy:
    """Explicit rollout and eligibility policy for one projection reader."""

    enabled: bool
    authorization_ref: str
    policy_id: str = DEFAULT_POLICY_ID
    eligible_claim_kinds: frozenset[str] | None = None
    projection_method: str = DEFAULT_PROJECTION_METHOD

    def __post_init__(self) -> None:
        # Let the public intent/snapshot models enforce the exact token/ref
        # grammar.  This constructor additionally prevents an enabled egress
        # policy from silently inventing an authorization reference.
        if not isinstance(self.enabled, bool):
            raise ProjectionValidationError("projection policy enabled must be boolean")
        if self.enabled and not str(self.authorization_ref).strip():
            raise ProjectionValidationError(
                "enabled Truth-to-Hindsight projection requires authorization_ref"
            )
        if self.eligible_claim_kinds is not None:
            kinds = frozenset(str(item).strip() for item in self.eligible_claim_kinds)
            if not kinds or any(not item for item in kinds):
                raise ProjectionValidationError(
                    "eligible_claim_kinds must be non-empty claim-kind tokens"
                )
            object.__setattr__(self, "eligible_claim_kinds", kinds)

    @property
    def semantic_sha256(self) -> str:
        """Fence effects when rollout, authorization, or eligibility changes."""

        return canonical_sha256(
            {
                "schema": "wb.truth-hindsight-policy/v1",
                "enabled": self.enabled,
                "authorization_ref": self.authorization_ref,
                "policy_id": self.policy_id,
                "eligible_claim_kinds": (
                    None
                    if self.eligible_claim_kinds is None
                    else sorted(self.eligible_claim_kinds)
                ),
                "projection_method": self.projection_method,
            }
        )


def projection_policy_from_config(
    config: Mapping[str, Any],
    *,
    store_id: str,
) -> TruthHindsightProjectionPolicy:
    """Parse the bounded global rollout policy without implicit egress.

    Projection is disabled unless ``hindsight.truth_projection.enabled`` is
    explicitly true.  A configured authorization reference is mandatory once
    enabled; absence is a configuration error rather than a fabricated grant.
    """

    hindsight = config.get("hindsight", {})
    raw = hindsight.get("truth_projection", {}) if isinstance(hindsight, Mapping) else {}
    if not isinstance(raw, Mapping):
        raise ProjectionValidationError("hindsight.truth_projection must be an object")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ProjectionValidationError(
            "hindsight.truth_projection.enabled must be boolean"
        )
    configured_ref = raw.get("authorization_ref")
    if enabled and (
        configured_ref is None or not str(configured_ref).strip()
    ):
        raise ProjectionValidationError(
            "enabled Truth-to-Hindsight projection requires authorization_ref"
        )
    authorization_ref = (
        str(configured_ref).strip()
        if configured_ref is not None
        else f"truth-store:{store_id}:hindsight-projection-disabled"
    )
    raw_kinds = raw.get("eligible_claim_kinds")
    if raw_kinds is None:
        kinds = None
    elif isinstance(raw_kinds, Sequence) and not isinstance(raw_kinds, (str, bytes)):
        kinds = frozenset(str(item) for item in raw_kinds)
    else:
        raise ProjectionValidationError(
            "hindsight.truth_projection.eligible_claim_kinds must be a list"
        )
    return TruthHindsightProjectionPolicy(
        enabled=enabled,
        authorization_ref=authorization_ref,
        policy_id=str(raw.get("policy_id") or DEFAULT_POLICY_ID),
        eligible_claim_kinds=kinds,
        projection_method=str(raw.get("projection_method") or DEFAULT_PROJECTION_METHOD),
    )


def configured_projection_policy(store: TruthStore) -> TruthHindsightProjectionPolicy:
    from work_buddy.config import load_config

    return projection_policy_from_config(load_config(), store_id=store.store_id)


@dataclass(frozen=True, slots=True)
class _SourceEvaluation:
    state: str
    digest: str
    dependencies: tuple[SourceDependency, ...]
    usable_supports: int


@dataclass(frozen=True, slots=True)
class _Evaluation:
    state: ClaimState
    event_id: str
    lifecycle_status: str
    source: _SourceEvaluation
    generation: str
    eligibility_sha256: str
    desired_state: DesiredProjectionState
    reason_code: str
    purge_projection_source: bool
    policy_eligible: bool
    valid_from: str | None
    valid_to: str | None


def _normalize_time(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProjectionValidationError(f"{label} must be an ISO date or timestamp")
    raw = value.strip()
    try:
        if "T" not in raw and " " not in raw:
            parsed = datetime.combine(date.fromisoformat(raw), time.min, timezone.utc)
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectionValidationError(
            f"{label} must be an ISO date or timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProjectionValidationError(f"{label} must include a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _time_key(value: str) -> datetime:
    normalized = _normalize_time(value, "timestamp")
    assert normalized is not None
    return datetime.fromisoformat(normalized.replace("Z", "+00:00"))


def _scope(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    return {"kind": value}


def _json_object(value: str | None) -> dict[str, Any]:
    if value is None:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ProjectionValidationError("stored selector is not a JSON object")
    return parsed


class TruthStoreProjectionReader:
    """Reconstruct current projection intent from canonical Truth rows."""

    def __init__(
        self,
        store: TruthStore,
        *,
        policy: TruthHindsightProjectionPolicy,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self._conn = conn
        if conn is not None:
            store._validate_connection_target(conn)

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        if self._conn is not None:
            yield self._conn
            return
        conn = self.store.connect()
        try:
            conn.execute("BEGIN")
            yield conn
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    @staticmethod
    def _latest_usage(conn: sqlite3.Connection, resolution_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM truth_source_usage_events "
            "WHERE resolution_record_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (resolution_id,),
        ).fetchone()

    def _source_evaluation(
        self,
        conn: sqlite3.Connection,
        state: ClaimState,
    ) -> _SourceEvaluation:
        relations = claim_evidence_relations(self.store, state.claim_id, conn=conn)
        relation_rows: list[dict[str, Any]] = []
        dependencies: dict[tuple[str, str, str, str], SourceDependency] = {}
        source_state = "clean"
        usable_supports = sum(1 for relation in relations if relation.usable_support)

        for relation in relations:
            evidence_state = conn.execute(
                "SELECT e.redacted_at AS evidence_redacted_at, "
                "s.redacted_at AS span_redacted_at "
                "FROM evidence AS e JOIN evidence_spans AS s ON s.evidence_id = e.id "
                "WHERE e.id = ? AND s.id = ?",
                (relation.evidence_id, relation.span_id),
            ).fetchone()
            relation_redacted = bool(
                evidence_state is not None
                and (
                    evidence_state["evidence_redacted_at"] is not None
                    or evidence_state["span_redacted_at"] is not None
                )
            )
            if relation_redacted:
                source_state = "redacted"
            resolutions = conn.execute(
                "SELECT * FROM evidence_source_resolution_records "
                "WHERE evidence_id = ? ORDER BY resolved_at, id",
                (relation.evidence_id,),
            ).fetchall()
            resolution_rows: list[dict[str, Any]] = []
            for resolution in resolutions:
                latest = self._latest_usage(conn, str(resolution["id"]))
                usage_status = None if latest is None else str(latest["status"])
                usage_event_id = None if latest is None else str(latest["id"])
                usage_epoch = (
                    int(resolution["redaction_epoch"])
                    if latest is None
                    else int(latest["redaction_epoch"])
                )
                resolution_rows.append(
                    {
                        "resolution_id": resolution["id"],
                        "canonical_sha256": resolution["canonical_sha256"],
                        "usage_event_id": usage_event_id,
                        "usage_status": usage_status,
                        "redaction_epoch": usage_epoch,
                    }
                )
                if usage_status == "redaction_pending":
                    source_state = "redacted"
                elif usage_status != "acknowledged" and source_state != "redacted":
                    source_state = "attention"
                if not relation.usable_support and source_state != "redacted":
                    source_state = "attention"
                if not relation.usable_support or usage_status != "acknowledged":
                    continue
                try:
                    source_value = json.loads(str(resolution["source_ref_json"]))
                    if not isinstance(source_value, dict):
                        raise ValueError("source ref is not an object")
                    source_ref = SourceRef.from_dict(source_value).uri
                    selector = _json_object(resolution["selector_json"])
                    relationship = (
                        relation.derivation_relationship
                        or relation.evidential_effect
                        or "support"
                    )
                    dependency = SourceDependency(
                        source_ref=source_ref,
                        representation_id=str(resolution["representation_id"]),
                        content_sha256=str(resolution["content_sha256"]),
                        relation=relationship,
                        selector=selector,
                    )
                except Exception:
                    if source_state != "redacted":
                        source_state = "attention"
                    continue
                key = (
                    dependency.source_ref,
                    dependency.representation_id,
                    canonical_sha256(dict(dependency.selector)),
                    dependency.relation,
                )
                dependencies[key] = dependency
            relation_rows.append(
                {
                    "link_id": relation.link_id,
                    "classification": relation.classification,
                    "evidential_effect": relation.evidential_effect,
                    "derivation_relationship": relation.derivation_relationship,
                    "usable_support": relation.usable_support,
                    "redacted": relation_redacted,
                    "resolutions": resolution_rows,
                }
            )

        digest = canonical_sha256(
            {
                "schema": "wb.truth-hindsight-source-state/v1",
                "claim_id": state.claim_id,
                "state": source_state,
                "relations": relation_rows,
            }
        )
        return _SourceEvaluation(
            state=source_state,
            digest=digest,
            dependencies=tuple(dependencies[key] for key in sorted(dependencies)),
            usable_supports=usable_supports,
        )

    def _evaluate(self, conn: sqlite3.Connection, claim_id: str, *, at: str) -> _Evaluation:
        states = resolve_claim_states(self.store, conn=conn)
        state = next((item for item in states if item.claim_id == claim_id), None)
        if state is None:
            raise ProjectionNotFound(f"Truth claim does not exist: {claim_id}")
        moment = _normalize_time(at, "at")
        assert moment is not None
        valid_from = _normalize_time(state.effective_valid_from, "valid_from")
        valid_to = _normalize_time(state.effective_valid_to, "valid_to")
        source = self._source_evaluation(conn, state)
        lifecycle_status = state.status or state.base_status or "unknown"
        event_id = state.overlay_event_id or state.base_status_event_id
        if event_id is None:
            raise ProjectionValidationError("claim has no lifecycle event identity")
        support_policy = self.store.profile.support_policy_for(state.claim.claim_kind)
        allowed_kind = (
            self.policy.eligible_claim_kinds is None
            or state.claim.claim_kind in self.policy.eligible_claim_kinds
        )
        current = (
            state.base_status == "confirmed"
            and lifecycle_status == "confirmed"
            and not state.needs_review
            and not state.voided
            and state.claim.redacted_at is None
            and state.health == "clean"
        )
        within_valid_time = (
            (valid_from is None or _time_key(moment) >= _time_key(valid_from))
            and (valid_to is None or _time_key(moment) < _time_key(valid_to))
        )
        enough_support = (
            source.usable_supports >= support_policy.minimum_usable_supports
        )
        if valid_from is not None and _time_key(moment) < _time_key(valid_from):
            valid_time_phase = "before"
        elif valid_to is not None and _time_key(moment) >= _time_key(valid_to):
            valid_time_phase = "after"
        else:
            valid_time_phase = "within"
        policy_eligible = (
            self.policy.enabled
            and allowed_kind
            and current
            and within_valid_time
            and enough_support
            and source.state == "clean"
        )

        # A projection generation is an identity fence for one stable desired
        # semantic state, not merely for the immutable claim row. In
        # particular, crossing a reviewed valid-time boundary or changing a
        # support/rollout rule must create a new generation; otherwise the
        # outbox would correctly reject the new upsert/remove semantics as a
        # conflicting reuse of the prior generation identity.
        generation_state_sha256 = canonical_sha256(
            {
                "schema": "wb.truth-hindsight-generation-state/v1",
                "source_state_sha256": source.digest,
                "policy_sha256": self.policy.semantic_sha256,
                "allowed_kind": allowed_kind,
                "current": current,
                "valid_time_phase": valid_time_phase,
                "minimum_usable_supports": (
                    support_policy.minimum_usable_supports
                ),
                "usable_supports": source.usable_supports,
            }
        )
        generation = projection_generation_sha256(
            claim_canonical_sha256=state.claim.canonical_sha256,
            lifecycle_event_id=event_id,
            lifecycle_status=lifecycle_status,
            policy_id=self.policy.policy_id,
            source_state_sha256=generation_state_sha256,
        )

        purge = False
        if state.claim.redacted_at is not None:
            reason = "claim_redacted"
            purge = True
        elif source.state == "redacted":
            reason = "source_redacted"
            purge = True
        elif not self.policy.enabled:
            reason = "projection_policy_disabled"
        elif source.state != "clean":
            reason = "source_requires_attention"
        elif not allowed_kind:
            reason = "claim_kind_ineligible"
        elif lifecycle_status != "confirmed" or state.base_status != "confirmed":
            reason = "claim_" + lifecycle_status
        elif state.needs_review or state.health != "clean" or state.voided:
            reason = "claim_integrity_attention"
        elif not within_valid_time:
            reason = (
                "claim_not_yet_valid"
                if valid_from is not None and _time_key(moment) < _time_key(valid_from)
                else "claim_expired"
            )
        elif not enough_support:
            reason = "support_policy_unsatisfied"
        else:
            reason = "claim_confirmed"

        desired_state = (
            DesiredProjectionState.UPSERT
            if policy_eligible
            else DesiredProjectionState.REMOVE
        )
        eligibility_sha256 = canonical_sha256(
            {
                "schema": "wb.truth-hindsight-eligibility/v1",
                "claim_id": state.claim_id,
                "claim_generation": generation,
                "policy_id": self.policy.policy_id,
                "policy_enabled": self.policy.enabled,
                "policy_sha256": self.policy.semantic_sha256,
                "allowed_kind": allowed_kind,
                "current": current,
                "within_valid_time": within_valid_time,
                "minimum_usable_supports": support_policy.minimum_usable_supports,
                "usable_supports": source.usable_supports,
                "source_state": source.state,
                "source_state_sha256": source.digest,
                "desired_state": desired_state.value,
                "reason_code": reason,
            }
        )
        return _Evaluation(
            state=state,
            event_id=event_id,
            lifecycle_status=lifecycle_status,
            source=source,
            generation=generation,
            eligibility_sha256=eligibility_sha256,
            desired_state=desired_state,
            reason_code=reason,
            purge_projection_source=purge,
            policy_eligible=policy_eligible,
            valid_from=valid_from,
            valid_to=valid_to,
        )

    def desired_for_claim(
        self,
        claim_id: str,
        policy_id: str,
        *,
        at: str,
    ) -> ProjectionIntentSpec:
        if policy_id != self.policy.policy_id:
            # A policy rename must still retire its old stable Hindsight
            # document. No exact content can cross this legacy reader: it is
            # permanently disabled and yields only an explicit removal.
            legacy = TruthHindsightProjectionPolicy(
                enabled=False,
                policy_id=policy_id,
                authorization_ref=(
                    f"truth-store:{self.store.store_id}:retired-hindsight-policy"
                ),
                projection_method=self.policy.projection_method,
            )
            return TruthStoreProjectionReader(
                self.store,
                policy=legacy,
                conn=self._conn,
            ).desired_for_claim(claim_id, policy_id, at=at)
        with self._read() as conn:
            evaluation = self._evaluate(conn, claim_id, at=at)
        return ProjectionIntentSpec(
            claim_id=claim_id,
            claim_generation=evaluation.generation,
            policy_id=self.policy.policy_id,
            desired_state=evaluation.desired_state,
            reason_code=evaluation.reason_code,
            eligibility_sha256=evaluation.eligibility_sha256,
            authorization_ref=self.policy.authorization_ref,
            purge_projection_source=evaluation.purge_projection_source,
            requested_at=_normalize_time(at, "at") or at,
        )

    def resolve_snapshot(
        self,
        intent: ProjectionIntentSpec,
        *,
        at: str,
    ) -> ProjectionClaimSnapshot:
        current = self.desired_for_claim(
            intent.claim_id,
            intent.policy_id,
            at=at,
        )
        if current.request_sha256 != intent.request_sha256:
            raise ProjectionIneligible("Truth desired state changed after enqueue")
        with self._read() as conn:
            evaluation = self._evaluate(conn, intent.claim_id, at=at)
        if not evaluation.policy_eligible:
            raise ProjectionIneligible("Truth claim is not currently projection eligible")
        snapshot = ProjectionClaimSnapshot(
            claim_id=evaluation.state.claim_id,
            policy_id=self.policy.policy_id,
            claim_generation=evaluation.generation,
            claim_canonical_sha256=evaluation.state.claim.canonical_sha256,
            proposition=evaluation.state.claim.proposition,
            claim_kind=evaluation.state.claim.claim_kind,
            lifecycle_status=evaluation.lifecycle_status,
            lifecycle_event_id=evaluation.event_id,
            applicability_scope=_scope(evaluation.state.claim.scope),
            valid_from=evaluation.valid_from,
            valid_to=evaluation.valid_to,
            current=True,
            policy_eligible=True,
            source_state=evaluation.source.state,
            eligibility_sha256=evaluation.eligibility_sha256,
            evaluated_at=_normalize_time(at, "at") or at,
            source_dependencies=evaluation.source.dependencies,
            projection_method=self.policy.projection_method,
        )
        snapshot.validate_for(intent, at=at)
        return snapshot

    def iter_desired(self, *, at: str) -> Iterable[ProjectionIntentSpec]:
        with self._read() as conn:
            claim_ids = tuple(
                str(row["id"])
                for row in conn.execute("SELECT id FROM claims ORDER BY created_at, id")
            )
            desired = tuple(
                TruthStoreProjectionReader(
                    self.store,
                    policy=self.policy,
                    conn=conn,
                ).desired_for_claim(claim_id, self.policy.policy_id, at=at)
                for claim_id in claim_ids
            )
            # Reconciliation treats omission as no command. Existing receipts
            # are checked separately and mapped through ``desired_for_claim``;
            # therefore never-projected ineligible claims do not create a sea
            # of meaningless DELETE effects.
            return tuple(
                item
                for item in desired
                if item.desired_state is DesiredProjectionState.UPSERT
            )


def enqueue_claim_projection_in_transaction(
    store: TruthStore,
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    at: str,
    policy: TruthHindsightProjectionPolicy | None = None,
) -> ProjectionIntentSpec:
    """Write one current desired state inside the caller's Truth transaction."""

    active_policy = policy or configured_projection_policy(store)
    reader = TruthStoreProjectionReader(store, policy=active_policy, conn=conn)
    desired = reader.desired_for_claim(claim_id, active_policy.policy_id, at=at)
    if desired.desired_state is DesiredProjectionState.REMOVE:
        tracked = conn.execute(
            "SELECT 1 FROM truth_hindsight_projection_heads "
            "WHERE claim_id = ? AND policy_id = ? "
            "UNION ALL SELECT 1 FROM truth_hindsight_projection_receipts "
            "WHERE claim_id = ? AND policy_id = ? LIMIT 1",
            (claim_id, active_policy.policy_id, claim_id, active_policy.policy_id),
        ).fetchone()
        if tracked is None:
            return desired
    TruthHindsightProjectionStore.enqueue_in_transaction(conn, desired)
    return desired


def record_source_redaction_attention(
    store: TruthStore,
    *,
    claim_id: str,
    source_ref: str,
    representation_id: str,
    redaction_event_id: str,
    redaction_epoch: int,
    actor: ActorRef,
    at: str,
    policy: TruthHindsightProjectionPolicy,
) -> ProjectionIntentSpec:
    """Record a Sources redaction observation, then enqueue current policy.

    The Sources event changes source usability, not claim standing. Truth still
    reconstructs the explicit remove intent from its own source receipts in the
    same transaction.
    """

    if isinstance(redaction_epoch, bool) or not isinstance(redaction_epoch, int):
        raise ProjectionValidationError("redaction_epoch must be an integer")
    if redaction_epoch < 1:
        raise ProjectionValidationError("redaction_epoch must be positive")
    parsed_ref = SourceRef.parse(source_ref)
    source_json = json.dumps(
        parsed_ref.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    from work_buddy.truth.source_provenance import record_source_usage_event

    with store.write_transaction() as conn:
        rows = conn.execute(
            "SELECT DISTINCT r.* "
            "FROM evidence_source_resolution_records AS r "
            "JOIN evidence_spans AS s ON s.evidence_id = r.evidence_id "
            "JOIN claim_links AS l ON l.to_kind = 'evidence_span' "
            "AND l.to_ref = s.id "
            "LEFT JOIN link_retractions AS x ON x.link_id = l.id "
            "WHERE l.from_claim_id = ? AND x.link_id IS NULL "
            "AND r.source_ref_json = ? AND r.representation_id = ? "
            "ORDER BY r.resolved_at, r.id",
            (claim_id, source_json, representation_id),
        ).fetchall()
        if not rows:
            raise ProjectionValidationError(
                "source redaction does not match a claim resolution receipt"
            )
        for resolution in rows:
            latest = conn.execute(
                "SELECT status FROM truth_source_usage_events "
                "WHERE resolution_record_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (resolution["id"],),
            ).fetchone()
            if latest is not None and latest["status"] == "redaction_pending":
                continue
            record_id = canonical_sha256(
                {
                    "schema": "wb.truth-source-redaction-attention/v1",
                    "resolution_record_id": resolution["id"],
                    "redaction_event_id": redaction_event_id,
                    "redaction_epoch": redaction_epoch,
                }
            )[:32]
            record_source_usage_event(
                store,
                resolution_record_id=str(resolution["id"]),
                usage_id=str(resolution["usage_id"]),
                status="redaction_pending",
                purpose="truth_hindsight_projection",
                consumer_ref=f"source-redaction:{redaction_event_id}",
                # The event marks the immutable resolution generation that is
                # now stale; the newer Sources epoch is bound into record_id.
                redaction_epoch=int(resolution["redaction_epoch"]),
                actor=actor,
                record_id=record_id,
                created_at=_normalize_time(at, "at"),
                conn=conn,
            )
        return enqueue_claim_projection_in_transaction(
            store,
            conn,
            claim_id=claim_id,
            at=at,
            policy=policy,
        )


__all__ = [
    "DEFAULT_POLICY_ID",
    "TruthHindsightProjectionPolicy",
    "TruthStoreProjectionReader",
    "configured_projection_policy",
    "enqueue_claim_projection_in_transaction",
    "projection_policy_from_config",
    "record_source_redaction_attention",
]

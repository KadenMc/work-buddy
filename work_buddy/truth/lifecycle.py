"""Append-only claim lifecycle and human gesture enforcement.

The store module owns durable inserts. This module owns the policy that decides
which status event may be appended and which exact human decision authorizes it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from work_buddy.truth.contracts import (
    Actor,
    GestureError,
    InvariantViolation,
    TERMINAL_STATUSES,
    TransitionError,
)
from work_buddy.truth.identity import (
    canonical_json,
    new_id,
    parse_truth_uri,
    sha256_text,
    utc_now,
)
from work_buddy.truth.store import (
    SUPERSESSION_REASONS,
    ClaimLinkRecord,
    ClaimRecord,
    GestureRecord,
    StatusEventRecord,
    TruthStore,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECORD_ID_RE = re.compile(r"^[0-9a-f]{32}$")

GESTURE_KINDS = frozenset(
    {
        "confirm",
        "confirm_quarantined_support",
        "reaffirm",
        "edit_confirm",
        "reject_plain",
        "reject_as_false",
        "reject_as_preference",
        "redact",
        "defer",
        "scope",
        "redirect",
        "endorse",
    }
)
CONFIRM_GESTURE_KINDS = frozenset({"confirm", "reaffirm", "edit_confirm"})
REJECTION_CLASSES = frozenset(
    {"reject_plain", "reject_as_false", "reject_as_preference"}
)
# Proposal-op allowed-kind sets (PRD §6 verb table). Accept and amend reuse the
# shipped confirm/edit_confirm kinds on a proposal subject, so no new accept
# kinds are minted. Reject reuses the shipped rejection classes.
PROPOSAL_ACCEPT_KINDS = frozenset({"confirm", "edit_confirm"})
PROPOSAL_REJECT_KINDS = REJECTION_CLASSES
PROPOSAL_ROUTING_KINDS = frozenset({"redirect", "defer", "endorse"})
REJECTION_BINDING_FIELDS = (
    "rejection_class",
    "source_canonical_sha256",
    "result_canonical_sha256",
)
REJECTION_BINDING_HASH_FIELD = "rejection_binding_sha256"
REVIEW_BASIS_KINDS = frozenset({"rule", "sweep", "conflict"})

_BASE_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "proposed": frozenset({"confirmed", "rejected", "expired", "retracted"}),
    "confirmed": frozenset({"challenged", "superseded", "retracted"}),
    "challenged": frozenset({"confirmed", "superseded", "retracted"}),
    "rejected": frozenset(),
    "expired": frozenset(),
    "superseded": frozenset(),
    "retracted": frozenset(),
}


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """The event selected or appended for one idempotent transition."""

    event: StatusEventRecord
    created: bool
    previous_status: str


@dataclass(frozen=True, slots=True)
class PremiseAssessment:
    """Fail-soft weakest-link assessment for all derivations of a claim."""

    local_unconfirmed: tuple[str, ...]
    unresolved_uris: tuple[str, ...]
    confirmed: bool


@dataclass(frozen=True, slots=True)
class SupportAssessment:
    """Usability and authorship summary for active support links."""

    support_span_ids: tuple[str, ...]
    usable_span_ids: tuple[str, ...]
    quarantined_only: bool
    agent_authored_only: bool
    store_derived_only: bool


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    """Atomic confirmation result, including supersession side effects."""

    event: StatusEventRecord | None
    created: bool
    gesture: GestureRecord
    superseded_events: tuple[StatusEventRecord, ...]
    needs_review_event: StatusEventRecord | None


@dataclass(frozen=True, slots=True)
class RejectionResult:
    """Reason-classed rejection and optional replacement assertion."""

    source_event: StatusEventRecord
    result_claim: ClaimRecord | None
    result_event: StatusEventRecord | None
    refutes_link: ClaimLinkRecord | None
    gesture: GestureRecord


def hash_context(value: Any) -> str:
    """Hash one canonical JSON context exactly as displayed by a surface."""

    try:
        return sha256_text(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise InvariantViolation("gesture context must be canonical JSON data") from exc


def negated_proposition(proposition: str) -> str:
    """Return the deterministic proposition attested by ``reject_as_false``."""

    return f"It is not the case that {_text(proposition, 'proposition')}"


def rejection_binding_role(
    *,
    rejection_class: str,
    source_canonical_sha256: str,
    result_canonical_sha256: str,
) -> dict[str, str]:
    """Build the immutable, non-content binding for a reasoned rejection."""

    rejection = _text(rejection_class, "rejection_class")
    if rejection not in REJECTION_CLASSES:
        raise TransitionError(f"unsupported rejection class {rejection!r}")
    binding = {
        "rejection_class": rejection,
        "source_canonical_sha256": _digest(
            source_canonical_sha256,
            "source_canonical_sha256",
        ),
        "result_canonical_sha256": _digest(
            result_canonical_sha256,
            "result_canonical_sha256",
        ),
    }
    return {
        **binding,
        REJECTION_BINDING_HASH_FIELD: sha256_text(canonical_json(binding)),
    }


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvariantViolation(f"{label} must be a nonempty string")
    return value.strip()


def _record_id(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise InvariantViolation(f"{label} must be a 32-character hexadecimal id")
    normalized = value.strip().lower()
    if _RECORD_ID_RE.fullmatch(normalized) is None:
        raise InvariantViolation(f"{label} must be a 32-character hexadecimal id")
    return normalized


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise InvariantViolation(f"{label} must be a SHA-256 digest")
    normalized = value.strip().lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise InvariantViolation(f"{label} must be a SHA-256 digest")
    return normalized


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvariantViolation(f"{label} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvariantViolation(f"{label} must include a UTC offset")
    return parsed


def _timestamp(value: str | None, label: str) -> str:
    result = utc_now() if value is None else value
    _parse_timestamp(result, label)
    return result


def _human(actor: Actor) -> str:
    if actor.kind != "human":
        raise GestureError("a human actor is required")
    if not str(actor.ref or "").strip():
        raise GestureError("a human actor requires a durable actor ref")
    return str(actor.ref).strip()


def _row_status(raw: sqlite3.Row | None) -> StatusEventRecord | None:
    return None if raw is None else StatusEventRecord(**dict(raw))


def _row_gesture(raw: sqlite3.Row | None) -> GestureRecord | None:
    return None if raw is None else GestureRecord(**dict(raw))


def _row_link(raw: sqlite3.Row | None) -> ClaimLinkRecord | None:
    return None if raw is None else ClaimLinkRecord(**dict(raw))


class TruthLifecycle:
    """Lifecycle policy bound to exactly one targeted truth store."""

    def __init__(self, store: TruthStore) -> None:
        if not isinstance(store, TruthStore):
            raise TypeError("store must be a TruthStore")
        self.store = store

    @contextmanager
    def _read_connection(
        self,
        conn: sqlite3.Connection | None,
    ) -> Iterator[sqlite3.Connection]:
        if conn is not None:
            self.store._validate_connection_target(conn)
            yield conn
            return
        owned = self.store.connect()
        try:
            yield owned
        finally:
            owned.close()

    def latest_status(
        self,
        claim_id: str,
        *,
        include_overlay: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> StatusEventRecord:
        """Return the latest status by insertion sequence, never timestamp."""

        identifier = _record_id(claim_id, "claim_id")
        with self._read_connection(conn) as read_conn:
            if self.store._get_claim_locked(read_conn, identifier) is None:
                raise InvariantViolation(f"claim does not exist: {identifier}")
            event = self.store._latest_status_locked(
                read_conn,
                identifier,
                include_overlay=include_overlay,
            )
            if event is None:
                raise InvariantViolation(f"claim has no status history: {identifier}")
            return event

    def _subject_payload_locked(
        self,
        conn: sqlite3.Connection,
        subject_ref: str,
    ) -> tuple[str, str]:
        identifier = _record_id(subject_ref, "subject_ref")
        matches: list[tuple[str, str]] = []
        claim = self.store._get_claim_locked(conn, identifier)
        if claim is not None:
            matches.append((claim.canonical_sha256, claim.proposition))
        evidence = self.store._get_evidence_locked(conn, identifier)
        if evidence is not None:
            excerpt = evidence.content or evidence.source_locator
            matches.append((evidence.content_sha256, excerpt))
        span = self.store._get_span_locked(conn, identifier)
        if span is not None:
            matches.append((span.span_sha256, span.quote_exact or span.selector_json))
        # The one-match-or-ambiguous rule below enforces global subject-id
        # uniqueness across all four gesture-subject kinds.
        proposal = self.store._get_proposal_locked(conn, identifier)
        if proposal is not None:
            if proposal.replacement is not None:
                reviewable = proposal.replacement
            else:
                reviewable = "[flag] " + (proposal.rationale or "")
            excerpt = (proposal.quote_exact or "") + " -> " + reviewable
            matches.append((proposal.canonical_sha256, excerpt))
        if not matches:
            raise InvariantViolation(f"gesture subject does not exist: {identifier}")
        if len(matches) != 1:
            raise InvariantViolation("gesture subject id is ambiguous in this store")
        digest, excerpt = matches[0]
        return digest, " ".join(excerpt.split())[:240]

    def mint_gesture(
        self,
        *,
        subject_ref: str,
        actor: Actor,
        surface: str,
        kind: str,
        displayed_payload_sha256: str,
        context_sha256: str | None = None,
        expires_at: str | None = None,
        gesture_id: str | None = None,
        at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> GestureRecord:
        """Mint a server-composed gesture bound to the current durable subject."""

        actor_ref = _human(actor)
        subject = _record_id(subject_ref, "subject_ref")
        gesture_kind = _text(kind, "kind")
        if gesture_kind not in GESTURE_KINDS:
            raise GestureError(f"unsupported gesture kind {gesture_kind!r}")
        surface_name = _text(surface, "surface")
        displayed_digest = _digest(
            displayed_payload_sha256,
            "displayed_payload_sha256",
        )
        context_digest = (
            None
            if context_sha256 is None
            else _digest(context_sha256, "context_sha256")
        )
        identifier = (
            new_id() if gesture_id is None else _record_id(gesture_id, "gesture_id")
        )
        timestamp = _timestamp(at, "gesture at")
        expiry = None if expires_at is None else _timestamp(expires_at, "expires_at")
        if expiry is not None and _parse_timestamp(
            expiry, "expires_at"
        ) <= _parse_timestamp(
            timestamp,
            "gesture at",
        ):
            raise GestureError("gesture expiry must be later than its decision time")

        with self.store.write_transaction(conn) as write_conn:
            payload_digest, excerpt = self._subject_payload_locked(write_conn, subject)
            if displayed_digest != payload_digest:
                raise GestureError(
                    "displayed payload hash does not match the durable subject"
                )
            record = GestureRecord(
                id=identifier,
                at=timestamp,
                surface=surface_name,
                actor_ref=actor_ref,
                kind=gesture_kind,
                subject_ref=subject,
                payload_sha256=payload_digest,
                payload_excerpt=excerpt,
                context_sha256=context_digest,
                expires_at=expiry,
                consumed_at=None,
            )
            existing = self.store._get_gesture_locked(write_conn, identifier)
            if existing is not None:
                if existing == record:
                    return existing
                raise GestureError("gesture id already identifies a different decision")
            return self.store._insert_gesture_locked(write_conn, record)

    def _verify_gesture_locked(
        self,
        conn: sqlite3.Connection,
        gesture_id: str,
        *,
        actor: Actor,
        subject_ref: str,
        payload_sha256: str,
        expected_context_sha256: str | None,
        allowed_kinds: Collection[str],
        observed_at: str | None,
    ) -> GestureRecord:
        actor_ref = _human(actor)
        identifier = _record_id(gesture_id, "gesture_id")
        subject = _record_id(subject_ref, "subject_ref")
        payload = _digest(payload_sha256, "payload_sha256")
        allowed = frozenset(
            _text(item, "allowed gesture kind") for item in allowed_kinds
        )
        if not allowed:
            raise GestureError("allowed_kinds cannot be empty")
        context = (
            None
            if expected_context_sha256 is None
            else _digest(expected_context_sha256, "expected_context_sha256")
        )
        observed = _timestamp(observed_at, "gesture observed_at")
        gesture = self.store._get_gesture_locked(conn, identifier)
        if gesture is None:
            raise GestureError(f"gesture does not exist: {identifier}")
        if _parse_timestamp(observed, "gesture observed_at") < _parse_timestamp(
            gesture.at,
            "gesture at",
        ):
            raise GestureError("gesture use cannot predate the human decision")
        if gesture.consumed_at is not None:
            raise GestureError("gesture has already been consumed")
        if gesture.actor_ref != actor_ref:
            raise GestureError("gesture actor does not match the acting human")
        if gesture.kind not in allowed:
            raise GestureError("gesture kind does not authorize this operation")
        if gesture.subject_ref != subject:
            raise GestureError("gesture subject does not match this operation")
        if gesture.payload_sha256 != payload:
            raise GestureError("gesture payload hash does not match this operation")
        if gesture.context_sha256 != context:
            raise GestureError("gesture context does not match the displayed receipts")
        if gesture.expires_at is not None and _parse_timestamp(
            observed,
            "gesture observed_at",
        ) >= _parse_timestamp(gesture.expires_at, "gesture expires_at"):
            raise GestureError("gesture has expired")
        return gesture

    def verify_gesture(
        self,
        gesture_id: str,
        *,
        actor: Actor,
        subject_ref: str,
        payload_sha256: str,
        expected_context_sha256: str | None,
        allowed_kinds: Collection[str],
        observed_at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> GestureRecord:
        """Verify an exact human decision without consuming it."""

        with self._read_connection(conn) as read_conn:
            return self._verify_gesture_locked(
                read_conn,
                gesture_id,
                actor=actor,
                subject_ref=subject_ref,
                payload_sha256=payload_sha256,
                expected_context_sha256=expected_context_sha256,
                allowed_kinds=allowed_kinds,
                observed_at=observed_at,
            )

    def verify_and_consume_gesture(
        self,
        gesture_id: str,
        *,
        actor: Actor,
        subject_ref: str,
        payload_sha256: str,
        expected_context_sha256: str | None,
        allowed_kinds: Collection[str],
        observed_at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> GestureRecord:
        """Verify and consume a gesture atomically, including redaction gestures."""

        observed = _timestamp(observed_at, "gesture observed_at")
        with self.store.write_transaction(conn) as write_conn:
            self._verify_gesture_locked(
                write_conn,
                gesture_id,
                actor=actor,
                subject_ref=subject_ref,
                payload_sha256=payload_sha256,
                expected_context_sha256=expected_context_sha256,
                allowed_kinds=allowed_kinds,
                observed_at=observed,
            )
            return self.store._consume_gesture_locked(
                write_conn,
                gesture_id,
                consumed_at=observed,
            )

    def _assess_premises_locked(
        self,
        conn: sqlite3.Connection,
        claim_id: str,
    ) -> PremiseAssessment:
        local_unconfirmed: set[str] = set()
        unresolved_uris: set[str] = set()
        rows = conn.execute(
            "SELECT p.premise_kind, p.premise_ref "
            "FROM derivations d JOIN derivation_premises p "
            "ON p.derivation_id = d.id WHERE d.claim_id = ? "
            "ORDER BY p.premise_ref",
            (claim_id,),
        ).fetchall()
        for row in rows:
            kind = row["premise_kind"]
            ref = row["premise_ref"]
            if kind == "local":
                event = self.store._latest_status_locked(
                    conn,
                    ref,
                    include_overlay=True,
                )
                if event is None or event.status != "confirmed":
                    local_unconfirmed.add(ref)
                continue
            try:
                parsed = parse_truth_uri(ref)
            except ValueError:
                unresolved_uris.add(ref)
                continue
            if parsed.store_id != self.store.store_id or parsed.kind != "claim":
                unresolved_uris.add(ref)
                continue
            event = self.store._latest_status_locked(
                conn,
                parsed.record_id,
                include_overlay=True,
            )
            if event is None or event.status != "confirmed":
                local_unconfirmed.add(parsed.record_id)
        local = tuple(sorted(local_unconfirmed))
        unresolved = tuple(sorted(unresolved_uris))
        return PremiseAssessment(
            local_unconfirmed=local,
            unresolved_uris=unresolved,
            confirmed=not local and not unresolved,
        )

    def assess_premises(
        self,
        claim_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> PremiseAssessment:
        """Assess premises without raising for cross-store or unavailable refs."""

        identifier = _record_id(claim_id, "claim_id")
        with self._read_connection(conn) as read_conn:
            if self.store._get_claim_locked(read_conn, identifier) is None:
                raise InvariantViolation(f"claim does not exist: {identifier}")
            return self._assess_premises_locked(read_conn, identifier)

    def _assess_support_locked(
        self,
        conn: sqlite3.Connection,
        claim_id: str,
    ) -> SupportAssessment:
        rows = conn.execute(
            "SELECT l.to_ref AS span_id, s.author_kind, s.redacted_at AS span_redacted, "
            "e.trust_class, e.derived_from_store, e.redacted_at AS evidence_redacted "
            "FROM claim_links l "
            "LEFT JOIN link_retractions lr ON lr.link_id = l.id "
            "LEFT JOIN evidence_spans s ON s.id = l.to_ref "
            "LEFT JOIN evidence e ON e.id = s.evidence_id "
            "WHERE l.from_claim_id = ? AND l.link_type = 'supports_span' "
            "AND l.to_kind = 'evidence_span' AND lr.link_id IS NULL "
            "ORDER BY l.to_ref",
            (claim_id,),
        ).fetchall()
        support_ids = tuple(sorted({row["span_id"] for row in rows}))
        usable: list[sqlite3.Row] = []
        store_derived_rows: list[sqlite3.Row] = []
        for row in rows:
            if row["span_redacted"] is not None or row["evidence_redacted"] is not None:
                continue
            if row["derived_from_store"] is not None:
                store_derived_rows.append(row)
                continue
            if row["trust_class"] is not None:
                usable.append(row)
        usable_ids = tuple(sorted({row["span_id"] for row in usable}))
        quarantined_only = bool(usable) and all(
            row["trust_class"] == "external_quarantined" for row in usable
        )
        agent_authored_only = bool(usable) and all(
            row["trust_class"] == "agent_authored" or row["author_kind"] == "agent_run"
            for row in usable
        )
        store_derived_only = (
            bool(rows) and not usable and len(store_derived_rows) == len(rows)
        )
        return SupportAssessment(
            support_span_ids=support_ids,
            usable_span_ids=usable_ids,
            quarantined_only=quarantined_only,
            agent_authored_only=agent_authored_only,
            store_derived_only=store_derived_only,
        )

    def _has_usable_support_at_locked(
        self,
        conn: sqlite3.Connection,
        claim_id: str,
        boundary_at: str,
    ) -> bool:
        """Return whether usable support already existed at a decision boundary.

        Challenge writes may receive an explicit historical timestamp. Current
        support alone is therefore insufficient: a later support edge must not
        make a backdated challenge valid. Rows are already present before the
        challenge event is appended, so equal timestamps are valid here. The
        ledger sequence disambiguates equal-time history during integrity reads.
        """

        row = conn.execute(
            "SELECT 1 FROM claim_links l "
            "JOIN evidence_spans s ON s.id = l.to_ref "
            "JOIN evidence e ON e.id = s.evidence_id "
            "LEFT JOIN link_retractions lr ON lr.link_id = l.id "
            "WHERE l.from_claim_id = ? AND l.link_type = 'supports_span' "
            "AND l.to_kind = 'evidence_span' AND lr.link_id IS NULL "
            "AND julianday(l.created_at) <= julianday(?) "
            "AND julianday(s.created_at) <= julianday(?) "
            "AND julianday(e.created_at) <= julianday(?) "
            "AND s.redacted_at IS NULL AND e.redacted_at IS NULL "
            "AND e.derived_from_store IS NULL AND e.trust_class IS NOT NULL "
            "LIMIT 1",
            (claim_id, boundary_at, boundary_at, boundary_at),
        ).fetchone()
        return row is not None

    def assess_support(
        self,
        claim_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> SupportAssessment:
        """Summarize current active support without counting store-derived rows."""

        identifier = _record_id(claim_id, "claim_id")
        with self._read_connection(conn) as read_conn:
            if self.store._get_claim_locked(read_conn, identifier) is None:
                raise InvariantViolation(f"claim does not exist: {identifier}")
            return self._assess_support_locked(read_conn, identifier)

    def _transition_locked(
        self,
        conn: sqlite3.Connection,
        *,
        claim_id: str,
        status: str,
        actor: Actor,
        basis_kind: str,
        basis_ref: str | None,
        note: str | None,
        event_id: str | None,
        at: str | None,
    ) -> TransitionResult:
        claim = self.store._get_claim_locked(conn, claim_id)
        if claim is None:
            raise InvariantViolation(f"claim does not exist: {claim_id}")
        full = self.store._latest_status_locked(conn, claim_id, include_overlay=True)
        base = self.store._latest_status_locked(conn, claim_id, include_overlay=False)
        if full is None or base is None:
            raise InvariantViolation(f"claim has no status history: {claim_id}")
        previous = full.status
        event_at = _timestamp(at, "status event at")
        if _parse_timestamp(event_at, "status event at") < _parse_timestamp(
            claim.created_at,
            "claim created_at",
        ):
            raise TransitionError("status event cannot predate claim creation")
        if status == "needs_review":
            if actor.kind != "system" or basis_kind not in REVIEW_BASIS_KINDS:
                raise TransitionError(
                    "needs_review may only be entered by a sweep or rule"
                )
            if full.status == "needs_review":
                return TransitionResult(full, False, previous)
            if base.status in TERMINAL_STATUSES:
                raise TransitionError("terminal claims cannot enter needs_review")
            if _parse_timestamp(event_at, "status event at") < _parse_timestamp(
                full.at,
                "latest status event at",
            ):
                raise TransitionError(
                    "status event cannot predate the latest status event"
                )
            event = self.store._insert_status_event_locked(
                conn,
                claim_id=claim_id,
                status=status,
                actor=actor,
                basis_kind=basis_kind,
                basis_ref=basis_ref,
                note=note,
                event_id=event_id,
                at=event_at,
            )
            return TransitionResult(event, True, previous)

        if status not in _BASE_TRANSITIONS:
            raise TransitionError(f"unsupported base status {status!r}")
        if status == "retracted":
            if basis_kind != "redaction" or not basis_ref:
                raise TransitionError(
                    "retraction requires a sanctioned redaction event"
                )
            redaction = conn.execute(
                "SELECT subject_kind, subject_ref, actor_ref, at "
                "FROM redaction_events WHERE id = ?",
                (basis_ref,),
            ).fetchone()
            if redaction is None:
                raise TransitionError(
                    "retraction basis does not identify a redaction event"
                )
            if (
                redaction["subject_kind"] != "claim"
                or redaction["subject_ref"] != claim_id
                or redaction["actor_ref"] != actor.ref
                or redaction["at"] != event_at
            ):
                raise TransitionError(
                    "retraction basis does not match this claim transition"
                )
        human_clear = actor.kind == "human" and basis_kind == "gesture"
        if base.status == status and not (
            full.status == "needs_review" and human_clear
        ):
            return TransitionResult(base, False, previous)
        overlay_clear = (
            base.status == status and full.status == "needs_review" and human_clear
        )
        if not overlay_clear and status not in _BASE_TRANSITIONS.get(
            base.status,
            frozenset(),
        ):
            raise TransitionError(f"cannot transition {base.status} to {status}")
        if _parse_timestamp(event_at, "status event at") < _parse_timestamp(
            full.at,
            "latest status event at",
        ):
            raise TransitionError("status event cannot predate the latest status event")
        if basis_kind == "gesture":
            if not basis_ref:
                raise TransitionError("gesture-based transitions require a gesture id")
            gesture = self.store._get_gesture_locked(conn, basis_ref)
            if gesture is None:
                raise TransitionError(f"gesture does not exist: {basis_ref}")
            if _parse_timestamp(event_at, "status event at") < _parse_timestamp(
                gesture.at,
                "gesture at",
            ):
                raise TransitionError(
                    "gesture-based status events cannot predate the human decision"
                )
        event = self.store._insert_status_event_locked(
            conn,
            claim_id=claim_id,
            status=status,
            actor=actor,
            basis_kind=basis_kind,
            basis_ref=basis_ref,
            note=note,
            event_id=event_id,
            at=event_at,
        )
        return TransitionResult(event, True, previous)

    def transition_claim(
        self,
        *,
        claim_id: str,
        status: str,
        actor: Actor,
        basis_kind: str,
        basis_ref: str | None = None,
        note: str | None = None,
        event_id: str | None = None,
        at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> TransitionResult:
        """Append the status half of an already-recorded sanctioned redaction.

        Callers cannot use this as a general-purpose terminal transition. The
        referenced redaction event must already exist for the same claim,
        actor, and timestamp in the enclosing transaction.
        """

        identifier = _record_id(claim_id, "claim_id")
        target = _text(status, "status")
        basis = _text(basis_kind, "basis_kind")
        if target != "retracted":
            raise TransitionError(
                "use the specialized lifecycle operation for guarded transitions"
            )
        if basis == "gesture" and actor.kind != "human":
            raise TransitionError("agents cannot claim a human gesture basis")
        with self.store.write_transaction(conn) as write_conn:
            return self._transition_locked(
                write_conn,
                claim_id=identifier,
                status=target,
                actor=actor,
                basis_kind=basis,
                basis_ref=basis_ref,
                note=note,
                event_id=event_id,
                at=at,
            )

    def mark_needs_review(
        self,
        *,
        claim_id: str,
        actor: Actor,
        basis_kind: str,
        basis_ref: str | None = None,
        note: str | None = None,
        event_id: str | None = None,
        at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> TransitionResult:
        """Append the non-terminal needs-review overlay from a rule or sweep."""

        identifier = _record_id(claim_id, "claim_id")
        basis = _text(basis_kind, "basis_kind")
        with self.store.write_transaction(conn) as write_conn:
            return self._transition_locked(
                write_conn,
                claim_id=identifier,
                status="needs_review",
                actor=actor,
                basis_kind=basis,
                basis_ref=basis_ref,
                note=note,
                event_id=event_id,
                at=at,
            )

    def _ensure_confirmation_ready_locked(
        self,
        conn: sqlite3.Connection,
        claim_id: str,
    ) -> SupportAssessment:
        premises = self._assess_premises_locked(conn, claim_id)
        if not premises.confirmed:
            detail = [*premises.local_unconfirmed, *premises.unresolved_uris]
            raise TransitionError(
                "weakest-link confirmation blocked by premises: " + ", ".join(detail)
            )
        support = self._assess_support_locked(conn, claim_id)
        if support.support_span_ids and not support.usable_span_ids:
            raise TransitionError(
                "confirmation has no usable non-store-derived support"
            )
        return support

    def _active_supersedes_locked(
        self,
        conn: sqlite3.Connection,
        successor_id: str,
    ) -> tuple[ClaimLinkRecord, ...]:
        rows = conn.execute(
            "SELECT l.* FROM claim_links l "
            "LEFT JOIN link_retractions lr ON lr.link_id = l.id "
            "WHERE l.from_claim_id = ? AND l.link_type = 'supersedes' "
            "AND l.to_kind = 'claim' AND lr.link_id IS NULL ORDER BY l.id",
            (successor_id,),
        ).fetchall()
        return tuple(ClaimLinkRecord(**dict(row)) for row in rows)

    def _supersession_conflicts_locked(
        self,
        conn: sqlite3.Connection,
        successor_id: str,
        links: tuple[ClaimLinkRecord, ...],
    ) -> tuple[str, ...]:
        conflicts: set[str] = set()
        for link in links:
            rows = conn.execute(
                "SELECT l.from_claim_id FROM claim_links l "
                "LEFT JOIN link_retractions lr ON lr.link_id = l.id "
                "WHERE l.link_type = 'supersedes' AND l.to_kind = 'claim' "
                "AND l.to_ref = ? AND l.from_claim_id != ? AND lr.link_id IS NULL",
                (link.to_ref, successor_id),
            ).fetchall()
            for row in rows:
                other_id = row["from_claim_id"]
                ever_confirmed = conn.execute(
                    "SELECT 1 FROM claim_status_events "
                    "WHERE claim_id = ? AND status = 'confirmed' LIMIT 1",
                    (other_id,),
                ).fetchone()
                if ever_confirmed is not None:
                    conflicts.add(other_id)
        return tuple(sorted(conflicts))

    def confirm_claim(
        self,
        *,
        claim_id: str,
        gesture_id: str,
        actor: Actor,
        expected_context_sha256: str | None,
        observed_at: str | None = None,
        event_id: str | None = None,
        at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> ConfirmationResult:
        """Confirm one exact claim and atomically apply supersession effects."""

        identifier = _record_id(claim_id, "claim_id")
        observed = _timestamp(observed_at or at, "confirmation observed_at")
        with self.store.write_transaction(conn) as write_conn:
            claim = self.store._get_claim_locked(write_conn, identifier)
            if claim is None:
                raise InvariantViolation(f"claim does not exist: {identifier}")
            base = self.store._latest_status_locked(
                write_conn,
                identifier,
                include_overlay=False,
            )
            if base is None or (
                base.status not in {"proposed", "challenged", "confirmed"}
            ):
                state = "missing" if base is None else base.status
                raise TransitionError(f"cannot confirm claim from {state}")
            full = self.store._latest_status_locked(
                write_conn,
                identifier,
                include_overlay=True,
            )
            if base.status == "confirmed" and (
                full is None or full.status != "needs_review"
            ):
                existing_gesture = self.store._get_gesture_locked(
                    write_conn,
                    gesture_id,
                )
                if (
                    existing_gesture is not None
                    and existing_gesture.consumed_at is not None
                ):
                    raise GestureError("gesture has already been consumed")
                raise TransitionError(
                    "claim is already confirmed and the new gesture was not consumed"
                )
            support = self._ensure_confirmation_ready_locked(write_conn, identifier)
            gesture = self.store._get_gesture_locked(write_conn, gesture_id)
            if gesture is None:
                raise GestureError(f"gesture does not exist: {gesture_id}")
            allowed = CONFIRM_GESTURE_KINDS
            if support.quarantined_only:
                allowed = frozenset({"confirm_quarantined_support"})
            elif gesture.kind == "confirm_quarantined_support":
                raise GestureError(
                    "quarantine override is only valid for quarantined-only support"
                )
            self._verify_gesture_locked(
                write_conn,
                gesture_id,
                actor=actor,
                subject_ref=identifier,
                payload_sha256=claim.canonical_sha256,
                expected_context_sha256=expected_context_sha256,
                allowed_kinds=allowed,
                observed_at=observed,
            )
            if gesture.surface not in self.store.profile.gate.confirmation_surfaces:
                raise GestureError(
                    f"confirmation surface {gesture.surface!r} is not allowed by profile"
                )
            supersedes = self._active_supersedes_locked(write_conn, identifier)
            conflicts = self._supersession_conflicts_locked(
                write_conn,
                identifier,
                supersedes,
            )
            consumed = self.store._consume_gesture_locked(
                write_conn,
                gesture_id,
                consumed_at=observed,
            )
            if conflicts:
                review = self._transition_locked(
                    write_conn,
                    claim_id=identifier,
                    status="needs_review",
                    actor=Actor("system", "truth-lifecycle"),
                    basis_kind="conflict",
                    basis_ref=gesture_id,
                    note="competing confirmed successors: " + ", ".join(conflicts),
                    event_id=event_id,
                    at=at or observed,
                )
                return ConfirmationResult(
                    event=None,
                    created=False,
                    gesture=consumed,
                    superseded_events=(),
                    needs_review_event=review.event,
                )
            transition = self._transition_locked(
                write_conn,
                claim_id=identifier,
                status="confirmed",
                actor=actor,
                basis_kind="gesture",
                basis_ref=gesture_id,
                note=None,
                event_id=event_id,
                at=at or observed,
            )
            superseded_events: list[StatusEventRecord] = []
            if transition.created:
                for link in supersedes:
                    predecessor = self._transition_locked(
                        write_conn,
                        claim_id=link.to_ref,
                        status="superseded",
                        actor=actor,
                        basis_kind="claim_link",
                        basis_ref=link.id,
                        note=None,
                        event_id=None,
                        at=at or observed,
                    )
                    superseded_events.append(predecessor.event)
            return ConfirmationResult(
                event=transition.event,
                created=transition.created,
                gesture=consumed,
                superseded_events=tuple(superseded_events),
                needs_review_event=None,
            )

    def challenge_claim(
        self,
        *,
        claim_id: str,
        challenging_claim_id: str,
        actor: Actor,
        note: str | None = None,
        link_id: str | None = None,
        event_id: str | None = None,
        at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> TransitionResult:
        """Challenge a confirmed claim using a supported conflicting claim."""

        target = _record_id(claim_id, "claim_id")
        challenger = _record_id(challenging_claim_id, "challenging_claim_id")
        if target == challenger:
            raise TransitionError("a claim cannot challenge itself")
        if actor.kind == "system":
            raise TransitionError("system actors cannot author a challenge")
        event_at = _timestamp(at, "challenge at")
        with self.store.write_transaction(conn) as write_conn:
            base = self.store._latest_status_locked(
                write_conn,
                target,
                include_overlay=False,
            )
            if base is None or base.status not in {"confirmed", "challenged"}:
                state = "missing" if base is None else base.status
                raise TransitionError(f"cannot challenge claim from {state}")
            challenging_claim = self.store._get_claim_locked(write_conn, challenger)
            if challenging_claim is None:
                raise InvariantViolation(
                    f"challenging claim does not exist: {challenger}"
                )
            challenger_base = self.store._latest_status_locked(
                write_conn,
                challenger,
                include_overlay=False,
            )
            if challenger_base is None:
                raise InvariantViolation(
                    f"challenging claim has no status history: {challenger}"
                )
            if challenger_base.status in TERMINAL_STATUSES:
                raise TransitionError(
                    "a terminal claim cannot serve as a live challenge"
                )
            if _parse_timestamp(event_at, "challenge at") < _parse_timestamp(
                challenger_base.at,
                "challenger latest status at",
            ):
                raise TransitionError(
                    "challenge cannot predate the challenger's latest status"
                )
            if challenging_claim.redacted_at is not None:
                raise TransitionError(
                    "a claim with redacted content cannot serve as a live challenge"
                )
            if not self._has_usable_support_at_locked(
                write_conn,
                challenger,
                event_at,
            ):
                raise TransitionError(
                    "challenging claim requires usable supporting evidence "
                    "at the challenge boundary"
                )
            raw = write_conn.execute(
                "SELECT l.* FROM claim_links l "
                "LEFT JOIN link_retractions lr ON lr.link_id = l.id "
                "WHERE l.from_claim_id = ? AND l.link_type = 'conflicts_with' "
                "AND l.to_kind = 'claim' AND l.to_ref = ? AND lr.link_id IS NULL "
                "ORDER BY l.id LIMIT 1",
                (challenger, target),
            ).fetchone()
            link = _row_link(raw)
            if link is None:
                link = self.store.add_link(
                    from_claim_id=challenger,
                    link_type="conflicts_with",
                    to_kind="claim",
                    to_ref=target,
                    actor=actor,
                    record_id=link_id,
                    created_at=event_at,
                    conn=write_conn,
                )
            elif _parse_timestamp(
                link.created_at,
                "conflict link created_at",
            ) > _parse_timestamp(event_at, "challenge at"):
                raise TransitionError("challenge cannot predate its conflict link")
            transition = self._transition_locked(
                write_conn,
                claim_id=target,
                status="challenged",
                actor=actor,
                basis_kind="conflict_link",
                basis_ref=link.id,
                note=note,
                event_id=event_id,
                at=event_at,
            )
            return transition

    def supersede_claim(
        self,
        *,
        successor_claim_id: str,
        predecessor_claim_id: str,
        reason: str,
        actor: Actor,
        note: str | None = None,
        link_id: str | None = None,
        created_at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> ClaimLinkRecord:
        """Propose a typed supersession link without changing either status."""

        successor_id = _record_id(successor_claim_id, "successor_claim_id")
        predecessor_id = _record_id(predecessor_claim_id, "predecessor_claim_id")
        relation_reason = _text(reason, "reason")
        if relation_reason not in SUPERSESSION_REASONS:
            raise TransitionError(
                f"unsupported supersession reason {relation_reason!r}"
            )
        with self.store.write_transaction(conn) as write_conn:
            successor = self.store._get_claim_locked(write_conn, successor_id)
            predecessor = self.store._get_claim_locked(write_conn, predecessor_id)
            if successor is None or predecessor is None:
                raise InvariantViolation("supersession claims must exist in this store")
            successor_status = self.store._latest_status_locked(
                write_conn,
                successor_id,
                include_overlay=False,
            )
            predecessor_status = self.store._latest_status_locked(
                write_conn,
                predecessor_id,
                include_overlay=False,
            )
            if successor_status is None or successor_status.status != "proposed":
                raise TransitionError("a supersession successor must still be proposed")
            if predecessor_status is None or predecessor_status.status not in {
                "confirmed",
                "challenged",
            }:
                raise TransitionError("a supersession predecessor must be confirmed")
            if (
                relation_reason
                in {
                    "updated",
                    "preference_changed",
                }
                and successor.valid_from is None
            ):
                raise TransitionError(
                    f"supersession reason {relation_reason!r} requires successor valid_from"
                )
            if relation_reason == "valid_time_closed" and successor.valid_to is None:
                raise TransitionError(
                    "supersession reason 'valid_time_closed' requires successor valid_to"
                )
            rows = write_conn.execute(
                "SELECT l.* FROM claim_links l "
                "LEFT JOIN link_retractions lr ON lr.link_id = l.id "
                "WHERE l.from_claim_id = ? AND l.link_type = 'supersedes' "
                "AND l.to_kind = 'claim' AND l.to_ref = ? AND lr.link_id IS NULL "
                "ORDER BY l.id",
                (successor_id, predecessor_id),
            ).fetchall()
            for row in rows:
                existing = ClaimLinkRecord(**dict(row))
                role = json.loads(existing.role_json or "{}")
                if role.get("supersession_reason") == relation_reason:
                    return existing
                raise TransitionError(
                    "an active supersession already uses a different reason"
                )
            role: dict[str, Any] = {"supersession_reason": relation_reason}
            if note is not None:
                role["note"] = _text(note, "note")
            return self.store.add_link(
                from_claim_id=successor_id,
                link_type="supersedes",
                to_kind="claim",
                to_ref=predecessor_id,
                actor=actor,
                role=role,
                record_id=link_id,
                created_at=created_at,
                conn=write_conn,
            )

    def expire_claim(
        self,
        *,
        claim_id: str,
        actor: Actor,
        observed_at: str | None = None,
        rule: str = "proposal_max_age",
        event_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> TransitionResult:
        """Expire an overdue proposal according to the current profile rule."""

        identifier = _record_id(claim_id, "claim_id")
        if actor.kind != "system":
            raise TransitionError("only a system rule may expire a proposal")
        observed = _timestamp(observed_at, "expiry observed_at")
        expiry_rule = _text(rule, "rule")
        if expiry_rule not in {"proposal_max_age", "session_end"}:
            raise TransitionError(f"unsupported expiry rule {expiry_rule!r}")
        max_age = self.store.profile.proposal_max_age_seconds
        if expiry_rule == "proposal_max_age" and max_age is None:
            raise TransitionError("this profile does not define proposal_max_age")
        with self.store.write_transaction(conn) as write_conn:
            base = self.store._latest_status_locked(
                write_conn,
                identifier,
                include_overlay=False,
            )
            if base is None:
                raise InvariantViolation(f"claim does not exist: {identifier}")
            if base.status == "expired":
                self._apply_terminal_content_policy_locked(
                    write_conn,
                    claim_id=identifier,
                    terminal_status="expired",
                    at=base.at,
                )
                return TransitionResult(base, False, base.status)
            if base.status != "proposed":
                raise TransitionError(f"cannot expire claim from {base.status}")
            proposed = _row_status(
                write_conn.execute(
                    "SELECT * FROM claim_status_events WHERE claim_id = ? "
                    "AND status = 'proposed' ORDER BY seq LIMIT 1",
                    (identifier,),
                ).fetchone()
            )
            if proposed is None:
                raise InvariantViolation("proposal has no proposed status event")
            if expiry_rule == "proposal_max_age":
                assert max_age is not None
                due = _parse_timestamp(proposed.at, "proposed at") + timedelta(
                    seconds=max_age
                )
                if _parse_timestamp(observed, "expiry observed_at") < due:
                    raise TransitionError("proposal has not reached its expiry time")
                basis_ref = f"proposal_max_age:{max_age}"
            else:
                basis_ref = "session_end"
            transition = self._transition_locked(
                write_conn,
                claim_id=identifier,
                status="expired",
                actor=actor,
                basis_kind="rule",
                basis_ref=basis_ref,
                note=None,
                event_id=event_id,
                at=observed,
            )
            self._apply_terminal_content_policy_locked(
                write_conn,
                claim_id=identifier,
                terminal_status="expired",
                at=transition.event.at,
            )
            return transition

    def _apply_terminal_content_policy_locked(
        self,
        conn: sqlite3.Connection,
        *,
        claim_id: str,
        terminal_status: str,
        at: str,
    ) -> None:
        """Apply the profile's terminal content policy in the same transaction."""

        reason: str | None = None
        if (
            terminal_status == "rejected"
            and self.store.profile.gate.rejected_content == "redact"
        ):
            reason = "rejected_content"
        elif terminal_status == "expired" and (
            self.store.profile.proposal_max_age_seconds is not None
            or self.store.profile.extensions.get("expired_proposal_content") == "redact"
        ):
            reason = "expired_content"
        if reason is None:
            return

        from work_buddy.truth.redact import TruthRedactor, policy_basis_ref

        TruthRedactor(self.store, lifecycle=self).redact(
            subject_kind="claim",
            subject_ref=claim_id,
            actor=Actor("system", "truth-lifecycle-policy"),
            reason=reason,
            basis_kind="policy",
            basis_ref=policy_basis_ref(self.store, reason),
            at=at,
            conn=conn,
        )

    def rejection_context_sha256(
        self,
        source_claim_id: str,
        displayed_receipts: Any,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Bind a replacement gesture to source identity, hash, and receipts."""

        identifier = _record_id(source_claim_id, "source_claim_id")
        with self._read_connection(conn) as read_conn:
            source = self.store._get_claim_locked(read_conn, identifier)
            if source is None:
                raise InvariantViolation(f"claim does not exist: {identifier}")
            return hash_context(
                {
                    "source_claim_id": source.id,
                    "source_canonical_sha256": source.canonical_sha256,
                    "receipts": displayed_receipts,
                }
            )

    def reject_claim(
        self,
        *,
        source_claim_id: str,
        gesture_id: str,
        actor: Actor,
        reason_class: str,
        expected_context_sha256: str | None,
        displayed_receipts: Any = None,
        result_claim_id: str | None = None,
        observed_at: str | None = None,
        source_event_id: str | None = None,
        result_event_id: str | None = None,
        link_id: str | None = None,
        at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> RejectionResult:
        """Apply a reason-classed rejection and the profile's content policy."""

        source_id = _record_id(source_claim_id, "source_claim_id")
        rejection = _text(reason_class, "reason_class")
        if rejection not in REJECTION_CLASSES:
            raise TransitionError(f"unsupported rejection class {rejection!r}")
        observed = _timestamp(observed_at or at, "rejection observed_at")
        with self.store.write_transaction(conn) as write_conn:
            source = self.store._get_claim_locked(write_conn, source_id)
            if source is None:
                raise InvariantViolation(f"claim does not exist: {source_id}")
            source_status = self.store._latest_status_locked(
                write_conn,
                source_id,
                include_overlay=False,
            )
            if source_status is None or source_status.status != "proposed":
                state = "missing" if source_status is None else source_status.status
                raise TransitionError(f"cannot reject claim from {state}")
            if rejection == "reject_plain":
                if result_claim_id is not None:
                    raise TransitionError("plain rejection cannot carry a result claim")
                gesture = self._verify_gesture_locked(
                    write_conn,
                    gesture_id,
                    actor=actor,
                    subject_ref=source_id,
                    payload_sha256=source.canonical_sha256,
                    expected_context_sha256=expected_context_sha256,
                    allowed_kinds={rejection},
                    observed_at=observed,
                )
                if gesture.surface not in self.store.profile.gate.confirmation_surfaces:
                    raise GestureError(
                        f"confirmation surface {gesture.surface!r} "
                        "is not allowed by profile"
                    )
                consumed = self.store._consume_gesture_locked(
                    write_conn,
                    gesture_id,
                    consumed_at=observed,
                )
                source_event = self._transition_locked(
                    write_conn,
                    claim_id=source_id,
                    status="rejected",
                    actor=actor,
                    basis_kind="gesture",
                    basis_ref=gesture_id,
                    note=rejection,
                    event_id=source_event_id,
                    at=at or observed,
                ).event
                self._apply_terminal_content_policy_locked(
                    write_conn,
                    claim_id=source_id,
                    terminal_status="rejected",
                    at=source_event.at,
                )
                return RejectionResult(
                    source_event=source_event,
                    result_claim=None,
                    result_event=None,
                    refutes_link=None,
                    gesture=consumed,
                )

            if result_claim_id is None:
                raise TransitionError(
                    f"{rejection} requires a preallocated result claim"
                )
            result_id = _record_id(result_claim_id, "result_claim_id")
            if result_id == source_id:
                raise TransitionError("the rejection result must be a different claim")
            result = self.store._get_claim_locked(write_conn, result_id)
            if result is None:
                raise InvariantViolation(f"result claim does not exist: {result_id}")
            if (
                rejection == "reject_as_preference"
                and result.claim_kind != "preference"
            ):
                raise TransitionError(
                    "preference rejection requires a preference result claim"
                )
            if rejection == "reject_as_false" and (
                result.proposition != negated_proposition(source.proposition)
                or result.claim_kind != source.claim_kind
                or result.scope != source.scope
            ):
                raise TransitionError(
                    "false rejection result must be the deterministic negation "
                    "of the source claim in the same kind and scope"
                )
            result_status = self.store._latest_status_locked(
                write_conn,
                result_id,
                include_overlay=False,
            )
            if result_status is None or result_status.status != "proposed":
                state = "missing" if result_status is None else result_status.status
                raise TransitionError(f"rejection result must be proposed, not {state}")
            bound_context = hash_context(
                {
                    "source_claim_id": source.id,
                    "source_canonical_sha256": source.canonical_sha256,
                    "receipts": displayed_receipts,
                }
            )
            if (
                expected_context_sha256 is not None
                and _digest(
                    expected_context_sha256,
                    "expected_context_sha256",
                )
                != bound_context
            ):
                raise GestureError(
                    "rejection context is not bound to the source and displayed receipts"
                )
            support = self._ensure_confirmation_ready_locked(write_conn, result_id)
            gesture = self.store._get_gesture_locked(write_conn, gesture_id)
            if gesture is None:
                raise GestureError(f"gesture does not exist: {gesture_id}")
            if support.quarantined_only:
                raise TransitionError(
                    "reason-classed rejection cannot bypass quarantined-only support"
                )
            self._verify_gesture_locked(
                write_conn,
                gesture_id,
                actor=actor,
                subject_ref=result_id,
                payload_sha256=result.canonical_sha256,
                expected_context_sha256=bound_context,
                allowed_kinds={rejection},
                observed_at=observed,
            )
            if gesture.surface not in self.store.profile.gate.confirmation_surfaces:
                raise GestureError(
                    f"confirmation surface {gesture.surface!r} is not allowed by profile"
                )
            supersedes = self._active_supersedes_locked(write_conn, result_id)
            conflicts = self._supersession_conflicts_locked(
                write_conn,
                result_id,
                supersedes,
            )
            if supersedes or conflicts:
                raise TransitionError(
                    "rejection result cannot also be a supersession proposal"
                )

            refutes: ClaimLinkRecord | None = None
            if rejection == "reject_as_false":
                binding_role = rejection_binding_role(
                    rejection_class=rejection,
                    source_canonical_sha256=source.canonical_sha256,
                    result_canonical_sha256=result.canonical_sha256,
                )
                existing = _row_link(
                    write_conn.execute(
                        "SELECT l.* FROM claim_links l "
                        "LEFT JOIN link_retractions lr ON lr.link_id = l.id "
                        "WHERE l.from_claim_id = ? AND l.link_type = 'refutes' "
                        "AND l.to_kind = 'claim' AND l.to_ref = ? "
                        "AND lr.link_id IS NULL ORDER BY l.id LIMIT 1",
                        (result_id, source_id),
                    ).fetchone()
                )
                if existing is not None:
                    if existing.role_json != canonical_json(binding_role):
                        raise TransitionError(
                            "existing refutes link has an incompatible rejection binding"
                        )
                    refutes = existing
                else:
                    refutes = self.store.add_link(
                        from_claim_id=result_id,
                        link_type="refutes",
                        to_kind="claim",
                        to_ref=source_id,
                        actor=actor,
                        role=binding_role,
                        record_id=link_id,
                        created_at=at or observed,
                        conn=write_conn,
                    )
            source_event = self._transition_locked(
                write_conn,
                claim_id=source_id,
                status="rejected",
                actor=actor,
                basis_kind="gesture",
                basis_ref=gesture_id,
                note=rejection,
                event_id=source_event_id,
                at=at or observed,
            ).event
            consumed = self.store._consume_gesture_locked(
                write_conn,
                gesture_id,
                consumed_at=observed,
            )
            result_event = self._transition_locked(
                write_conn,
                claim_id=result_id,
                status="confirmed",
                actor=actor,
                basis_kind="gesture",
                basis_ref=gesture_id,
                note=rejection,
                event_id=result_event_id,
                at=at or observed,
            ).event
            self._apply_terminal_content_policy_locked(
                write_conn,
                claim_id=source_id,
                terminal_status="rejected",
                at=source_event.at,
            )
            return RejectionResult(
                source_event=source_event,
                result_claim=result,
                result_event=result_event,
                refutes_link=refutes,
                gesture=consumed,
            )


__all__ = [
    "CONFIRM_GESTURE_KINDS",
    "GESTURE_KINDS",
    "PROPOSAL_ACCEPT_KINDS",
    "PROPOSAL_REJECT_KINDS",
    "PROPOSAL_ROUTING_KINDS",
    "REJECTION_BINDING_FIELDS",
    "REJECTION_BINDING_HASH_FIELD",
    "REJECTION_CLASSES",
    "ConfirmationResult",
    "PremiseAssessment",
    "RejectionResult",
    "SupportAssessment",
    "TransitionResult",
    "TruthLifecycle",
    "hash_context",
    "negated_proposition",
    "rejection_binding_role",
]

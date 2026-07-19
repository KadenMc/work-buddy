"""Ledger-backed truth queries, projections, sweeps, and integrity checks.

Every authoritative result in this module is derived from immutable ledger
rows. ``claims_current`` is only a rebuildable read model. It is never used as
the source for current or historical claim answers.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any
from urllib.parse import urlparse

from work_buddy.truth.anchors import parse_selector
from work_buddy.truth.migrations import REDACTED_SELECTOR_JSON
from work_buddy.truth.contracts import (
    InvariantViolation,
    TERMINAL_STATUSES,
    VALID_ACTOR_KINDS,
    VALID_STATUSES,
    validate_agent_producer_meta,
)
from work_buddy.truth.fingerprints import (
    FingerprintStatus,
    IMMUTABLE_LINK_TYPES,
    MUTABLE_LINK_TYPES,
    fingerprint_status,
)
from work_buddy.truth.identity import (
    canonical_json,
    claim_sha256,
    new_id,
    parse_truth_uri,
    sha256_text,
    truth_uri,
    utc_now,
)
from work_buddy.truth.locators import (
    DEFAULT_LOCATOR_REGISTRY,
    LocatorError,
    LocatorRegistry,
)
from work_buddy.truth.lifecycle import (
    CONFIRM_GESTURE_KINDS,
    GESTURE_KINDS,
    REJECTION_BINDING_FIELDS,
    REJECTION_BINDING_HASH_FIELD,
    REJECTION_CLASSES,
    REVIEW_BASIS_KINDS,
    negated_proposition,
    rejection_binding_role,
)
from work_buddy.truth.redact import (
    REDACTION_BASIS_KINDS,
    REDACTION_REASONS,
    policy_basis_ref,
)
from work_buddy.truth.store import (
    ACQUISITION_METHODS,
    AUTHORSHIP_KINDS,
    EVIDENCE_KINDS,
    LINK_TARGETS,
    SUPERSESSION_REASONS,
    ClaimLinkRecord,
    ClaimRecord,
    EvidenceRecord,
    TruthStore,
)


# This is the one status resolver used by current, historical, projection, and
# review queries. Base lifecycle state and the needs-review overlay are kept as
# separate columns. A human gesture after an overlay clears it. Terminal base
# states suppress obsolete overlays.
STATUS_RESOLUTION_CTE = """
eligible_status_events AS (
    SELECT e.*
    FROM claim_status_events AS e
    WHERE :belief_at IS NULL
       OR julianday(e.at) <= julianday(:belief_at)
),
ranked_base_statuses AS (
    SELECT
        e.*,
        ROW_NUMBER() OVER (
            PARTITION BY e.claim_id
            ORDER BY e.seq DESC
        ) AS status_rank
    FROM eligible_status_events AS e
    WHERE e.status != 'needs_review'
),
ranked_review_overlays AS (
    SELECT
        e.*,
        ROW_NUMBER() OVER (
            PARTITION BY e.claim_id
            ORDER BY e.seq DESC
        ) AS overlay_rank
    FROM eligible_status_events AS e
    WHERE e.status = 'needs_review'
),
human_overlay_clears AS (
    SELECT e.claim_id, MAX(e.seq) AS clear_seq
    FROM eligible_status_events AS e
    WHERE e.status != 'needs_review'
      AND e.actor_kind = 'human'
      AND e.basis_kind = 'gesture'
    GROUP BY e.claim_id
),
status_resolution AS (
    SELECT
        c.id AS claim_id,
        b.status AS base_status,
        b.seq AS base_status_seq,
        b.id AS base_status_event_id,
        b.at AS base_status_at,
        o.seq AS overlay_seq,
        o.id AS overlay_event_id,
        o.at AS overlay_at,
        CASE
            WHEN o.seq IS NOT NULL
             AND o.seq > COALESCE(h.clear_seq, 0)
             AND COALESCE(b.status, '') NOT IN (
                 'rejected', 'expired', 'superseded', 'retracted'
             )
            THEN 1
            ELSE 0
        END AS needs_review,
        CASE
            WHEN o.seq IS NOT NULL
             AND o.seq > COALESCE(h.clear_seq, 0)
             AND COALESCE(b.status, '') NOT IN (
                 'rejected', 'expired', 'superseded', 'retracted'
             )
            THEN 'needs_review'
            ELSE b.status
        END AS resolved_status,
        CASE
            WHEN o.seq IS NOT NULL
             AND o.seq > COALESCE(h.clear_seq, 0)
             AND COALESCE(b.status, '') NOT IN (
                 'rejected', 'expired', 'superseded', 'retracted'
             )
            THEN o.seq
            ELSE b.seq
        END AS resolved_status_seq
    FROM claims AS c
    LEFT JOIN ranked_base_statuses AS b
      ON b.claim_id = c.id AND b.status_rank = 1
    LEFT JOIN ranked_review_overlays AS o
      ON o.claim_id = c.id AND o.overlay_rank = 1
    LEFT JOIN human_overlay_clears AS h
      ON h.claim_id = c.id
)
"""


DERIVATION_DEPENDENCY_CTE = """
WITH RECURSIVE dependency_walk(
    claim_id, depth, path, via_derivation_id
) AS (
    SELECT
        d.claim_id,
        1,
        ',' || :root_claim_id || ',' || d.claim_id || ',',
        d.id
    FROM derivation_premises AS p
    JOIN derivations AS d ON d.id = p.derivation_id
    JOIN claims AS c ON c.id = d.claim_id
    WHERE (
        p.premise_kind = 'local' AND p.premise_ref = :root_claim_id
    ) OR (
        p.premise_kind = 'uri' AND p.premise_ref = :root_claim_uri
    )

    UNION ALL

    SELECT
        d.claim_id,
        w.depth + 1,
        w.path || d.claim_id || ',',
        d.id
    FROM dependency_walk AS w
    JOIN derivation_premises AS p
      ON (
          p.premise_kind = 'local' AND p.premise_ref = w.claim_id
      ) OR (
          p.premise_kind = 'uri'
          AND p.premise_ref = (
              'wb-truth://' || :store_id || '/claim/' || w.claim_id
          )
      )
    JOIN derivations AS d ON d.id = p.derivation_id
    JOIN claims AS c ON c.id = d.claim_id
    WHERE instr(w.path, ',' || d.claim_id || ',') = 0
),
ranked_dependencies AS (
    SELECT
        w.*,
        ROW_NUMBER() OVER (
            PARTITION BY w.claim_id
            ORDER BY w.depth, w.path, w.via_derivation_id
        ) AS dependency_rank
    FROM dependency_walk AS w
)
SELECT claim_id, depth, path, via_derivation_id
FROM ranked_dependencies
WHERE dependency_rank = 1
ORDER BY depth, claim_id
"""


SUPPORT_DEPENDENCY_CTE = """
WITH RECURSIVE support_roots(claim_id) AS (
    SELECT DISTINCT l.from_claim_id
    FROM claim_links AS l
    JOIN evidence_spans AS s
      ON l.to_kind = 'evidence_span' AND l.to_ref = s.id
    JOIN evidence AS e ON e.id = s.evidence_id
    WHERE l.link_type = 'supports_span'
      AND NOT EXISTS (
          SELECT 1 FROM link_retractions AS r WHERE r.link_id = l.id
      )
      AND (
          (:span_id IS NOT NULL AND s.id = :span_id)
          OR (:evidence_id IS NOT NULL AND e.id = :evidence_id)
      )
),
dependency_walk(claim_id, depth, path, via_derivation_id) AS (
    SELECT
        r.claim_id,
        1,
        ',' || r.claim_id || ',',
        NULL
    FROM support_roots AS r

    UNION ALL

    SELECT
        d.claim_id,
        w.depth + 1,
        w.path || d.claim_id || ',',
        d.id
    FROM dependency_walk AS w
    JOIN derivation_premises AS p
      ON (
          p.premise_kind = 'local' AND p.premise_ref = w.claim_id
      ) OR (
          p.premise_kind = 'uri'
          AND p.premise_ref = (
              'wb-truth://' || :store_id || '/claim/' || w.claim_id
          )
      )
    JOIN derivations AS d ON d.id = p.derivation_id
    JOIN claims AS c ON c.id = d.claim_id
    WHERE instr(w.path, ',' || d.claim_id || ',') = 0
),
ranked_dependencies AS (
    SELECT
        w.*,
        ROW_NUMBER() OVER (
            PARTITION BY w.claim_id
            ORDER BY w.depth, w.path, COALESCE(w.via_derivation_id, '')
        ) AS dependency_rank
    FROM dependency_walk AS w
)
SELECT claim_id, depth, path, via_derivation_id
FROM ranked_dependencies
WHERE dependency_rank = 1
ORDER BY depth, claim_id
"""


_CLAIM_COLUMNS = (
    "id",
    "proposition",
    "canonical_sha256",
    "claim_kind",
    "structured_json",
    "scope",
    "valid_from",
    "valid_to",
    "confidence_extraction",
    "meta_json",
    "redacted_at",
    "created_at",
    "created_by_kind",
    "created_by_ref",
)
_CLOSING_AT_SUCCESSOR_START = frozenset({"updated", "preference_changed"})
_VOIDING_REASONS = frozenset({"corrected"})
_INHERITED_INTERVAL_REASONS = frozenset({"refined", "valid_time_closed"})
_NON_CLOSING_REASONS = frozenset({"refined"})


@dataclass(frozen=True, slots=True)
class ClaimState:
    """One claim resolved from ledger status and supersession history."""

    claim: ClaimRecord
    base_status: str | None
    base_status_seq: int | None
    base_status_event_id: str | None
    base_status_at: str | None
    status: str | None
    status_seq: int | None
    needs_review: bool
    overlay_event_id: str | None
    overlay_at: str | None
    voided: bool
    effective_valid_from: str | None
    effective_valid_to: str | None
    health: str
    health_reason: str | None

    @property
    def claim_id(self) -> str:
        return self.claim.id


@dataclass(frozen=True, slots=True)
class SuccessorRace:
    predecessor_id: str
    successor_ids: tuple[str, ...]
    link_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConflictState:
    link_id: str
    from_claim_id: str
    to_claim_id: str
    conflict_type: str | None
    conflict_class: str | None
    role: Mapping[str, Any]
    from_status: str | None
    to_status: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class NeedsReviewItem:
    subject_kind: str
    subject_ref: str
    base_status: str | None
    overlay_event_id: str | None
    overlay_seq: int | None
    finding_ids: tuple[str, ...]
    findings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SweepCandidate:
    subject_kind: str
    subject_ref: str
    finding: str
    depth: int
    path: tuple[str, ...]
    via_derivation_id: str | None


@dataclass(frozen=True, slots=True)
class SweepFindingSpec:
    subject_kind: str
    subject_ref: str
    finding: str


@dataclass(frozen=True, slots=True)
class RecordedSweep:
    sweep_id: str
    kind: str
    at: str
    finding_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceIntegrityState:
    evidence_id: str
    locator: str
    locator_scheme: str | None
    verifiability_class: str | None
    integrity_recipe: Mapping[str, Any]
    snapshot_present: bool
    state: str
    detail: str | None


@dataclass(frozen=True, slots=True)
class LinkFingerprintState:
    link_id: str
    link_type: str
    to_kind: str
    to_ref: str
    stored_fingerprint: str | None
    current_fingerprint: str | None
    current_fingerprint_known: bool
    status: FingerprintStatus
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class PremiseResolution:
    exists: bool
    status: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class IntegrityFinding:
    code: str
    subject_kind: str
    subject_ref: str
    severity: str
    detail: str


@dataclass(frozen=True, slots=True)
class _SupersessionEdge:
    link: ClaimLinkRecord
    predecessor_id: str
    successor_id: str
    reason: str | None


CrossStoreResolver = Callable[
    [str], PremiseResolution | Mapping[str, Any] | bool | None
]
TargetFingerprintSource = (
    Mapping[str, str | None] | Callable[[ClaimLinkRecord], str | None]
)


def _normalize_query_time(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvariantViolation(f"{label} must be an ISO 8601 date or timestamp")
    raw = value.strip()
    try:
        if "T" not in raw and " " not in raw:
            parsed = datetime.combine(
                date.fromisoformat(raw),
                time.min,
                tzinfo=timezone.utc,
            )
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvariantViolation(
            f"{label} must be an ISO 8601 date or timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvariantViolation(f"{label} timestamps must include a UTC offset")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _time_key(value: str, label: str) -> datetime:
    normalized = _normalize_query_time(value, label)
    assert normalized is not None
    return datetime.fromisoformat(normalized.replace("Z", "+00:00"))


def _try_json_object(value: str | None) -> tuple[dict[str, Any], str | None]:
    if value is None:
        return {}, None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        return {}, str(exc)
    if not isinstance(parsed, dict):
        return {}, "JSON value is not an object"
    return parsed, None


@contextmanager
def _read_connection(
    store: TruthStore,
    conn: sqlite3.Connection | None = None,
) -> Iterator[sqlite3.Connection]:
    if conn is not None:
        store._validate_connection_target(conn)
        yield conn
        return
    owned = store.connect()
    try:
        owned.execute("BEGIN")
        yield owned
    finally:
        if owned.in_transaction:
            owned.execute("ROLLBACK")
        owned.close()


def _claim_from_row(row: sqlite3.Row) -> ClaimRecord:
    return ClaimRecord(**{name: row[name] for name in _CLAIM_COLUMNS})


def _active_supersession_edges(
    conn: sqlite3.Connection,
    *,
    belief_at: str | None,
) -> tuple[_SupersessionEdge, ...]:
    rows = conn.execute(
        f"""
        WITH {STATUS_RESOLUTION_CTE}
        SELECT l.*
        FROM claim_links AS l
        WHERE l.link_type = 'supersedes'
          AND (
              :belief_at IS NULL
              OR julianday(l.created_at) <= julianday(:belief_at)
          )
          AND NOT EXISTS (
              SELECT 1
              FROM link_retractions AS r
              WHERE r.link_id = l.id
                AND (
                    :belief_at IS NULL
                    OR julianday(r.at) <= julianday(:belief_at)
                )
          )
          AND EXISTS (
              SELECT 1
              FROM eligible_status_events AS e
              WHERE e.claim_id = l.from_claim_id
                AND e.status = 'confirmed'
          )
        ORDER BY l.created_at, l.id
        """,
        {"belief_at": belief_at},
    ).fetchall()
    edges: list[_SupersessionEdge] = []
    for row in rows:
        link = ClaimLinkRecord(**dict(row))
        role, _ = _try_json_object(link.role_json)
        reason = role.get("supersession_reason")
        edges.append(
            _SupersessionEdge(
                link=link,
                predecessor_id=link.to_ref,
                successor_id=link.from_claim_id,
                reason=reason if isinstance(reason, str) else None,
            )
        )
    return tuple(edges)


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        while parent != self.parent[parent]:
            parent = self.parent[parent]
        while value != parent:
            next_value = self.parent[value]
            self.parent[value] = parent
            value = next_value
        return parent

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        self.parent[second] = first


def _earliest_valid_time(
    values: Iterable[str],
    *,
    label: str,
    issue_refs: Iterable[str],
    issues: dict[str, set[str]],
) -> str | None:
    parsed: list[tuple[datetime, str]] = []
    for value in values:
        try:
            parsed.append((_time_key(value, label), value))
        except InvariantViolation:
            for claim_id in issue_refs:
                issues[claim_id].add(f"invalid_{label}")
    if not parsed:
        return None
    parsed.sort(key=lambda item: (item[0], item[1]))
    return parsed[0][1]


def _derive_intervals(
    claims: Mapping[str, ClaimRecord],
    edges: Sequence[_SupersessionEdge],
) -> tuple[
    dict[str, tuple[str | None, str | None]],
    dict[str, set[str]],
    frozenset[str],
    tuple[SuccessorRace, ...],
]:
    issues: dict[str, set[str]] = defaultdict(set)
    voided: set[str] = set()
    union = _UnionFind(claims)
    valid_edges: list[_SupersessionEdge] = []
    outgoing: dict[str, list[_SupersessionEdge]] = defaultdict(list)
    for edge in edges:
        if edge.predecessor_id not in claims:
            issues[edge.successor_id].add("dangling_supersession_predecessor")
            continue
        if edge.successor_id not in claims:
            issues[edge.predecessor_id].add("dangling_supersession_successor")
            continue
        valid_edges.append(edge)
        outgoing[edge.predecessor_id].append(edge)
        if edge.reason in _INHERITED_INTERVAL_REASONS:
            union.union(edge.predecessor_id, edge.successor_id)

    races: list[SuccessorRace] = []
    for predecessor_id, predecessor_edges in sorted(outgoing.items()):
        successor_ids = tuple(sorted({edge.successor_id for edge in predecessor_edges}))
        if len(successor_ids) <= 1:
            continue
        issues[predecessor_id].add("single_confirmed_successor_race")
        races.append(
            SuccessorRace(
                predecessor_id=predecessor_id,
                successor_ids=successor_ids,
                link_ids=tuple(sorted(edge.link.id for edge in predecessor_edges)),
            )
        )

    groups: dict[str, list[str]] = defaultdict(list)
    for claim_id in claims:
        groups[union.find(claim_id)].append(claim_id)
    for members in groups.values():
        members.sort()

    group_starts: dict[str, list[str]] = defaultdict(list)
    group_ends: dict[str, list[str]] = defaultdict(list)
    for group_id, members in groups.items():
        for claim_id in members:
            claim = claims[claim_id]
            if claim.valid_from is not None:
                group_starts[group_id].append(claim.valid_from)
            if claim.valid_to is not None:
                group_ends[group_id].append(claim.valid_to)
        distinct_starts: set[datetime] = set()
        for value in group_starts[group_id]:
            try:
                distinct_starts.add(_time_key(value, "valid_from"))
            except InvariantViolation:
                for claim_id in members:
                    issues[claim_id].add("invalid_valid_from")
        if len(distinct_starts) > 1:
            for claim_id in members:
                issues[claim_id].add("inherited_valid_from_conflict")

    for edge in valid_edges:
        predecessor = claims[edge.predecessor_id]
        successor = claims[edge.successor_id]
        predecessor_group = union.find(predecessor.id)
        if edge.reason in _CLOSING_AT_SUCCESSOR_START:
            if successor.valid_from is None:
                issues[predecessor.id].add("supersession_requires_successor_valid_from")
                issues[successor.id].add("supersession_requires_successor_valid_from")
            else:
                group_ends[predecessor_group].append(successor.valid_from)
        elif edge.reason in _VOIDING_REASONS:
            voided.add(predecessor.id)
            issues[predecessor.id].add("voided_by_correction")
        elif edge.reason == "valid_time_closed":
            if successor.valid_to is None:
                issues[predecessor.id].add(
                    "valid_time_closed_requires_successor_valid_to"
                )
                issues[successor.id].add(
                    "valid_time_closed_requires_successor_valid_to"
                )
        elif edge.reason in _NON_CLOSING_REASONS:
            continue
        elif edge.reason == "source_retracted":
            issues[predecessor.id].add("source_retracted")
        else:
            issues[predecessor.id].add(
                f"unknown_supersession_reason:{edge.reason or 'missing'}"
            )

    intervals: dict[str, tuple[str | None, str | None]] = {}
    for group_id, members in sorted(groups.items()):
        start = _earliest_valid_time(
            group_starts[group_id],
            label="valid_from",
            issue_refs=members,
            issues=issues,
        )
        end = _earliest_valid_time(
            group_ends[group_id],
            label="valid_to",
            issue_refs=members,
            issues=issues,
        )
        if start is not None and end is not None:
            try:
                if _time_key(end, "valid_to") < _time_key(start, "valid_from"):
                    for claim_id in members:
                        issues[claim_id].add("effective_valid_interval_inverted")
            except InvariantViolation:
                pass
        for claim_id in members:
            intervals[claim_id] = (start, end)
    return intervals, issues, frozenset(voided), tuple(races)


def _health_for_state(
    claim: ClaimRecord,
    *,
    belief_at: str | None,
    base_status: str | None,
    needs_review: bool,
    voided: bool,
    issues: set[str],
) -> tuple[str, str | None]:
    reasons = set(issues)
    if base_status is None:
        reasons.add("missing_base_status")
    if needs_review:
        reasons.add("active_needs_review_overlay")
    if claim.redacted_at is not None and belief_at is not None:
        try:
            if _time_key(claim.redacted_at, "redacted_at") > _time_key(
                belief_at,
                "belief_at",
            ):
                reasons.add("content_redacted_after_belief")
        except InvariantViolation:
            reasons.add("invalid_redacted_at")
    reason = ",".join(sorted(reasons)) or None
    if claim.redacted_at is not None:
        return "redacted", reason
    if voided:
        return "voided", reason
    if "single_confirmed_successor_race" in reasons:
        return "conflict", reason
    if any(
        item.startswith(("invalid_", "dangling_", "unknown_", "missing_"))
        or "requires_" in item
        or item.endswith("_inverted")
        for item in reasons
    ):
        return "failed", reason
    if reasons:
        return "needs_review", reason
    return "clean", None


def _resolve_claim_states_locked(
    conn: sqlite3.Connection,
    *,
    belief_at: str | None,
) -> tuple[tuple[ClaimState, ...], tuple[SuccessorRace, ...]]:
    rows = conn.execute(
        f"""
        WITH {STATUS_RESOLUTION_CTE}
        SELECT c.*, s.*
        FROM claims AS c
        JOIN status_resolution AS s ON s.claim_id = c.id
        ORDER BY c.created_at, c.id
        """,
        {"belief_at": belief_at},
    ).fetchall()
    claims = {row["id"]: _claim_from_row(row) for row in rows}
    edges = _active_supersession_edges(conn, belief_at=belief_at)
    intervals, interval_issues, voided_claims, races = _derive_intervals(
        claims,
        edges,
    )
    states: list[ClaimState] = []
    for row in rows:
        claim = claims[row["id"]]
        needs_review = bool(row["needs_review"])
        voided = claim.id in voided_claims
        health, reason = _health_for_state(
            claim,
            belief_at=belief_at,
            base_status=row["base_status"],
            needs_review=needs_review,
            voided=voided,
            issues=interval_issues[claim.id],
        )
        effective_from, effective_to = intervals.get(
            claim.id,
            (claim.valid_from, claim.valid_to),
        )
        states.append(
            ClaimState(
                claim=claim,
                base_status=row["base_status"],
                base_status_seq=row["base_status_seq"],
                base_status_event_id=row["base_status_event_id"],
                base_status_at=row["base_status_at"],
                status=row["resolved_status"],
                status_seq=row["resolved_status_seq"],
                needs_review=needs_review,
                overlay_event_id=row["overlay_event_id"],
                overlay_at=row["overlay_at"],
                voided=voided,
                effective_valid_from=effective_from,
                effective_valid_to=effective_to,
                health=health,
                health_reason=reason,
            )
        )
    return tuple(states), races


def resolve_claim_states(
    store: TruthStore,
    *,
    belief_at: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[ClaimState, ...]:
    """Resolve every claim directly from ledger rows at one belief time."""
    cutoff = _normalize_query_time(belief_at, "belief_at")
    with _read_connection(store, conn) as read_conn:
        states, _ = _resolve_claim_states_locked(
            read_conn,
            belief_at=cutoff,
        )
        return states


def successor_races(
    store: TruthStore,
    *,
    belief_at: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[SuccessorRace, ...]:
    """Return predecessors with more than one confirmed active successor."""
    cutoff = _normalize_query_time(belief_at, "belief_at")
    with _read_connection(store, conn) as read_conn:
        _, races = _resolve_claim_states_locked(read_conn, belief_at=cutoff)
        return races


def _valid_at(state: ClaimState, valid_at: str) -> bool:
    if state.voided:
        return False
    instant = _time_key(valid_at, "valid_at")
    try:
        if state.effective_valid_from is not None and instant < _time_key(
            state.effective_valid_from,
            "effective_valid_from",
        ):
            return False
        if state.effective_valid_to is not None and instant >= _time_key(
            state.effective_valid_to,
            "effective_valid_to",
        ):
            return False
    except InvariantViolation:
        return False
    return True


def _redacted_by_belief(state: ClaimState, belief_at: str) -> bool:
    """Fail closed only when redaction had occurred by the belief boundary."""

    if state.claim.redacted_at is None:
        return False
    try:
        return _time_key(state.claim.redacted_at, "redacted_at") <= _time_key(
            belief_at,
            "belief_at",
        )
    except InvariantViolation:
        return True


def claims_as_of(
    store: TruthStore,
    *,
    belief_at: str,
    valid_at: str | None = None,
    scope: str | None = None,
    claim_kind: str | None = None,
    include_needs_review: bool = False,
    conn: sqlite3.Connection | None = None,
) -> tuple[ClaimState, ...]:
    """Return claims held confirmed at a historical belief-time boundary.

    Valid intervals are half-open. A claim is valid at its ``valid_from`` and
    is no longer valid at its effective ``valid_to``.
    """
    cutoff = _normalize_query_time(belief_at, "belief_at")
    assert cutoff is not None
    valid_cutoff = _normalize_query_time(valid_at, "valid_at")
    states = resolve_claim_states(store, belief_at=cutoff, conn=conn)
    return tuple(
        state
        for state in states
        if state.base_status == "confirmed"
        and not _redacted_by_belief(state, cutoff)
        and not state.voided
        and (include_needs_review or not state.needs_review)
        and (scope is None or state.claim.scope == scope)
        and (claim_kind is None or state.claim.claim_kind == claim_kind)
        and (valid_cutoff is None or _valid_at(state, valid_cutoff))
    )


def current_claims(
    store: TruthStore,
    *,
    valid_at: str | None = None,
    scope: str | None = None,
    claim_kind: str | None = None,
    include_needs_review: bool = False,
    conn: sqlite3.Connection | None = None,
) -> tuple[ClaimState, ...]:
    """Return claims currently held confirmed, optionally at a valid time."""
    valid_cutoff = _normalize_query_time(valid_at, "valid_at")
    states = resolve_claim_states(store, conn=conn)
    return tuple(
        state
        for state in states
        if state.base_status == "confirmed"
        and state.claim.redacted_at is None
        and not state.voided
        and (include_needs_review or not state.needs_review)
        and (scope is None or state.claim.scope == scope)
        and (claim_kind is None or state.claim.claim_kind == claim_kind)
        and (valid_cutoff is None or _valid_at(state, valid_cutoff))
    )


def rebuild_claims_current(
    store: TruthStore,
    *,
    rebuilt_at: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[ClaimState, ...]:
    """Transactionally rebuild the disposable ``claims_current`` projection."""
    timestamp = _normalize_query_time(
        utc_now() if rebuilt_at is None else rebuilt_at,
        "rebuilt_at",
    )
    assert timestamp is not None
    with store.write_transaction(conn) as write_conn:
        states, _ = _resolve_claim_states_locked(write_conn, belief_at=None)
        write_conn.execute("DELETE FROM claims_current")
        write_conn.executemany(
            "INSERT INTO claims_current "
            "(claim_id, status, status_seq, effective_valid_from, "
            "effective_valid_to, health, health_reason, rebuilt_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    state.claim_id,
                    state.status or "unknown",
                    state.status_seq or 0,
                    state.effective_valid_from,
                    state.effective_valid_to,
                    state.health,
                    state.health_reason,
                    timestamp,
                )
                for state in states
            ],
        )
        return states


def conflicts(
    store: TruthStore,
    *,
    claim_id: str | None = None,
    belief_at: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[ConflictState, ...]:
    """Return active conflict and refutation links at one belief time."""
    cutoff = _normalize_query_time(belief_at, "belief_at")
    with _read_connection(store, conn) as read_conn:
        rows = read_conn.execute(
            f"""
            WITH {STATUS_RESOLUTION_CTE}
            SELECT
                l.id AS link_id,
                l.from_claim_id,
                l.to_ref AS to_claim_id,
                l.link_type,
                l.role_json,
                l.created_at,
                source.resolved_status AS from_status,
                target.resolved_status AS to_status
            FROM claim_links AS l
            LEFT JOIN status_resolution AS source
              ON source.claim_id = l.from_claim_id
            LEFT JOIN status_resolution AS target
              ON target.claim_id = l.to_ref
            WHERE l.link_type IN ('conflicts_with', 'refutes')
              AND l.to_kind = 'claim'
              AND (
                  :belief_at IS NULL
                  OR julianday(l.created_at) <= julianday(:belief_at)
              )
              AND (
                  :claim_id IS NULL
                  OR l.from_claim_id = :claim_id
                  OR l.to_ref = :claim_id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM link_retractions AS r
                  WHERE r.link_id = l.id
                    AND (
                        :belief_at IS NULL
                        OR julianday(r.at) <= julianday(:belief_at)
                    )
              )
            ORDER BY l.created_at, l.id
            """,
            {"belief_at": cutoff, "claim_id": claim_id},
        ).fetchall()
    results: list[ConflictState] = []
    for row in rows:
        role, _ = _try_json_object(row["role_json"])
        conflict_type = role.get("conflict_type")
        conflict_class = role.get("conflict_class", row["link_type"])
        results.append(
            ConflictState(
                link_id=row["link_id"],
                from_claim_id=row["from_claim_id"],
                to_claim_id=row["to_claim_id"],
                conflict_type=(
                    conflict_type if isinstance(conflict_type, str) else None
                ),
                conflict_class=(
                    conflict_class if isinstance(conflict_class, str) else None
                ),
                role=role,
                from_status=row["from_status"],
                to_status=row["to_status"],
                created_at=row["created_at"],
            )
        )
    return tuple(results)


def needs_review(
    store: TruthStore,
    *,
    belief_at: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[NeedsReviewItem, ...]:
    """Merge active status overlays and unresolved sweep findings."""
    cutoff = _normalize_query_time(belief_at, "belief_at")
    with _read_connection(store, conn) as read_conn:
        status_rows = read_conn.execute(
            f"""
            WITH {STATUS_RESOLUTION_CTE}
            SELECT * FROM status_resolution ORDER BY claim_id
            """,
            {"belief_at": cutoff},
        ).fetchall()
        finding_rows = read_conn.execute(
            """
            SELECT f.*, s.at AS sweep_at
            FROM sweep_findings AS f
            JOIN sweeps AS s ON s.id = f.sweep_id
            WHERE (
                :belief_at IS NULL
                OR julianday(s.at) <= julianday(:belief_at)
            )
              AND (
                  f.resolved_at IS NULL
                  OR (
                      :belief_at IS NOT NULL
                      AND julianday(f.resolved_at) > julianday(:belief_at)
                  )
              )
            ORDER BY s.at, f.id
            """,
            {"belief_at": cutoff},
        ).fetchall()

    status_by_claim = {row["claim_id"]: row for row in status_rows}
    collected: dict[tuple[str, str], dict[str, Any]] = {}
    for row in status_rows:
        if not bool(row["needs_review"]):
            continue
        key = ("claim", row["claim_id"])
        collected[key] = {
            "base_status": row["base_status"],
            "overlay_event_id": row["overlay_event_id"],
            "overlay_seq": row["overlay_seq"],
            "finding_pairs": [],
        }
    for row in finding_rows:
        key = (row["subject_kind"], row["subject_ref"])
        item = collected.setdefault(
            key,
            {
                "base_status": (
                    status_by_claim.get(row["subject_ref"], {})["base_status"]
                    if row["subject_kind"] == "claim"
                    and row["subject_ref"] in status_by_claim
                    else None
                ),
                "overlay_event_id": None,
                "overlay_seq": None,
                "finding_pairs": [],
            },
        )
        item["finding_pairs"].append((row["id"], row["finding"]))

    results: list[NeedsReviewItem] = []
    for (subject_kind, subject_ref), item in sorted(collected.items()):
        pairs = sorted(item["finding_pairs"])
        results.append(
            NeedsReviewItem(
                subject_kind=subject_kind,
                subject_ref=subject_ref,
                base_status=item["base_status"],
                overlay_event_id=item["overlay_event_id"],
                overlay_seq=item["overlay_seq"],
                finding_ids=tuple(pair[0] for pair in pairs),
                findings=tuple(pair[1] for pair in pairs),
            )
        )
    return tuple(results)


def _decode_dependency_path(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.strip(",").split(",") if part)


def supersession_sweep_candidates(
    store: TruthStore,
    root_claim_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[SweepCandidate, ...]:
    """Find all local derivation dependents of one superseded claim."""
    try:
        root_uri = truth_uri(store.store_id, "claim", root_claim_id)
    except ValueError as exc:
        raise InvariantViolation("root_claim_id is not a truth record id") from exc
    with _read_connection(store, conn) as read_conn:
        rows = read_conn.execute(
            DERIVATION_DEPENDENCY_CTE,
            {
                "root_claim_id": root_claim_id,
                "root_claim_uri": root_uri,
                "store_id": store.store_id,
            },
        ).fetchall()
    return tuple(
        SweepCandidate(
            subject_kind="claim",
            subject_ref=row["claim_id"],
            finding=f"depends_on_superseded_claim:{root_claim_id}",
            depth=int(row["depth"]),
            path=_decode_dependency_path(row["path"]),
            via_derivation_id=row["via_derivation_id"],
        )
        for row in rows
    )


def source_sweep_candidates(
    store: TruthStore,
    *,
    evidence_id: str | None = None,
    span_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[SweepCandidate, ...]:
    """Find claims directly or transitively dependent on one source."""
    if (evidence_id is None) == (span_id is None):
        raise InvariantViolation(
            "exactly one of evidence_id or span_id must be supplied"
        )
    source_kind = "evidence" if evidence_id is not None else "evidence_span"
    source_ref = evidence_id if evidence_id is not None else span_id
    with _read_connection(store, conn) as read_conn:
        rows = read_conn.execute(
            SUPPORT_DEPENDENCY_CTE,
            {
                "evidence_id": evidence_id,
                "span_id": span_id,
                "store_id": store.store_id,
            },
        ).fetchall()
    return tuple(
        SweepCandidate(
            subject_kind="claim",
            subject_ref=row["claim_id"],
            finding=f"depends_on_{source_kind}:{source_ref}",
            depth=int(row["depth"]),
            path=_decode_dependency_path(row["path"]),
            via_derivation_id=row["via_derivation_id"],
        )
        for row in rows
    )


def record_sweep(
    store: TruthStore,
    *,
    kind: str,
    findings: Sequence[SweepFindingSpec | SweepCandidate] = (),
    params: Mapping[str, Any] | None = None,
    at: str | None = None,
    sweep_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> RecordedSweep:
    """Append one sweep and its findings as ledger-addressable rows."""
    if not isinstance(kind, str) or not kind.strip():
        raise InvariantViolation("sweep kind must be a nonempty string")
    timestamp = _normalize_query_time(
        utc_now() if at is None else at,
        "sweep at",
    )
    assert timestamp is not None
    if sweep_id is not None and not isinstance(sweep_id, str):
        raise InvariantViolation("sweep_id must be a 32-character hexadecimal id")
    identifier = new_id() if sweep_id is None else sweep_id.strip().lower()
    if len(identifier) != 32:
        raise InvariantViolation("sweep_id must be a 32-character hexadecimal id")
    try:
        int(identifier, 16)
    except ValueError as exc:
        raise InvariantViolation(
            "sweep_id must be a 32-character hexadecimal id"
        ) from exc
    try:
        params_json = canonical_json(dict(params)) if params is not None else None
    except (TypeError, ValueError) as exc:
        raise InvariantViolation("sweep params must be JSON-compatible") from exc

    normalized: list[SweepFindingSpec] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in findings:
        spec = (
            finding
            if isinstance(finding, SweepFindingSpec)
            else SweepFindingSpec(
                subject_kind=finding.subject_kind,
                subject_ref=finding.subject_ref,
                finding=finding.finding,
            )
        )
        values = (spec.subject_kind, spec.subject_ref, spec.finding)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise InvariantViolation("sweep finding fields must be nonempty strings")
        stripped = tuple(value.strip() for value in values)
        if stripped in seen:
            continue
        seen.add(stripped)
        normalized.append(SweepFindingSpec(*stripped))
    normalized.sort(
        key=lambda item: (item.subject_kind, item.subject_ref, item.finding)
    )

    with store.write_transaction(conn) as write_conn:
        existing = write_conn.execute(
            "SELECT * FROM sweeps WHERE id = ?", (identifier,)
        ).fetchone()
        if existing is not None:
            if (
                existing["kind"] != kind.strip()
                or existing["at"] != timestamp
                or existing["params_json"] != params_json
            ):
                raise InvariantViolation("sweep_id already identifies another sweep")
            existing_findings = write_conn.execute(
                "SELECT id, subject_kind, subject_ref, finding "
                "FROM sweep_findings WHERE sweep_id = ? "
                "ORDER BY subject_kind, subject_ref, finding, id",
                (identifier,),
            ).fetchall()
            existing_specs = tuple(
                (row["subject_kind"], row["subject_ref"], row["finding"])
                for row in existing_findings
            )
            requested_specs = tuple(
                (item.subject_kind, item.subject_ref, item.finding)
                for item in normalized
            )
            if existing_specs != requested_specs:
                raise InvariantViolation(
                    "sweep_id already identifies a different finding set"
                )
            finding_ids = tuple(row["id"] for row in existing_findings)
            return RecordedSweep(
                sweep_id=identifier,
                kind=existing["kind"],
                at=existing["at"],
                finding_ids=finding_ids,
            )

        write_conn.execute(
            "INSERT INTO sweeps (id, kind, at, params_json) VALUES (?, ?, ?, ?)",
            (identifier, kind.strip(), timestamp, params_json),
        )
        store._insert_ledger_record_locked(write_conn, "sweep", identifier)
        finding_ids: list[str] = []
        for spec in normalized:
            finding_id = new_id()
            write_conn.execute(
                "INSERT INTO sweep_findings "
                "(id, sweep_id, subject_kind, subject_ref, finding, "
                "resolved_at, resolved_by_ref) VALUES (?, ?, ?, ?, ?, NULL, NULL)",
                (
                    finding_id,
                    identifier,
                    spec.subject_kind,
                    spec.subject_ref,
                    spec.finding,
                ),
            )
            store._insert_ledger_record_locked(
                write_conn,
                "sweep_finding",
                finding_id,
            )
            finding_ids.append(finding_id)
    return RecordedSweep(
        sweep_id=identifier,
        kind=kind.strip(),
        at=timestamp,
        finding_ids=tuple(finding_ids),
    )


def _looks_like_sha256(value: str | None) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def _locator_scheme_hint(locator: str) -> str | None:
    if not isinstance(locator, str) or not locator.strip():
        raise LocatorError("source locator must be a nonempty string")
    if (len(locator) >= 3 and locator[1:3] in {":\\", ":/"}) or locator.startswith(
        ("/", "\\\\")
    ):
        return "file"
    parsed = urlparse(locator)
    return parsed.scheme.lower() or None


def source_integrity_states(
    store: TruthStore,
    *,
    locator_registry: LocatorRegistry = DEFAULT_LOCATOR_REGISTRY,
    conn: sqlite3.Connection | None = None,
) -> tuple[SourceIntegrityState, ...]:
    """Classify locator contracts and captured bytes without network I/O."""
    with _read_connection(store, conn) as read_conn:
        rows = read_conn.execute(
            "SELECT * FROM evidence ORDER BY created_at, id"
        ).fetchall()
        results: list[SourceIntegrityState] = []
        for row in rows:
            evidence = EvidenceRecord(**dict(row))
            locator_display = (
                evidence.source_locator
                if isinstance(evidence.source_locator, str)
                else repr(evidence.source_locator)
            )
            try:
                scheme_hint = _locator_scheme_hint(evidence.source_locator)
            except (LocatorError, TypeError, ValueError) as exc:
                results.append(
                    SourceIntegrityState(
                        evidence_id=evidence.id,
                        locator=locator_display,
                        locator_scheme=None,
                        verifiability_class=None,
                        integrity_recipe={},
                        snapshot_present=False,
                        state="invalid_locator",
                        detail=str(exc),
                    )
                )
                continue

            meta, meta_error = _try_json_object(evidence.meta_json)
            snapshot_present = (
                evidence.content is not None or evidence.content_path is not None
            )
            if meta_error is not None:
                results.append(
                    SourceIntegrityState(
                        evidence_id=evidence.id,
                        locator=locator_display,
                        locator_scheme=scheme_hint,
                        verifiability_class=None,
                        integrity_recipe={},
                        snapshot_present=snapshot_present,
                        state="invalid_meta",
                        detail=meta_error,
                    )
                )
                continue
            if not _looks_like_sha256(evidence.content_sha256):
                results.append(
                    SourceIntegrityState(
                        evidence_id=evidence.id,
                        locator=locator_display,
                        locator_scheme=scheme_hint,
                        verifiability_class=None,
                        integrity_recipe={},
                        snapshot_present=snapshot_present,
                        state="invalid_hash",
                        detail="content_sha256 is not a lowercase SHA-256 digest",
                    )
                )
                continue

            byte_error: str | None = None
            if snapshot_present and evidence.redacted_at is None:
                try:
                    store.read_evidence_bytes(evidence, conn=read_conn)
                except Exception as exc:  # corrupt SQLite values must stay inspectable
                    byte_error = str(exc)

            registry_meta = dict(meta)
            if not snapshot_present and evidence.redacted_at is None:
                registry_meta.pop("snapshot_sha256", None)
                registry_meta.pop("transcript_sha256", None)
            try:
                validation = locator_registry.validate(
                    evidence.kind,
                    evidence.source_locator,
                    registry_meta,
                    (
                        evidence.content_sha256
                        if snapshot_present or evidence.redacted_at is not None
                        else None
                    ),
                )
            except Exception as exc:  # one malformed raw row cannot abort integrity
                missing_snapshot = (
                    not snapshot_present
                    and evidence.redacted_at is None
                    and scheme_hint in {"file", "wb-session"}
                )
                results.append(
                    SourceIntegrityState(
                        evidence_id=evidence.id,
                        locator=locator_display,
                        locator_scheme=scheme_hint,
                        verifiability_class=None,
                        integrity_recipe={},
                        snapshot_present=snapshot_present,
                        state=(
                            "missing_snapshot"
                            if missing_snapshot
                            else "invalid_locator"
                        ),
                        detail=str(exc),
                    )
                )
                continue

            if evidence.redacted_at is not None:
                state = "redacted"
                detail = None
            elif byte_error is not None:
                state = "corrupt_snapshot"
                detail = byte_error
            elif validation.locator != evidence.source_locator:
                state = "noncanonical_locator"
                detail = f"canonical locator is {validation.locator}"
            else:
                state = "valid"
                detail = None
            results.append(
                SourceIntegrityState(
                    evidence_id=evidence.id,
                    locator=locator_display,
                    locator_scheme=validation.locator_scheme,
                    verifiability_class=validation.verifiability_class,
                    integrity_recipe=dict(validation.integrity_recipe),
                    snapshot_present=snapshot_present,
                    state=state,
                    detail=detail,
                )
            )
    return tuple(results)


def link_fingerprint_states(
    store: TruthStore,
    *,
    current_targets: TargetFingerprintSource | None = None,
    belief_at: str | None = None,
    include_retracted: bool = False,
    conn: sqlite3.Connection | None = None,
) -> tuple[LinkFingerprintState, ...]:
    """Compare stored mutable-target fingerprints with supplied current hashes."""
    cutoff = _normalize_query_time(belief_at, "belief_at")
    with _read_connection(store, conn) as read_conn:
        rows = read_conn.execute(
            """
            SELECT l.*
            FROM claim_links AS l
            WHERE (
                :belief_at IS NULL
                OR julianday(l.created_at) <= julianday(:belief_at)
            )
              AND (
                  :include_retracted = 1
                  OR NOT EXISTS (
                      SELECT 1 FROM link_retractions AS r
                      WHERE r.link_id = l.id
                        AND (
                            :belief_at IS NULL
                            OR julianday(r.at) <= julianday(:belief_at)
                        )
                  )
              )
            ORDER BY l.created_at, l.id
            """,
            {
                "belief_at": cutoff,
                "include_retracted": int(include_retracted),
            },
        ).fetchall()

    results: list[LinkFingerprintState] = []
    for row in rows:
        link = ClaimLinkRecord(**dict(row))
        current: str | None = None
        known = False
        detail: str | None = None
        if current_targets is not None and link.link_type in MUTABLE_LINK_TYPES:
            try:
                if callable(current_targets):
                    current = current_targets(link)
                    known = True
                elif link.id in current_targets:
                    current = current_targets[link.id]
                    known = True
                elif link.to_ref in current_targets:
                    current = current_targets[link.to_ref]
                    known = True
            except Exception as exc:  # resolver failures become inspectable state
                detail = f"target fingerprint resolver failed: {exc}"
        try:
            status = fingerprint_status(
                link.link_type,
                link.target_fingerprint,
                current,
            )
        except ValueError as exc:
            status = FingerprintStatus.STALE
            detail = str(exc)
        results.append(
            LinkFingerprintState(
                link_id=link.id,
                link_type=link.link_type,
                to_kind=link.to_kind,
                to_ref=link.to_ref,
                stored_fingerprint=link.target_fingerprint,
                current_fingerprint=current,
                current_fingerprint_known=known,
                status=status,
                detail=detail,
            )
        )
    return tuple(results)


_BASE_TRANSITIONS: Mapping[str | None, frozenset[str]] = {
    None: frozenset({"proposed"}),
    "proposed": frozenset({"confirmed", "rejected", "expired", "retracted"}),
    "confirmed": frozenset({"challenged", "superseded", "retracted"}),
    "challenged": frozenset({"confirmed", "superseded", "retracted"}),
    "rejected": frozenset(),
    "expired": frozenset(),
    "superseded": frozenset(),
    "retracted": frozenset(),
}


def _coerce_premise_resolution(value: Any) -> PremiseResolution:
    if isinstance(value, PremiseResolution):
        return value
    if isinstance(value, bool):
        return PremiseResolution(exists=value)
    if value is None:
        return PremiseResolution(exists=False)
    if isinstance(value, Mapping):
        status = value.get("status")
        detail = value.get("detail")
        exists_value = value.get("exists", status is not None)
        return PremiseResolution(
            exists=bool(exists_value),
            status=status if isinstance(status, str) else None,
            detail=detail if isinstance(detail, str) else None,
        )
    exists = getattr(value, "exists", None)
    if exists is not None:
        status = getattr(value, "status", None)
        detail = getattr(value, "detail", None)
        return PremiseResolution(
            exists=bool(exists),
            status=status if isinstance(status, str) else None,
            detail=detail if isinstance(detail, str) else None,
        )
    raise TypeError("premise resolver returned an unsupported result")


_DOCUMENT_SURFACE_TABLES = (
    "documents",
    "document_spans",
    "expressions",
    "proposals",
    "proposal_status_events",
    "doc_events",
)


def _document_surface_tables_present(conn: sqlite3.Connection) -> bool:
    """Return whether every v2 co-work table exists in this store.

    In the integrated engine the _m002 migration creates these tables in every
    store, so this is always true there and the guard is a no-op. It only lets
    the sweep run unchanged against a pre-v2 store.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
        "(?, ?, ?, ?, ?, ?)",
        _DOCUMENT_SURFACE_TABLES,
    ).fetchall()
    return len({row[0] for row in rows}) == len(_DOCUMENT_SURFACE_TABLES)


def _recompute_proposal_canonical(row: sqlite3.Row) -> str | None:
    """Recompute a proposal canonical hash via the proposals engine module.

    Returns None when the proposals module is unavailable or when the stored
    row cannot be reconstructed, so an unrecomputable proposal never yields a
    false canonical-mismatch error. The single source
    of the hash is proposals.proposal_canonical_sha256, never duplicated here.
    """
    try:
        from work_buddy.truth.proposals import proposal_canonical_sha256
    except ImportError:
        return None
    try:
        # Mirror the insert-time payload exactly: proposals.py hashes the
        # selector as the parsed JSON value, never a CompositeSelector object.
        selector = json.loads(row["selector_json"])
        raw_refs = row["claim_refs_json"]
        claim_refs = json.loads(raw_refs) if raw_refs else None
        return proposal_canonical_sha256(
            document_id=row["document_id"],
            base_content_sha256=row["base_content_sha256"],
            selector=selector,
            quote_exact=row["quote_exact"],
            replacement=row["replacement"],
            rationale=row["rationale"],
            tldr=row["tldr"],
            claim_refs=claim_refs,
        )
    except Exception:  # noqa: BLE001 - any reconstruction failure means skip
        return None


def _document_integrity_findings(
    conn: sqlite3.Connection,
    store: TruthStore,
    add: Callable[..., None],
    *,
    claims_by_id: Mapping[str, ClaimRecord],
    evidence_by_id: Mapping[str, sqlite3.Row],
    spans_by_id: Mapping[str, sqlite3.Row],
    gestures: Mapping[str, sqlite3.Row],
) -> dict[str, set[str]]:
    """Inspect the co-work document ledger in the one integrity surface.

    Errors block import through _validate_staged_integrity. Warnings are
    portable and do not block. Returns the document-side expected ledger keys
    so the caller can fold them into the store-wide completeness check.
    """
    documents = {
        row["id"]: row
        for row in conn.execute("SELECT * FROM documents").fetchall()
    }
    document_spans = {
        row["id"]: row
        for row in conn.execute("SELECT * FROM document_spans").fetchall()
    }
    expressions = conn.execute("SELECT * FROM expressions").fetchall()
    proposals = conn.execute("SELECT * FROM proposals").fetchall()
    status_rows = conn.execute(
        "SELECT * FROM proposal_status_events ORDER BY proposal_id, seq"
    ).fetchall()
    doc_events = conn.execute("SELECT * FROM doc_events").fetchall()

    latest_status: dict[str, sqlite3.Row] = {}
    for row in status_rows:
        latest_status[row["proposal_id"]] = row

    # Dangling document-graph refs (errors). Each kind is checked against its
    # actual parent ref: spans/proposals/doc_events hang off document_id, an
    # expression hangs off its document_span_id.
    for span_id, span in document_spans.items():
        if span["document_id"] not in documents:
            add(
                "document-dangling-ref",
                "document_span",
                span_id,
                f"document_id {span['document_id']} has no document row",
            )
    for prop in proposals:
        if prop["document_id"] not in documents:
            add(
                "document-dangling-ref",
                "proposal",
                prop["id"],
                f"document_id {prop['document_id']} has no document row",
            )
    for event in doc_events:
        if event["document_id"] not in documents:
            add(
                "document-dangling-ref",
                "doc_event",
                event["id"],
                f"document_id {event['document_id']} has no document row",
            )
    for expr in expressions:
        if expr["document_span_id"] not in document_spans:
            add(
                "document-dangling-ref",
                "expression",
                expr["id"],
                f"document_span_id {expr['document_span_id']} has no span row",
            )

    # Snapshot blob liveness (error), the evidence-blob liveness analogue.
    for doc_id, doc in documents.items():
        digest = doc["ydoc_snapshot_sha256"]
        if digest:
            path = store.resolve_blob_path(f"blobs/{digest}")
            if not path.exists():
                add(
                    "ydoc-snapshot-blob-missing",
                    "document",
                    doc_id,
                    f"ydoc snapshot blob {digest} is absent",
                )

    for prop in proposals:
        proposal_id = prop["id"]

        # Global subject-id uniqueness (error), Bucket 2 contract: a proposal
        # id must not also resolve as a claim, evidence, or span id.
        if (
            proposal_id in claims_by_id
            or proposal_id in evidence_by_id
            or proposal_id in spans_by_id
        ):
            add(
                "proposal-subject-collision",
                "proposal",
                proposal_id,
                "proposal id also resolves as a claim, evidence, or span id",
            )

        # Redaction anti-anchoring shape (error).
        if prop["redacted_at"] is not None:
            retained = [
                column
                for column in (
                    "quote_exact",
                    "replacement",
                    "rationale",
                    "tldr",
                    "claim_refs_json",
                )
                if prop[column] is not None
            ]
            if prop["selector_json"] != REDACTED_SELECTOR_JSON:
                retained.append("selector_json")
            if retained:
                add(
                    "proposal-redacted-content-retained",
                    "proposal",
                    proposal_id,
                    f"redacted proposal retained content: {sorted(retained)}",
                )
        else:
            # Canonical binding (error), recomputed only when the engine hash
            # is available, and stale-base (warning) against the live document.
            recomputed = _recompute_proposal_canonical(prop)
            if recomputed is not None and recomputed != prop["canonical_sha256"]:
                add(
                    "proposal-canonical-mismatch",
                    "proposal",
                    proposal_id,
                    "recomputed canonical_sha256 does not match the stored value",
                )
            document = documents.get(prop["document_id"])
            if (
                document is not None
                and prop["base_content_sha256"] != document["content_sha256"]
            ):
                add(
                    "proposal-stale-base",
                    "proposal",
                    proposal_id,
                    "base_content_sha256 differs from the document latest hash",
                    severity="warning",
                )
            # Dangling local claim refs (warning), the intra-store analogue of
            # the omitted cross-store FK. Cross-store URIs are not checked.
            raw_refs = prop["claim_refs_json"]
            if raw_refs:
                try:
                    parsed_refs = json.loads(raw_refs)
                except (TypeError, json.JSONDecodeError):
                    parsed_refs = None
                if isinstance(parsed_refs, list):
                    for ref in parsed_refs:
                        if not isinstance(ref, Mapping):
                            continue
                        claim_value = ref.get("claim")
                        if not isinstance(claim_value, str):
                            continue
                        try:
                            parse_truth_uri(claim_value)
                            continue
                        except ValueError:
                            pass
                        if claim_value not in claims_by_id:
                            add(
                                "document-dangling-claim-ref",
                                "proposal",
                                proposal_id,
                                f"local claim ref {claim_value} resolves to no claim",
                                severity="warning",
                            )

        # Status-basis discipline (error): applied/closed require a bound and
        # consumed gesture, expired requires a rule or sweep basis.
        latest = latest_status.get(proposal_id)
        if latest is not None:
            status = latest["status"]
            basis_kind = latest["basis_kind"]
            basis_ref = latest["basis_ref"]
            if status in {"applied", "closed"}:
                gesture = gestures.get(basis_ref) if basis_ref is not None else None
                if not (
                    basis_kind == "gesture"
                    and gesture is not None
                    and gesture["consumed_at"] is not None
                ):
                    add(
                        "proposal-status-basis",
                        "proposal",
                        proposal_id,
                        f"{status} proposal requires a bound consumed gesture",
                    )
            elif status == "expired":
                if basis_kind not in {"rule", "sweep"}:
                    add(
                        "proposal-status-basis",
                        "proposal",
                        proposal_id,
                        "expired proposal requires a rule or sweep basis",
                    )

    # Expression fingerprint staleness (warnings) and dangling local claim
    # refs (warning).
    for expr in expressions:
        span = document_spans.get(expr["document_span_id"])
        if span is not None and expr["span_sha256"] != span["span_sha256"]:
            add(
                "expression-span-side-stale",
                "expression",
                expr["id"],
                "span-side fingerprint drifted from the current document span",
                severity="warning",
            )
        if expr["claim_ref_kind"] == "local":
            claim = claims_by_id.get(expr["claim_ref"])
            if claim is None:
                add(
                    "document-dangling-claim-ref",
                    "expression",
                    expr["id"],
                    f"local claim_ref {expr['claim_ref']} resolves to no claim",
                    severity="warning",
                )
            elif expr["claim_canonical_sha256"] != claim.canonical_sha256:
                add(
                    "expression-claim-side-stale",
                    "expression",
                    expr["id"],
                    "claim-side fingerprint drifted from the current claim",
                    severity="warning",
                )

    return {
        "document": set(documents),
        "document_span": set(document_spans),
        "expression": {row["id"] for row in expressions},
        "proposal": {row["id"] for row in proposals},
        "proposal_status_event": {row["id"] for row in status_rows},
        "doc_event": {row["id"] for row in doc_events},
    }


def integrity_findings(
    store: TruthStore,
    *,
    cross_store_resolver: CrossStoreResolver | None = None,
    current_targets: TargetFingerprintSource | None = None,
    locator_registry: LocatorRegistry = DEFAULT_LOCATOR_REGISTRY,
    conn: sqlite3.Connection | None = None,
) -> tuple[IntegrityFinding, ...]:
    """Inspect raw rows without trusting projections or foreign-key history.

    External premise lookup is deliberately fail-soft. Missing stores, unknown
    claims, and resolver exceptions become findings so one unavailable store
    cannot abort the integrity pass for the local ledger.
    """
    found: dict[tuple[str, str, str, str], IntegrityFinding] = {}

    def add(
        code: str,
        subject_kind: str,
        subject_ref: str,
        detail: str,
        *,
        severity: str = "error",
    ) -> None:
        key = (code, subject_kind, subject_ref, detail)
        found[key] = IntegrityFinding(
            code=code,
            subject_kind=subject_kind,
            subject_ref=subject_ref,
            severity=severity,
            detail=detail,
        )

    with _read_connection(store, conn) as read_conn:
        ledger_rows = read_conn.execute(
            "SELECT * FROM ledger_records ORDER BY seq"
        ).fetchall()
        ledger_sequence = {
            (row["record_type"], row["record_key"]): int(row["seq"])
            for row in ledger_rows
        }
        claim_rows = read_conn.execute("SELECT * FROM claims ORDER BY id").fetchall()
        claims_by_id = {row["id"]: ClaimRecord(**dict(row)) for row in claim_rows}
        link_rows = read_conn.execute(
            "SELECT * FROM claim_links ORDER BY id"
        ).fetchall()
        links_by_id = {row["id"]: ClaimLinkRecord(**dict(row)) for row in link_rows}
        span_rows = read_conn.execute(
            "SELECT * FROM evidence_spans ORDER BY id"
        ).fetchall()
        spans_by_id = {row["id"]: row for row in span_rows}
        evidence_rows = read_conn.execute(
            "SELECT * FROM evidence ORDER BY id"
        ).fetchall()
        evidence_by_id = {row["id"]: row for row in evidence_rows}
        # Proposals participate in the same gesture-subject and redaction-event
        # surfaces as claims/evidence/spans. Load them (and their status history)
        # so the resolvers below recognize proposal subjects. Guarded because a
        # pre-v2 store has no co-work tables.
        proposals_by_id: dict[str, sqlite3.Row] = {}
        proposal_status_by_proposal: dict[str, list[sqlite3.Row]] = defaultdict(list)
        if _document_surface_tables_present(read_conn):
            proposals_by_id = {
                row["id"]: row
                for row in read_conn.execute(
                    "SELECT * FROM proposals ORDER BY id"
                ).fetchall()
            }
            for row in read_conn.execute(
                "SELECT * FROM proposal_status_events ORDER BY proposal_id, seq"
            ).fetchall():
                proposal_status_by_proposal[row["proposal_id"]].append(row)
        retraction_rows = read_conn.execute(
            "SELECT * FROM link_retractions ORDER BY link_id"
        ).fetchall()
        retractions_by_link = {row["link_id"]: row for row in retraction_rows}
        redaction_rows = read_conn.execute(
            "SELECT * FROM redaction_events ORDER BY id"
        ).fetchall()
        redaction_sequence_by_subject: dict[tuple[str, str, str], int] = {}
        for row in redaction_rows:
            subject_kind = (
                "span"
                if row["subject_kind"] == "evidence_span"
                else row["subject_kind"]
            )
            sequence = ledger_sequence.get(("redaction_event", row["id"]))
            if sequence is None:
                continue
            key = (subject_kind, row["subject_ref"], row["at"])
            previous = redaction_sequence_by_subject.get(key)
            if previous is None or sequence < previous:
                redaction_sequence_by_subject[key] = sequence

        def boundary_parts(
            boundary_event: sqlite3.Row,
        ) -> tuple[datetime | None, int | None]:
            try:
                boundary_time = _time_key(boundary_event["at"], "status event at")
            except InvariantViolation:
                boundary_time = None
            boundary_sequence = ledger_sequence.get(
                ("claim_status_event", boundary_event["id"])
            )
            return boundary_time, boundary_sequence

        def record_existed_at(
            *,
            record_at: Any,
            record_type: str,
            record_key: str,
            boundary_event: sqlite3.Row,
        ) -> bool:
            boundary_time, boundary_sequence = boundary_parts(boundary_event)
            if boundary_time is None:
                return False
            try:
                created = _time_key(record_at, f"{record_type} created_at")
            except InvariantViolation:
                return False
            record_sequence = ledger_sequence.get((record_type, record_key))
            return (
                created <= boundary_time
                and record_sequence is not None
                and boundary_sequence is not None
                and record_sequence < boundary_sequence
            )

        def subject_available_at(
            *,
            subject_kind: str,
            subject_ref: str,
            redacted_at: Any,
            boundary_event: sqlite3.Row,
        ) -> bool:
            if redacted_at is None:
                return True
            boundary_time, boundary_sequence = boundary_parts(boundary_event)
            if boundary_time is None:
                return False
            try:
                redacted = _time_key(redacted_at, "redacted_at")
            except InvariantViolation:
                return True
            if redacted > boundary_time:
                return True
            redaction_sequence = redaction_sequence_by_subject.get(
                (subject_kind, subject_ref, redacted_at)
            )
            was_effective = (
                redaction_sequence is not None
                and boundary_sequence is not None
                and redaction_sequence < boundary_sequence
            )
            return not was_effective

        def link_active_at_event(
            link: sqlite3.Row | ClaimLinkRecord,
            boundary_event: sqlite3.Row,
        ) -> bool:
            link_id = link["id"] if isinstance(link, sqlite3.Row) else link.id
            created_at = (
                link["created_at"] if isinstance(link, sqlite3.Row) else link.created_at
            )
            if not record_existed_at(
                record_at=created_at,
                record_type="claim_link",
                record_key=link_id,
                boundary_event=boundary_event,
            ):
                return False
            retraction = retractions_by_link.get(link_id)
            if retraction is None:
                return True
            boundary_time, boundary_sequence = boundary_parts(boundary_event)
            if boundary_time is None:
                return False
            try:
                retracted = _time_key(retraction["at"], "link retraction at")
            except InvariantViolation:
                return True
            if retracted > boundary_time:
                return True
            retraction_sequence = ledger_sequence.get(("link_retraction", link_id))
            was_effective = (
                retraction_sequence is not None
                and boundary_sequence is not None
                and retraction_sequence < boundary_sequence
            )
            return not was_effective

        states, races = _resolve_claim_states_locked(read_conn, belief_at=None)
        states_by_id = {state.claim_id: state for state in states}

        for claim in claims_by_id.values():
            if claim.created_by_kind not in VALID_ACTOR_KINDS:
                add(
                    "invalid_claim_actor",
                    "claim",
                    claim.id,
                    f"unknown created_by_kind {claim.created_by_kind!r}",
                )
            try:
                _time_key(claim.created_at, "claim created_at")
            except InvariantViolation as exc:
                add("invalid_claim_time", "claim", claim.id, str(exc))
            claim_meta, claim_meta_error = _try_json_object(claim.meta_json)
            if claim_meta_error is not None:
                add("invalid_claim_meta", "claim", claim.id, claim_meta_error)
            elif claim.created_by_kind == "agent_run":
                try:
                    validate_agent_producer_meta(claim_meta)
                except InvariantViolation as exc:
                    add(
                        "missing_agent_producer_identity",
                        "claim",
                        claim.id,
                        str(exc),
                    )
            if not _looks_like_sha256(claim.canonical_sha256):
                add(
                    "invalid_claim_hash",
                    "claim",
                    claim.id,
                    "canonical_sha256 is not a lowercase SHA-256 digest",
                )
            structured: Mapping[str, Any] | str | None = claim.structured_json
            if claim.structured_json is not None:
                try:
                    parsed_structured = json.loads(claim.structured_json)
                    if not isinstance(parsed_structured, dict):
                        raise ValueError("structured_json is not an object")
                    structured = parsed_structured
                except (json.JSONDecodeError, ValueError) as exc:
                    add("invalid_claim_structured", "claim", claim.id, str(exc))
                    structured = None
            if claim.redacted_at is None:
                try:
                    expected_hash = claim_sha256(
                        proposition=claim.proposition,
                        claim_kind=claim.claim_kind,
                        structured=structured,
                        scope=claim.scope,
                        valid_from=claim.valid_from,
                        valid_to=claim.valid_to,
                    )
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    add("invalid_claim_payload", "claim", claim.id, str(exc))
                else:
                    if expected_hash != claim.canonical_sha256:
                        add(
                            "claim_hash_mismatch",
                            "claim",
                            claim.id,
                            "canonical_sha256 does not match the immutable payload",
                        )
            elif claim.proposition != "[redacted]" or claim.structured_json is not None:
                add(
                    "invalid_claim_redaction_shape",
                    "claim",
                    claim.id,
                    "redacted claim retained proposition or structured content",
                )

        live_hashes: dict[str, list[str]] = defaultdict(list)
        for state in states:
            if (
                state.claim.redacted_at is None
                and state.base_status not in TERMINAL_STATUSES
            ):
                live_hashes[state.claim.canonical_sha256].append(state.claim_id)
            if state.claim.redacted_at is not None and state.base_status not in {
                "retracted",
                "rejected",
                "expired",
                "superseded",
            }:
                add(
                    "redacted_claim_live",
                    "claim",
                    state.claim_id,
                    f"redacted claim has live base status {state.base_status!r}",
                )
            if state.base_status is None:
                add(
                    "missing_base_status",
                    "claim",
                    state.claim_id,
                    "claim has no base lifecycle event",
                )
        for digest, claim_ids in live_hashes.items():
            if len(claim_ids) > 1:
                for claim_id in claim_ids:
                    add(
                        "duplicate_live_claim_hash",
                        "claim",
                        claim_id,
                        f"canonical hash {digest} is shared by {sorted(claim_ids)}",
                    )

        gesture_rows = read_conn.execute(
            "SELECT * FROM gestures ORDER BY id"
        ).fetchall()
        gestures = {row["id"]: row for row in gesture_rows}
        gesture_uses: dict[str, list[str]] = defaultdict(list)
        status_rows = read_conn.execute(
            "SELECT * FROM claim_status_events ORDER BY claim_id, seq"
        ).fetchall()
        highest_status_ledger_seq = 0
        highest_status_event_id: str | None = None
        for row in sorted(status_rows, key=lambda item: int(item["seq"])):
            event_id = row["id"]
            event_ledger_seq = ledger_sequence.get(
                ("claim_status_event", event_id)
            )
            if event_ledger_seq is None:
                add(
                    "missing_ledger_record",
                    "claim_status_event",
                    event_id,
                    "durable row is absent from ledger_records",
                )
                continue
            if event_ledger_seq < highest_status_ledger_seq:
                add(
                    "status_sequence_ledger_order_mismatch",
                    "status_event",
                    event_id,
                    f"status seq {row['seq']} follows event "
                    f"{highest_status_event_id} but ledger seq "
                    f"{event_ledger_seq} precedes {highest_status_ledger_seq}",
                )
                continue
            highest_status_ledger_seq = event_ledger_seq
            highest_status_event_id = event_id
        events_by_gesture: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in status_rows:
            if row["basis_kind"] == "gesture" and row["basis_ref"] is not None:
                events_by_gesture[row["basis_ref"]].append(row)
        reasoned_rejection_sources: set[str] = set()
        reasoned_rejection_gestures: set[str] = set()
        for gesture_id, events in events_by_gesture.items():
            gesture = gestures.get(gesture_id)
            if gesture is None or len(events) != 2:
                continue
            rejection_kind = gesture["kind"]
            if rejection_kind not in {
                "reject_as_false",
                "reject_as_preference",
            }:
                continue
            rejected = [row for row in events if row["status"] == "rejected"]
            confirmed = [row for row in events if row["status"] == "confirmed"]
            if len(rejected) != 1 or len(confirmed) != 1:
                continue
            source_event = rejected[0]
            result_event = confirmed[0]
            source = claims_by_id.get(source_event["claim_id"])
            result = claims_by_id.get(result_event["claim_id"])
            if source is None or result is None or source.id == result.id:
                continue
            if (
                source_event["note"] != rejection_kind
                or result_event["note"] != rejection_kind
                or source_event["at"] != result_event["at"]
                or source_event["actor_kind"] != "human"
                or result_event["actor_kind"] != "human"
                or source_event["actor_ref"] != gesture["actor_ref"]
                or result_event["actor_ref"] != gesture["actor_ref"]
                or gesture["subject_ref"] != result.id
                or gesture["payload_sha256"] != result.canonical_sha256
                or not _looks_like_sha256(gesture["context_sha256"])
            ):
                continue
            # The one gesture intentionally attests both halves of a reasoned
            # rejection.  Recognize that event shape independently from the
            # refutation binding so a corrupted link produces one precise
            # binding finding instead of spurious replay/subject/kind noise.
            reasoned_rejection_sources.add(source_event["id"])
            reasoned_rejection_gestures.add(gesture_id)
            if rejection_kind == "reject_as_false":
                if (
                    result.claim_kind != source.claim_kind
                    or result.scope != source.scope
                ):
                    add(
                        "invalid_rejection_semantics",
                        "status_event",
                        source_event["id"],
                        "false rejection result must preserve source kind and scope",
                    )
                if (
                    source.redacted_at is None
                    and result.proposition != negated_proposition(source.proposition)
                ):
                    add(
                        "invalid_rejection_semantics",
                        "status_event",
                        source_event["id"],
                        "false rejection result is not the deterministic negation",
                    )
                refutations = [
                    link
                    for link in link_rows
                    if link["from_claim_id"] == result.id
                    and link["link_type"] == "refutes"
                    and link["to_kind"] == "claim"
                    and link["to_ref"] == source.id
                    and link_active_at_event(link, source_event)
                ]
                if not refutations:
                    add(
                        "missing_rejection_binding_link",
                        "status_event",
                        source_event["id"],
                        "false rejection has no active refutes link at decision time",
                    )
                else:
                    expected_role = rejection_binding_role(
                        rejection_class=rejection_kind,
                        source_canonical_sha256=source.canonical_sha256,
                        result_canonical_sha256=result.canonical_sha256,
                    )
                    if len(refutations) != 1:
                        add(
                            "invalid_rejection_binding",
                            "status_event",
                            source_event["id"],
                            "false rejection must have exactly one active bound "
                            "refutes link at decision time",
                        )
                    for refutation in refutations:
                        role, role_error = _try_json_object(refutation["role_json"])
                        if role_error is not None or role != expected_role:
                            add(
                                "invalid_rejection_binding",
                                "claim_link",
                                refutation["id"],
                                "refutes link does not preserve the exact rejection binding",
                            )
            elif result.claim_kind != "preference":
                add(
                    "invalid_rejection_semantics",
                    "status_event",
                    source_event["id"],
                    "preference rejection result must be a preference claim",
                )
        # The proposal decide_reject_as_false path (S3) binds ONE proposal-subject
        # gesture to both the proposal closure and the confirmed negation claim it
        # mints. That negation confirm's basis gesture is bound to the proposal
        # (subject_ref plus canonical hash), not to the negation claim, so
        # recognize the closure -> negation shape here and exempt the negation
        # confirm from the claim-subject gesture checks in the loop below. This is
        # the proposal-minted analogue of reasoned_rejection above.
        proposal_reasoned_result_events: set[str] = set()
        _negation_note_prefix = "reject_as_false:negation_claim="
        for proposal_id, proposal_events in proposal_status_by_proposal.items():
            proposal_row = proposals_by_id.get(proposal_id)
            if proposal_row is None:
                continue
            for closure in proposal_events:
                if (
                    closure["decision"] != "reject_as_false"
                    or closure["basis_kind"] != "gesture"
                ):
                    continue
                closure_gesture_id = closure["basis_ref"]
                closure_gesture = (
                    gestures.get(closure_gesture_id) if closure_gesture_id else None
                )
                closure_note = closure["note"] or ""
                if (
                    closure_gesture is None
                    or closure_gesture["kind"] != "reject_as_false"
                    or closure_gesture["subject_ref"] != proposal_id
                    or closure_gesture["payload_sha256"]
                    != proposal_row["canonical_sha256"]
                    or not closure_note.startswith(_negation_note_prefix)
                ):
                    continue
                negation_id = closure_note[len(_negation_note_prefix):]
                for claim_event in events_by_gesture.get(closure_gesture_id, []):
                    if (
                        claim_event["status"] == "confirmed"
                        and claim_event["claim_id"] == negation_id
                    ):
                        proposal_reasoned_result_events.add(claim_event["id"])
        previous_status: dict[str, str | None] = defaultdict(lambda: None)
        active_review_overlay: dict[str, bool] = defaultdict(bool)
        previous_event_time: dict[str, datetime] = {}
        for row in status_rows:
            event_id = row["id"]
            claim_id = row["claim_id"]
            status = row["status"]
            claim = claims_by_id.get(claim_id)
            if claim is None:
                add(
                    "dangling_status_claim",
                    "status_event",
                    event_id,
                    f"claim {claim_id} does not exist",
                )
            if status not in VALID_STATUSES:
                add(
                    "invalid_status",
                    "status_event",
                    event_id,
                    f"unknown status {status!r}",
                )
            if row["actor_kind"] not in VALID_ACTOR_KINDS:
                add(
                    "invalid_status_actor",
                    "status_event",
                    event_id,
                    f"unknown actor_kind {row['actor_kind']!r}",
                )
            event_time: datetime | None = None
            try:
                event_time = _time_key(row["at"], "status event at")
            except InvariantViolation as exc:
                add("invalid_status_time", "status_event", event_id, str(exc))
            if event_time is not None:
                last = previous_event_time.get(claim_id)
                if last is not None and event_time < last:
                    add(
                        "status_time_regression",
                        "status_event",
                        event_id,
                        "event time moves backward relative to status seq",
                    )
                previous_event_time[claim_id] = event_time
                if claim is not None:
                    try:
                        if event_time < _time_key(claim.created_at, "claim created_at"):
                            add(
                                "status_before_claim",
                                "status_event",
                                event_id,
                                "status event predates its claim",
                            )
                    except InvariantViolation:
                        pass

            if status == "needs_review":
                if row["actor_kind"] != "system":
                    add(
                        "invalid_review_overlay_actor",
                        "status_event",
                        event_id,
                        "needs_review overlays require a system actor",
                    )
                if row["basis_kind"] not in REVIEW_BASIS_KINDS:
                    add(
                        "invalid_review_overlay_basis",
                        "status_event",
                        event_id,
                        "needs_review overlays require rule, sweep, or conflict basis",
                    )
                if (
                    not isinstance(row["basis_ref"], str)
                    or not row["basis_ref"].strip()
                ):
                    add(
                        "invalid_review_overlay_basis_ref",
                        "status_event",
                        event_id,
                        "needs_review overlays require a nonempty basis_ref",
                    )
                if previous_status[claim_id] in TERMINAL_STATUSES:
                    add(
                        "review_overlay_on_terminal_claim",
                        "status_event",
                        event_id,
                        "needs_review overlay follows a terminal base status",
                    )
                active_review_overlay[claim_id] = True
            elif status in VALID_STATUSES:
                prior = previous_status[claim_id]
                allowed = _BASE_TRANSITIONS.get(prior, frozenset())
                clears_overlay = (
                    active_review_overlay[claim_id]
                    and status == prior
                    and row["actor_kind"] == "human"
                    and row["basis_kind"] == "gesture"
                )
                if status not in allowed and not clears_overlay:
                    add(
                        "invalid_status_transition",
                        "status_event",
                        event_id,
                        f"transition {prior!r} -> {status!r} is not allowed",
                    )
                previous_status[claim_id] = status
                if row["actor_kind"] == "human" and row["basis_kind"] == "gesture":
                    active_review_overlay[claim_id] = False

            if status == "proposed" and (
                row["basis_kind"] != "rule" or row["basis_ref"] != claim_id
            ):
                add(
                    "invalid_proposal_basis",
                    "status_event",
                    event_id,
                    "proposed status requires rule basis bound to its claim",
                )
            if status == "confirmed" and (
                row["actor_kind"] != "human" or row["basis_kind"] != "gesture"
            ):
                add(
                    "confirmation_without_human_gesture",
                    "status_event",
                    event_id,
                    "confirmed status requires a human actor and gesture basis",
                )
            if status == "rejected" and (
                row["actor_kind"] != "human" or row["basis_kind"] != "gesture"
            ):
                add(
                    "rejection_without_human_gesture",
                    "status_event",
                    event_id,
                    "rejected status requires a human actor and gesture basis",
                )
            if status == "expired" and (
                row["actor_kind"] != "system" or row["basis_kind"] != "rule"
            ):
                add(
                    "invalid_expiry_basis",
                    "status_event",
                    event_id,
                    "expired status requires a system actor and rule basis",
                )
            if status == "expired" and (
                not isinstance(row["basis_ref"], str) or not row["basis_ref"].strip()
            ):
                add(
                    "invalid_expiry_basis_ref",
                    "status_event",
                    event_id,
                    "expired status requires a nonempty rule basis_ref",
                )
            if status == "challenged" and (
                row["actor_kind"] == "system"
                or row["basis_kind"] != "conflict_link"
                or not row["basis_ref"]
            ):
                add(
                    "invalid_challenge_basis",
                    "status_event",
                    event_id,
                    "challenged status requires a human or agent conflict_link basis",
                )
            if status == "superseded" and (
                row["actor_kind"] != "human"
                or row["basis_kind"] != "claim_link"
                or not row["basis_ref"]
            ):
                add(
                    "invalid_superseded_basis",
                    "status_event",
                    event_id,
                    "superseded status requires a human claim_link basis",
                )
            if status == "retracted" and (
                row["basis_kind"] != "redaction" or not row["basis_ref"]
            ):
                add(
                    "invalid_retraction_basis",
                    "status_event",
                    event_id,
                    "retracted status requires a redaction-event basis",
                )
            if row["basis_kind"] == "gesture":
                gesture_id = row["basis_ref"]
                if not isinstance(gesture_id, str) or not gesture_id:
                    add(
                        "missing_gesture_reference",
                        "status_event",
                        event_id,
                        "gesture-based status event has no basis_ref",
                    )
                    continue
                gesture_uses[gesture_id].append(event_id)
                gesture = gestures.get(gesture_id)
                if gesture is None:
                    add(
                        "dangling_status_gesture",
                        "status_event",
                        event_id,
                        f"gesture {gesture_id} does not exist",
                    )
                    continue
                is_reasoned_source = event_id in reasoned_rejection_sources
                is_reasoned_result = (
                    status == "confirmed" and gesture_id in reasoned_rejection_gestures
                )
                is_proposal_reasoned_result = (
                    event_id in proposal_reasoned_result_events
                )
                if (
                    gesture["subject_ref"] != claim_id
                    and not is_reasoned_source
                    and not is_proposal_reasoned_result
                ):
                    add(
                        "gesture_subject_mismatch",
                        "status_event",
                        event_id,
                        f"gesture subject is {gesture['subject_ref']}, not {claim_id}",
                    )
                if gesture["actor_ref"] != row["actor_ref"]:
                    add(
                        "gesture_actor_mismatch",
                        "status_event",
                        event_id,
                        "gesture actor does not match the transition actor",
                    )
                if (
                    status == "confirmed"
                    and not is_reasoned_result
                    and not is_proposal_reasoned_result
                    and gesture["kind"]
                    not in CONFIRM_GESTURE_KINDS | {"confirm_quarantined_support"}
                ):
                    add(
                        "invalid_confirmation_gesture_kind",
                        "status_event",
                        event_id,
                        f"gesture kind {gesture['kind']!r} cannot confirm a claim",
                    )
                if status == "confirmed":
                    support_rows: list[
                        tuple[sqlite3.Row, sqlite3.Row | None, sqlite3.Row | None]
                    ] = []
                    for support_link in link_rows:
                        if (
                            support_link["from_claim_id"] != claim_id
                            or support_link["link_type"] != "supports_span"
                            or support_link["to_kind"] != "evidence_span"
                        ):
                            continue
                        span = spans_by_id.get(support_link["to_ref"])
                        evidence = (
                            None
                            if span is None
                            else evidence_by_id.get(span["evidence_id"])
                        )
                        support_rows.append((support_link, span, evidence))
                    usable_support = [
                        (support_link, span, evidence)
                        for support_link, span, evidence in support_rows
                        if span is not None
                        and evidence is not None
                        and link_active_at_event(support_link, row)
                        and record_existed_at(
                            record_at=span["created_at"],
                            record_type="evidence_span",
                            record_key=span["id"],
                            boundary_event=row,
                        )
                        and record_existed_at(
                            record_at=evidence["created_at"],
                            record_type="evidence",
                            record_key=evidence["id"],
                            boundary_event=row,
                        )
                        and subject_available_at(
                            subject_kind="span",
                            subject_ref=span["id"],
                            redacted_at=span["redacted_at"],
                            boundary_event=row,
                        )
                        and subject_available_at(
                            subject_kind="evidence",
                            subject_ref=evidence["id"],
                            redacted_at=evidence["redacted_at"],
                            boundary_event=row,
                        )
                        and evidence["derived_from_store"] is None
                    ]
                    if support_rows and not usable_support:
                        add(
                            "confirmation_without_usable_support",
                            "status_event",
                            event_id,
                            "claim support is entirely missing, redacted, or store-derived",
                        )
                    quarantined_only = bool(usable_support) and all(
                        evidence is not None
                        and evidence["trust_class"] == "external_quarantined"
                        for _support_link, _span, evidence in usable_support
                    )
                    if (
                        quarantined_only
                        and gesture["kind"] != "confirm_quarantined_support"
                    ):
                        add(
                            "quarantined_confirmation_without_override",
                            "status_event",
                            event_id,
                            "quarantined-only support requires an explicit override gesture",
                        )
                    if (
                        not quarantined_only
                        and gesture["kind"] == "confirm_quarantined_support"
                    ):
                        add(
                            "unnecessary_quarantine_override",
                            "status_event",
                            event_id,
                            "quarantine override gesture has no quarantined-only support",
                        )
                if status == "rejected" and (
                    gesture["kind"] not in REJECTION_CLASSES
                    or row["note"] != gesture["kind"]
                ):
                    add(
                        "invalid_rejection_gesture_kind",
                        "status_event",
                        event_id,
                        "rejected status note and gesture kind must name the same rejection class",
                    )
                if (
                    claim is not None
                    and gesture["payload_sha256"] != claim.canonical_sha256
                    and not is_reasoned_source
                    and not is_proposal_reasoned_result
                ):
                    add(
                        "gesture_payload_mismatch",
                        "status_event",
                        event_id,
                        "gesture payload hash does not match the claim",
                    )
                if gesture["consumed_at"] is None:
                    add(
                        "unconsumed_status_gesture",
                        "status_event",
                        event_id,
                        f"gesture {gesture_id} was not consumed",
                    )
                if event_time is not None and gesture["expires_at"] is not None:
                    try:
                        if event_time >= _time_key(
                            gesture["expires_at"], "gesture expires_at"
                        ):
                            add(
                                "expired_status_gesture",
                                "status_event",
                                event_id,
                                "gesture was expired at transition time",
                            )
                    except InvariantViolation as exc:
                        add("invalid_gesture_time", "gesture", gesture_id, str(exc))
                if event_time is not None:
                    try:
                        if event_time < _time_key(gesture["at"], "gesture at"):
                            add(
                                "status_before_gesture",
                                "status_event",
                                event_id,
                                "status event predates its gesture",
                            )
                    except InvariantViolation as exc:
                        add("invalid_gesture_time", "gesture", gesture_id, str(exc))
        for row in gesture_rows:
            gesture_id = row["id"]
            if row["kind"] not in GESTURE_KINDS:
                add(
                    "invalid_gesture_kind",
                    "gesture",
                    gesture_id,
                    f"unsupported gesture kind {row['kind']!r}",
                )
            for column in ("surface", "actor_ref", "payload_excerpt"):
                if not isinstance(row[column], str) or not row[column].strip():
                    add(
                        f"invalid_gesture_{column}",
                        "gesture",
                        gesture_id,
                        f"{column} must be a nonempty string",
                    )
            subject_ref = row["subject_ref"]
            subject_matches: list[tuple[str, str, str | None]] = []
            claim_subject = claims_by_id.get(subject_ref)
            if claim_subject is not None:
                subject_matches.append(
                    (
                        "claim",
                        claim_subject.canonical_sha256,
                        claim_subject.redacted_at,
                    )
                )
            evidence_subject = read_conn.execute(
                "SELECT content_sha256, redacted_at FROM evidence WHERE id = ?",
                (subject_ref,),
            ).fetchone()
            if evidence_subject is not None:
                subject_matches.append(
                    (
                        "evidence",
                        evidence_subject["content_sha256"],
                        evidence_subject["redacted_at"],
                    )
                )
            span_subject = read_conn.execute(
                "SELECT span_sha256, redacted_at FROM evidence_spans WHERE id = ?",
                (subject_ref,),
            ).fetchone()
            if span_subject is not None:
                subject_matches.append(
                    ("span", span_subject["span_sha256"], span_subject["redacted_at"])
                )
            proposal_subject = proposals_by_id.get(subject_ref)
            if proposal_subject is not None:
                # A decision gesture binds to the proposal's canonical hash, so a
                # proposal subject matches on canonical_sha256, the same way a
                # claim subject matches on its canonical hash.
                subject_matches.append(
                    (
                        "proposal",
                        proposal_subject["canonical_sha256"],
                        proposal_subject["redacted_at"],
                    )
                )
            if not subject_matches:
                add(
                    "dangling_gesture_subject",
                    "gesture",
                    gesture_id,
                    f"subject {subject_ref} does not exist",
                )
            elif len(subject_matches) > 1:
                add(
                    "ambiguous_gesture_subject",
                    "gesture",
                    gesture_id,
                    f"subject {subject_ref} exists as multiple record kinds",
                )
            elif row["payload_sha256"] != subject_matches[0][1]:
                add(
                    "gesture_payload_mismatch",
                    "gesture",
                    gesture_id,
                    f"payload hash does not match {subject_matches[0][0]} subject",
                )
            if (
                len(subject_matches) == 1
                and subject_matches[0][2] is not None
                and row["payload_excerpt"] != "[redacted]"
            ):
                add(
                    "gesture_excerpt_retains_redacted_content",
                    "gesture",
                    gesture_id,
                    "gesture receipt for a redacted subject was not tombstoned",
                )
            if not _looks_like_sha256(row["payload_sha256"]):
                add(
                    "invalid_gesture_payload_hash",
                    "gesture",
                    gesture_id,
                    "payload_sha256 is not a lowercase SHA-256 digest",
                )
            if row["context_sha256"] is not None and not _looks_like_sha256(
                row["context_sha256"]
            ):
                add(
                    "invalid_gesture_context_hash",
                    "gesture",
                    gesture_id,
                    "context_sha256 is not a lowercase SHA-256 digest",
                )
            parsed_times: dict[str, datetime] = {}
            for column in ("at", "expires_at", "consumed_at"):
                if row[column] is None:
                    continue
                try:
                    parsed_times[column] = _time_key(row[column], f"gesture {column}")
                except InvariantViolation as exc:
                    add("invalid_gesture_time", "gesture", gesture_id, str(exc))
            if (
                "consumed_at" in parsed_times
                and "at" in parsed_times
                and parsed_times["consumed_at"] < parsed_times["at"]
            ):
                add(
                    "gesture_consumed_before_mint",
                    "gesture",
                    gesture_id,
                    "consumed_at precedes gesture at",
                )
            if (
                "expires_at" in parsed_times
                and "at" in parsed_times
                and parsed_times["expires_at"] <= parsed_times["at"]
            ):
                add(
                    "gesture_expiry_not_after_mint",
                    "gesture",
                    gesture_id,
                    "expires_at must be later than gesture at",
                )

        confirmed_events_by_claim: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for event in status_rows:
            if event["status"] == "confirmed":
                confirmed_events_by_claim[event["claim_id"]].append(event)

        def status_event_precedes(
            candidate: sqlite3.Row,
            boundary_event: sqlite3.Row,
        ) -> bool:
            """Order cross-claim status history by time and canonical ledger seq."""

            try:
                candidate_at = _time_key(candidate["at"], "candidate status at")
                boundary_at = _time_key(boundary_event["at"], "boundary status at")
            except InvariantViolation:
                return False
            candidate_ledger_seq = ledger_sequence.get(
                ("claim_status_event", candidate["id"])
            )
            boundary_ledger_seq = ledger_sequence.get(
                ("claim_status_event", boundary_event["id"])
            )
            return (
                candidate_at <= boundary_at
                and candidate_ledger_seq is not None
                and boundary_ledger_seq is not None
                and candidate_ledger_seq < boundary_ledger_seq
            )

        def ordered_status_events_before(
            claim_id: str,
            boundary_event: sqlite3.Row,
        ) -> list[sqlite3.Row]:
            """Return eligible history in canonical ledger order."""

            eligible = [
                candidate
                for candidate in status_rows
                if candidate["claim_id"] == claim_id
                and status_event_precedes(candidate, boundary_event)
            ]
            return sorted(
                eligible,
                key=lambda candidate: ledger_sequence[
                    ("claim_status_event", candidate["id"])
                ],
            )

        def base_status_at_event(
            claim_id: str,
            boundary_event: sqlite3.Row,
        ) -> sqlite3.Row | None:
            return next(
                (
                    candidate
                    for candidate in reversed(
                        ordered_status_events_before(claim_id, boundary_event)
                    )
                    if candidate["status"] != "needs_review"
                ),
                None,
            )

        def resolved_status_at_event(
            claim_id: str,
            boundary_event: sqlite3.Row,
        ) -> sqlite3.Row | None:
            """Resolve base state plus review overlay immediately before a boundary."""

            eligible = ordered_status_events_before(claim_id, boundary_event)
            base = next(
                (
                    candidate
                    for candidate in reversed(eligible)
                    if candidate["status"] != "needs_review"
                ),
                None,
            )
            if base is None:
                return None
            overlay = next(
                (
                    candidate
                    for candidate in reversed(eligible)
                    if candidate["status"] == "needs_review"
                ),
                None,
            )
            human_clear_ledger_seq = max(
                (
                    ledger_sequence[("claim_status_event", candidate["id"])]
                    for candidate in eligible
                    if candidate["status"] != "needs_review"
                    and candidate["actor_kind"] == "human"
                    and candidate["basis_kind"] == "gesture"
                ),
                default=0,
            )
            if (
                overlay is not None
                and ledger_sequence[("claim_status_event", overlay["id"])]
                > human_clear_ledger_seq
                and base["status"] not in TERMINAL_STATUSES
            ):
                return overlay
            return base

        def challenger_has_usable_support_at(
            challenger_id: str,
            boundary_event: sqlite3.Row,
        ) -> bool:
            for link in link_rows:
                if (
                    link["from_claim_id"] != challenger_id
                    or link["link_type"] != "supports_span"
                    or link["to_kind"] != "evidence_span"
                    or not link_active_at_event(link, boundary_event)
                ):
                    continue
                span = spans_by_id.get(link["to_ref"])
                if span is None:
                    continue
                evidence = evidence_by_id.get(span["evidence_id"])
                if evidence is None:
                    continue
                if (
                    record_existed_at(
                        record_at=span["created_at"],
                        record_type="evidence_span",
                        record_key=span["id"],
                        boundary_event=boundary_event,
                    )
                    and record_existed_at(
                        record_at=evidence["created_at"],
                        record_type="evidence",
                        record_key=evidence["id"],
                        boundary_event=boundary_event,
                    )
                    and subject_available_at(
                        subject_kind="span",
                        subject_ref=span["id"],
                        redacted_at=span["redacted_at"],
                        boundary_event=boundary_event,
                    )
                    and subject_available_at(
                        subject_kind="evidence",
                        subject_ref=evidence["id"],
                        redacted_at=evidence["redacted_at"],
                        boundary_event=boundary_event,
                    )
                    and evidence["derived_from_store"] is None
                    and evidence["trust_class"] is not None
                ):
                    return True
            return False

        def confirmation_precedes_status(
            confirmation: sqlite3.Row,
            status_event: sqlite3.Row,
        ) -> bool:
            try:
                confirmed_at = _time_key(confirmation["at"], "confirmation at")
                transition_at = _time_key(status_event["at"], "status event at")
            except InvariantViolation:
                return False
            return confirmed_at < transition_at or (
                confirmed_at == transition_at
                and int(confirmation["seq"]) < int(status_event["seq"])
            )

        def confirmation_matches_supersession(
            confirmation: sqlite3.Row,
            status_event: sqlite3.Row,
            link: ClaimLinkRecord,
        ) -> bool:
            """Require the atomic pair's transaction timestamps to agree exactly."""

            try:
                confirmed_at = _time_key(confirmation["at"], "confirmation at")
                transition_at = _time_key(status_event["at"], "status event at")
            except InvariantViolation:
                return False
            return (
                confirmed_at == transition_at
                and int(confirmation["seq"]) < int(status_event["seq"])
                and link_active_at_event(link, confirmation)
            )

        for event in status_rows:
            if event["status"] not in {"challenged", "superseded"}:
                continue
            basis = links_by_id.get(event["basis_ref"])
            if event["status"] == "challenged":
                if (
                    basis is None
                    or basis.link_type != "conflicts_with"
                    or basis.to_kind != "claim"
                    or basis.to_ref != event["claim_id"]
                    or not link_active_at_event(basis, event)
                ):
                    add(
                        "invalid_challenge_link",
                        "status_event",
                        event["id"],
                        "challenge basis is not an active conflict link targeting the claim",
                    )
                    continue
                challenger = claims_by_id.get(basis.from_claim_id)
                challenger_status = base_status_at_event(basis.from_claim_id, event)
                if (
                    challenger_status is None
                    or challenger_status["status"] in TERMINAL_STATUSES
                ):
                    state = (
                        None
                        if challenger_status is None
                        else challenger_status["status"]
                    )
                    add(
                        "invalid_challenge_challenger_status",
                        "status_event",
                        event["id"],
                        f"challenger had non-live base status {state!r} at challenge time",
                    )
                if challenger is not None and not subject_available_at(
                    subject_kind="claim",
                    subject_ref=challenger.id,
                    redacted_at=challenger.redacted_at,
                    boundary_event=event,
                ):
                    add(
                        "redacted_challenge_challenger",
                        "status_event",
                        event["id"],
                        "challenger content was redacted at challenge time",
                    )
                if not challenger_has_usable_support_at(
                    basis.from_claim_id,
                    event,
                ):
                    add(
                        "challenge_without_usable_support",
                        "status_event",
                        event["id"],
                        "challenger had no usable support at challenge time",
                    )
                continue
            if (
                basis is None
                or basis.link_type != "supersedes"
                or basis.to_kind != "claim"
                or basis.to_ref != event["claim_id"]
                or not link_active_at_event(basis, event)
            ):
                add(
                    "invalid_superseded_link",
                    "status_event",
                    event["id"],
                    "superseded basis is not an active supersedes link targeting the claim",
                )
                continue
            confirmations = confirmed_events_by_claim.get(basis.from_claim_id, [])
            preceding = [
                item
                for item in confirmations
                if confirmation_precedes_status(item, event)
            ]
            matching = [
                item
                for item in preceding
                if confirmation_matches_supersession(item, event, basis)
            ]
            if not preceding:
                add(
                    "superseded_before_successor_confirmation",
                    "status_event",
                    event["id"],
                    "predecessor was superseded before its successor was confirmed",
                )
            elif not matching:
                add(
                    "supersession_time_mismatch",
                    "status_event",
                    event["id"],
                    "predecessor supersession must share the successor confirmation timestamp",
                )
            elif (
                event["actor_ref"] != matching[-1]["actor_ref"]
                or event["actor_kind"] != matching[-1]["actor_kind"]
            ):
                add(
                    "supersession_actor_mismatch",
                    "status_event",
                    event["id"],
                    "predecessor and successor confirmation actors do not match",
                )
        for row in link_rows:
            link_id = row["id"]
            link_type = row["link_type"]
            if row["from_claim_id"] not in claims_by_id:
                add(
                    "dangling_link_source",
                    "claim_link",
                    link_id,
                    f"claim {row['from_claim_id']} does not exist",
                )
            allowed_targets = LINK_TARGETS.get(link_type)
            if allowed_targets is None:
                add(
                    "invalid_link_type",
                    "claim_link",
                    link_id,
                    f"unknown link_type {link_type!r}",
                )
            elif row["to_kind"] not in allowed_targets:
                add(
                    "invalid_link_target_kind",
                    "claim_link",
                    link_id,
                    f"{link_type} cannot target {row['to_kind']}",
                )
            if row["created_by_kind"] not in VALID_ACTOR_KINDS:
                add(
                    "invalid_link_actor",
                    "claim_link",
                    link_id,
                    f"unknown created_by_kind {row['created_by_kind']!r}",
                )
            try:
                _time_key(row["created_at"], "link created_at")
            except InvariantViolation as exc:
                add("invalid_link_time", "claim_link", link_id, str(exc))
            role, role_error = _try_json_object(row["role_json"])
            if role_error is not None:
                add("invalid_link_role", "claim_link", link_id, role_error)
            rejection_binding_keys = set(REJECTION_BINDING_FIELDS) | {
                REJECTION_BINDING_HASH_FIELD
            }
            if link_type == "refutes" and rejection_binding_keys.intersection(role):
                source = claims_by_id.get(row["to_ref"])
                result = claims_by_id.get(row["from_claim_id"])
                if source is not None and result is not None:
                    try:
                        expected_binding = rejection_binding_role(
                            rejection_class=role.get("rejection_class", ""),
                            source_canonical_sha256=source.canonical_sha256,
                            result_canonical_sha256=result.canonical_sha256,
                        )
                    except InvariantViolation:
                        expected_binding = None
                    if role != expected_binding:
                        add(
                            "invalid_rejection_binding",
                            "claim_link",
                            link_id,
                            "refutes link does not preserve the exact rejection binding",
                        )
            if link_type == "supersedes":
                reason = role.get("supersession_reason")
                if reason not in SUPERSESSION_REASONS:
                    add(
                        "invalid_supersession_reason",
                        "claim_link",
                        link_id,
                        f"unsupported supersession_reason {reason!r}",
                    )
                successor = claims_by_id.get(row["from_claim_id"])
                if (
                    reason in _CLOSING_AT_SUCCESSOR_START
                    and successor is not None
                    and successor.valid_from is None
                ):
                    add(
                        "supersession_missing_successor_valid_from",
                        "claim_link",
                        link_id,
                        f"{reason} supersession requires successor valid_from",
                    )
                if (
                    reason == "valid_time_closed"
                    and successor is not None
                    and successor.valid_to is None
                ):
                    add(
                        "supersession_missing_successor_valid_to",
                        "claim_link",
                        link_id,
                        "valid_time_closed supersession requires successor valid_to",
                    )
            if row["to_kind"] == "claim":
                if row["to_ref"] not in claims_by_id:
                    add(
                        "dangling_link_claim",
                        "claim_link",
                        link_id,
                        f"target claim {row['to_ref']} does not exist",
                    )
                if link_type == "supersedes" and row["to_ref"] == row["from_claim_id"]:
                    add(
                        "self_supersession",
                        "claim_link",
                        link_id,
                        "a claim cannot supersede itself",
                    )
            elif row["to_kind"] == "evidence_span":
                span = spans_by_id.get(row["to_ref"])
                if span is None:
                    add(
                        "dangling_link_span",
                        "claim_link",
                        link_id,
                        f"evidence span {row['to_ref']} does not exist",
                    )
                else:
                    evidence = evidence_by_id.get(span["evidence_id"])
                    if span["redacted_at"] is not None or (
                        evidence is not None and evidence["redacted_at"] is not None
                    ):
                        add(
                            "support_targets_redacted_source",
                            "claim_link",
                            link_id,
                            "support link targets redacted evidence",
                        )
            elif (
                row["to_kind"] == "external_uri" and not urlparse(row["to_ref"]).scheme
            ):
                add(
                    "invalid_external_uri",
                    "claim_link",
                    link_id,
                    "external_uri target has no named URI scheme",
                )
            if link_type in IMMUTABLE_LINK_TYPES and (
                row["target_fingerprint"] is not None
                or row["fingerprint_reviewed_at"] is not None
            ):
                add(
                    "fingerprint_on_immutable_link",
                    "claim_link",
                    link_id,
                    "immutable link target carries fingerprint state",
                )
            if link_type in MUTABLE_LINK_TYPES:
                fingerprint = row["target_fingerprint"]
                reviewed = row["fingerprint_reviewed_at"]
                if fingerprint is not None and not _looks_like_sha256(fingerprint):
                    add(
                        "invalid_target_fingerprint",
                        "claim_link",
                        link_id,
                        "target_fingerprint is not a lowercase SHA-256 digest",
                    )
                if (fingerprint is None) != (reviewed is None):
                    add(
                        "incomplete_fingerprint_review",
                        "claim_link",
                        link_id,
                        "fingerprint and review time must be present together",
                    )
                if reviewed is not None:
                    try:
                        _time_key(reviewed, "fingerprint_reviewed_at")
                    except InvariantViolation as exc:
                        add(
                            "invalid_fingerprint_review_time",
                            "claim_link",
                            link_id,
                            str(exc),
                        )

        current_base_events: dict[str, sqlite3.Row] = {}
        for status_event in status_rows:
            if status_event["status"] != "needs_review":
                current_base_events[status_event["claim_id"]] = status_event

        for row in retraction_rows:
            link = links_by_id.get(row["link_id"])
            if link is None:
                add(
                    "dangling_link_retraction",
                    "link_retraction",
                    row["link_id"],
                    "retracted link does not exist",
                )
                continue
            if row["actor_kind"] not in VALID_ACTOR_KINDS:
                add(
                    "invalid_retraction_actor",
                    "link_retraction",
                    row["link_id"],
                    f"unknown actor_kind {row['actor_kind']!r}",
                )
            try:
                if _time_key(row["at"], "link retraction at") < _time_key(
                    link.created_at, "link created_at"
                ):
                    add(
                        "retraction_before_link",
                        "link_retraction",
                        row["link_id"],
                        "link retraction predates the link",
                    )
            except InvariantViolation as exc:
                add(
                    "invalid_retraction_time",
                    "link_retraction",
                    row["link_id"],
                    str(exc),
                )
            if link.to_kind == "claim":
                current = current_base_events.get(link.to_ref)
                if (
                    link.link_type == "supersedes"
                    and current is not None
                    and current["status"] == "superseded"
                    and current["basis_kind"] == "claim_link"
                    and current["basis_ref"] == link.id
                ):
                    add(
                        "retracted_current_supersession_authority",
                        "link_retraction",
                        row["link_id"],
                        "retraction removes the link authorizing the current "
                        "superseded status",
                    )
                if (
                    link.link_type == "conflicts_with"
                    and current is not None
                    and current["status"] == "challenged"
                    and current["basis_kind"] == "conflict_link"
                    and current["basis_ref"] == link.id
                ):
                    add(
                        "retracted_current_challenge_authority",
                        "link_retraction",
                        row["link_id"],
                        "retraction removes the link authorizing the current "
                        "challenged status",
                    )

        for race in races:
            add(
                "single_confirmed_successor_race",
                "claim",
                race.predecessor_id,
                f"active confirmed successors are {list(race.successor_ids)}",
            )
        active_edges = _active_supersession_edges(read_conn, belief_at=None)
        predecessors_with_successors = {edge.predecessor_id for edge in active_edges}
        superseded_events_by_link = {
            (row["claim_id"], row["basis_ref"]): row
            for row in status_rows
            if row["status"] == "superseded" and row["basis_kind"] == "claim_link"
        }
        for edge in active_edges:
            state = states_by_id.get(edge.predecessor_id)
            event = superseded_events_by_link.get((edge.predecessor_id, edge.link.id))
            if state is None or state.base_status != "superseded" or event is None:
                add(
                    "active_supersession_without_status",
                    "claim_link",
                    edge.link.id,
                    "confirmed supersession has no matching predecessor superseded event",
                )
        for state in states:
            if (
                state.base_status == "superseded"
                and state.claim_id not in predecessors_with_successors
            ):
                add(
                    "superseded_without_successor",
                    "claim",
                    state.claim_id,
                    "superseded status has no active confirmed successor link",
                )

        current_race_predecessors = {race.predecessor_id for race in races}
        activation_periods: dict[
            str,
            list[tuple[str, datetime, datetime | None, str]],
        ] = defaultdict(list)
        for row in link_rows:
            if row["link_type"] != "supersedes" or row["to_kind"] != "claim":
                continue
            confirmations = confirmed_events_by_claim.get(row["from_claim_id"], [])
            if not confirmations:
                continue
            try:
                start = max(
                    _time_key(row["created_at"], "link created_at"),
                    _time_key(confirmations[0]["at"], "confirmation at"),
                )
                retraction = retractions_by_link.get(row["id"])
                end = (
                    None
                    if retraction is None
                    else _time_key(retraction["at"], "link retraction at")
                )
            except InvariantViolation:
                continue
            if end is not None and end <= start:
                continue
            activation_periods[row["to_ref"]].append(
                (row["from_claim_id"], start, end, row["id"])
            )
        for predecessor_id, periods in activation_periods.items():
            if predecessor_id in current_race_predecessors:
                continue
            overlapping: set[str] = set()
            for index, left in enumerate(periods):
                for right in periods[index + 1 :]:
                    if left[0] == right[0]:
                        continue
                    left_end = left[2]
                    right_end = right[2]
                    if (left_end is None or right[1] < left_end) and (
                        right_end is None or left[1] < right_end
                    ):
                        overlapping.update((left[0], right[0]))
            if overlapping:
                add(
                    "single_confirmed_successor_race",
                    "claim",
                    predecessor_id,
                    "historically overlapping confirmed successors are "
                    f"{sorted(overlapping)}",
                )

        derivation_rows = read_conn.execute(
            "SELECT * FROM derivations ORDER BY id"
        ).fetchall()
        premises_by_derivation: dict[str, list[sqlite3.Row]] = defaultdict(list)
        premise_rows = read_conn.execute(
            "SELECT * FROM derivation_premises ORDER BY derivation_id, premise_ref"
        ).fetchall()
        for row in premise_rows:
            premises_by_derivation[row["derivation_id"]].append(row)
        derivation_ids = {row["id"] for row in derivation_rows}
        for row in derivation_rows:
            derivation_id = row["id"]
            conclusion_id = row["claim_id"]
            conclusion = states_by_id.get(conclusion_id)
            if conclusion_id not in claims_by_id:
                add(
                    "dangling_derivation_conclusion",
                    "derivation",
                    derivation_id,
                    f"claim {conclusion_id} does not exist",
                )
            premises = premises_by_derivation.get(derivation_id, [])
            if not premises:
                add(
                    "derivation_without_premises",
                    "derivation",
                    derivation_id,
                    "derivation has no premises",
                )
            for premise in premises:
                premise_kind = premise["premise_kind"]
                premise_ref = premise["premise_ref"]
                local_premise_id: str | None = None
                premise_status: str | None = None
                premise_exists = False
                if premise_kind == "local":
                    local_premise_id = premise_ref
                    premise_state = states_by_id.get(premise_ref)
                    premise_exists = premise_state is not None
                    premise_status = (
                        premise_state.status if premise_state is not None else None
                    )
                    if not premise_exists:
                        add(
                            "dangling_local_premise",
                            "derivation",
                            derivation_id,
                            f"local premise {premise_ref} does not exist",
                        )
                elif premise_kind == "uri":
                    try:
                        parsed = parse_truth_uri(premise_ref)
                    except ValueError as exc:
                        add(
                            "invalid_premise_uri",
                            "derivation",
                            derivation_id,
                            f"{premise_ref}: {exc}",
                        )
                        continue
                    if parsed.kind != "claim":
                        add(
                            "invalid_premise_uri_kind",
                            "derivation",
                            derivation_id,
                            f"URI premise targets {parsed.kind}, not claim",
                        )
                        continue
                    if parsed.store_id == store.store_id:
                        local_premise_id = parsed.record_id
                        premise_state = states_by_id.get(parsed.record_id)
                        premise_exists = premise_state is not None
                        premise_status = (
                            premise_state.status if premise_state is not None else None
                        )
                        if not premise_exists:
                            add(
                                "dangling_same_store_premise",
                                "derivation",
                                derivation_id,
                                f"same-store premise {premise_ref} does not exist",
                            )
                    else:
                        if cross_store_resolver is None:
                            add(
                                "external_premise_unresolved",
                                "derivation",
                                derivation_id,
                                f"no resolver is available for {premise_ref}",
                                severity="warning",
                            )
                            continue
                        try:
                            resolution = _coerce_premise_resolution(
                                cross_store_resolver(premise_ref)
                            )
                        except Exception as exc:
                            add(
                                "external_premise_resolution_failed",
                                "derivation",
                                derivation_id,
                                f"{premise_ref}: {exc}",
                                severity="warning",
                            )
                            continue
                        premise_exists = resolution.exists
                        premise_status = resolution.status
                        if not resolution.exists:
                            add(
                                "external_premise_unresolved",
                                "derivation",
                                derivation_id,
                                resolution.detail or f"{premise_ref} was not found",
                                severity="warning",
                            )
                        elif resolution.status != "confirmed":
                            add(
                                "external_premise_unconfirmed",
                                "derivation",
                                derivation_id,
                                resolution.detail
                                or f"{premise_ref} has status {resolution.status!r}",
                                severity="warning",
                            )
                else:
                    add(
                        "invalid_premise_kind",
                        "derivation",
                        derivation_id,
                        f"unknown premise_kind {premise_kind!r}",
                    )
                    continue
                if local_premise_id is None:
                    continue

                confirmation_errors = False
                premise_ledger_key = canonical_json(
                    {
                        "derivation_id": derivation_id,
                        "premise_ref": premise_ref,
                    }
                )
                premise_ledger_seq = ledger_sequence.get(
                    ("derivation_premise", premise_ledger_key)
                )
                # add_derivation intentionally permits later provenance
                # attachment.  Only confirmations made after this derivation
                # and this premise row existed exercised the weakest-link gate.
                conclusion_confirmations = [
                    confirmation
                    for confirmation in confirmed_events_by_claim.get(
                        conclusion_id,
                        [],
                    )
                    if record_existed_at(
                        record_at=row["created_at"],
                        record_type="derivation",
                        record_key=derivation_id,
                        boundary_event=confirmation,
                    )
                    and premise_ledger_seq is not None
                    and (
                        ledger_sequence.get(
                            ("claim_status_event", confirmation["id"])
                        )
                        or 0
                    )
                    > premise_ledger_seq
                ]
                for confirmation in conclusion_confirmations:
                    premise_at_confirmation = resolved_status_at_event(
                        local_premise_id,
                        confirmation,
                    )
                    if (
                        premise_at_confirmation is not None
                        and premise_at_confirmation["status"] == "confirmed"
                    ):
                        continue
                    confirmation_errors = True
                    status_at_confirmation = (
                        None
                        if premise_at_confirmation is None
                        else premise_at_confirmation["status"]
                    )
                    add(
                        "confirmed_derivation_has_unconfirmed_premise",
                        "derivation",
                        derivation_id,
                        f"premise {premise_ref} had status "
                        f"{status_at_confirmation!r} when conclusion confirmation "
                        f"{confirmation['id']} occurred",
                    )
                if (
                    conclusion is not None
                    and conclusion.base_status == "confirmed"
                    and not confirmation_errors
                    and (not premise_exists or premise_status != "confirmed")
                ):
                    detail = (
                        f"premise {premise_ref} is no longer confirmed after a "
                        "valid conclusion confirmation"
                        if conclusion_confirmations
                        else f"premise {premise_ref} is unconfirmed on a derivation "
                        "attached after the conclusion was confirmed"
                    )
                    add(
                        "confirmed_derivation_has_unconfirmed_premise",
                        "derivation",
                        derivation_id,
                        detail,
                        severity="warning",
                    )
        for row in premise_rows:
            if row["derivation_id"] not in derivation_ids:
                add(
                    "dangling_derivation_premise",
                    "derivation_premise",
                    row["premise_ref"],
                    f"derivation {row['derivation_id']} does not exist",
                )

        source_states = source_integrity_states(
            store,
            locator_registry=locator_registry,
            conn=read_conn,
        )
        for state in source_states:
            if state.state not in {"valid", "redacted"}:
                add(
                    f"source_{state.state}",
                    "evidence",
                    state.evidence_id,
                    state.detail or state.state,
                )
        for row in evidence_rows:
            evidence_id = row["id"]
            if row["kind"] not in EVIDENCE_KINDS:
                add(
                    "invalid_evidence_kind",
                    "evidence",
                    evidence_id,
                    f"unknown evidence kind {row['kind']!r}",
                )
            if row["acquisition_method"] not in ACQUISITION_METHODS:
                add(
                    "invalid_acquisition_method",
                    "evidence",
                    evidence_id,
                    f"unknown acquisition_method {row['acquisition_method']!r}",
                )
            if row["trust_class"] not in AUTHORSHIP_KINDS:
                add(
                    "invalid_trust_class",
                    "evidence",
                    evidence_id,
                    f"unknown trust_class {row['trust_class']!r}",
                )
            if (
                row["trust_class"] in {"user_authored", "user_curated"}
                and row["acquired_by_kind"] != "human"
            ):
                add(
                    "human_trust_without_human_origin",
                    "evidence",
                    evidence_id,
                    f"{row['trust_class']} evidence requires a human acquisition actor",
                )
            if (
                row["trust_class"] == "external"
                and row["acquired_by_kind"] == "agent_run"
            ):
                add(
                    "agent_cleared_external_quarantine",
                    "evidence",
                    evidence_id,
                    "agent acquisitions cannot assign reviewed external trust",
                )
            evidence_meta, evidence_meta_error = _try_json_object(row["meta_json"])
            if evidence_meta_error is None and row["acquired_by_kind"] == "agent_run":
                try:
                    validate_agent_producer_meta(evidence_meta)
                except InvariantViolation as exc:
                    add(
                        "missing_agent_producer_identity",
                        "evidence",
                        evidence_id,
                        str(exc),
                    )
            if row["content"] is not None and row["content_path"] is not None:
                add(
                    "evidence_has_two_payloads",
                    "evidence",
                    row["id"],
                    "evidence has both inline content and content_path",
                )
            if row["redacted_at"] is not None and (
                row["content"] is not None or row["content_path"] is not None
            ):
                add(
                    "invalid_evidence_redaction_shape",
                    "evidence",
                    row["id"],
                    "redacted evidence retained captured content",
                )
            if row["acquired_by_kind"] not in VALID_ACTOR_KINDS:
                add(
                    "invalid_evidence_actor",
                    "evidence",
                    row["id"],
                    f"unknown acquired_by_kind {row['acquired_by_kind']!r}",
                )
            try:
                acquired_at = _time_key(row["acquired_at"], "acquired_at")
                created_at = _time_key(row["created_at"], "evidence created_at")
                if acquired_at > created_at:
                    add(
                        "evidence_acquired_after_creation",
                        "evidence",
                        row["id"],
                        "acquired_at is later than created_at",
                    )
            except InvariantViolation as exc:
                add("invalid_evidence_time", "evidence", row["id"], str(exc))
        for row in span_rows:
            span_id = row["id"]
            parent_evidence = evidence_by_id.get(row["evidence_id"])
            if parent_evidence is None:
                add(
                    "dangling_span_evidence",
                    "evidence_span",
                    span_id,
                    f"evidence {row['evidence_id']} does not exist",
                )
            try:
                parse_selector(row["selector_json"])
            except InvariantViolation as exc:
                add("invalid_span_selector", "evidence_span", span_id, str(exc))
            if not _looks_like_sha256(row["span_sha256"]):
                add(
                    "invalid_span_hash",
                    "evidence_span",
                    span_id,
                    "span_sha256 is not a lowercase SHA-256 digest",
                )
            elif (
                row["quote_exact"] is not None
                and sha256_text(row["quote_exact"]) != row["span_sha256"]
            ):
                add(
                    "span_hash_mismatch",
                    "evidence_span",
                    span_id,
                    "span_sha256 does not match quote_exact",
                )
            if row["redacted_at"] is not None and row["quote_exact"] is not None:
                add(
                    "invalid_span_redaction_shape",
                    "evidence_span",
                    span_id,
                    "redacted span retained quote_exact",
                )
            if row["created_by_kind"] not in VALID_ACTOR_KINDS:
                add(
                    "invalid_span_actor",
                    "evidence_span",
                    span_id,
                    f"unknown created_by_kind {row['created_by_kind']!r}",
                )
            if row["author_kind"] not in {None, "human", "agent_run", "unknown"}:
                add(
                    "invalid_span_author_kind",
                    "evidence_span",
                    span_id,
                    f"unknown author_kind {row['author_kind']!r}",
                )
            if parent_evidence is not None:
                trust_class = parent_evidence["trust_class"]
                expected_author = {
                    "user_authored": "human",
                    "agent_authored": "agent_run",
                }.get(trust_class)
                if trust_class == "mixed" and row["author_kind"] is None:
                    add(
                        "mixed_span_missing_authorship",
                        "evidence_span",
                        span_id,
                        "mixed evidence requires explicit span author_kind",
                    )
                if (
                    expected_author is not None
                    and row["author_kind"] != expected_author
                ):
                    add(
                        "span_authorship_mismatch",
                        "evidence_span",
                        span_id,
                        f"{trust_class} evidence requires {expected_author} span authorship",
                    )
            if (
                row["author_kind"] == "agent_run"
                and not str(row["author_ref"] or "").strip()
            ):
                add(
                    "missing_span_author_ref",
                    "evidence_span",
                    span_id,
                    "agent_run span authorship requires author_ref",
                )
            if row["author_kind"] == "unknown" and row["author_ref"] is not None:
                add(
                    "unknown_span_has_author_ref",
                    "evidence_span",
                    span_id,
                    "unknown span authorship cannot carry author_ref",
                )
            if row["created_by_kind"] == "agent_run" and row["author_kind"] == "human":
                add(
                    "agent_asserted_human_span",
                    "evidence_span",
                    span_id,
                    "agent callers cannot assert human span authorship",
                )

        redactions_by_subject: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(
            list
        )
        redactable: Mapping[str, Mapping[str, sqlite3.Row | ClaimRecord]] = {
            "claim": claims_by_id,
            "evidence": evidence_by_id,
            "span": spans_by_id,
            "proposal": proposals_by_id,
        }
        evidence_cascade_redactions = {
            (
                row["subject_ref"],
                row["at"],
                row["basis_ref"],
                row["reason"],
            )
            for row in redaction_rows
            if row["subject_kind"] == "evidence" and row["basis_kind"] == "gesture"
        }
        redaction_gesture_uses: dict[str, list[str]] = defaultdict(list)
        for row in redaction_rows:
            if row["basis_kind"] not in REDACTION_BASIS_KINDS:
                add(
                    "invalid_redaction_basis",
                    "redaction_event",
                    row["id"],
                    f"unsupported redaction basis {row['basis_kind']!r}",
                )
            if row["reason"] not in REDACTION_REASONS:
                add(
                    "invalid_redaction_reason",
                    "redaction_event",
                    row["id"],
                    f"unsupported redaction reason {row['reason']!r}",
                )
            if not isinstance(row["basis_ref"], str) or not row["basis_ref"].strip():
                add(
                    "invalid_redaction_basis_ref",
                    "redaction_event",
                    row["id"],
                    "redaction basis_ref must be nonempty",
                )
            if not isinstance(row["actor_ref"], str) or not row["actor_ref"].strip():
                add(
                    "invalid_redaction_actor_ref",
                    "redaction_event",
                    row["id"],
                    "redaction actor_ref must be nonempty",
                )
            raw_subject_kind = row["subject_kind"]
            subject_kind = (
                "span" if raw_subject_kind == "evidence_span" else raw_subject_kind
            )
            if raw_subject_kind == "evidence_span":
                add(
                    "redaction_subject_kind_alias",
                    "redaction_event",
                    row["id"],
                    "use canonical subject_kind 'span' instead of 'evidence_span'",
                    severity="warning",
                )
            key = (subject_kind, row["subject_ref"])
            redactions_by_subject[key].append(row)
            subject_rows = redactable.get(subject_kind)
            if subject_rows is None:
                add(
                    "invalid_redaction_subject_kind",
                    "redaction_event",
                    row["id"],
                    f"unknown subject_kind {raw_subject_kind!r}",
                )
            subject = (
                subject_rows.get(row["subject_ref"])
                if subject_rows is not None
                else None
            )
            if subject is None:
                add(
                    "dangling_redaction_subject",
                    "redaction_event",
                    row["id"],
                    f"{raw_subject_kind} {row['subject_ref']} does not exist",
                )
                continue
            subject_created_at = (
                subject.created_at
                if isinstance(subject, ClaimRecord)
                else subject["created_at"]
            )
            redaction_time: datetime | None = None
            try:
                redaction_time = _time_key(row["at"], "redaction at")
                if redaction_time < _time_key(
                    subject_created_at,
                    "subject created_at",
                ):
                    add(
                        "redaction_before_subject",
                        "redaction_event",
                        row["id"],
                        "redaction predates its subject",
                    )
            except InvariantViolation as exc:
                add("invalid_redaction_time", "redaction_event", row["id"], str(exc))
            redacted_at = (
                subject.redacted_at
                if isinstance(subject, ClaimRecord)
                else subject["redacted_at"]
            )
            if redacted_at is None:
                add(
                    "redaction_event_without_redaction",
                    "redaction_event",
                    row["id"],
                    "subject row is not redacted",
                )
            elif redacted_at != row["at"]:
                add(
                    "redaction_time_mismatch",
                    "redaction_event",
                    row["id"],
                    "redaction event time differs from subject redacted_at",
                )
            if row["basis_kind"] == "gesture":
                gesture = gestures.get(row["basis_ref"])
                if gesture is None:
                    add(
                        "dangling_redaction_gesture",
                        "redaction_event",
                        row["id"],
                        f"gesture {row['basis_ref']} does not exist",
                    )
                else:
                    gesture_matches = gesture["subject_ref"] == row["subject_ref"]
                    cascade_match = False
                    if not gesture_matches and subject_kind == "span":
                        span = spans_by_id.get(row["subject_ref"])
                        if span is not None:
                            cascade_match = (
                                gesture["subject_ref"] == span["evidence_id"]
                                and (
                                    span["evidence_id"],
                                    row["at"],
                                    row["basis_ref"],
                                    row["reason"],
                                )
                                in evidence_cascade_redactions
                            )
                            gesture_matches = cascade_match
                    if not gesture_matches:
                        add(
                            "redaction_gesture_subject_mismatch",
                            "redaction_event",
                            row["id"],
                            "redaction gesture targets another subject",
                        )
                    if gesture["actor_ref"] != row["actor_ref"]:
                        add(
                            "redaction_gesture_actor_mismatch",
                            "redaction_event",
                            row["id"],
                            "redaction actor does not match the gesture actor",
                        )
                    if gesture["kind"] != "redact":
                        add(
                            "invalid_redaction_gesture_kind",
                            "redaction_event",
                            row["id"],
                            f"gesture kind {gesture['kind']!r} cannot redact content",
                        )
                    if gesture["consumed_at"] is None:
                        add(
                            "unconsumed_redaction_gesture",
                            "redaction_event",
                            row["id"],
                            "redaction gesture was not consumed",
                        )
                    if redaction_time is not None:
                        try:
                            decision_time = _time_key(gesture["at"], "gesture at")
                            if redaction_time < decision_time:
                                add(
                                    "redaction_before_gesture",
                                    "redaction_event",
                                    row["id"],
                                    "redaction predates its gesture",
                                )
                            if gesture[
                                "expires_at"
                            ] is not None and redaction_time >= _time_key(
                                gesture["expires_at"],
                                "gesture expires_at",
                            ):
                                add(
                                    "expired_redaction_gesture",
                                    "redaction_event",
                                    row["id"],
                                    "redaction gesture was expired at use time",
                                )
                            if (
                                gesture["consumed_at"] is not None
                                and _time_key(
                                    gesture["consumed_at"],
                                    "gesture consumed_at",
                                )
                                != redaction_time
                            ):
                                add(
                                    "redaction_gesture_consumption_mismatch",
                                    "redaction_event",
                                    row["id"],
                                    "gesture consumed_at must equal the redaction time",
                                )
                        except InvariantViolation as exc:
                            add(
                                "invalid_gesture_time",
                                "gesture",
                                row["basis_ref"],
                                str(exc),
                            )
                    if gesture_matches and not cascade_match:
                        redaction_gesture_uses[row["basis_ref"]].append(row["id"])
            elif row["basis_kind"] == "policy" and subject_kind == "proposal":
                # A standing policy may scrub a rejected (closed) or expired
                # proposal, mirroring the claim policy shape against the proposal
                # status ledger. Kept in lockstep with
                # TruthRedactor._validate_proposal_policy_locked so an
                # engine-produced store round-trips.
                proposal_expected_status = {
                    "rejected_content": "closed",
                    "expired_content": "expired",
                }.get(row["reason"])
                if proposal_expected_status is None:
                    add(
                        "invalid_policy_redaction_reason",
                        "redaction_event",
                        row["id"],
                        "standing policy supports rejected_content or expired_content only",
                    )
                else:
                    try:
                        expected_basis = policy_basis_ref(store, row["reason"])
                    except InvariantViolation:
                        expected_basis = None
                    if row["basis_ref"] != expected_basis:
                        add(
                            "invalid_policy_redaction_basis_ref",
                            "redaction_event",
                            row["id"],
                            f"policy basis must be exactly {expected_basis!r}",
                        )
                    proposal_events = proposal_status_by_proposal.get(
                        row["subject_ref"], []
                    )
                    latest_proposal_status = (
                        proposal_events[-1]["status"] if proposal_events else None
                    )
                    if latest_proposal_status != proposal_expected_status:
                        add(
                            "invalid_policy_redaction_status",
                            "redaction_event",
                            row["id"],
                            f"{row['reason']} policy requires proposal status "
                            f"{proposal_expected_status!r}, found "
                            f"{latest_proposal_status!r}",
                        )
                    if any(
                        event["status"] == "applied" for event in proposal_events
                    ):
                        add(
                            "policy_redaction_of_confirmed",
                            "redaction_event",
                            row["id"],
                            "a proposal that was ever applied requires a human gesture",
                        )
            elif row["basis_kind"] == "policy":
                if subject_kind != "claim":
                    add(
                        "policy_redaction_of_non_claim",
                        "redaction_event",
                        row["id"],
                        "standing profile policy can redact claim content only",
                    )
                    continue
                expected_status = {
                    "rejected_content": "rejected",
                    "expired_content": "expired",
                }.get(row["reason"])
                if expected_status is None:
                    add(
                        "invalid_policy_redaction_reason",
                        "redaction_event",
                        row["id"],
                        "standing policy supports rejected_content or expired_content only",
                    )
                else:
                    try:
                        expected_basis = policy_basis_ref(store, row["reason"])
                    except InvariantViolation:
                        expected_basis = None
                    if row["basis_ref"] != expected_basis:
                        add(
                            "invalid_policy_redaction_basis_ref",
                            "redaction_event",
                            row["id"],
                            f"policy basis must be exactly {expected_basis!r}",
                        )
                    prior_base_events: list[sqlite3.Row] = []
                    for status_event in status_rows:
                        if (
                            status_event["claim_id"] != row["subject_ref"]
                            or status_event["status"] == "needs_review"
                            or (
                                status_event["basis_kind"] == "redaction"
                                and status_event["basis_ref"] == row["id"]
                            )
                        ):
                            continue
                        try:
                            if _time_key(
                                status_event["at"],
                                "status event at",
                            ) <= _time_key(row["at"], "redaction at"):
                                prior_base_events.append(status_event)
                        except InvariantViolation:
                            continue
                    prior_status = (
                        prior_base_events[-1]["status"] if prior_base_events else None
                    )
                    if prior_status != expected_status:
                        add(
                            "invalid_policy_redaction_status",
                            "redaction_event",
                            row["id"],
                            f"{row['reason']} policy requires prior base status "
                            f"{expected_status!r}, found {prior_status!r}",
                        )
                    if any(
                        event["claim_id"] == row["subject_ref"]
                        and event["status"] == "confirmed"
                        for event in status_rows
                    ):
                        add(
                            "policy_redaction_of_confirmed",
                            "redaction_event",
                            row["id"],
                            "content that was ever confirmed requires a human gesture",
                        )

        for (subject_kind, subject_ref), events in redactions_by_subject.items():
            if len(events) > 1:
                add(
                    "duplicate_redaction_event",
                    subject_kind,
                    subject_ref,
                    f"subject has {len(events)} redaction events",
                )

        all_gesture_ids = set(gesture_uses) | set(redaction_gesture_uses)
        for gesture_id in all_gesture_ids:
            status_uses = gesture_uses.get(gesture_id, [])
            operation_count = len(status_uses)
            if gesture_id in reasoned_rejection_gestures:
                operation_count = 1
            operation_count += len(redaction_gesture_uses.get(gesture_id, []))
            if operation_count > 1:
                add(
                    "gesture_replay",
                    "gesture",
                    gesture_id,
                    f"gesture was consumed by {operation_count} operations",
                )
        redactions_by_id = {row["id"]: row for row in redaction_rows}
        status_redactions_by_id: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in status_rows:
            if row["basis_kind"] != "redaction":
                continue
            status_redactions_by_id[row["basis_ref"]].append(row)
            event = redactions_by_id.get(row["basis_ref"])
            if event is None:
                add(
                    "dangling_status_redaction",
                    "status_event",
                    row["id"],
                    f"redaction event {row['basis_ref']} does not exist",
                )
            elif (
                event["subject_kind"] != "claim"
                or event["subject_ref"] != row["claim_id"]
            ):
                add(
                    "status_redaction_subject_mismatch",
                    "status_event",
                    row["id"],
                    "redaction basis targets another subject",
                )
            else:
                if row["status"] != "retracted":
                    add(
                        "invalid_status_redaction_kind",
                        "status_event",
                        row["id"],
                        "redaction basis can produce only a retracted status",
                    )
                if row["at"] != event["at"]:
                    add(
                        "status_redaction_time_mismatch",
                        "status_event",
                        row["id"],
                        "claim retraction time differs from its redaction event",
                    )
                if row["actor_ref"] != event["actor_ref"]:
                    add(
                        "status_redaction_actor_mismatch",
                        "status_event",
                        row["id"],
                        "claim retraction actor differs from its redaction event",
                    )
                if row["note"] != event["reason"]:
                    add(
                        "status_redaction_reason_mismatch",
                        "status_event",
                        row["id"],
                        "claim retraction note differs from its redaction reason",
                    )
                if event["basis_kind"] == "gesture" and row["actor_kind"] != "human":
                    add(
                        "status_redaction_actor_kind_mismatch",
                        "status_event",
                        row["id"],
                        "gesture redaction requires a human retraction actor",
                    )

        for row in redaction_rows:
            if row["subject_kind"] != "claim":
                continue
            prior_base_events = []
            for status_event in status_rows:
                if (
                    status_event["claim_id"] != row["subject_ref"]
                    or status_event["status"] == "needs_review"
                    or (
                        status_event["basis_kind"] == "redaction"
                        and status_event["basis_ref"] == row["id"]
                    )
                ):
                    continue
                try:
                    if _time_key(
                        status_event["at"],
                        "status event at",
                    ) <= _time_key(row["at"], "redaction at"):
                        prior_base_events.append(status_event)
                except InvariantViolation:
                    continue
            prior_status = (
                prior_base_events[-1]["status"] if prior_base_events else None
            )
            co_statuses = status_redactions_by_id.get(row["id"], [])
            if prior_status not in TERMINAL_STATUSES and not co_statuses:
                add(
                    "missing_claim_redaction_status",
                    "redaction_event",
                    row["id"],
                    f"redaction from live status {prior_status!r} lacks retraction event",
                )
            if len(co_statuses) > 1:
                add(
                    "duplicate_claim_redaction_status",
                    "redaction_event",
                    row["id"],
                    f"redaction has {len(co_statuses)} retraction events",
                )
        for kind, subject_rows in redactable.items():
            if kind == "proposal":
                # A proposal's content is often scrubbed by the decision-path
                # policy redaction (reject/dismiss under gate.rejected_content ==
                # redact), which records the scrub via redacted_at plus the
                # rejection status event rather than a standalone redaction
                # event. A redacted proposal therefore is not required to carry
                # one. Any event that does exist is still validated above.
                continue
            for subject_ref, subject in subject_rows.items():
                redacted_at = (
                    subject.redacted_at
                    if isinstance(subject, ClaimRecord)
                    else subject["redacted_at"]
                )
                if redacted_at is not None and not redactions_by_subject.get(
                    (kind, subject_ref)
                ):
                    add(
                        "redaction_without_event",
                        kind,
                        subject_ref,
                        "redacted row has no redaction event",
                    )

        sweep_rows = read_conn.execute("SELECT * FROM sweeps ORDER BY id").fetchall()
        sweep_ids = {row["id"] for row in sweep_rows}
        for row in sweep_rows:
            try:
                _time_key(row["at"], "sweep at")
            except InvariantViolation as exc:
                add("invalid_sweep_time", "sweep", row["id"], str(exc))
            if row["params_json"] is not None:
                _, error = _try_json_object(row["params_json"])
                if error is not None:
                    add("invalid_sweep_params", "sweep", row["id"], error)
        local_subject_tables: Mapping[str, set[str]] = {
            "claim": set(claims_by_id),
            "claim_link": set(links_by_id),
            "link": set(links_by_id),
            "evidence": set(evidence_by_id),
            "evidence_span": set(spans_by_id),
            "derivation": derivation_ids,
        }
        finding_rows = read_conn.execute(
            "SELECT * FROM sweep_findings ORDER BY id"
        ).fetchall()
        for row in finding_rows:
            if row["sweep_id"] not in sweep_ids:
                add(
                    "dangling_sweep",
                    "sweep_finding",
                    row["id"],
                    f"sweep {row['sweep_id']} does not exist",
                )
            subjects = local_subject_tables.get(row["subject_kind"])
            if subjects is not None and row["subject_ref"] not in subjects:
                add(
                    "dangling_sweep_subject",
                    "sweep_finding",
                    row["id"],
                    f"{row['subject_kind']} {row['subject_ref']} does not exist",
                )
            if (row["resolved_at"] is None) != (row["resolved_by_ref"] is None):
                add(
                    "incomplete_sweep_resolution",
                    "sweep_finding",
                    row["id"],
                    "resolved_at and resolved_by_ref must be present together",
                )
            if row["resolved_at"] is not None:
                try:
                    resolved_at = _time_key(
                        row["resolved_at"], "sweep finding resolved_at"
                    )
                    sweep = next(
                        (item for item in sweep_rows if item["id"] == row["sweep_id"]),
                        None,
                    )
                    if sweep is not None and resolved_at < _time_key(
                        sweep["at"], "sweep at"
                    ):
                        add(
                            "sweep_resolved_before_run",
                            "sweep_finding",
                            row["id"],
                            "resolved_at predates its sweep",
                        )
                except InvariantViolation as exc:
                    add(
                        "invalid_sweep_resolution_time",
                        "sweep_finding",
                        row["id"],
                        str(exc),
                    )

        if current_targets is not None:
            for state in link_fingerprint_states(
                store,
                current_targets=current_targets,
                conn=read_conn,
            ):
                if state.status is FingerprintStatus.UNREVIEWED:
                    add(
                        "fingerprint_unreviewed",
                        "claim_link",
                        state.link_id,
                        state.detail or "mutable target has no reviewed fingerprint",
                        severity="warning",
                    )
                elif state.status is FingerprintStatus.STALE:
                    add(
                        "fingerprint_stale",
                        "claim_link",
                        state.link_id,
                        state.detail or "mutable target fingerprint is stale",
                        severity="warning",
                    )

        document_ledger: dict[str, set[str]] = {}
        if _document_surface_tables_present(read_conn):
            document_ledger = _document_integrity_findings(
                read_conn,
                store,
                add,
                claims_by_id=claims_by_id,
                evidence_by_id=evidence_by_id,
                spans_by_id=spans_by_id,
                gestures=gestures,
            )

        expected_ledger: dict[str, set[str]] = {
            "evidence": set(evidence_by_id),
            "evidence_span": set(spans_by_id),
            "claim": set(claims_by_id),
            "derivation": derivation_ids,
            "claim_link": set(links_by_id),
            "link_retraction": {row["link_id"] for row in retraction_rows},
            "claim_status_event": {row["id"] for row in status_rows},
            "gesture": set(gestures),
            "redaction_event": {row["id"] for row in redaction_rows},
            "sweep": sweep_ids,
            "sweep_finding": {row["id"] for row in finding_rows},
            "derivation_premise": {
                canonical_json(
                    {
                        "derivation_id": row["derivation_id"],
                        "premise_ref": row["premise_ref"],
                    }
                )
                for row in premise_rows
            },
        }
        # Fold in the six co-work ledger types so the store-wide completeness
        # check does not flag document rows as unknown record types.
        expected_ledger.update(document_ledger)
        actual_ledger: dict[str, set[str]] = defaultdict(set)
        for row in ledger_rows:
            actual_ledger[row["record_type"]].add(row["record_key"])
            expected = expected_ledger.get(row["record_type"])
            if expected is None:
                add(
                    "unknown_ledger_record_type",
                    "ledger_record",
                    str(row["seq"]),
                    f"unknown record_type {row['record_type']!r}",
                )
            elif row["record_key"] not in expected:
                add(
                    "orphan_ledger_record",
                    "ledger_record",
                    str(row["seq"]),
                    f"{row['record_type']} key {row['record_key']} has no row",
                )
        for record_type, expected_keys in expected_ledger.items():
            missing = expected_keys - actual_ledger.get(record_type, set())
            for record_key in sorted(missing):
                add(
                    "missing_ledger_record",
                    record_type,
                    record_key,
                    "durable row is absent from ledger_records",
                )

    return tuple(
        sorted(
            found.values(),
            key=lambda item: (
                item.severity,
                item.code,
                item.subject_kind,
                item.subject_ref,
                item.detail,
            ),
        )
    )


# Explicit query-oriented aliases keep the public call sites readable while the
# short names remain convenient for direct engine use.
query_current_claims = current_claims
query_claims_as_of = claims_as_of
query_conflicts = conflicts
query_needs_review = needs_review
run_integrity_check = integrity_findings


__all__ = [
    "ConflictState",
    "ClaimState",
    "CrossStoreResolver",
    "DERIVATION_DEPENDENCY_CTE",
    "IntegrityFinding",
    "LinkFingerprintState",
    "NeedsReviewItem",
    "PremiseResolution",
    "RecordedSweep",
    "STATUS_RESOLUTION_CTE",
    "SUPPORT_DEPENDENCY_CTE",
    "SourceIntegrityState",
    "SuccessorRace",
    "SweepCandidate",
    "SweepFindingSpec",
    "claims_as_of",
    "conflicts",
    "current_claims",
    "integrity_findings",
    "link_fingerprint_states",
    "needs_review",
    "query_claims_as_of",
    "query_conflicts",
    "query_current_claims",
    "query_needs_review",
    "rebuild_claims_current",
    "record_sweep",
    "resolve_claim_states",
    "run_integrity_check",
    "source_integrity_states",
    "source_sweep_candidates",
    "successor_races",
    "supersession_sweep_candidates",
]

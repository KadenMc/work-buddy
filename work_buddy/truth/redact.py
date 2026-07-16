"""Controlled content redaction for the append-only truth ledger.

Redaction is the kernel's sole sanctioned mutation.  Identity, hashes, links,
and the event history survive. Only human-readable content is destroyed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from work_buddy.truth.contracts import Actor, InvariantViolation, TERMINAL_STATUSES
from work_buddy.truth.migrations import REDACTED_SELECTOR_JSON
from work_buddy.truth.store import (
    PostCommitHookError,
    TruthStore,
    _parse_time,
    _record_id,
    _timestamp,
)

if TYPE_CHECKING:
    from work_buddy.truth.lifecycle import TruthLifecycle
    from work_buddy.truth.store import StatusEventRecord


SUBJECT_KINDS = frozenset({"claim", "evidence", "span"})
REDACTION_REASONS = frozenset(
    {"rejected_content", "expired_content", "privacy", "source_takedown"}
)
REDACTION_BASIS_KINDS = frozenset({"gesture", "policy"})


@dataclass(frozen=True, slots=True)
class RedactionEventRecord:
    """One immutable audit record describing destroyed content."""

    id: str
    subject_kind: str
    subject_ref: str
    at: str
    actor_ref: str
    basis_kind: str
    basis_ref: str
    reason: str


@dataclass(frozen=True, slots=True)
class RedactionResult:
    """The durable result and any post-commit blob cleanup state."""

    event: RedactionEventRecord
    cascade_events: tuple[RedactionEventRecord, ...] = ()
    status_event: StatusEventRecord | None = None
    created: bool = True
    blob_sha256: str | None = None
    blob_deleted: bool = False
    blob_cleanup_pending: bool = False


@dataclass(frozen=True, slots=True)
class _Subject:
    kind: str
    ref: str
    payload_sha256: str
    created_at: str
    redacted_at: str | None
    content_path: str | None = None


def policy_basis_ref(store: TruthStore, reason: str) -> str:
    """Return the exact standing-policy key accepted for one profile reason."""

    if reason == "rejected_content":
        key = "gate.rejected_content"
    elif reason == "expired_content":
        key = "proposal_max_age"
    else:
        raise InvariantViolation(
            "only rejected_content and expired_content have standing policies"
        )
    return f"profile:{store.profile.profile}:{key}"


class TruthRedactor:
    """Apply trigger-shaped redactions and append their audit companions."""

    def __init__(
        self,
        store: TruthStore,
        *,
        lifecycle: TruthLifecycle | None = None,
    ) -> None:
        self.store = store
        if lifecycle is None:
            from work_buddy.truth.lifecycle import TruthLifecycle

            lifecycle = TruthLifecycle(store)
        self.lifecycle = lifecycle

    @contextmanager
    def _write_with_cleanup_on_hook_failure(
        self,
        conn: sqlite3.Connection | None,
        pending_blob: Callable[[], str | None],
    ) -> Iterator[sqlite3.Connection]:
        """Finish sensitive blob cleanup when a post-commit hook fails."""

        body_completed = False
        try:
            with self.store.write_transaction(conn) as write_conn:
                yield write_conn
                body_completed = True
        except PostCommitHookError:
            if not body_completed:
                raise
            digest = pending_blob()
            if digest is not None:
                try:
                    self.store._finish_blob_cleanup(digest)
                except Exception as cleanup_exc:
                    raise PostCommitHookError(
                        "redaction committed but blob cleanup failed after a "
                        "post-commit hook failure"
                    ) from cleanup_exc
            raise

    @staticmethod
    def _require_actor_ref(actor: Actor) -> str:
        if not isinstance(actor.ref, str) or not actor.ref.strip():
            raise InvariantViolation("redaction actor requires a durable actor ref")
        return actor.ref.strip()

    def _subject_locked(
        self,
        conn: sqlite3.Connection,
        subject_kind: str,
        subject_ref: str,
    ) -> _Subject:
        if subject_kind == "claim":
            row = self.store._get_claim_locked(conn, subject_ref)
            if row is None:
                raise InvariantViolation(f"claim does not exist: {subject_ref}")
            return _Subject(
                kind="claim",
                ref=row.id,
                payload_sha256=row.canonical_sha256,
                created_at=row.created_at,
                redacted_at=row.redacted_at,
            )
        if subject_kind == "evidence":
            row = self.store._get_evidence_locked(conn, subject_ref)
            if row is None:
                raise InvariantViolation(f"evidence does not exist: {subject_ref}")
            return _Subject(
                kind="evidence",
                ref=row.id,
                payload_sha256=row.content_sha256,
                created_at=row.created_at,
                redacted_at=row.redacted_at,
                content_path=row.content_path,
            )
        row = self.store._get_span_locked(conn, subject_ref)
        if row is None:
            raise InvariantViolation(f"evidence span does not exist: {subject_ref}")
        return _Subject(
            kind="span",
            ref=row.id,
            payload_sha256=row.span_sha256,
            created_at=row.created_at,
            redacted_at=row.redacted_at,
        )

    @staticmethod
    def _redaction_event_locked(
        conn: sqlite3.Connection,
        subject_kind: str,
        subject_ref: str,
    ) -> RedactionEventRecord | None:
        row = conn.execute(
            "SELECT * FROM redaction_events "
            "WHERE subject_kind = ? AND subject_ref = ? "
            "ORDER BY rowid LIMIT 1",
            (subject_kind, subject_ref),
        ).fetchone()
        return RedactionEventRecord(**dict(row)) if row is not None else None

    @staticmethod
    def _ever_confirmed_locked(
        conn: sqlite3.Connection,
        subject_kind: str,
        subject_ref: str,
    ) -> bool:
        if subject_kind == "claim":
            row = conn.execute(
                "SELECT 1 FROM claim_status_events "
                "WHERE claim_id = ? AND status = 'confirmed' LIMIT 1",
                (subject_ref,),
            ).fetchone()
        elif subject_kind == "span":
            row = conn.execute(
                "SELECT 1 FROM claim_links AS link "
                "JOIN claim_status_events AS event "
                "ON event.claim_id = link.from_claim_id "
                "WHERE link.link_type = 'supports_span' "
                "AND link.to_kind = 'evidence_span' AND link.to_ref = ? "
                "AND event.status = 'confirmed' LIMIT 1",
                (subject_ref,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM evidence_spans AS span "
                "JOIN claim_links AS link ON link.link_type = 'supports_span' "
                "AND link.to_kind = 'evidence_span' AND link.to_ref = span.id "
                "JOIN claim_status_events AS event "
                "ON event.claim_id = link.from_claim_id "
                "WHERE span.evidence_id = ? AND event.status = 'confirmed' LIMIT 1",
                (subject_ref,),
            ).fetchone()
        return row is not None

    def _validate_policy_locked(
        self,
        conn: sqlite3.Connection,
        subject: _Subject,
        *,
        basis_ref: str,
        reason: str,
    ) -> None:
        if subject.kind != "claim":
            raise InvariantViolation(
                "standing profile policy can redact claim content only"
            )
        if self._ever_confirmed_locked(conn, subject.kind, subject.ref):
            raise InvariantViolation(
                "content that was ever confirmed requires a human redaction gesture"
            )
        expected = policy_basis_ref(self.store, reason)
        if basis_ref != expected:
            raise InvariantViolation(
                f"redaction policy basis must be exactly {expected!r}"
            )
        latest = self.store._latest_status_locked(
            conn,
            subject.ref,
            include_overlay=False,
        )
        if latest is None:
            raise InvariantViolation("claim has no base lifecycle status")
        expected_status = {
            "rejected_content": "rejected",
            "expired_content": "expired",
        }[reason]
        if latest.status != expected_status:
            raise InvariantViolation(
                f"{reason} policy requires claim status {expected_status!r}"
            )
        if reason == "rejected_content":
            if self.store.profile.gate.rejected_content != "redact":
                raise InvariantViolation("profile retains rejected claim content")
        elif self.store.profile.proposal_max_age_seconds is None and (
            self.store.profile.extensions.get("expired_proposal_content") != "redact"
        ):
            raise InvariantViolation(
                "profile does not declare expired-content redaction"
            )

    def _insert_event_locked(
        self,
        conn: sqlite3.Connection,
        *,
        subject_kind: str,
        subject_ref: str,
        actor_ref: str,
        basis_kind: str,
        basis_ref: str,
        reason: str,
        at: str,
        event_id: str | None = None,
    ) -> RedactionEventRecord:
        record = RedactionEventRecord(
            id=_record_id(event_id, "redaction event id"),
            subject_kind=subject_kind,
            subject_ref=subject_ref,
            at=at,
            actor_ref=actor_ref,
            basis_kind=basis_kind,
            basis_ref=basis_ref,
            reason=reason,
        )
        conn.execute(
            "INSERT INTO redaction_events "
            "(id, subject_kind, subject_ref, at, actor_ref, basis_kind, "
            "basis_ref, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.subject_kind,
                record.subject_ref,
                record.at,
                record.actor_ref,
                record.basis_kind,
                record.basis_ref,
                record.reason,
            ),
        )
        self.store._insert_ledger_record_locked(
            conn,
            "redaction_event",
            record.id,
        )
        return record

    def _redact_content_locked(
        self,
        conn: sqlite3.Connection,
        subject: _Subject,
        at: str,
    ) -> None:
        if subject.kind == "claim":
            conn.execute(
                "UPDATE claims SET proposition = '[redacted]', "
                "structured_json = NULL, redacted_at = ? WHERE id = ?",
                (at, subject.ref),
            )
        elif subject.kind == "evidence":
            conn.execute(
                "UPDATE evidence SET content = NULL, content_path = NULL, "
                "redacted_at = ? WHERE id = ?",
                (at, subject.ref),
            )
        else:
            conn.execute(
                "UPDATE evidence_spans SET selector_json = ?, "
                "quote_exact = NULL, redacted_at = ? WHERE id = ?",
                (REDACTED_SELECTOR_JSON, at, subject.ref),
            )
        self._redact_gesture_excerpts_locked(conn, subject.ref)

    @staticmethod
    def _redact_gesture_excerpts_locked(
        conn: sqlite3.Connection,
        subject_ref: str,
    ) -> None:
        """Destroy readable receipts bound to a now-redacted subject."""

        conn.execute(
            "UPDATE gestures SET payload_excerpt = '[redacted]' "
            "WHERE subject_ref = ? AND payload_excerpt <> '[redacted]'",
            (subject_ref,),
        )

    def _cascade_evidence_spans_locked(
        self,
        conn: sqlite3.Connection,
        *,
        evidence_ref: str,
        actor_ref: str,
        basis_kind: str,
        basis_ref: str,
        reason: str,
        at: str,
    ) -> tuple[RedactionEventRecord, ...]:
        rows = conn.execute(
            "SELECT id, span_sha256, created_at, redacted_at "
            "FROM evidence_spans WHERE evidence_id = ? ORDER BY created_at, id",
            (evidence_ref,),
        ).fetchall()
        events: list[RedactionEventRecord] = []
        for row in rows:
            if row["redacted_at"] is not None:
                continue
            span = _Subject(
                kind="span",
                ref=row["id"],
                payload_sha256=row["span_sha256"],
                created_at=row["created_at"],
                redacted_at=None,
            )
            self._redact_content_locked(conn, span, at)
            events.append(
                self._insert_event_locked(
                    conn,
                    subject_kind="span",
                    subject_ref=span.ref,
                    actor_ref=actor_ref,
                    basis_kind=basis_kind,
                    basis_ref=basis_ref,
                    reason=reason,
                    at=at,
                )
            )
        return tuple(events)

    def redact(
        self,
        *,
        subject_kind: str,
        subject_ref: str,
        actor: Actor,
        reason: str,
        basis_kind: str,
        basis_ref: str,
        expected_context_sha256: str | None = None,
        event_id: str | None = None,
        at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> RedactionResult:
        """Destroy content while retaining identity. Evidence includes its quotes."""

        kind = str(subject_kind).strip().lower()
        if kind not in SUBJECT_KINDS:
            raise InvariantViolation(
                f"subject_kind must be one of {sorted(SUBJECT_KINDS)}"
            )
        reference = str(subject_ref).strip().lower()
        if not reference:
            raise InvariantViolation("subject_ref must be nonempty")
        if reason not in REDACTION_REASONS:
            raise InvariantViolation(
                f"redaction reason must be one of {sorted(REDACTION_REASONS)}"
            )
        if basis_kind not in REDACTION_BASIS_KINDS:
            raise InvariantViolation("redaction basis_kind must be gesture or policy")
        if not isinstance(basis_ref, str) or not basis_ref.strip():
            raise InvariantViolation("redaction basis_ref must be nonempty")
        actor_ref = self._require_actor_ref(actor)
        timestamp = _timestamp(at, "redaction at")

        # A process can be interrupted after a prior redaction commits but
        # before its blob is unlinked.  Normal store opening already retries
        # these durable intents. Doing the same here also makes a same-process
        # idempotent retry repair that window before inspecting the tombstone.
        if conn is None:
            self.store.recover_pending_redactions()
            self.store.recover_pending_blob_cleanups()

        blob_digest: str | None = None
        cascade_events: tuple[RedactionEventRecord, ...] = ()
        status_event: StatusEventRecord | None = None
        with self._write_with_cleanup_on_hook_failure(
            conn,
            lambda: blob_digest,
        ) as write_conn:
            # SQLite otherwise may leave the replaced quote/excerpt bytes in a
            # b-tree freeblock even though SQL readers see only tombstones.
            # This overwrites retired payload bytes in the committed database.
            # A reader that already owns an older WAL snapshot can retain that
            # pre-redaction view until it releases the snapshot.
            write_conn.execute("PRAGMA secure_delete = ON")
            subject = self._subject_locked(write_conn, kind, reference)
            if subject.redacted_at is not None:
                existing = self._redaction_event_locked(write_conn, kind, reference)
                if existing is None:
                    raise InvariantViolation(
                        "redacted subject is missing its redaction audit event"
                    )
                # A later engine-created gesture can only display the tombstone,
                # but scrub it here as well so idempotent calls preserve the
                # stronger invariant for imported or repaired stores.
                self._redact_gesture_excerpts_locked(write_conn, subject.ref)
                return RedactionResult(event=existing, created=False)
            if _parse_time(timestamp, "redaction at") < _parse_time(
                subject.created_at,
                "subject created_at",
            ):
                raise InvariantViolation("redaction cannot predate its subject")
            if subject.kind == "claim":
                latest_event = self.store._latest_status_locked(
                    write_conn,
                    subject.ref,
                    include_overlay=True,
                )
                if latest_event is None:
                    raise InvariantViolation("claim has no lifecycle status")
                if _parse_time(timestamp, "redaction at") < _parse_time(
                    latest_event.at,
                    "latest status at",
                ):
                    raise InvariantViolation(
                        "redaction cannot predate the latest claim status"
                    )
            if subject.content_path is not None and conn is not None:
                raise InvariantViolation(
                    "blob-backed evidence cannot be redacted inside a "
                    "caller-owned transaction"
                )

            if basis_kind == "gesture":
                if actor.kind != "human":
                    raise InvariantViolation(
                        "gesture-based redaction requires a human actor"
                    )
                self.lifecycle.verify_and_consume_gesture(
                    gesture_id=basis_ref,
                    actor=actor,
                    subject_ref=subject.ref,
                    payload_sha256=subject.payload_sha256,
                    expected_context_sha256=expected_context_sha256,
                    allowed_kinds={"redact"},
                    observed_at=timestamp,
                    conn=write_conn,
                )
            else:
                self._validate_policy_locked(
                    write_conn,
                    subject,
                    basis_ref=basis_ref,
                    reason=reason,
                )

            if subject.content_path is not None:
                blob_digest = subject.payload_sha256
            self._redact_content_locked(write_conn, subject, timestamp)
            event = self._insert_event_locked(
                write_conn,
                subject_kind=kind,
                subject_ref=subject.ref,
                actor_ref=actor_ref,
                basis_kind=basis_kind,
                basis_ref=basis_ref,
                reason=reason,
                at=timestamp,
                event_id=event_id,
            )
            if kind == "evidence":
                cascade_events = self._cascade_evidence_spans_locked(
                    write_conn,
                    evidence_ref=subject.ref,
                    actor_ref=actor_ref,
                    basis_kind=basis_kind,
                    basis_ref=basis_ref,
                    reason=reason,
                    at=timestamp,
                )
            if kind == "claim":
                latest = self.store._latest_status_locked(
                    write_conn,
                    subject.ref,
                    include_overlay=False,
                )
                if latest is None:
                    raise InvariantViolation("claim has no base lifecycle status")
                if latest.status not in TERMINAL_STATUSES:
                    transition = self.lifecycle.transition_claim(
                        claim_id=subject.ref,
                        status="retracted",
                        actor=actor,
                        basis_kind="redaction",
                        basis_ref=event.id,
                        note=reason,
                        at=timestamp,
                        conn=write_conn,
                    )
                    status_event = transition.event if transition.created else None
            # Remove the pre-redaction recovery export before this transaction
            # can commit.  The content-free event marker survives any crash
            # until the post-commit hook republishes the redacted projection.
            self.store._queue_redaction_recovery_locked(write_conn, event.id)
            if blob_digest is not None:
                # The digest-only marker must exist before the redaction commit
                # so interruption at any later point remains recoverable.
                self.store._queue_blob_cleanup_locked(write_conn, blob_digest)

        cleanup_pending = conn is not None and blob_digest is not None
        blob_deleted = False
        if conn is None and blob_digest is not None:
            try:
                blob_deleted = self.store._finish_blob_cleanup(blob_digest)
            except Exception as exc:
                raise PostCommitHookError(
                    "redaction committed but blob cleanup failed"
                ) from exc
        return RedactionResult(
            event=event,
            cascade_events=cascade_events,
            status_event=status_event,
            blob_sha256=blob_digest,
            blob_deleted=blob_deleted,
            blob_cleanup_pending=cleanup_pending,
        )

    def cleanup_redacted_blob(self, digest: str) -> bool:
        """Delete a deferred blob iff no live evidence row references it."""

        path = self.store.resolve_blob_path(f"blobs/{digest}")
        existed = path.exists()
        intent = self.store._blob_cleanup_intent_path(digest)
        if intent.is_file():
            deleted = self.store._finish_blob_cleanup(digest)
            return deleted or (existed and not path.exists())
        # Compatibility for callers cleaning stores written before durable
        # intents existed.  New redactions always take the intent path above.
        self.store._remove_unreferenced_blob(digest)
        return existed and not path.exists()

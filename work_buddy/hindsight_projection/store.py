"""SQLite outbox, destination receipts, and reconciliation cursors.

The tables live in the Truth database.  ``enqueue_in_transaction`` never opens
or commits a transaction, allowing a Truth lifecycle mutation and its desired
projection state to become durable atomically.  Delivery runs later through
leases and never changes canonical Truth records.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Sequence

from work_buddy.backups.source_foundation_restore import (
    require_source_foundation_writable,
    source_foundation_read_only,
)
from work_buddy.hindsight_projection.contracts import (
    DependencyUsage,
    DesiredProjectionState,
    DisclosureDeliveryReceipt,
    OutboxState,
    ProjectionConflict,
    ProjectionEffect,
    ProjectionIntentSpec,
    ProjectionLease,
    ProjectionLeaseConflict,
    ProjectionNotFound,
    ProjectionReceipt,
    ProjectionValidationError,
    ReceiptState,
    canonical_json,
    utc_now,
)
from work_buddy.hindsight_projection.schema import projection_schema_present


SQLITE_TIMEOUT_SECONDS = 10.0
SQLITE_BUSY_TIMEOUT_MS = 10_000


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ProjectionValidationError("stored timestamp is missing a timezone")
    return parsed.astimezone(timezone.utc)


def _plus_seconds(value: str, seconds: int) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise ProjectionValidationError("lease_seconds must be a positive integer")
    return (_parse_time(value) + timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _usages_json(usages: Sequence[DependencyUsage]) -> str:
    return canonical_json([usage.to_dict() for usage in usages])


def _parse_usages(value: str) -> tuple[DependencyUsage, ...]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ProjectionValidationError("stored dependency usages are invalid") from exc
    if not isinstance(parsed, list):
        raise ProjectionValidationError("stored dependency usages are invalid")
    return tuple(DependencyUsage.from_dict(item) for item in parsed)


class TruthHindsightProjectionStore:
    """Runtime access to projection state embedded in one Truth database."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    def connect(self) -> sqlite3.Connection:
        read_only = source_foundation_read_only()
        conn = sqlite3.connect(
            (
                f"file:{self.db_path.resolve()}?mode=ro"
                if read_only
                else str(self.db_path)
            ),
            timeout=SQLITE_TIMEOUT_SECONDS,
            isolation_level=None,
            uri=read_only,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        if read_only:
            conn.execute("PRAGMA query_only = ON")
        else:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
        if not projection_schema_present(conn):
            conn.close()
            raise ProjectionValidationError(
                "Truth Hindsight projection schema is not installed"
            )
        return conn

    def has_tracked_projection_state(self) -> bool:
        """Return whether a disabled rollout still has work or cleanup.

        Disabled means no new upserts; it does not abandon a removal,
        dependency acknowledgement/release, generated-source cleanup, or a
        Sources redaction already in flight.
        """

        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT EXISTS ("
                "SELECT 1 FROM truth_hindsight_projection_receipts "
                "WHERE receipt_state = 'present' "
                "UNION ALL SELECT 1 FROM truth_hindsight_projection_heads "
                "WHERE desired_state = 'upsert' "
                "UNION ALL SELECT 1 FROM truth_hindsight_projection_outbox "
                "WHERE state IN ('pending','delivering','reconciling','failed_retryable') "
                "UNION ALL SELECT 1 FROM truth_hindsight_projection_dependencies "
                "WHERE active = 1 OR released_at IS NULL "
                "UNION ALL SELECT 1 FROM truth_hindsight_projection_source_cleanup "
                "WHERE state = 'pending'"
                ")"
            ).fetchone()
            return bool(row[0])
        finally:
            conn.close()

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        require_source_foundation_writable("hindsight_projection.write")
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def enqueue_in_transaction(
        conn: sqlite3.Connection,
        spec: ProjectionIntentSpec,
        *,
        effect_id: str | None = None,
    ) -> ProjectionEffect:
        """Write an intent beside the authoritative Truth mutation.

        The caller must already hold the Truth write transaction.  The method
        neither begins nor commits, and an idempotent replay returns the exact
        original effect.
        """

        require_source_foundation_writable("hindsight_projection.enqueue")

        if not conn.in_transaction:
            raise ProjectionValidationError(
                "projection intent must be enqueued inside a Truth transaction"
            )
        if not projection_schema_present(conn):
            raise ProjectionValidationError(
                "Truth Hindsight projection schema is not installed"
            )
        existing = conn.execute(
            "SELECT * FROM truth_hindsight_projection_outbox "
            "WHERE claim_id = ? AND claim_generation = ? AND policy_id = ?",
            (spec.claim_id, spec.claim_generation, spec.policy_id),
        ).fetchone()
        if existing is not None:
            if str(existing["request_sha256"]) != spec.request_sha256:
                raise ProjectionConflict(
                    "the same claim generation was enqueued with different semantics"
                )
            return _effect_from_row(existing)

        identifier = effect_id or uuid.uuid4().hex
        if not isinstance(identifier, str) or not identifier or len(identifier) > 256:
            raise ProjectionValidationError("effect_id is invalid")
        now = spec.requested_at
        conn.execute(
            "INSERT INTO truth_hindsight_projection_outbox "
            "(effect_id, claim_id, claim_generation, policy_id, desired_state, "
            " reason_code, eligibility_sha256, authorization_ref, "
            " purge_projection_source, request_sha256, state, attempt_count, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)",
            (
                identifier,
                spec.claim_id,
                spec.claim_generation,
                spec.policy_id,
                spec.desired_state.value,
                spec.reason_code,
                spec.eligibility_sha256,
                spec.authorization_ref,
                int(spec.purge_projection_source),
                spec.request_sha256,
                now,
                now,
            ),
        )
        # Work that has not crossed a delivery boundary is safely superseded.
        # Delivering/reconciling work may still complete, but the head fence
        # ensures the newer intent is subsequently applied.
        conn.execute(
            "UPDATE truth_hindsight_projection_outbox "
            "SET state = 'superseded', updated_at = ? "
            "WHERE claim_id = ? AND policy_id = ? AND effect_id <> ? "
            "AND state IN ('pending', 'failed_retryable')",
            (now, spec.claim_id, spec.policy_id, identifier),
        )
        conn.execute(
            "INSERT INTO truth_hindsight_projection_heads "
            "(claim_id, policy_id, claim_generation, desired_state, effect_id, "
            " request_sha256, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(claim_id, policy_id) DO UPDATE SET "
            "claim_generation = excluded.claim_generation, "
            "desired_state = excluded.desired_state, effect_id = excluded.effect_id, "
            "request_sha256 = excluded.request_sha256, updated_at = excluded.updated_at",
            (
                spec.claim_id,
                spec.policy_id,
                spec.claim_generation,
                spec.desired_state.value,
                identifier,
                spec.request_sha256,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM truth_hindsight_projection_outbox WHERE effect_id = ?",
            (identifier,),
        ).fetchone()
        assert row is not None
        return _effect_from_row(row)

    def enqueue(self, spec: ProjectionIntentSpec) -> ProjectionEffect:
        """Convenience for reconciliation; lifecycle writes use the static seam."""

        with self.write_transaction() as conn:
            return self.enqueue_in_transaction(conn, spec)

    def get_effect(self, effect_id: str) -> ProjectionEffect:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM truth_hindsight_projection_outbox WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                raise ProjectionNotFound()
            return _effect_from_row(row)
        finally:
            conn.close()

    def current_effect(self, claim_id: str, policy_id: str) -> ProjectionEffect | None:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT o.* FROM truth_hindsight_projection_heads h "
                "JOIN truth_hindsight_projection_outbox o ON o.effect_id = h.effect_id "
                "WHERE h.claim_id = ? AND h.policy_id = ?",
                (claim_id, policy_id),
            ).fetchone()
            return _effect_from_row(row) if row is not None else None
        finally:
            conn.close()

    def requeue_delivered(
        self,
        effect_id: str,
        *,
        error_code: str,
        at: str | None = None,
    ) -> bool:
        """Requeue one current delivered intent after destination drift."""

        now = at or utc_now()
        with self.write_transaction() as conn:
            updated = conn.execute(
                "UPDATE truth_hindsight_projection_outbox "
                "SET state = 'failed_retryable', next_attempt_at = NULL, "
                "last_error_code = ?, updated_at = ? "
                "WHERE effect_id = ? AND state = 'delivered' "
                "AND EXISTS (SELECT 1 FROM truth_hindsight_projection_heads h "
                "WHERE h.effect_id = truth_hindsight_projection_outbox.effect_id)",
                (error_code, now, effect_id),
            )
            return updated.rowcount == 1

    def acquire_next(
        self,
        *,
        worker_id: str,
        at: str | None = None,
        lease_seconds: int = 60,
    ) -> ProjectionLease | None:
        now = at or utc_now()
        _parse_time(now)
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 256:
            raise ProjectionValidationError("worker_id is invalid")
        expires = _plus_seconds(now, lease_seconds)
        with self.write_transaction() as conn:
            # An expired delivery lease may have crossed the irreversible
            # boundary, so it becomes reconciliation work rather than replay.
            conn.execute(
                "UPDATE truth_hindsight_projection_outbox "
                "SET state = 'reconciling', lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ?, "
                "last_error_code = COALESCE(last_error_code, 'worker_lease_expired') "
                "WHERE state = 'delivering' AND lease_expires_at <= ?",
                (now, now),
            )

            row = conn.execute(
                "SELECT * FROM truth_hindsight_projection_outbox "
                "WHERE state = 'reconciling' "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                "ORDER BY CASE desired_state WHEN 'remove' THEN 0 ELSE 1 END, "
                "created_at, effect_id LIMIT 1",
                (now,),
            ).fetchone()
            if row is not None:
                effect = _effect_from_row(row)
                if effect.attempt_count <= 0:
                    raise ProjectionValidationError(
                        "reconciling effect has no prior delivery attempt"
                    )
                conn.execute(
                    "UPDATE truth_hindsight_projection_outbox "
                    "SET state = 'delivering', lease_owner = ?, lease_expires_at = ?, "
                    "updated_at = ? WHERE effect_id = ? AND state = 'reconciling'",
                    (worker_id, expires, now, effect.effect_id),
                )
                refreshed = conn.execute(
                    "SELECT * FROM truth_hindsight_projection_outbox WHERE effect_id = ?",
                    (effect.effect_id,),
                ).fetchone()
                assert refreshed is not None
                return ProjectionLease(
                    effect=_effect_from_row(refreshed),
                    attempt_no=effect.attempt_count,
                    worker_id=worker_id,
                    reconcile_existing=True,
                )

            row = conn.execute(
                "SELECT * FROM truth_hindsight_projection_outbox "
                "WHERE state IN ('pending', 'failed_retryable') "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                "ORDER BY CASE desired_state WHEN 'remove' THEN 0 ELSE 1 END, "
                "created_at, effect_id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            effect = _effect_from_row(row)
            attempt_no = effect.attempt_count + 1
            updated = conn.execute(
                "UPDATE truth_hindsight_projection_outbox "
                "SET state = 'delivering', attempt_count = ?, lease_owner = ?, "
                "lease_expires_at = ?, next_attempt_at = NULL, last_error_code = NULL, "
                "updated_at = ? WHERE effect_id = ? "
                "AND state IN ('pending', 'failed_retryable')",
                (
                    attempt_no,
                    worker_id,
                    expires,
                    now,
                    effect.effect_id,
                ),
            )
            if updated.rowcount != 1:
                raise ProjectionLeaseConflict()
            conn.execute(
                "INSERT INTO truth_hindsight_projection_attempts "
                "(effect_id, attempt_no, worker_id, state, started_at) "
                "VALUES (?, ?, ?, 'started', ?)",
                (effect.effect_id, attempt_no, worker_id, now),
            )
            refreshed = conn.execute(
                "SELECT * FROM truth_hindsight_projection_outbox WHERE effect_id = ?",
                (effect.effect_id,),
            ).fetchone()
            assert refreshed is not None
            return ProjectionLease(
                effect=_effect_from_row(refreshed),
                attempt_no=attempt_no,
                worker_id=worker_id,
            )

    @staticmethod
    def _assert_lease(
        conn: sqlite3.Connection,
        lease: ProjectionLease,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM truth_hindsight_projection_outbox WHERE effect_id = ?",
            (lease.effect.effect_id,),
        ).fetchone()
        if (
            row is None
            or row["state"] != "delivering"
            or row["lease_owner"] != lease.worker_id
            or int(row["attempt_count"]) != lease.attempt_no
        ):
            raise ProjectionLeaseConflict()
        return row

    def record_dependencies(
        self,
        lease: ProjectionLease,
        usages: Sequence[DependencyUsage],
    ) -> None:
        encoded = _usages_json(tuple(usages))
        with self.write_transaction() as conn:
            self._assert_lease(conn, lease)
            conn.execute(
                "UPDATE truth_hindsight_projection_attempts "
                "SET dependency_usages_json = ?, state = 'prepared' "
                "WHERE effect_id = ? AND attempt_no = ?",
                (encoded, lease.effect.effect_id, lease.attempt_no),
            )
            for usage in usages:
                conn.execute(
                    "INSERT INTO truth_hindsight_projection_dependencies "
                    "(claim_id, policy_id, claim_generation, usage_id, source_ref, "
                    " representation_id, redaction_epoch, active, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?) "
                    "ON CONFLICT(claim_id, policy_id, claim_generation, usage_id) "
                    "DO UPDATE SET active = 1, released_at = NULL",
                    (
                        lease.effect.spec.claim_id,
                        lease.effect.spec.policy_id,
                        lease.effect.spec.claim_generation,
                        usage.usage_id,
                        usage.source_ref,
                        usage.representation_id,
                        usage.redaction_epoch,
                        utc_now(),
                    ),
                )

    def attempt_usages(self, lease: ProjectionLease) -> tuple[DependencyUsage, ...]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT dependency_usages_json FROM truth_hindsight_projection_attempts "
                "WHERE effect_id = ? AND attempt_no = ?",
                (lease.effect.effect_id, lease.attempt_no),
            ).fetchone()
            if row is None:
                raise ProjectionNotFound()
            return _parse_usages(str(row["dependency_usages_json"]))
        finally:
            conn.close()

    def mark_reconciling(
        self,
        lease: ProjectionLease,
        *,
        error_code: str,
        retry_at: str | None = None,
    ) -> None:
        now = utc_now()
        with self.write_transaction() as conn:
            self._assert_lease(conn, lease)
            conn.execute(
                "UPDATE truth_hindsight_projection_outbox "
                "SET state = 'reconciling', lease_owner = NULL, lease_expires_at = NULL, "
                "next_attempt_at = ?, last_error_code = ?, updated_at = ? "
                "WHERE effect_id = ?",
                (retry_at, error_code, now, lease.effect.effect_id),
            )
            conn.execute(
                "UPDATE truth_hindsight_projection_attempts "
                "SET state = 'ambiguous', error_code = ? "
                "WHERE effect_id = ? AND attempt_no = ?",
                (error_code, lease.effect.effect_id, lease.attempt_no),
            )

    def mark_retryable(
        self,
        lease: ProjectionLease,
        *,
        error_code: str,
        retry_at: str | None = None,
    ) -> None:
        now = utc_now()
        with self.write_transaction() as conn:
            self._assert_lease(conn, lease)
            conn.execute(
                "UPDATE truth_hindsight_projection_outbox "
                "SET state = 'failed_retryable', lease_owner = NULL, "
                "lease_expires_at = NULL, next_attempt_at = ?, last_error_code = ?, "
                "updated_at = ? WHERE effect_id = ?",
                (retry_at, error_code, now, lease.effect.effect_id),
            )
            conn.execute(
                "UPDATE truth_hindsight_projection_attempts "
                "SET state = 'not_sent', error_code = ?, completed_at = ? "
                "WHERE effect_id = ? AND attempt_no = ?",
                (error_code, now, lease.effect.effect_id, lease.attempt_no),
            )

    def mark_terminal(self, lease: ProjectionLease, *, error_code: str) -> None:
        now = utc_now()
        with self.write_transaction() as conn:
            self._assert_lease(conn, lease)
            conn.execute(
                "UPDATE truth_hindsight_projection_outbox "
                "SET state = 'failed_terminal', lease_owner = NULL, "
                "lease_expires_at = NULL, last_error_code = ?, updated_at = ? "
                "WHERE effect_id = ?",
                (error_code, now, lease.effect.effect_id),
            )
            conn.execute(
                "UPDATE truth_hindsight_projection_attempts "
                "SET state = 'failed_terminal', error_code = ?, completed_at = ? "
                "WHERE effect_id = ? AND attempt_no = ?",
                (error_code, now, lease.effect.effect_id, lease.attempt_no),
            )

    def mark_superseded(self, lease: ProjectionLease) -> None:
        now = utc_now()
        with self.write_transaction() as conn:
            self._assert_lease(conn, lease)
            conn.execute(
                "UPDATE truth_hindsight_projection_outbox "
                "SET state = 'superseded', lease_owner = NULL, lease_expires_at = NULL, "
                "updated_at = ? WHERE effect_id = ?",
                (now, lease.effect.effect_id),
            )
            conn.execute(
                "UPDATE truth_hindsight_projection_attempts "
                "SET state = 'superseded', completed_at = ? "
                "WHERE effect_id = ? AND attempt_no = ?",
                (now, lease.effect.effect_id, lease.attempt_no),
            )

    def complete_upsert(
        self,
        lease: ProjectionLease,
        *,
        snapshot,
        delivery: DisclosureDeliveryReceipt,
        dependency_usages: Sequence[DependencyUsage],
    ) -> ProjectionReceipt | None:
        """Record an acknowledged derivative and return the prior live receipt."""

        now = delivery.destination.acknowledged_at
        scope_json = canonical_json(dict(snapshot.applicability_scope))
        usages = tuple(dependency_usages)
        usages_json = _usages_json(usages)
        with self.write_transaction() as conn:
            self._assert_lease(conn, lease)
            prior_row = conn.execute(
                "SELECT * FROM truth_hindsight_projection_receipts "
                "WHERE claim_id = ? AND policy_id = ?",
                (snapshot.claim_id, lease.effect.spec.policy_id),
            ).fetchone()
            prior = _receipt_from_row(prior_row) if prior_row is not None else None
            conn.execute(
                "UPDATE truth_hindsight_projection_dependencies SET active = 0, "
                "released_at = NULL "
                "WHERE claim_id = ? AND policy_id = ? AND active = 1",
                (snapshot.claim_id, lease.effect.spec.policy_id),
            )
            for usage in usages:
                conn.execute(
                    "INSERT INTO truth_hindsight_projection_dependencies "
                    "(claim_id, policy_id, claim_generation, usage_id, source_ref, "
                    " representation_id, redaction_epoch, active, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?) "
                    "ON CONFLICT(claim_id, policy_id, claim_generation, usage_id) "
                    "DO UPDATE SET active = 1, released_at = NULL",
                    (
                        snapshot.claim_id,
                        lease.effect.spec.policy_id,
                        snapshot.claim_generation,
                        usage.usage_id,
                        usage.source_ref,
                        usage.representation_id,
                        usage.redaction_epoch,
                        now,
                    ),
                )
            conn.execute(
                "INSERT INTO truth_hindsight_projection_receipts "
                "(claim_id, policy_id, claim_generation, receipt_state, "
                " destination_document_id, projection_method, lifecycle_status, "
                " applicability_scope_json, valid_from, valid_to, "
                " captured_source_ref, captured_representation_id, content_sha256, "
                " disclosure_run_id, disclosure_entry_id, "
                " disclosure_manifest_sha256, dependency_usages_json, "
                " last_effect_id, observed_at) "
                "VALUES (?, ?, ?, 'present', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(claim_id, policy_id) DO UPDATE SET "
                "claim_generation = excluded.claim_generation, "
                "receipt_state = excluded.receipt_state, "
                "destination_document_id = excluded.destination_document_id, "
                "projection_method = excluded.projection_method, "
                "lifecycle_status = excluded.lifecycle_status, "
                "applicability_scope_json = excluded.applicability_scope_json, "
                "valid_from = excluded.valid_from, valid_to = excluded.valid_to, "
                "captured_source_ref = excluded.captured_source_ref, "
                "captured_representation_id = excluded.captured_representation_id, "
                "content_sha256 = excluded.content_sha256, "
                "disclosure_run_id = excluded.disclosure_run_id, "
                "disclosure_entry_id = excluded.disclosure_entry_id, "
                "disclosure_manifest_sha256 = excluded.disclosure_manifest_sha256, "
                "dependency_usages_json = excluded.dependency_usages_json, "
                "last_effect_id = excluded.last_effect_id, observed_at = excluded.observed_at",
                (
                    snapshot.claim_id,
                    lease.effect.spec.policy_id,
                    snapshot.claim_generation,
                    delivery.destination.document_id,
                    snapshot.projection_method,
                    snapshot.lifecycle_status,
                    scope_json,
                    snapshot.valid_from,
                    snapshot.valid_to,
                    delivery.captured_source_ref,
                    delivery.captured_representation_id,
                    delivery.content_sha256,
                    delivery.disclosure_run_id,
                    delivery.disclosure_entry_id,
                    delivery.disclosure_manifest_sha256,
                    usages_json,
                    lease.effect.effect_id,
                    now,
                ),
            )
            self._complete_attempt_locked(
                conn,
                lease,
                state="applied",
                destination_document_id=delivery.destination.document_id,
                delivery=delivery,
                at=now,
            )
            conn.execute(
                "UPDATE truth_hindsight_projection_outbox "
                "SET state = 'delivered', lease_owner = NULL, lease_expires_at = NULL, "
                "last_error_code = NULL, updated_at = ? WHERE effect_id = ?",
                (now, lease.effect.effect_id),
            )
            return prior

    def complete_remove(
        self,
        lease: ProjectionLease,
        *,
        document_id: str,
        observed_at: str,
    ) -> ProjectionReceipt | None:
        with self.write_transaction() as conn:
            self._assert_lease(conn, lease)
            prior_row = conn.execute(
                "SELECT * FROM truth_hindsight_projection_receipts "
                "WHERE claim_id = ? AND policy_id = ?",
                (lease.effect.spec.claim_id, lease.effect.spec.policy_id),
            ).fetchone()
            prior = _receipt_from_row(prior_row) if prior_row is not None else None
            conn.execute(
                "UPDATE truth_hindsight_projection_dependencies SET active = 0, "
                "released_at = NULL "
                "WHERE claim_id = ? AND policy_id = ? AND active = 1",
                (
                    lease.effect.spec.claim_id,
                    lease.effect.spec.policy_id,
                ),
            )
            prior_method = prior.projection_method if prior else "hindsight_llm_retain_v1"
            prior_scope = canonical_json(dict(prior.applicability_scope)) if prior else "{}"
            if (
                lease.effect.spec.purge_projection_source
                and prior is not None
                and prior.captured_source_ref is not None
            ):
                conn.execute(
                    "INSERT OR IGNORE INTO truth_hindsight_projection_source_cleanup "
                    "(cleanup_id, effect_id, source_ref, authorization_ref, "
                    " reason_code, state, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (
                        uuid.uuid4().hex,
                        lease.effect.effect_id,
                        prior.captured_source_ref,
                        lease.effect.spec.authorization_ref,
                        "truth_projection_source_redacted",
                        observed_at,
                    ),
                )
            conn.execute(
                "INSERT INTO truth_hindsight_projection_receipts "
                "(claim_id, policy_id, claim_generation, receipt_state, "
                " destination_document_id, projection_method, lifecycle_status, "
                " applicability_scope_json, valid_from, valid_to, "
                " dependency_usages_json, last_effect_id, observed_at) "
                "VALUES (?, ?, ?, 'absent', ?, ?, ?, ?, NULL, NULL, '[]', ?, ?) "
                "ON CONFLICT(claim_id, policy_id) DO UPDATE SET "
                "claim_generation = excluded.claim_generation, "
                "receipt_state = 'absent', "
                "destination_document_id = excluded.destination_document_id, "
                "lifecycle_status = excluded.lifecycle_status, "
                "applicability_scope_json = excluded.applicability_scope_json, "
                "valid_from = NULL, valid_to = NULL, captured_source_ref = NULL, "
                "captured_representation_id = NULL, content_sha256 = NULL, "
                "disclosure_run_id = NULL, disclosure_entry_id = NULL, "
                "disclosure_manifest_sha256 = NULL, dependency_usages_json = '[]', "
                "last_effect_id = excluded.last_effect_id, observed_at = excluded.observed_at",
                (
                    lease.effect.spec.claim_id,
                    lease.effect.spec.policy_id,
                    lease.effect.spec.claim_generation,
                    document_id,
                    prior_method,
                    lease.effect.spec.reason_code,
                    prior_scope,
                    lease.effect.effect_id,
                    observed_at,
                ),
            )
            self._complete_attempt_locked(
                conn,
                lease,
                state="removed",
                destination_document_id=document_id,
                delivery=None,
                at=observed_at,
            )
            conn.execute(
                "UPDATE truth_hindsight_projection_outbox "
                "SET state = 'delivered', lease_owner = NULL, lease_expires_at = NULL, "
                "last_error_code = NULL, updated_at = ? WHERE effect_id = ?",
                (observed_at, lease.effect.effect_id),
            )
            return prior

    @staticmethod
    def _complete_attempt_locked(
        conn: sqlite3.Connection,
        lease: ProjectionLease,
        *,
        state: str,
        destination_document_id: str,
        delivery: DisclosureDeliveryReceipt | None,
        at: str,
    ) -> None:
        conn.execute(
            "UPDATE truth_hindsight_projection_attempts SET state = ?, "
            "destination_document_id = ?, captured_source_ref = ?, "
            "captured_representation_id = ?, content_sha256 = ?, "
            "disclosure_run_id = ?, disclosure_entry_id = ?, "
            "disclosure_manifest_sha256 = ?, error_code = NULL, completed_at = ? "
            "WHERE effect_id = ? AND attempt_no = ?",
            (
                state,
                destination_document_id,
                delivery.captured_source_ref if delivery else None,
                delivery.captured_representation_id if delivery else None,
                delivery.content_sha256 if delivery else None,
                delivery.disclosure_run_id if delivery else None,
                delivery.disclosure_entry_id if delivery else None,
                delivery.disclosure_manifest_sha256 if delivery else None,
                at,
                lease.effect.effect_id,
                lease.attempt_no,
            ),
        )

    def receipt(self, claim_id: str, policy_id: str) -> ProjectionReceipt | None:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM truth_hindsight_projection_receipts "
                "WHERE claim_id = ? AND policy_id = ?",
                (claim_id, policy_id),
            ).fetchone()
            return _receipt_from_row(row) if row is not None else None
        finally:
            conn.close()

    def list_receipts(self) -> tuple[ProjectionReceipt, ...]:
        conn = self.connect()
        try:
            return tuple(
                _receipt_from_row(row)
                for row in conn.execute(
                    "SELECT * FROM truth_hindsight_projection_receipts "
                    "ORDER BY claim_id, policy_id"
                )
            )
        finally:
            conn.close()

    def claims_for_usage(self, usage_id: str) -> tuple[tuple[str, str], ...]:
        conn = self.connect()
        try:
            return tuple(
                (str(row["claim_id"]), str(row["policy_id"]))
                for row in conn.execute(
                    "SELECT DISTINCT claim_id, policy_id "
                    "FROM truth_hindsight_projection_dependencies "
                    "WHERE usage_id = ? AND active = 1 ORDER BY claim_id, policy_id",
                    (usage_id,),
                )
            )
        finally:
            conn.close()

    def dependency_targets_for_usage(
        self,
        usage_id: str,
    ) -> tuple[tuple[str, str, str, str, bool], ...]:
        """Return claim/policy/source/representation bindings for one usage."""

        conn = self.connect()
        try:
            return tuple(
                (
                    str(row["claim_id"]),
                    str(row["policy_id"]),
                    str(row["source_ref"]),
                    str(row["representation_id"]),
                    bool(row["active"]),
                )
                for row in conn.execute(
                    "SELECT DISTINCT claim_id, policy_id, source_ref, "
                    "representation_id, active "
                    "FROM truth_hindsight_projection_dependencies "
                    "WHERE usage_id = ? ORDER BY claim_id, policy_id",
                    (usage_id,),
                )
            )
        finally:
            conn.close()

    def source_redaction_settled(
        self,
        *,
        claim_id: str,
        policy_id: str,
        usage_id: str,
    ) -> bool:
        """Prove removal, dependency release, and derived-source cleanup."""

        conn = self.connect()
        try:
            dependency = conn.execute(
                "SELECT active, released_at "
                "FROM truth_hindsight_projection_dependencies "
                "WHERE claim_id = ? AND policy_id = ? AND usage_id = ?",
                (claim_id, policy_id, usage_id),
            ).fetchone()
            receipt = conn.execute(
                "SELECT receipt_state FROM truth_hindsight_projection_receipts "
                "WHERE claim_id = ? AND policy_id = ?",
                (claim_id, policy_id),
            ).fetchone()
            cleanup = conn.execute(
                "SELECT 1 FROM truth_hindsight_projection_source_cleanup AS c "
                "JOIN truth_hindsight_projection_outbox AS o "
                "ON o.effect_id = c.effect_id "
                "WHERE o.claim_id = ? AND o.policy_id = ? AND c.state != 'completed' "
                "LIMIT 1",
                (claim_id, policy_id),
            ).fetchone()
            return bool(
                dependency is not None
                and not bool(dependency["active"])
                and dependency["released_at"] is not None
                and receipt is not None
                and receipt["receipt_state"] == "absent"
                and cleanup is None
            )
        finally:
            conn.close()

    def mark_usages_acknowledged(
        self, usages: Sequence[DependencyUsage], *, at: str | None = None
    ) -> None:
        if not usages:
            return
        now = at or utc_now()
        with self.write_transaction() as conn:
            for usage in usages:
                conn.execute(
                    "UPDATE truth_hindsight_projection_dependencies "
                    "SET acknowledged_at = COALESCE(acknowledged_at, ?) "
                    "WHERE usage_id = ?",
                    (now, usage.usage_id),
                )

    def mark_usages_released(
        self, usages: Sequence[DependencyUsage], *, at: str | None = None
    ) -> None:
        if not usages:
            return
        now = at or utc_now()
        with self.write_transaction() as conn:
            for usage in usages:
                conn.execute(
                    "UPDATE truth_hindsight_projection_dependencies "
                    "SET active = 0, released_at = COALESCE(released_at, ?) "
                    "WHERE usage_id = ?",
                    (now, usage.usage_id),
                )

    def pending_dependency_accounting(
        self,
    ) -> tuple[tuple[DependencyUsage, bool], ...]:
        """Return Sources usage acknowledgements/releases that need repair."""

        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT usage_id, source_ref, representation_id, redaction_epoch, "
                "active, acknowledged_at, released_at "
                "FROM truth_hindsight_projection_dependencies "
                "WHERE (active = 1 AND acknowledged_at IS NULL) "
                "OR (active = 0 AND released_at IS NULL) "
                "ORDER BY created_at, usage_id"
            ).fetchall()
            return tuple(
                (
                    DependencyUsage(
                        usage_id=str(row["usage_id"]),
                        source_ref=str(row["source_ref"]),
                        representation_id=str(row["representation_id"]),
                        redaction_epoch=int(row["redaction_epoch"]),
                    ),
                    bool(row["active"]),
                )
                for row in rows
            )
        finally:
            conn.close()

    def pending_source_cleanup(
        self,
    ) -> tuple[tuple[str, str, str, str], ...]:
        """Return ``(cleanup_id, source_ref, authorization_ref, reason_code)``."""

        conn = self.connect()
        try:
            return tuple(
                (
                    str(row["cleanup_id"]),
                    str(row["source_ref"]),
                    str(row["authorization_ref"]),
                    str(row["reason_code"]),
                )
                for row in conn.execute(
                    "SELECT cleanup_id, source_ref, authorization_ref, reason_code "
                    "FROM truth_hindsight_projection_source_cleanup "
                    "WHERE state = 'pending' ORDER BY created_at, cleanup_id"
                )
            )
        finally:
            conn.close()

    def complete_source_cleanup(
        self, cleanup_id: str, *, at: str | None = None
    ) -> None:
        now = at or utc_now()
        with self.write_transaction() as conn:
            conn.execute(
                "UPDATE truth_hindsight_projection_source_cleanup "
                "SET state = 'completed', completed_at = COALESCE(completed_at, ?) "
                "WHERE cleanup_id = ?",
                (now, cleanup_id),
            )


def _spec_from_row(row: sqlite3.Row) -> ProjectionIntentSpec:
    return ProjectionIntentSpec(
        claim_id=str(row["claim_id"]),
        claim_generation=str(row["claim_generation"]),
        policy_id=str(row["policy_id"]),
        desired_state=DesiredProjectionState(str(row["desired_state"])),
        reason_code=str(row["reason_code"]),
        eligibility_sha256=str(row["eligibility_sha256"]),
        authorization_ref=str(row["authorization_ref"]),
        purge_projection_source=bool(row["purge_projection_source"]),
        requested_at=str(row["created_at"]),
    )


def _effect_from_row(row: sqlite3.Row) -> ProjectionEffect:
    return ProjectionEffect(
        effect_id=str(row["effect_id"]),
        spec=_spec_from_row(row),
        state=OutboxState(str(row["state"])),
        attempt_count=int(row["attempt_count"]),
        lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
        lease_expires_at=(
            str(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None
        ),
        last_error_code=(
            str(row["last_error_code"]) if row["last_error_code"] is not None else None
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _receipt_from_row(row: sqlite3.Row) -> ProjectionReceipt:
    try:
        scope = json.loads(str(row["applicability_scope_json"]))
    except json.JSONDecodeError as exc:
        raise ProjectionValidationError("stored applicability scope is invalid") from exc
    if not isinstance(scope, dict):
        raise ProjectionValidationError("stored applicability scope is invalid")
    return ProjectionReceipt(
        claim_id=str(row["claim_id"]),
        policy_id=str(row["policy_id"]),
        claim_generation=str(row["claim_generation"]),
        state=ReceiptState(str(row["receipt_state"])),
        document_id=str(row["destination_document_id"]),
        projection_method=str(row["projection_method"]),
        lifecycle_status=str(row["lifecycle_status"]),
        applicability_scope=scope,
        valid_from=str(row["valid_from"]) if row["valid_from"] is not None else None,
        valid_to=str(row["valid_to"]) if row["valid_to"] is not None else None,
        captured_source_ref=(
            str(row["captured_source_ref"])
            if row["captured_source_ref"] is not None
            else None
        ),
        captured_representation_id=(
            str(row["captured_representation_id"])
            if row["captured_representation_id"] is not None
            else None
        ),
        content_sha256=(
            str(row["content_sha256"]) if row["content_sha256"] is not None else None
        ),
        disclosure_run_id=(
            str(row["disclosure_run_id"])
            if row["disclosure_run_id"] is not None
            else None
        ),
        disclosure_entry_id=(
            str(row["disclosure_entry_id"])
            if row["disclosure_entry_id"] is not None
            else None
        ),
        disclosure_manifest_sha256=(
            str(row["disclosure_manifest_sha256"])
            if row["disclosure_manifest_sha256"] is not None
            else None
        ),
        dependency_usages=_parse_usages(str(row["dependency_usages_json"])),
        last_effect_id=str(row["last_effect_id"]),
        observed_at=str(row["observed_at"]),
    )

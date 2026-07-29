"""Durable operational state for job-scoped Co-work Verify workers.

Portable evaluation meaning lives in the project-local Truth ledger.  This
module stores only the host-level dispatch state needed to reconnect a detached
account-backed worker to that ledger after a dashboard or gateway restart.
Document content is never copied into this database; job context is rebuilt
from the immutable action snapshot when the constrained worker asks for it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from work_buddy.cowork.execution_identity import CoworkVerifyRole
from work_buddy.paths import data_dir
from work_buddy.truth.identity import canonical_json, sha256_text, utc_now


_DB_PATH = data_dir("agents") / "cowork_verify_jobs.db"
_SCHEMA_LOCK = threading.RLock()
_VALID_STATUSES = frozenset(
    {
        "prepared",
        "launching",
        "running",
        "submitted",
        "completed",
        "unavailable",
        "failed",
    }
)
_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "prepared": frozenset({"prepared", "launching", "submitted", "unavailable", "failed"}),
    "launching": frozenset(
        {"launching", "running", "submitted", "completed", "unavailable", "failed"}
    ),
    "running": frozenset(
        {"running", "submitted", "completed", "unavailable", "failed"}
    ),
    "submitted": frozenset({"submitted", "completed", "failed"}),
    "completed": frozenset({"completed"}),
    "unavailable": frozenset({"unavailable"}),
    "failed": frozenset({"failed"}),
}


@dataclass(frozen=True, slots=True)
class VerifyRuntimeJob:
    job_id: str
    store_id: str
    document_id: str
    evaluation_run_id: str
    action_snapshot_id: str
    plan_snapshot_id: str | None
    role: CoworkVerifyRole
    status: str
    selection: Mapping[str, str]
    authorization_receipt_id: str
    context_sha256: str
    request: Mapping[str, Any]
    parent_job_id: str | None
    session_id: str
    pid: int | None
    output_sha256: str | None
    output: Mapping[str, Any] | None
    error_code: str
    error: str
    created_at: str
    updated_at: str
    launch_owner: str = ""
    launch_lease_expires_at: str | None = None
    projection_owner: str = ""
    projection_lease_expires_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "VerifyRuntimeJob":
        columns = frozenset(row.keys())
        output = (
            None
            if row["output_json"] is None
            else json.loads(str(row["output_json"]))
        )
        return cls(
            job_id=str(row["job_id"]),
            store_id=str(row["store_id"]),
            document_id=str(row["document_id"]),
            evaluation_run_id=str(row["evaluation_run_id"]),
            action_snapshot_id=str(row["action_snapshot_id"]),
            plan_snapshot_id=(
                None
                if row["plan_snapshot_id"] is None
                else str(row["plan_snapshot_id"])
            ),
            role=CoworkVerifyRole(str(row["role"])),
            status=str(row["status"]),
            selection=json.loads(str(row["selection_json"])),
            authorization_receipt_id=str(row["authorization_receipt_id"]),
            context_sha256=str(row["context_sha256"]),
            request=json.loads(str(row["request_json"])),
            parent_job_id=(
                None
                if row["parent_job_id"] is None
                else str(row["parent_job_id"])
            ),
            session_id=str(row["session_id"]),
            pid=None if row["pid"] is None else int(row["pid"]),
            output_sha256=(
                None if row["output_sha256"] is None else str(row["output_sha256"])
            ),
            output=output,
            error_code=str(row["error_code"] or ""),
            error=str(row["error"] or ""),
            launch_owner=(
                ""
                if "launch_owner" not in columns
                else str(row["launch_owner"] or "")
            ),
            launch_lease_expires_at=(
                None
                if (
                    "launch_lease_expires_at" not in columns
                    or row["launch_lease_expires_at"] is None
                )
                else str(row["launch_lease_expires_at"])
            ),
            projection_owner=(
                ""
                if "projection_owner" not in columns
                else str(row["projection_owner"] or "")
            ),
            projection_lease_expires_at=(
                None
                if (
                    "projection_lease_expires_at" not in columns
                    or row["projection_lease_expires_at"] is None
                )
                else str(row["projection_lease_expires_at"])
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


def _connect(path: Path | None = None) -> sqlite3.Connection:
    target = (_DB_PATH if path is None else path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _connect_read_only(path: Path | None = None) -> sqlite3.Connection | None:
    target = (_DB_PATH if path is None else path).expanduser().resolve()
    if not target.is_file():
        return None
    conn = sqlite3.connect(
        f"{target.as_uri()}?mode=ro",
        timeout=10,
        uri=True,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _has_schema(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'cowork_verify_jobs'"
        ).fetchone()
        is not None
    )


def _ensure_schema(conn: sqlite3.Connection) -> None:
    with _SCHEMA_LOCK:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cowork_verify_jobs (
                job_id                    TEXT PRIMARY KEY,
                store_id                  TEXT NOT NULL,
                document_id               TEXT NOT NULL,
                evaluation_run_id         TEXT NOT NULL,
                action_snapshot_id        TEXT NOT NULL,
                plan_snapshot_id          TEXT,
                role                      TEXT NOT NULL
                    CHECK (role IN ('specialist', 'reviser', 'coordinator', 'cothink')),
                status                    TEXT NOT NULL
                    CHECK (status IN (
                        'prepared', 'launching', 'running', 'submitted',
                        'completed', 'unavailable', 'failed'
                    )),
                selection_json            TEXT NOT NULL,
                authorization_receipt_id  TEXT NOT NULL,
                context_sha256            TEXT NOT NULL,
                request_json              TEXT NOT NULL,
                parent_job_id             TEXT REFERENCES cowork_verify_jobs(job_id),
                session_id                TEXT NOT NULL UNIQUE,
                pid                       INTEGER,
                output_sha256             TEXT,
                output_json               TEXT,
                error_code                TEXT NOT NULL DEFAULT '',
                error                     TEXT NOT NULL DEFAULT '',
                launch_owner              TEXT NOT NULL DEFAULT '',
                launch_lease_expires_at   TEXT,
                projection_owner          TEXT NOT NULL DEFAULT '',
                projection_lease_expires_at TEXT,
                created_at                TEXT NOT NULL,
                updated_at                TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_cowork_verify_jobs_run
            ON cowork_verify_jobs(store_id, evaluation_run_id, created_at, job_id);

            CREATE INDEX IF NOT EXISTS idx_cowork_verify_jobs_action
            ON cowork_verify_jobs(store_id, action_snapshot_id, created_at, job_id);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_cowork_verify_jobs_coordinator_parent
            ON cowork_verify_jobs(parent_job_id, role)
            WHERE parent_job_id IS NOT NULL AND role = 'coordinator';
            """
        )
        columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(cowork_verify_jobs)"
            ).fetchall()
        }
        if "launch_owner" not in columns:
            conn.execute(
                "ALTER TABLE cowork_verify_jobs "
                "ADD COLUMN launch_owner TEXT NOT NULL DEFAULT ''"
            )
        if "launch_lease_expires_at" not in columns:
            conn.execute(
                "ALTER TABLE cowork_verify_jobs "
                "ADD COLUMN launch_lease_expires_at TEXT"
            )
        if "projection_owner" not in columns:
            conn.execute(
                "ALTER TABLE cowork_verify_jobs "
                "ADD COLUMN projection_owner TEXT NOT NULL DEFAULT ''"
            )
        if "projection_lease_expires_at" not in columns:
            conn.execute(
                "ALTER TABLE cowork_verify_jobs "
                "ADD COLUMN projection_lease_expires_at TEXT"
            )
        conn.commit()


def create_job(
    *,
    job_id: str,
    store_id: str,
    document_id: str,
    evaluation_run_id: str,
    action_snapshot_id: str,
    plan_snapshot_id: str | None,
    role: CoworkVerifyRole,
    selection: Mapping[str, str],
    authorization_receipt_id: str,
    context_sha256: str,
    request: Mapping[str, Any],
    session_id: str,
    parent_job_id: str | None = None,
    at: str | None = None,
) -> VerifyRuntimeJob:
    """Insert one immutable binding in prepared state."""

    timestamp = at or utc_now()
    selection_json = canonical_json(dict(selection))
    request_json = canonical_json(dict(request))
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO cowork_verify_jobs (
                job_id, store_id, document_id, evaluation_run_id,
                action_snapshot_id, plan_snapshot_id, role, status,
                selection_json, authorization_receipt_id, context_sha256,
                request_json, parent_job_id, session_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                store_id,
                document_id,
                evaluation_run_id,
                action_snapshot_id,
                plan_snapshot_id,
                role.value,
                selection_json,
                authorization_receipt_id,
                context_sha256,
                request_json,
                parent_job_id,
                session_id,
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT * FROM cowork_verify_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    assert row is not None
    return VerifyRuntimeJob.from_row(row)


def get_job(job_id: str) -> VerifyRuntimeJob | None:
    conn = _connect_read_only()
    if conn is None:
        return None
    try:
        if not _has_schema(conn):
            return None
        row = conn.execute(
            "SELECT * FROM cowork_verify_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else VerifyRuntimeJob.from_row(row)


def jobs_for_run(
    store_id: str,
    evaluation_run_id: str,
) -> tuple[VerifyRuntimeJob, ...]:
    conn = _connect_read_only()
    if conn is None:
        return ()
    try:
        if not _has_schema(conn):
            return ()
        rows = conn.execute(
            """
            SELECT * FROM cowork_verify_jobs
            WHERE store_id = ? AND evaluation_run_id = ?
            ORDER BY created_at, job_id
            """,
            (store_id, evaluation_run_id),
        ).fetchall()
    finally:
        conn.close()
    return tuple(VerifyRuntimeJob.from_row(row) for row in rows)


def jobs_for_document(
    store_id: str,
    document_id: str,
) -> tuple[VerifyRuntimeJob, ...]:
    conn = _connect_read_only()
    if conn is None:
        return ()
    try:
        if not _has_schema(conn):
            return ()
        rows = conn.execute(
            """
            SELECT * FROM cowork_verify_jobs
            WHERE store_id = ? AND document_id = ?
            ORDER BY created_at, job_id
            """,
            (store_id, document_id),
        ).fetchall()
    finally:
        conn.close()
    return tuple(VerifyRuntimeJob.from_row(row) for row in rows)


def reconcilable_jobs() -> tuple[VerifyRuntimeJob, ...]:
    """Return nonterminal launch states for sidecar recovery.

    This path opens the writable database only to ensure an older operational
    database has the current columns before it is scanned. It never changes a
    job's state.
    """

    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT * FROM cowork_verify_jobs
            WHERE status IN ('prepared', 'launching', 'running', 'submitted')
            ORDER BY created_at, job_id
            """
        ).fetchall()
    return tuple(VerifyRuntimeJob.from_row(row) for row in rows)


def claim_job_projection(
    job_id: str,
    *,
    projection_owner: str,
    lease_expires_at: str | None = None,
    at: str | None = None,
) -> tuple[VerifyRuntimeJob, bool]:
    """Lease one submitted output for idempotent consequence projection."""

    owner = str(projection_owner).strip()
    if not owner:
        raise ValueError("Verify projection_owner must not be empty")
    timestamp = at or utc_now()
    if lease_expires_at is None:
        lease_expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=10)
        ).isoformat(timespec="milliseconds")
    try:
        deadline = datetime.fromisoformat(
            str(lease_expires_at).replace("Z", "+00:00")
        )
        claimed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "Verify projection lease must be an ISO timestamp"
        ) from exc
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    if deadline <= claimed_at:
        raise ValueError(
            "Verify projection lease must expire after it is claimed"
        )
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM cowork_verify_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if current is None:
            raise KeyError(f"unknown Co-work Verify job: {job_id}")
        current_status = str(current["status"])
        current_deadline = current["projection_lease_expires_at"]
        lease_available = not str(current["projection_owner"] or "")
        if current_deadline is not None:
            try:
                parsed = datetime.fromisoformat(
                    str(current_deadline).replace("Z", "+00:00")
                )
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                lease_available = parsed <= claimed_at
            except ValueError:
                lease_available = True
        claimed = current_status == "submitted" and lease_available
        if claimed:
            conn.execute(
                """
                UPDATE cowork_verify_jobs
                SET projection_owner = ?,
                    projection_lease_expires_at = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (owner, lease_expires_at, timestamp, job_id),
            )
        row = conn.execute(
            "SELECT * FROM cowork_verify_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    assert row is not None
    return VerifyRuntimeJob.from_row(row), claimed


def claim_job_launch(
    job_id: str,
    *,
    launch_owner: str | None = None,
    lease_expires_at: str | None = None,
    at: str | None = None,
) -> tuple[VerifyRuntimeJob, bool]:
    """Atomically claim one prepared job so it can be spawned only once."""

    timestamp = at or utc_now()
    owner = str(launch_owner or f"direct:{job_id}").strip()
    if not owner:
        raise ValueError("Verify launch_owner must not be empty")
    if lease_expires_at is None:
        lease_expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat(timespec="milliseconds")
    try:
        lease_deadline = datetime.fromisoformat(
            str(lease_expires_at).replace("Z", "+00:00")
        )
        claimed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Verify launch lease must be an ISO timestamp") from exc
    if lease_deadline.tzinfo is None:
        lease_deadline = lease_deadline.replace(tzinfo=timezone.utc)
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    if lease_deadline <= claimed_at:
        raise ValueError("Verify launch lease must expire after it is claimed")
    with _connect() as conn:
        _ensure_schema(conn)
        cursor = conn.execute(
            """
            UPDATE cowork_verify_jobs
            SET status = 'launching', launch_owner = ?,
                launch_lease_expires_at = ?, updated_at = ?
            WHERE job_id = ? AND status = 'prepared'
            """,
            (owner, lease_deadline.isoformat(), timestamp, job_id),
        )
        row = conn.execute(
            "SELECT * FROM cowork_verify_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        raise KeyError(f"unknown Co-work Verify job: {job_id}")
    return VerifyRuntimeJob.from_row(row), cursor.rowcount == 1


def update_job(
    job_id: str,
    *,
    status: str,
    pid: int | None = None,
    output_sha256: str | None = None,
    output: Mapping[str, Any] | None = None,
    error_code: str = "",
    error: str = "",
    expected_launch_owner: str | None = None,
    expected_projection_owner: str | None = None,
    at: str | None = None,
) -> VerifyRuntimeJob:
    """Advance operational state without changing the server-owned binding."""

    if status not in _VALID_STATUSES:
        raise ValueError(f"unsupported Verify job status: {status}")
    timestamp = at or utc_now()
    output_json = None if output is None else canonical_json(dict(output))
    if output_json is not None:
        computed_output_sha256 = sha256_text(output_json)
        if output_sha256 is None:
            output_sha256 = computed_output_sha256
        elif output_sha256 != computed_output_sha256:
            raise ValueError("Verify job output_sha256 does not match output")
    with _connect() as conn:
        _ensure_schema(conn)
        current = conn.execute(
            "SELECT * FROM cowork_verify_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if current is None:
            raise KeyError(f"unknown Co-work Verify job: {job_id}")
        if (
            expected_launch_owner is not None
            and str(current["launch_owner"] or "") != expected_launch_owner
        ):
            raise ValueError("Verify job launch owner no longer holds the fence")
        if (
            expected_projection_owner is not None
            and str(current["projection_owner"] or "")
            != expected_projection_owner
        ):
            raise ValueError(
                "Verify job projection owner no longer holds the fence"
            )
        current_status = str(current["status"])
        if status not in _STATUS_TRANSITIONS[current_status]:
            raise ValueError(
                "unsupported Verify job status transition: "
                f"{current_status} -> {status}"
            )
        current_output_sha256 = (
            None
            if current["output_sha256"] is None
            else str(current["output_sha256"])
        )
        current_output_json = (
            None if current["output_json"] is None else str(current["output_json"])
        )
        if output_sha256 is not None:
            if current_output_sha256 is None and output_json is None:
                raise ValueError(
                    "the first Verify job output digest requires readable output"
                )
            if (
                current_output_sha256 is not None
                and current_output_sha256 != output_sha256
            ):
                raise ValueError("Verify job output is immutable after submission")
        if (
            output_json is not None
            and current_output_json is not None
            and current_output_json != output_json
        ):
            raise ValueError("Verify job output is immutable after submission")
        conn.execute(
            """
            UPDATE cowork_verify_jobs
            SET status = ?, pid = COALESCE(?, pid),
                output_sha256 = COALESCE(?, output_sha256),
                output_json = CASE
                    WHEN ? IS NULL THEN output_json
                    ELSE ?
                END,
                error_code = ?, error = ?,
                launch_lease_expires_at = CASE
                    WHEN ? = 'launching' THEN launch_lease_expires_at
                    ELSE NULL
                END,
                projection_owner = CASE
                    WHEN ? = 'submitted' THEN projection_owner
                    ELSE ''
                END,
                projection_lease_expires_at = CASE
                    WHEN ? = 'submitted' THEN projection_lease_expires_at
                    ELSE NULL
                END,
                updated_at = ?
            WHERE job_id = ?
            """,
            (
                status,
                pid,
                output_sha256,
                output_json,
                output_json,
                error_code,
                error,
                status,
                status,
                status,
                timestamp,
                job_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM cowork_verify_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    assert row is not None
    return VerifyRuntimeJob.from_row(row)


def redact_job_output(job_id: str, *, at: str | None = None) -> VerifyRuntimeJob:
    """Drop readable private draft output after its durable consequences exist."""

    timestamp = at or utc_now()
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            UPDATE cowork_verify_jobs
            SET output_json = NULL, updated_at = ?
            WHERE job_id = ?
            """,
            (timestamp, job_id),
        )
        row = conn.execute(
            "SELECT * FROM cowork_verify_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        raise KeyError(f"unknown Co-work Verify job: {job_id}")
    return VerifyRuntimeJob.from_row(row)


__all__ = [
    "VerifyRuntimeJob",
    "claim_job_launch",
    "claim_job_projection",
    "create_job",
    "get_job",
    "jobs_for_document",
    "jobs_for_run",
    "reconcilable_jobs",
    "redact_job_output",
    "update_job",
]

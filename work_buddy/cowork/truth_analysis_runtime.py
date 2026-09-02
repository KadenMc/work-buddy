"""Durable private staging for AI-assisted Co-work Truth analysis.

This database is deliberately outside the project-local Truth ledger.  It owns
only operational execution state, typed candidate output, and the receipts for
later human staging decisions.  Creating or completing a run therefore cannot
create a claim, expression, evidence record, or claim status event.

The immutable ``action_snapshot_id`` points at the existing exact browser
capture in Truth.  Document bytes are not copied here; orchestration rebuilds
worker context from that snapshot and the immutable request envelope.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from work_buddy.paths import data_dir
from work_buddy.truth.identity import canonical_json, sha256_text, utc_now


_DB_PATH = data_dir("agents") / "cowork_truth_analysis.db"
_SCHEMA_LOCK = threading.RLock()
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 30 * 60

_VALID_STATUSES = frozenset(
    {"prepared", "launching", "running", "completed", "unavailable", "failed"}
)
_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "prepared": frozenset(
        {"prepared", "launching", "running", "completed", "unavailable", "failed"}
    ),
    "launching": frozenset(
        {"launching", "running", "completed", "unavailable", "failed"}
    ),
    "running": frozenset({"running", "completed", "unavailable", "failed"}),
    "completed": frozenset({"completed"}),
    "unavailable": frozenset({"unavailable"}),
    "failed": frozenset({"failed"}),
}
_DECISIONS = frozenset({"save_as_proposed", "connect_existing", "dismiss"})


class TruthAnalysisRunConflict(ValueError):
    """A document already owns an active run or unresolved candidate review."""

    def __init__(
        self,
        *,
        run_id: str,
        status: str,
        pending_candidates: int | None,
    ) -> None:
        super().__init__(
            "Finish reviewing the current Truth analysis before starting another passage."
        )
        self.run_id = run_id
        self.status = status
        self.pending_candidates = pending_candidates


def _output_json(value: Mapping[str, Any]) -> str:
    """Serialize worker output without normalizing evidence quote whitespace."""

    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _json_mapping(value: str, label: str) -> dict[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} is not a JSON object")
    return decoded


def _json_list(value: str, label: str) -> list[Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise ValueError(f"{label} is not a JSON array")
    return decoded


def _utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


@dataclass(frozen=True, slots=True)
class TruthAnalysisRuntimeRun:
    run_id: str
    store_id: str
    document_id: str
    action_snapshot_id: str
    status: str
    selection: Mapping[str, str]
    authorization_receipt_id: str
    context_sha256: str
    request: Mapping[str, Any]
    session_id: str
    pid: int | None
    output_sha256: str | None
    output: Mapping[str, Any] | None
    error_code: str
    error: str
    launch_owner: str
    launch_lease_expires_at: str | None
    execution_deadline_at: str
    created_at: str
    updated_at: str
    activation_revision: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TruthAnalysisRuntimeRun":
        return cls(
            run_id=str(row["run_id"]),
            store_id=str(row["store_id"]),
            document_id=str(row["document_id"]),
            action_snapshot_id=str(row["action_snapshot_id"]),
            status=str(row["status"]),
            selection=_json_mapping(str(row["selection_json"]), "selection"),
            authorization_receipt_id=str(row["authorization_receipt_id"]),
            context_sha256=str(row["context_sha256"]),
            request=_json_mapping(str(row["request_json"]), "request"),
            session_id=str(row["session_id"]),
            pid=None if row["pid"] is None else int(row["pid"]),
            output_sha256=(
                None if row["output_sha256"] is None else str(row["output_sha256"])
            ),
            output=(
                None
                if row["output_json"] is None
                else _json_mapping(str(row["output_json"]), "output")
            ),
            error_code=str(row["error_code"] or ""),
            error=str(row["error"] or ""),
            launch_owner=str(row["launch_owner"] or ""),
            launch_lease_expires_at=(
                None
                if row["launch_lease_expires_at"] is None
                else str(row["launch_lease_expires_at"])
            ),
            execution_deadline_at=(
                str(row["execution_deadline_at"])
                if (
                    "execution_deadline_at" in row.keys()
                    and row["execution_deadline_at"] is not None
                )
                else (
                    datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
                    + timedelta(seconds=DEFAULT_EXECUTION_TIMEOUT_SECONDS)
                ).isoformat(timespec="milliseconds")
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            activation_revision=(
                int(row["activation_revision"])
                if "activation_revision" in row.keys()
                else 0
            ),
        )


@dataclass(frozen=True, slots=True)
class TruthAnalysisCandidateDecision:
    decision_id: str
    run_id: str
    candidate_id: str
    candidate_canonical_sha256: str
    decision: str
    edits: Mapping[str, Any]
    result: Mapping[str, Any]
    decided_by_ref: str
    decided_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TruthAnalysisCandidateDecision":
        return cls(
            decision_id=str(row["decision_id"]),
            run_id=str(row["run_id"]),
            candidate_id=str(row["candidate_id"]),
            candidate_canonical_sha256=str(row["candidate_canonical_sha256"]),
            decision=str(row["decision"]),
            edits=_json_mapping(str(row["edits_json"]), "decision edits"),
            result=_json_mapping(str(row["result_json"]), "decision result"),
            decided_by_ref=str(row["decided_by_ref"]),
            decided_at=str(row["decided_at"]),
        )


@dataclass(frozen=True, slots=True)
class TruthAnalysisSearchReceipt:
    search_id: str
    run_id: str
    query: str
    status: str
    hits: tuple[Mapping[str, Any], ...]
    external_egress: bool
    error: str
    searched_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TruthAnalysisSearchReceipt":
        raw_hits = _json_list(str(row["hits_json"]), "search hits")
        return cls(
            search_id=str(row["search_id"]),
            run_id=str(row["run_id"]),
            query=str(row["query"]),
            status=str(row["status"]),
            hits=tuple(dict(item) for item in raw_hits if isinstance(item, Mapping)),
            external_egress=bool(row["external_egress"]),
            error=str(row["error"] or ""),
            searched_at=str(row["searched_at"]),
        )


@dataclass(frozen=True, slots=True)
class TruthAnalysisFetchReceipt:
    fetch_id: str
    run_id: str
    hit_id: str
    status: str
    url: str
    canonical_url: str
    title: str
    text: str
    content_sha256: str
    extractor: str
    external_egress: bool
    error: str
    fetched_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TruthAnalysisFetchReceipt":
        return cls(
            fetch_id=str(row["fetch_id"]),
            run_id=str(row["run_id"]),
            hit_id=str(row["hit_id"]),
            status=str(row["status"]),
            url=str(row["url"]),
            canonical_url=str(row["canonical_url"]),
            title=str(row["title"]),
            text=str(row["text"]),
            content_sha256=str(row["content_sha256"]),
            extractor=str(row["extractor"]),
            external_egress=bool(row["external_egress"]),
            error=str(row["error"] or ""),
            fetched_at=str(row["fetched_at"]),
        )


def _connect(path: Path | None = None) -> sqlite3.Connection:
    target = (_DB_PATH if path is None else path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
    return conn


def _connect_read_only(path: Path | None = None) -> sqlite3.Connection | None:
    target = (_DB_PATH if path is None else path).expanduser().resolve()
    if not target.is_file():
        return None
    conn = sqlite3.connect(f"{target.as_uri()}?mode=ro", timeout=10, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _has_schema(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'cowork_truth_analysis_runs'"
        ).fetchone()
        is not None
    )


def _ensure_schema(conn: sqlite3.Connection) -> None:
    with _SCHEMA_LOCK:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cowork_truth_analysis_runs (
                run_id                    TEXT PRIMARY KEY,
                store_id                  TEXT NOT NULL,
                document_id               TEXT NOT NULL,
                activation_revision       INTEGER NOT NULL DEFAULT 0
                    CHECK(activation_revision >= 0),
                action_snapshot_id        TEXT NOT NULL,
                status                    TEXT NOT NULL CHECK(status IN (
                    'prepared', 'launching', 'running', 'completed',
                    'unavailable', 'failed'
                )),
                selection_json            TEXT NOT NULL,
                authorization_receipt_id  TEXT NOT NULL,
                context_sha256            TEXT NOT NULL,
                request_json              TEXT NOT NULL,
                session_id                TEXT NOT NULL UNIQUE,
                pid                       INTEGER,
                output_sha256             TEXT,
                output_json               TEXT,
                error_code                TEXT NOT NULL DEFAULT '',
                error                     TEXT NOT NULL DEFAULT '',
                launch_owner              TEXT NOT NULL DEFAULT '',
                launch_lease_expires_at   TEXT,
                execution_deadline_at     TEXT NOT NULL,
                created_at                TEXT NOT NULL,
                updated_at                TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_truth_analysis_document
            ON cowork_truth_analysis_runs(store_id, document_id, created_at, run_id);

            CREATE INDEX IF NOT EXISTS idx_truth_analysis_action
            ON cowork_truth_analysis_runs(store_id, action_snapshot_id, created_at, run_id);

            CREATE TABLE IF NOT EXISTS cowork_truth_analysis_candidate_decisions (
                decision_id                 TEXT PRIMARY KEY,
                run_id                     TEXT NOT NULL
                    REFERENCES cowork_truth_analysis_runs(run_id),
                candidate_id               TEXT NOT NULL,
                candidate_canonical_sha256 TEXT NOT NULL,
                decision                   TEXT NOT NULL
                    CHECK(decision IN (
                        'save_as_proposed', 'connect_existing', 'dismiss'
                    )),
                edits_json                 TEXT NOT NULL,
                result_json                TEXT NOT NULL,
                decided_by_ref             TEXT NOT NULL,
                decided_at                 TEXT NOT NULL,
                UNIQUE(run_id, candidate_id)
            );

            CREATE TABLE IF NOT EXISTS cowork_truth_analysis_decision_intents (
                intent_id                  TEXT PRIMARY KEY,
                run_id                     TEXT NOT NULL
                    REFERENCES cowork_truth_analysis_runs(run_id),
                candidate_id               TEXT NOT NULL,
                candidate_canonical_sha256 TEXT NOT NULL,
                decision                   TEXT NOT NULL
                    CHECK(decision IN (
                        'save_as_proposed', 'connect_existing', 'dismiss'
                    )),
                edits_json                 TEXT NOT NULL,
                decided_by_ref             TEXT NOT NULL,
                prepared_at                TEXT NOT NULL,
                UNIQUE(run_id, candidate_id)
            );

            CREATE TABLE IF NOT EXISTS cowork_truth_analysis_searches (
                search_id        TEXT PRIMARY KEY,
                run_id           TEXT NOT NULL
                    REFERENCES cowork_truth_analysis_runs(run_id),
                query            TEXT NOT NULL,
                status           TEXT NOT NULL
                    CHECK(status IN ('completed', 'failed')),
                hits_json        TEXT NOT NULL,
                external_egress  INTEGER NOT NULL
                    CHECK(external_egress IN (0, 1)),
                error            TEXT NOT NULL DEFAULT '',
                searched_at      TEXT NOT NULL,
                UNIQUE(run_id, query)
            );

            CREATE INDEX IF NOT EXISTS idx_truth_analysis_searches_run
            ON cowork_truth_analysis_searches(run_id, searched_at, search_id);

            CREATE TABLE IF NOT EXISTS cowork_truth_analysis_fetches (
                fetch_id         TEXT PRIMARY KEY,
                run_id           TEXT NOT NULL
                    REFERENCES cowork_truth_analysis_runs(run_id),
                hit_id           TEXT NOT NULL,
                status           TEXT NOT NULL
                    CHECK(status IN ('completed', 'unavailable', 'failed')),
                url              TEXT NOT NULL,
                canonical_url    TEXT NOT NULL,
                title            TEXT NOT NULL,
                text             TEXT NOT NULL,
                content_sha256   TEXT NOT NULL,
                extractor        TEXT NOT NULL,
                external_egress  INTEGER NOT NULL
                    CHECK(external_egress IN (0, 1)),
                error            TEXT NOT NULL DEFAULT '',
                fetched_at       TEXT NOT NULL,
                UNIQUE(run_id, hit_id)
            );

            CREATE INDEX IF NOT EXISTS idx_truth_analysis_fetches_run
            ON cowork_truth_analysis_fetches(run_id, fetched_at, fetch_id);
            """
        )
        columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(cowork_truth_analysis_runs)"
            ).fetchall()
        }
        if "execution_deadline_at" not in columns:
            conn.execute(
                "ALTER TABLE cowork_truth_analysis_runs "
                "ADD COLUMN execution_deadline_at TEXT"
            )
            conn.execute(
                "UPDATE cowork_truth_analysis_runs SET execution_deadline_at = "
                "datetime(created_at, '+30 minutes') "
                "WHERE execution_deadline_at IS NULL"
            )
        if "activation_revision" not in columns:
            conn.execute(
                "ALTER TABLE cowork_truth_analysis_runs "
                "ADD COLUMN activation_revision INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()


def create_run(
    *,
    run_id: str,
    store_id: str,
    document_id: str,
    action_snapshot_id: str,
    selection: Mapping[str, str],
    authorization_receipt_id: str,
    context_sha256: str,
    request: Mapping[str, Any],
    session_id: str,
    activation_revision: int = 0,
    at: str | None = None,
    execution_deadline_at: str | None = None,
) -> TruthAnalysisRuntimeRun:
    """Insert one immutable staged-run binding in ``prepared`` state."""

    timestamp = at or utc_now()
    deadline = execution_deadline_at or (
        datetime.now(timezone.utc)
        + timedelta(seconds=DEFAULT_EXECUTION_TIMEOUT_SECONDS)
    ).isoformat(timespec="milliseconds")
    deadline_value = _utc_datetime(deadline)
    if deadline_value <= datetime.now(timezone.utc):
        raise ValueError("Truth analysis execution deadline must follow creation")
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM cowork_truth_analysis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if existing is not None:
            return TruthAnalysisRuntimeRun.from_row(existing)
        rows = conn.execute(
            "SELECT * FROM cowork_truth_analysis_runs "
            "WHERE store_id = ? AND document_id = ? "
            "ORDER BY created_at DESC, rowid DESC",
            (store_id, document_id),
        ).fetchall()
        for prior_row in rows:
            prior = TruthAnalysisRuntimeRun.from_row(prior_row)
            if (
                prior.status in {"prepared", "launching", "running"}
                and _utc_datetime(prior.execution_deadline_at)
                <= datetime.now(timezone.utc)
            ):
                conn.execute(
                    "UPDATE cowork_truth_analysis_runs SET status = 'failed', "
                    "error_code = 'execution_deadline_exceeded', "
                    "error = 'Truth analysis exceeded its execution deadline.', "
                    "launch_lease_expires_at = NULL, updated_at = ? WHERE run_id = ?",
                    (utc_now(), prior.run_id),
                )
                continue
            pending: int | None = 0
            if prior.status in {"prepared", "launching", "running"}:
                pending = None
            elif prior.status == "completed":
                candidates = (
                    None
                    if prior.output is None
                    else prior.output.get("candidates")
                )
                if not isinstance(candidates, list):
                    pending = 1
                else:
                    candidate_ids = {
                        str(item.get("candidate_id"))
                        for item in candidates
                        if isinstance(item, Mapping)
                        and isinstance(item.get("candidate_id"), str)
                    }
                    decided = {
                        str(row["candidate_id"])
                        for row in conn.execute(
                            "SELECT candidate_id FROM "
                            "cowork_truth_analysis_candidate_decisions "
                            "WHERE run_id = ?",
                            (prior.run_id,),
                        ).fetchall()
                    }
                    pending = len(candidate_ids - decided)
            if pending is None or pending > 0:
                raise TruthAnalysisRunConflict(
                    run_id=prior.run_id,
                    status=prior.status,
                    pending_candidates=pending,
                )
        conn.execute(
            """
            INSERT INTO cowork_truth_analysis_runs (
                run_id, store_id, document_id, activation_revision,
                action_snapshot_id, status,
                selection_json, authorization_receipt_id, context_sha256,
                request_json, session_id, created_at, updated_at
                , execution_deadline_at
            ) VALUES (?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                store_id,
                document_id,
                int(activation_revision),
                action_snapshot_id,
                canonical_json(dict(selection)),
                authorization_receipt_id,
                context_sha256,
                canonical_json(dict(request)),
                session_id,
                timestamp,
                timestamp,
                deadline,
            ),
        )
        row = conn.execute(
            "SELECT * FROM cowork_truth_analysis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    assert row is not None
    return TruthAnalysisRuntimeRun.from_row(row)


def get_run(run_id: str) -> TruthAnalysisRuntimeRun | None:
    conn = _connect_read_only()
    if conn is None:
        return None
    try:
        if not _has_schema(conn):
            return None
        row = conn.execute(
            "SELECT * FROM cowork_truth_analysis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    run = None if row is None else TruthAnalysisRuntimeRun.from_row(row)
    if (
        run is not None
        and run.status in {"prepared", "launching", "running"}
        and _utc_datetime(run.execution_deadline_at) <= datetime.now(timezone.utc)
    ):
        run, _ = expire_run_if_overdue(run.run_id)
    return run


def expire_run_if_overdue(
    run_id: str,
    *,
    at: str | None = None,
) -> tuple[TruthAnalysisRuntimeRun | None, bool]:
    now = _utc_datetime(at or utc_now())
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM cowork_truth_analysis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None, False
        run = TruthAnalysisRuntimeRun.from_row(row)
        if (
            run.status not in {"prepared", "launching", "running"}
            or _utc_datetime(run.execution_deadline_at) > now
        ):
            return run, False
        timestamp = at or utc_now()
        conn.execute(
            "UPDATE cowork_truth_analysis_runs SET status = 'failed', "
            "error_code = 'execution_deadline_exceeded', "
            "error = 'Truth analysis exceeded its execution deadline.', "
            "launch_lease_expires_at = NULL, updated_at = ? WHERE run_id = ?",
            (timestamp, run_id),
        )
        saved = conn.execute(
            "SELECT * FROM cowork_truth_analysis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    assert saved is not None
    return TruthAnalysisRuntimeRun.from_row(saved), True


def runs_for_document(
    store_id: str, document_id: str
) -> tuple[TruthAnalysisRuntimeRun, ...]:
    conn = _connect_read_only()
    if conn is None:
        return ()
    try:
        if not _has_schema(conn):
            return ()
        rows = conn.execute(
            """
            SELECT * FROM cowork_truth_analysis_runs
            WHERE store_id = ? AND document_id = ?
            ORDER BY created_at, rowid
            """,
            (store_id, document_id),
        ).fetchall()
    finally:
        conn.close()
    values: list[TruthAnalysisRuntimeRun] = []
    for row in rows:
        stored = TruthAnalysisRuntimeRun.from_row(row)
        current = get_run(stored.run_id)
        values.append(stored if current is None else current)
    return tuple(values)


def invalidate_active_runs_for_document(
    store_id: str,
    document_id: str,
    *,
    valid_activation_revision: int | None,
    at: str | None = None,
) -> tuple[TruthAnalysisRuntimeRun, ...]:
    """Fence every operational run no longer admitted by document policy."""

    timestamp = at or utc_now()
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        params: list[Any] = [store_id, document_id]
        revision_clause = ""
        if valid_activation_revision is not None:
            revision_clause = " AND activation_revision != ?"
            params.append(int(valid_activation_revision))
        rows = conn.execute(
            "SELECT * FROM cowork_truth_analysis_runs "
            "WHERE store_id = ? AND document_id = ? "
            "AND status IN ('prepared','launching','running')"
            + revision_clause
            + " ORDER BY created_at, run_id",
            tuple(params),
        ).fetchall()
        run_ids = [str(row["run_id"]) for row in rows]
        if run_ids:
            marks = ",".join("?" for _ in run_ids)
            conn.execute(
                "UPDATE cowork_truth_analysis_runs SET status='unavailable', "
                "error_code='truth_activation_changed', "
                "error='Document Truth settings changed while this analysis was running.', "
                "launch_lease_expires_at=NULL, updated_at=? "
                f"WHERE run_id IN ({marks}) "
                "AND status IN ('prepared','launching','running')",
                (timestamp, *run_ids),
            )
            saved = conn.execute(
                "SELECT * FROM cowork_truth_analysis_runs "
                f"WHERE run_id IN ({marks}) ORDER BY created_at, run_id",
                tuple(run_ids),
            ).fetchall()
        else:
            saved = []
    return tuple(TruthAnalysisRuntimeRun.from_row(row) for row in saved)


def reconcilable_runs() -> tuple[TruthAnalysisRuntimeRun, ...]:
    """Return operational states that require launch/recovery inspection."""

    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT * FROM cowork_truth_analysis_runs
            WHERE status IN ('prepared', 'launching', 'running')
            ORDER BY created_at, run_id
            """
        ).fetchall()
    return tuple(TruthAnalysisRuntimeRun.from_row(row) for row in rows)


def claim_run_launch(
    run_id: str,
    *,
    launch_owner: str | None = None,
    lease_expires_at: str | None = None,
    at: str | None = None,
) -> tuple[TruthAnalysisRuntimeRun, bool]:
    """Atomically lease one prepared run so it can be spawned only once."""

    timestamp = at or utc_now()
    owner = str(launch_owner or f"direct:{run_id}").strip()
    if not owner:
        raise ValueError("Truth analysis launch_owner must not be empty")
    deadline_text = lease_expires_at or (
        datetime.now(timezone.utc) + timedelta(minutes=5)
    ).isoformat(timespec="milliseconds")
    try:
        deadline = datetime.fromisoformat(deadline_text.replace("Z", "+00:00"))
        claimed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Truth analysis launch lease must be an ISO timestamp") from exc
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    if deadline <= claimed_at:
        raise ValueError("Truth analysis launch lease must expire after it is claimed")
    with _connect() as conn:
        _ensure_schema(conn)
        cursor = conn.execute(
            """
            UPDATE cowork_truth_analysis_runs
            SET status = 'launching', launch_owner = ?,
                launch_lease_expires_at = ?, updated_at = ?
            WHERE run_id = ? AND status = 'prepared'
            """,
            (owner, deadline.isoformat(), timestamp, run_id),
        )
        row = conn.execute(
            "SELECT * FROM cowork_truth_analysis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"unknown Co-work Truth analysis run: {run_id}")
    return TruthAnalysisRuntimeRun.from_row(row), cursor.rowcount == 1


def update_run(
    run_id: str,
    *,
    status: str,
    pid: int | None = None,
    output_sha256: str | None = None,
    output: Mapping[str, Any] | None = None,
    error_code: str = "",
    error: str = "",
    expected_launch_owner: str | None = None,
    at: str | None = None,
) -> TruthAnalysisRuntimeRun:
    """Advance operational state while keeping binding and output immutable."""

    if status not in _VALID_STATUSES:
        raise ValueError(f"unsupported Truth analysis status: {status}")
    timestamp = at or utc_now()
    serialized = None if output is None else _output_json(output)
    if serialized is not None:
        computed = sha256_text(serialized)
        if output_sha256 is None:
            output_sha256 = computed
        elif output_sha256 != computed:
            raise ValueError("Truth analysis output_sha256 does not match output")
    if output_sha256 is not None and status != "completed":
        raise ValueError("Truth analysis output can only be stored as completed")
    with _connect() as conn:
        _ensure_schema(conn)
        current = conn.execute(
            "SELECT * FROM cowork_truth_analysis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if current is None:
            raise KeyError(f"unknown Co-work Truth analysis run: {run_id}")
        if (
            expected_launch_owner is not None
            and str(current["launch_owner"] or "") != expected_launch_owner
        ):
            raise ValueError("Truth analysis launch owner no longer holds the fence")
        current_status = str(current["status"])
        if status not in _STATUS_TRANSITIONS[current_status]:
            raise ValueError(
                "unsupported Truth analysis status transition: "
                f"{current_status} -> {status}"
            )
        current_digest = (
            None
            if current["output_sha256"] is None
            else str(current["output_sha256"])
        )
        current_json = (
            None if current["output_json"] is None else str(current["output_json"])
        )
        if output_sha256 is not None:
            if current_digest is None and serialized is None:
                raise ValueError("the first Truth analysis output digest requires output")
            if current_digest is not None and current_digest != output_sha256:
                raise ValueError("Truth analysis output is immutable after completion")
        if serialized is not None and current_json is not None and serialized != current_json:
            raise ValueError("Truth analysis output is immutable after completion")
        conn.execute(
            """
            UPDATE cowork_truth_analysis_runs
            SET status = ?, pid = COALESCE(?, pid),
                output_sha256 = COALESCE(?, output_sha256),
                output_json = CASE WHEN ? IS NULL THEN output_json ELSE ? END,
                error_code = ?, error = ?,
                launch_lease_expires_at = CASE
                    WHEN ? = 'launching' THEN launch_lease_expires_at ELSE NULL END,
                updated_at = ?
            WHERE run_id = ?
            """,
            (
                status,
                pid,
                output_sha256,
                serialized,
                serialized,
                error_code,
                error,
                status,
                timestamp,
                run_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM cowork_truth_analysis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    assert row is not None
    return TruthAnalysisRuntimeRun.from_row(row)


def record_search_receipt(
    *,
    run_id: str,
    query: str,
    status: str,
    hits: list[Mapping[str, Any]],
    external_egress: bool,
    error: str = "",
    max_searches: int,
    at: str | None = None,
) -> tuple[TruthAnalysisSearchReceipt, bool]:
    """Persist one bounded job-scoped web search, idempotently by query."""

    normalized_query = str(query or "").strip()
    if not normalized_query:
        raise ValueError("Truth analysis search query must not be empty")
    if status not in {"completed", "failed"}:
        raise ValueError("Truth analysis search status is invalid")
    if max_searches < 1:
        raise ValueError("Truth analysis search limit must be positive")
    searched_at = at or utc_now()
    hits_json = json.dumps(
        [dict(item) for item in hits],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    search_id = sha256_text(
        canonical_json(
            {
                "domain": "work-buddy.cowork-truth-search/v1",
                "run_id": run_id,
                "query": normalized_query,
            }
        )
    )[:32]
    with _connect() as conn:
        _ensure_schema(conn)
        run = conn.execute(
            "SELECT status FROM cowork_truth_analysis_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise KeyError(f"unknown Co-work Truth analysis run: {run_id}")
        if str(run["status"]) not in {"prepared", "launching", "running"}:
            raise ValueError("Truth analysis search is closed for this run")
        existing = conn.execute(
            "SELECT * FROM cowork_truth_analysis_searches "
            "WHERE run_id = ? AND query = ?",
            (run_id, normalized_query),
        ).fetchone()
        if existing is not None:
            receipt = TruthAnalysisSearchReceipt.from_row(existing)
            immutable_matches = (
                receipt.search_id == search_id
                and receipt.status == status
                and list(receipt.hits) == [dict(item) for item in hits]
                and receipt.external_egress is bool(external_egress)
                and receipt.error == str(error or "")
            )
            if not immutable_matches:
                raise ValueError("Truth analysis search replay changed its result")
            return receipt, True
        count = int(
            conn.execute(
                "SELECT COUNT(*) FROM cowork_truth_analysis_searches WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        if count >= max_searches:
            raise ValueError("Truth analysis search limit reached")
        conn.execute(
            "INSERT INTO cowork_truth_analysis_searches ("
            "search_id, run_id, query, status, hits_json, external_egress, "
            "error, searched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                search_id,
                run_id,
                normalized_query,
                status,
                hits_json,
                int(external_egress),
                str(error or ""),
                searched_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM cowork_truth_analysis_searches WHERE search_id = ?",
            (search_id,),
        ).fetchone()
    assert row is not None
    return TruthAnalysisSearchReceipt.from_row(row), False


def search_receipts_for_run(run_id: str) -> tuple[TruthAnalysisSearchReceipt, ...]:
    conn = _connect_read_only()
    if conn is None:
        return ()
    try:
        if not _has_schema(conn):
            return ()
        rows = conn.execute(
            "SELECT * FROM cowork_truth_analysis_searches WHERE run_id = ? "
            "ORDER BY searched_at, search_id",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    return tuple(TruthAnalysisSearchReceipt.from_row(row) for row in rows)


def search_hit_for_run(run_id: str, hit_id: str) -> Mapping[str, Any] | None:
    for receipt in search_receipts_for_run(run_id):
        for hit in receipt.hits:
            if hit.get("hit_id") == hit_id:
                return dict(hit)
    return None


def record_fetch_receipt(
    *,
    run_id: str,
    hit_id: str,
    status: str,
    url: str,
    canonical_url: str,
    title: str,
    text: str,
    content_sha256: str,
    extractor: str,
    external_egress: bool,
    error: str = "",
    max_fetches: int,
    at: str | None = None,
) -> tuple[TruthAnalysisFetchReceipt, bool]:
    """Persist one admitted-hit fetch receipt; arbitrary URLs never enter here."""

    if status not in {"completed", "unavailable", "failed"}:
        raise ValueError("Truth analysis fetch status is invalid")
    if max_fetches < 1:
        raise ValueError("Truth analysis fetch limit must be positive")
    normalized_hit_id = str(hit_id or "").strip()
    if not normalized_hit_id or search_hit_for_run(run_id, normalized_hit_id) is None:
        raise ValueError("Truth analysis fetch requires an admitted search hit")
    fetched_at = at or utc_now()
    fetch_id = sha256_text(
        canonical_json(
            {
                "domain": "work-buddy.cowork-truth-fetch/v1",
                "run_id": run_id,
                "hit_id": normalized_hit_id,
            }
        )
    )[:32]
    with _connect() as conn:
        _ensure_schema(conn)
        run = conn.execute(
            "SELECT status FROM cowork_truth_analysis_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise KeyError(f"unknown Co-work Truth analysis run: {run_id}")
        if str(run["status"]) not in {"prepared", "launching", "running"}:
            raise ValueError("Truth analysis fetch is closed for this run")
        existing = conn.execute(
            "SELECT * FROM cowork_truth_analysis_fetches "
            "WHERE run_id = ? AND hit_id = ?",
            (run_id, normalized_hit_id),
        ).fetchone()
        if existing is not None:
            receipt = TruthAnalysisFetchReceipt.from_row(existing)
            immutable_matches = (
                receipt.fetch_id == fetch_id
                and receipt.status == status
                and receipt.url == str(url)
                and receipt.canonical_url == str(canonical_url)
                and receipt.title == str(title)
                and receipt.text == str(text)
                and receipt.content_sha256 == str(content_sha256)
                and receipt.extractor == str(extractor)
                and receipt.external_egress is bool(external_egress)
                and receipt.error == str(error or "")
            )
            if not immutable_matches:
                raise ValueError("Truth analysis fetch replay changed its result")
            return receipt, True
        count = int(
            conn.execute(
                "SELECT COUNT(*) FROM cowork_truth_analysis_fetches WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        if count >= max_fetches:
            raise ValueError("Truth analysis fetch limit reached")
        conn.execute(
            "INSERT INTO cowork_truth_analysis_fetches ("
            "fetch_id, run_id, hit_id, status, url, canonical_url, title, text, "
            "content_sha256, extractor, external_egress, error, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fetch_id,
                run_id,
                normalized_hit_id,
                status,
                str(url),
                str(canonical_url),
                str(title),
                str(text),
                str(content_sha256),
                str(extractor),
                int(external_egress),
                str(error or ""),
                fetched_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM cowork_truth_analysis_fetches WHERE fetch_id = ?",
            (fetch_id,),
        ).fetchone()
    assert row is not None
    return TruthAnalysisFetchReceipt.from_row(row), False


def fetch_receipts_for_run(run_id: str) -> tuple[TruthAnalysisFetchReceipt, ...]:
    conn = _connect_read_only()
    if conn is None:
        return ()
    try:
        if not _has_schema(conn):
            return ()
        rows = conn.execute(
            "SELECT * FROM cowork_truth_analysis_fetches WHERE run_id = ? "
            "ORDER BY fetched_at, fetch_id",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    return tuple(TruthAnalysisFetchReceipt.from_row(row) for row in rows)


def get_fetch_receipt(
    run_id: str, fetch_id: str
) -> TruthAnalysisFetchReceipt | None:
    return next(
        (
            item
            for item in fetch_receipts_for_run(run_id)
            if item.fetch_id == fetch_id
        ),
        None,
    )


def _candidate(run: TruthAnalysisRuntimeRun, candidate_id: str) -> Mapping[str, Any]:
    output = run.output
    candidates = None if output is None else output.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("Truth analysis run has no staged candidates")
    matches = [
        item
        for item in candidates
        if isinstance(item, Mapping) and item.get("candidate_id") == candidate_id
    ]
    if len(matches) != 1:
        raise KeyError(f"unknown Truth analysis candidate: {candidate_id}")
    return matches[0]


def prepare_candidate_decision(
    *,
    run_id: str,
    candidate_id: str,
    candidate_canonical_sha256: str,
    decision: str,
    edits: Mapping[str, Any] | None,
    decided_by_ref: str,
    at: str | None = None,
) -> bool:
    """Persist immutable human intent before crossing into the Truth ledger."""

    if decision not in _DECISIONS:
        raise ValueError(
            "decision must be save_as_proposed, connect_existing, or dismiss"
        )
    actor_ref = str(decided_by_ref or "").strip()
    if not actor_ref:
        raise ValueError("decided_by_ref must not be empty")
    edits_value = {} if edits is None else dict(edits)
    prepared_at = at or utc_now()
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM cowork_truth_analysis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown Co-work Truth analysis run: {run_id}")
        run = TruthAnalysisRuntimeRun.from_row(row)
        if run.status != "completed":
            raise ValueError("Truth analysis candidates are not ready for a decision")
        candidate = _candidate(run, candidate_id)
        observed_hash = str(candidate.get("canonical_sha256") or "")
        if observed_hash != str(candidate_canonical_sha256 or "").strip().lower():
            raise ValueError("Truth analysis candidate changed after it was shown")
        immutable = {
            "run_id": run_id,
            "candidate_id": candidate_id,
            "candidate_canonical_sha256": observed_hash,
            "decision": decision,
            "edits": edits_value,
            "decided_by_ref": actor_ref,
        }
        intent_id = sha256_text(
            canonical_json(
                {"domain": "work-buddy.cowork-truth-decision-intent/v1", **immutable}
            )
        )[:32]
        existing = conn.execute(
            "SELECT intent_id FROM cowork_truth_analysis_decision_intents "
            "WHERE run_id = ? AND candidate_id = ?",
            (run_id, candidate_id),
        ).fetchone()
        if existing is not None:
            if str(existing["intent_id"]) != intent_id:
                raise ValueError("Truth analysis candidate already has another decision")
            return True
        conn.execute(
            "INSERT INTO cowork_truth_analysis_decision_intents ("
            "intent_id, run_id, candidate_id, candidate_canonical_sha256, "
            "decision, edits_json, decided_by_ref, prepared_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                intent_id,
                run_id,
                candidate_id,
                observed_hash,
                decision,
                canonical_json(edits_value),
                actor_ref,
                prepared_at,
            ),
        )
    return False


def clear_candidate_decision_intent(
    *,
    run_id: str,
    candidate_id: str,
    candidate_canonical_sha256: str,
    decision: str,
    edits: Mapping[str, Any] | None,
    decided_by_ref: str,
) -> bool:
    """Release an exact prepared intent when no canonical write crossed.

    The exact immutable identity is recomputed so one failing request can never
    clear another actor's or another payload's intent.  A recorded decision is
    an irreversible fence and therefore always prevents deletion.
    """

    immutable = {
        "run_id": run_id,
        "candidate_id": candidate_id,
        "candidate_canonical_sha256": str(
            candidate_canonical_sha256 or ""
        ).strip().lower(),
        "decision": decision,
        "edits": {} if edits is None else dict(edits),
        "decided_by_ref": str(decided_by_ref or "").strip(),
    }
    intent_id = sha256_text(
        canonical_json(
            {"domain": "work-buddy.cowork-truth-decision-intent/v1", **immutable}
        )
    )[:32]
    with _connect() as conn:
        _ensure_schema(conn)
        decided = conn.execute(
            "SELECT 1 FROM cowork_truth_analysis_candidate_decisions "
            "WHERE run_id = ? AND candidate_id = ?",
            (run_id, candidate_id),
        ).fetchone()
        if decided is not None:
            return False
        cursor = conn.execute(
            "DELETE FROM cowork_truth_analysis_decision_intents "
            "WHERE run_id = ? AND candidate_id = ? AND intent_id = ?",
            (run_id, candidate_id, intent_id),
        )
    return cursor.rowcount == 1


def record_candidate_decision(
    *,
    run_id: str,
    candidate_id: str,
    candidate_canonical_sha256: str,
    decision: str,
    edits: Mapping[str, Any] | None,
    result: Mapping[str, Any],
    decided_by_ref: str,
    at: str | None = None,
) -> tuple[TruthAnalysisCandidateDecision, bool]:
    """Record one post-commit human decision, idempotently.

    Orchestration calls this only after any canonical Truth mutation succeeds.
    The runtime checks the exact staged candidate hash so a stale UI cannot
    attach a ledger result to a different model candidate.
    """

    if decision not in _DECISIONS:
        raise ValueError(
            "decision must be save_as_proposed, connect_existing, or dismiss"
        )
    actor_ref = str(decided_by_ref or "").strip()
    if not actor_ref:
        raise ValueError("decided_by_ref must not be empty")
    edits_value = {} if edits is None else dict(edits)
    result_value = dict(result)
    decided_at = at or utc_now()
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM cowork_truth_analysis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown Co-work Truth analysis run: {run_id}")
        run = TruthAnalysisRuntimeRun.from_row(row)
        if run.status != "completed":
            raise ValueError("Truth analysis candidates are not ready for a decision")
        candidate = _candidate(run, candidate_id)
        observed_hash = str(candidate.get("canonical_sha256") or "")
        if observed_hash != str(candidate_canonical_sha256 or "").strip().lower():
            raise ValueError("Truth analysis candidate changed after it was shown")
        immutable = {
            "run_id": run_id,
            "candidate_id": candidate_id,
            "candidate_canonical_sha256": observed_hash,
            "decision": decision,
            "edits": edits_value,
            "result": result_value,
            "decided_by_ref": actor_ref,
        }
        decision_id = sha256_text(
            canonical_json({"domain": "work-buddy.cowork-truth-decision/v1", **immutable})
        )[:32]
        existing = conn.execute(
            """
            SELECT * FROM cowork_truth_analysis_candidate_decisions
            WHERE run_id = ? AND candidate_id = ?
            """,
            (run_id, candidate_id),
        ).fetchone()
        if existing is not None:
            replay = TruthAnalysisCandidateDecision.from_row(existing)
            if replay.decision_id != decision_id:
                raise ValueError("Truth analysis candidate already has another decision")
            return replay, True
        conn.execute(
            """
            INSERT INTO cowork_truth_analysis_candidate_decisions (
                decision_id, run_id, candidate_id, candidate_canonical_sha256,
                decision, edits_json, result_json, decided_by_ref, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                run_id,
                candidate_id,
                observed_hash,
                decision,
                canonical_json(edits_value),
                canonical_json(result_value),
                actor_ref,
                decided_at,
            ),
        )
        saved = conn.execute(
            "SELECT * FROM cowork_truth_analysis_candidate_decisions "
            "WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
    assert saved is not None
    return TruthAnalysisCandidateDecision.from_row(saved), False


def candidate_decisions_for_run(
    run_id: str,
) -> tuple[TruthAnalysisCandidateDecision, ...]:
    conn = _connect_read_only()
    if conn is None:
        return ()
    try:
        if not _has_schema(conn):
            return ()
        rows = conn.execute(
            """
            SELECT * FROM cowork_truth_analysis_candidate_decisions
            WHERE run_id = ? ORDER BY decided_at, decision_id
            """,
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    return tuple(TruthAnalysisCandidateDecision.from_row(row) for row in rows)


__all__ = [
    "DEFAULT_EXECUTION_TIMEOUT_SECONDS",
    "TruthAnalysisCandidateDecision",
    "TruthAnalysisFetchReceipt",
    "TruthAnalysisRunConflict",
    "TruthAnalysisRuntimeRun",
    "TruthAnalysisSearchReceipt",
    "candidate_decisions_for_run",
    "clear_candidate_decision_intent",
    "claim_run_launch",
    "create_run",
    "expire_run_if_overdue",
    "fetch_receipts_for_run",
    "get_fetch_receipt",
    "get_run",
    "prepare_candidate_decision",
    "record_fetch_receipt",
    "reconcilable_runs",
    "record_candidate_decision",
    "record_search_receipt",
    "runs_for_document",
    "search_hit_for_run",
    "search_receipts_for_run",
    "update_run",
]

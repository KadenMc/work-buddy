"""Append-only persistence primitives for the Co-work Verify ledger."""

from __future__ import annotations

import sqlite3
from dataclasses import fields
from typing import Any, TypeVar

from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.store import TruthStore

from .contracts import (
    ActionSnapshot,
    CheckDefinitionVersion,
    CheckExecution,
    CothinkItem,
    CothinkItemStatusEvent,
    CoworkCoordinationJob,
    CoworkCoordinationStatusEvent,
    CoworkReviewApplication,
    CriterionActivation,
    CriterionCheckBinding,
    CriterionDefinitionVersion,
    EvaluationPlanSnapshot,
    EvaluationResult,
    EvaluationRun,
    ModelCallAuthorizationReceipt,
    ResultRelation,
    RoutingDisposition,
)


RecordT = TypeVar("RecordT")

_RECORDS: dict[type[Any], tuple[str, str]] = {
    CriterionDefinitionVersion: (
        "criterion_definition_versions",
        "criterion_definition_version",
    ),
    CheckDefinitionVersion: (
        "check_definition_versions",
        "check_definition_version",
    ),
    CriterionCheckBinding: (
        "criterion_check_bindings",
        "criterion_check_binding",
    ),
    CriterionActivation: ("criterion_activations", "criterion_activation"),
    ActionSnapshot: ("action_snapshots", "action_snapshot"),
    EvaluationPlanSnapshot: (
        "evaluation_plan_snapshots",
        "evaluation_plan_snapshot",
    ),
    EvaluationRun: ("evaluation_runs", "evaluation_run"),
    CheckExecution: ("check_executions", "check_execution"),
    EvaluationResult: ("evaluation_results", "evaluation_result"),
    RoutingDisposition: ("routing_dispositions", "routing_disposition"),
    ResultRelation: ("result_relations", "result_relation"),
    ModelCallAuthorizationReceipt: (
        "model_call_authorization_receipts",
        "model_call_authorization_receipt",
    ),
    CothinkItem: ("cothink_items", "cothink_item"),
    CothinkItemStatusEvent: (
        "cothink_item_status_events",
        "cothink_item_status_event",
    ),
    CoworkCoordinationJob: (
        "cowork_coordination_jobs",
        "cowork_coordination_job",
    ),
    CoworkCoordinationStatusEvent: (
        "cowork_coordination_status_events",
        "cowork_coordination_status_event",
    ),
    CoworkReviewApplication: (
        "cowork_review_applications",
        "cowork_review_application",
    ),
}


def _definition(record_type: type[RecordT]) -> tuple[str, str]:
    try:
        return _RECORDS[record_type]
    except KeyError as exc:
        raise TypeError(f"unsupported Verify record type: {record_type!r}") from exc


def _from_row(record_type: type[RecordT], row: sqlite3.Row | None) -> RecordT | None:
    return None if row is None else record_type(**dict(row))


def insert_record(
    store: TruthStore,
    record: RecordT,
    *,
    conn: sqlite3.Connection | None = None,
) -> RecordT:
    """Insert one typed row and its global ledger-order record atomically."""

    record_type = type(record)
    table, ledger_type = _definition(record_type)
    column_names = tuple(field.name for field in fields(record))
    if "id" not in column_names:
        raise TypeError("portable Verify records require an id field")
    values = tuple(getattr(record, name) for name in column_names)
    with store.write_transaction(conn) as write_conn:
        write_conn.execute(
            f"INSERT INTO {table} ({', '.join(column_names)}) "
            f"VALUES ({', '.join('?' for _ in column_names)})",
            values,
        )
        store._insert_ledger_record_locked(
            write_conn,
            ledger_type,
            str(getattr(record, "id")),
        )
    return record


def get_record(
    store: TruthStore,
    record_type: type[RecordT],
    record_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> RecordT | None:
    table, _ = _definition(record_type)

    def _read(read_conn: sqlite3.Connection) -> RecordT | None:
        return _from_row(
            record_type,
            read_conn.execute(
                f"SELECT * FROM {table} WHERE id = ?",
                (record_id,),
            ).fetchone(),
        )

    if conn is not None:
        return _read(conn)
    with store._read_connection() as read_conn:
        return _read(read_conn)


def get_by_canonical_sha256(
    store: TruthStore,
    record_type: type[RecordT],
    canonical_sha256: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> RecordT | None:
    table, _ = _definition(record_type)

    def _read(read_conn: sqlite3.Connection) -> RecordT | None:
        return _from_row(
            record_type,
            read_conn.execute(
                f"SELECT * FROM {table} WHERE canonical_sha256 = ? "
                "ORDER BY rowid LIMIT 1",
                (canonical_sha256,),
            ).fetchone(),
        )

    if conn is not None:
        return _read(conn)
    with store._read_connection() as read_conn:
        return _read(read_conn)


def list_records(
    store: TruthStore,
    record_type: type[RecordT],
    *,
    where: str = "",
    params: tuple[Any, ...] = (),
    conn: sqlite3.Connection | None = None,
) -> tuple[RecordT, ...]:
    """Read typed rows in immutable ledger order.

    ``where`` is intentionally an internal domain seam, not caller input.
    Public service functions expose constrained query methods.
    """

    table, ledger_type = _definition(record_type)
    sql = (
        f"SELECT source.* FROM {table} AS source "
        "JOIN ledger_records AS ledger "
        f"ON ledger.record_type = '{ledger_type}' "
        "AND ledger.record_key = source.id "
    )
    if where:
        sql += f"WHERE {where} "
    sql += "ORDER BY ledger.seq"

    def _read(read_conn: sqlite3.Connection) -> tuple[RecordT, ...]:
        return tuple(
            record_type(**dict(row))
            for row in read_conn.execute(sql, params).fetchall()
        )

    if conn is not None:
        return _read(conn)
    with store._read_connection() as read_conn:
        return _read(read_conn)


def latest_disposition(
    store: TruthStore,
    result_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> RoutingDisposition | None:
    def _read(read_conn: sqlite3.Connection) -> RoutingDisposition | None:
        row = read_conn.execute(
            "SELECT disposition.* FROM routing_dispositions AS disposition "
            "JOIN ledger_records AS ledger "
            "ON ledger.record_type = 'routing_disposition' "
            "AND ledger.record_key = disposition.id "
            "WHERE disposition.evaluation_result_id = ? "
            "ORDER BY ledger.seq DESC LIMIT 1",
            (result_id,),
        ).fetchone()
        return _from_row(RoutingDisposition, row)

    if conn is not None:
        return _read(conn)
    with store._read_connection() as read_conn:
        return _read(read_conn)


def latest_cothink_status(
    store: TruthStore,
    cothink_item_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> CothinkItemStatusEvent | None:
    """Return current lifecycle state by insertion order, never timestamp."""

    def _read(read_conn: sqlite3.Connection) -> CothinkItemStatusEvent | None:
        row = read_conn.execute(
            "SELECT event.* FROM cothink_item_status_events AS event "
            "JOIN ledger_records AS ledger "
            "ON ledger.record_type = 'cothink_item_status_event' "
            "AND ledger.record_key = event.id "
            "WHERE event.cothink_item_id = ? "
            "ORDER BY ledger.seq DESC LIMIT 1",
            (cothink_item_id,),
        ).fetchone()
        return _from_row(CothinkItemStatusEvent, row)

    if conn is not None:
        return _read(conn)
    with store._read_connection() as read_conn:
        return _read(read_conn)


def latest_coordination_status(
    store: TruthStore,
    coordination_job_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> CoworkCoordinationStatusEvent | None:
    """Return the latest portable job state by ledger order."""

    def _read(
        read_conn: sqlite3.Connection,
    ) -> CoworkCoordinationStatusEvent | None:
        row = read_conn.execute(
            "SELECT event.* "
            "FROM cowork_coordination_status_events AS event "
            "JOIN ledger_records AS ledger "
            "ON ledger.record_type = 'cowork_coordination_status_event' "
            "AND ledger.record_key = event.id "
            "WHERE event.coordination_job_id = ? "
            "ORDER BY ledger.seq DESC LIMIT 1",
            (coordination_job_id,),
        ).fetchone()
        return _from_row(CoworkCoordinationStatusEvent, row)

    if conn is not None:
        return _read(conn)
    with store._read_connection() as read_conn:
        return _read(read_conn)


def surfaced_result_rows(
    store: TruthStore,
    *,
    document_id: str | None = None,
    action_snapshot_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[tuple[EvaluationResult, RoutingDisposition, ActionSnapshot], ...]:
    """Return results whose latest routing disposition explicitly surfaces them."""

    if document_id is not None and action_snapshot_id is not None:
        raise InvariantViolation(
            "surface projection accepts document_id or action_snapshot_id, not both"
        )
    filters: list[str] = []
    params: list[Any] = []
    if document_id is not None:
        filters.append("snapshot.document_id = ?")
        params.append(document_id)
    if action_snapshot_id is not None:
        filters.append("snapshot.id = ?")
        params.append(action_snapshot_id)
    where = "" if not filters else "AND " + " AND ".join(filters)
    sql = (
        "SELECT "
        + ", ".join(
            f"result.{field.name} AS result__{field.name}"
            for field in fields(EvaluationResult)
        )
        + ", "
        + ", ".join(
            f"disposition.{field.name} AS disposition__{field.name}"
            for field in fields(RoutingDisposition)
        )
        + ", "
        + ", ".join(
            f"snapshot.{field.name} AS snapshot__{field.name}"
            for field in fields(ActionSnapshot)
        )
        + " FROM evaluation_results AS result "
        "JOIN evaluation_runs AS run ON run.id = result.evaluation_run_id "
        "JOIN action_snapshots AS snapshot ON snapshot.id = run.action_snapshot_id "
        "JOIN routing_dispositions AS disposition "
        "ON disposition.evaluation_result_id = result.id "
        "JOIN ledger_records AS result_ledger "
        "ON result_ledger.record_type = 'evaluation_result' "
        "AND result_ledger.record_key = result.id "
        "JOIN ledger_records AS disposition_ledger "
        "ON disposition_ledger.record_type = 'routing_disposition' "
        "AND disposition_ledger.record_key = disposition.id "
        "WHERE disposition.decision IN ('surface', 'route_to_correction') "
        "AND disposition_ledger.seq = ("
        "SELECT MAX(latest_ledger.seq) "
        "FROM routing_dispositions AS latest "
        "JOIN ledger_records AS latest_ledger "
        "ON latest_ledger.record_type = 'routing_disposition' "
        "AND latest_ledger.record_key = latest.id "
        "WHERE latest.evaluation_result_id = result.id"
        f") {where} ORDER BY result_ledger.seq"
    )

    def _read(
        read_conn: sqlite3.Connection,
    ) -> tuple[tuple[EvaluationResult, RoutingDisposition, ActionSnapshot], ...]:
        values: list[
            tuple[EvaluationResult, RoutingDisposition, ActionSnapshot]
        ] = []
        for row in read_conn.execute(sql, tuple(params)).fetchall():
            data = dict(row)
            values.append(
                (
                    EvaluationResult(
                        **{
                            field.name: data[f"result__{field.name}"]
                            for field in fields(EvaluationResult)
                        }
                    ),
                    RoutingDisposition(
                        **{
                            field.name: data[f"disposition__{field.name}"]
                            for field in fields(RoutingDisposition)
                        }
                    ),
                    ActionSnapshot(
                        **{
                            field.name: data[f"snapshot__{field.name}"]
                            for field in fields(ActionSnapshot)
                        }
                    ),
                )
            )
        return tuple(values)

    if conn is not None:
        return _read(conn)
    with store._read_connection() as read_conn:
        return _read(read_conn)


__all__ = [
    "get_by_canonical_sha256",
    "get_record",
    "insert_record",
    "latest_coordination_status",
    "latest_cothink_status",
    "latest_disposition",
    "list_records",
    "surfaced_result_rows",
]

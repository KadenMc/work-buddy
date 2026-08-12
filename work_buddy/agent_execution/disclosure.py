"""Durable, run-owned accounting for source-bearing agent disclosures.

The manifest deliberately stores references and integrity metadata, never the
source bytes that crossed a model/provider boundary.  Agent Execution owns the
run and send state; a narrowly injected Sources adapter owns source resolution,
redaction-epoch reservation, and usage acknowledgement.

The write-ahead boundary is intentionally conservative: ``possibly_sent`` is
committed *before* a caller invokes an irreversible transport.  A process crash
therefore blocks automatic replay.  Recovery may reconcile the content-free
record after an operator or transport proves the outcome, but it never sends
the content again itself.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Protocol, TypeVar

from work_buddy.backups.source_foundation_restore import (
    require_source_foundation_writable,
    source_foundation_read_only,
)
from work_buddy.storage.migrations import Migration, MigrationRunner

from .models import is_safe_session_id


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_T = TypeVar("_T")


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _required_text(value: str, field: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str):
        raise DisclosureValidationError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise DisclosureValidationError(
            f"{field} must contain 1-{maximum} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise DisclosureValidationError(f"{field} contains a control character")
    return normalized


def _optional_ref(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _required_digest(value: str, field: str) -> str:
    normalized = _required_text(value, field, maximum=64).lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise DisclosureValidationError(f"{field} must be a lowercase SHA-256 digest")
    return normalized


def _optional_digest(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _required_digest(value, field)


class DisclosureDirection(str, Enum):
    """The boundary crossed by one manifest entry."""

    INBOUND_TO_MODEL = "inbound_to_model"
    OUTBOUND_TO_PROVIDER = "outbound_to_provider"


class DisclosureState(str, Enum):
    """Conservative knowledge about the irreversible send."""

    NOT_SENT = "not_sent"
    POSSIBLY_SENT = "possibly_sent"
    SENT = "sent"


class SourceAcknowledgementState(str, Enum):
    """Whether Sources has recorded the known transport outcome."""

    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"


class DisclosureError(RuntimeError):
    """Base class for content-free disclosure failures."""

    error_code = "disclosure_error"


class DisclosureValidationError(DisclosureError, ValueError):
    error_code = "invalid_disclosure"


class DisclosureIdempotencyConflict(DisclosureError):
    error_code = "disclosure_idempotency_conflict"


class DisclosureRunConflict(DisclosureError):
    error_code = "disclosure_run_conflict"


class DisclosureStateConflict(DisclosureError):
    error_code = "disclosure_state_conflict"


class DisclosureReplayBlocked(DisclosureError):
    error_code = "disclosure_replay_blocked"


class DisclosureSourceError(DisclosureError):
    error_code = "disclosure_source_error"


class DisclosureReservationMismatch(DisclosureError):
    error_code = "disclosure_reservation_mismatch"


@dataclass(frozen=True, slots=True)
class DisclosureSelector:
    """A content-free selector for one bounded source representation.

    Exact quote/prefix/suffix text is intentionally not representable here.
    Callers may use offsets or an opaque selector reference plus its digest.
    """

    kind: str
    unit: str | None = None
    start: int | None = None
    end: int | None = None
    selector_ref: str | None = None
    selector_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _required_text(self.kind, "selector.kind", maximum=64))
        object.__setattr__(self, "unit", _optional_ref(self.unit, "selector.unit"))
        object.__setattr__(
            self,
            "selector_ref",
            _optional_ref(self.selector_ref, "selector.selector_ref"),
        )
        object.__setattr__(
            self,
            "selector_sha256",
            _optional_digest(self.selector_sha256, "selector.selector_sha256"),
        )
        if (self.start is None) != (self.end is None):
            raise DisclosureValidationError(
                "selector.start and selector.end must be provided together"
            )
        if self.start is not None:
            if isinstance(self.start, bool) or isinstance(self.end, bool):
                raise DisclosureValidationError("selector offsets must be integers")
            if not isinstance(self.start, int) or not isinstance(self.end, int):
                raise DisclosureValidationError("selector offsets must be integers")
            if self.start < 0 or self.end <= self.start:
                raise DisclosureValidationError(
                    "selector offsets must describe a non-empty forward range"
                )
            if self.unit is None:
                raise DisclosureValidationError(
                    "selector.unit is required when offsets are present"
                )
        if self.kind == "whole" and (
            self.start is not None
            or self.selector_ref is not None
            or self.selector_sha256 is not None
        ):
            raise DisclosureValidationError(
                "a whole-representation selector cannot include a subrange"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "unit": self.unit,
            "start": self.start,
            "end": self.end,
            "selector_ref": self.selector_ref,
            "selector_sha256": self.selector_sha256,
        }

    @classmethod
    def from_json(cls, value: str) -> "DisclosureSelector":
        data = json.loads(value)
        if not isinstance(data, dict):
            raise DisclosureValidationError("stored selector is invalid")
        return cls(
            kind=data.get("kind"),
            unit=data.get("unit"),
            start=data.get("start"),
            end=data.get("end"),
            selector_ref=data.get("selector_ref"),
            selector_sha256=data.get("selector_sha256"),
        )


@dataclass(frozen=True, slots=True)
class DisclosurePreflight:
    """Immutable metadata for one prospective source-bearing handoff."""

    run_id: str
    worker_session_id: str
    tool_call_id: str
    idempotency_key: str
    direction: DisclosureDirection
    source_ref: str
    representation_id: str
    selector: DisclosureSelector
    content_sha256: str
    byte_length: int
    recipient: str
    provider_id: str
    model_id: str
    authorization_ref: str
    purpose: str
    derivation_ref: str | None = None
    input_manifest_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.selector, DisclosureSelector):
            raise DisclosureValidationError(
                "selector must be a content-free DisclosureSelector"
            )
        if not isinstance(self.direction, DisclosureDirection):
            try:
                object.__setattr__(self, "direction", DisclosureDirection(self.direction))
            except (TypeError, ValueError) as exc:
                raise DisclosureValidationError("direction is invalid") from exc
        for field, maximum in (
            ("run_id", 256),
            ("worker_session_id", 256),
            ("tool_call_id", 256),
            ("idempotency_key", 256),
            ("source_ref", 1024),
            ("representation_id", 512),
            ("recipient", 512),
            ("provider_id", 256),
            ("model_id", 256),
            ("authorization_ref", 512),
            ("purpose", 128),
        ):
            object.__setattr__(
                self,
                field,
                _required_text(getattr(self, field), field, maximum=maximum),
            )
        if not is_safe_session_id(self.run_id):
            raise DisclosureValidationError("run_id is not a safe execution identity")
        if not is_safe_session_id(self.worker_session_id):
            raise DisclosureValidationError(
                "worker_session_id is not a safe execution identity"
            )
        object.__setattr__(
            self,
            "content_sha256",
            _required_digest(self.content_sha256, "content_sha256"),
        )
        object.__setattr__(
            self,
            "derivation_ref",
            _optional_ref(self.derivation_ref, "derivation_ref"),
        )
        object.__setattr__(
            self,
            "input_manifest_sha256",
            _optional_digest(
                self.input_manifest_sha256,
                "input_manifest_sha256",
            ),
        )
        if isinstance(self.byte_length, bool) or not isinstance(self.byte_length, int):
            raise DisclosureValidationError("byte_length must be an integer")
        if self.byte_length < 0:
            raise DisclosureValidationError("byte_length must not be negative")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "worker_session_id": self.worker_session_id,
            "tool_call_id": self.tool_call_id,
            "idempotency_key": self.idempotency_key,
            "direction": self.direction.value,
            "source_ref": self.source_ref,
            "representation_id": self.representation_id,
            "selector": self.selector.to_dict(),
            "content_sha256": self.content_sha256,
            "byte_length": self.byte_length,
            "recipient": self.recipient,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "authorization_ref": self.authorization_ref,
            "purpose": self.purpose,
            "derivation_ref": self.derivation_ref,
            "input_manifest_sha256": self.input_manifest_sha256,
        }

    @property
    def request_sha256(self) -> str:
        return _sha256_json(self.fingerprint_payload())


@dataclass(frozen=True, slots=True)
class SourceDisclosureReservation:
    """Content-free Sources result proving an exact reserved resolution."""

    reservation_id: str
    redaction_epoch: int
    content_sha256: str
    byte_length: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reservation_id",
            _required_text(self.reservation_id, "reservation_id"),
        )
        object.__setattr__(
            self,
            "content_sha256",
            _required_digest(self.content_sha256, "reservation.content_sha256"),
        )
        if isinstance(self.redaction_epoch, bool) or not isinstance(
            self.redaction_epoch, int
        ):
            raise DisclosureValidationError("redaction_epoch must be an integer")
        if self.redaction_epoch < 0:
            raise DisclosureValidationError("redaction_epoch must not be negative")
        if isinstance(self.byte_length, bool) or not isinstance(self.byte_length, int):
            raise DisclosureValidationError(
                "reservation.byte_length must be an integer"
            )
        if self.byte_length < 0:
            raise DisclosureValidationError(
                "reservation.byte_length must not be negative"
            )


class SourcesDisclosureAdapter(Protocol):
    """The only Sources operations Agent Execution needs for disclosure."""

    def reserve_disclosure(
        self,
        preflight: DisclosurePreflight,
        *,
        reservation_idempotency_key: str,
    ) -> SourceDisclosureReservation:
        """Resolve the exact source boundary and reserve its redaction epoch."""

    def acknowledge_disclosure(
        self,
        *,
        reservation_id: str,
        manifest_entry_id: str,
        outcome: DisclosureState,
        acknowledgement_idempotency_key: str,
    ) -> None:
        """Idempotently finalize the source-side usage outcome."""


@dataclass(frozen=True, slots=True)
class DisclosureRun:
    """Minimal durable owner record for one Agent Execution manifest."""

    run_id: str
    worker_session_id: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "worker_session_id": self.worker_session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class DisclosureEntry:
    id: str
    run_id: str
    worker_session_id: str
    sequence_no: int
    tool_call_id: str
    idempotency_key: str
    request_sha256: str
    direction: DisclosureDirection
    source_ref: str
    representation_id: str
    selector: DisclosureSelector
    content_sha256: str
    byte_length: int
    recipient: str
    provider_id: str
    model_id: str
    authorization_ref: str
    purpose: str
    reservation_id: str
    redaction_epoch: int
    derivation_ref: str | None
    input_manifest_sha256: str | None
    state: DisclosureState
    send_attempted: bool
    source_acknowledgement: SourceAcknowledgementState
    source_ack_error_code: str | None
    created_at: str
    possibly_sent_at: str | None
    sent_at: str | None
    reconciled_at: str | None

    def to_dict(self) -> dict[str, object]:
        """Return the complete content-free public projection."""

        return {
            "id": self.id,
            "run_id": self.run_id,
            "worker_session_id": self.worker_session_id,
            "sequence_no": self.sequence_no,
            "tool_call_id": self.tool_call_id,
            "idempotency_key": self.idempotency_key,
            "request_sha256": self.request_sha256,
            "direction": self.direction.value,
            "source_ref": self.source_ref,
            "representation_id": self.representation_id,
            "selector": self.selector.to_dict(),
            "content_sha256": self.content_sha256,
            "byte_length": self.byte_length,
            "recipient": self.recipient,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "authorization_ref": self.authorization_ref,
            "purpose": self.purpose,
            "reservation_id": self.reservation_id,
            "redaction_epoch": self.redaction_epoch,
            "derivation_ref": self.derivation_ref,
            "input_manifest_sha256": self.input_manifest_sha256,
            "state": self.state.value,
            "send_attempted": self.send_attempted,
            "source_acknowledgement": self.source_acknowledgement.value,
            "source_ack_error_code": self.source_ack_error_code,
            "created_at": self.created_at,
            "possibly_sent_at": self.possibly_sent_at,
            "sent_at": self.sent_at,
            "reconciled_at": self.reconciled_at,
        }


@dataclass(frozen=True, slots=True)
class ManifestDigest:
    run_id: str
    manifest_sha256: str
    entry_count: int
    through_sequence: int
    direction: DisclosureDirection | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "manifest_sha256": self.manifest_sha256,
            "entry_count": self.entry_count,
            "through_sequence": self.through_sequence,
            "direction": self.direction.value if self.direction else None,
        }


@dataclass(frozen=True, slots=True)
class OutputManifestBinding:
    id: str
    run_id: str
    output_ref: str
    idempotency_key: str
    manifest_sha256: str
    entry_count: int
    through_sequence: int
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "output_ref": self.output_ref,
            "idempotency_key": self.idempotency_key,
            "manifest_sha256": self.manifest_sha256,
            "entry_count": self.entry_count,
            "through_sequence": self.through_sequence,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class RecoveryItem:
    entry: DisclosureEntry
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {"reason": self.reason, "entry": self.entry.to_dict()}


def _m001_disclosure_manifest(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_execution_disclosure_runs (
            run_id              TEXT PRIMARY KEY,
            worker_session_id   TEXT NOT NULL,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_execution_disclosure_entries (
            id                       TEXT PRIMARY KEY,
            run_id                   TEXT NOT NULL,
            worker_session_id        TEXT NOT NULL,
            sequence_no              INTEGER NOT NULL,
            tool_call_id             TEXT NOT NULL,
            idempotency_key          TEXT NOT NULL,
            request_sha256           TEXT NOT NULL,
            direction                TEXT NOT NULL CHECK (
                direction IN ('inbound_to_model', 'outbound_to_provider')
            ),
            source_ref               TEXT NOT NULL,
            representation_id        TEXT NOT NULL,
            selector_json            TEXT NOT NULL,
            content_sha256           TEXT NOT NULL,
            byte_length              INTEGER NOT NULL CHECK (byte_length >= 0),
            recipient                TEXT NOT NULL,
            provider_id              TEXT NOT NULL,
            model_id                 TEXT NOT NULL,
            authorization_ref        TEXT NOT NULL,
            purpose                  TEXT NOT NULL,
            reservation_id           TEXT NOT NULL,
            redaction_epoch          INTEGER NOT NULL CHECK (redaction_epoch >= 0),
            derivation_ref           TEXT,
            input_manifest_sha256    TEXT,
            state                    TEXT NOT NULL CHECK (
                state IN ('not_sent', 'possibly_sent', 'sent')
            ),
            send_attempted           INTEGER NOT NULL DEFAULT 0 CHECK (
                send_attempted IN (0, 1)
            ),
            source_acknowledgement   TEXT NOT NULL CHECK (
                source_acknowledgement IN ('pending', 'acknowledged')
            ),
            source_ack_error_code    TEXT,
            created_at               TEXT NOT NULL,
            possibly_sent_at         TEXT,
            sent_at                  TEXT,
            reconciled_at            TEXT,
            FOREIGN KEY (run_id) REFERENCES agent_execution_disclosure_runs(run_id),
            UNIQUE (run_id, sequence_no),
            UNIQUE (run_id, idempotency_key)
        );

        CREATE INDEX IF NOT EXISTS idx_agent_execution_disclosure_recovery
            ON agent_execution_disclosure_entries(
                state, send_attempted, source_acknowledgement, created_at
            );
        CREATE INDEX IF NOT EXISTS idx_agent_execution_disclosure_tool_call
            ON agent_execution_disclosure_entries(run_id, tool_call_id);

        CREATE TABLE IF NOT EXISTS agent_execution_disclosure_outputs (
            id                    TEXT PRIMARY KEY,
            run_id                TEXT NOT NULL,
            output_ref            TEXT NOT NULL,
            idempotency_key       TEXT NOT NULL,
            request_sha256        TEXT NOT NULL,
            manifest_sha256       TEXT NOT NULL,
            entry_count           INTEGER NOT NULL CHECK (entry_count >= 0),
            through_sequence      INTEGER NOT NULL CHECK (through_sequence >= 0),
            created_at            TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES agent_execution_disclosure_runs(run_id),
            UNIQUE (run_id, output_ref),
            UNIQUE (run_id, idempotency_key)
        );
        """
    )


_MIGRATIONS = MigrationRunner(
    "agent_execution_disclosure",
    [Migration(1, "run-owned directional source disclosure manifest", _m001_disclosure_manifest)],
)


class DisclosureManifestStore:
    """SQLite authority for ordered run manifests and output bindings."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        if source_foundation_read_only():
            if not self.db_path.is_file():
                require_source_foundation_writable("agent_execution.initialize")
            conn = sqlite3.connect(
                f"file:{self.db_path.resolve()}?mode=ro", uri=True
            )
            try:
                if (
                    conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                    or int(conn.execute("PRAGMA user_version").fetchone()[0])
                    != _MIGRATIONS.target_version
                ):
                    raise DisclosureValidationError(
                        "agent execution state is invalid during restore reconciliation"
                    )
            finally:
                conn.close()
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as conn:
                _MIGRATIONS.run(conn)

    def _connect(self) -> sqlite3.Connection:
        read_only = source_foundation_read_only()
        conn = sqlite3.connect(
            (
                f"file:{self.db_path.resolve()}?mode=ro"
                if read_only
                else str(self.db_path)
            ),
            timeout=10,
            uri=read_only,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if read_only:
            conn.execute("PRAGMA query_only = ON")
        else:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _run(row: sqlite3.Row) -> DisclosureRun:
        return DisclosureRun(
            run_id=row["run_id"],
            worker_session_id=row["worker_session_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _entry(row: sqlite3.Row) -> DisclosureEntry:
        return DisclosureEntry(
            id=row["id"],
            run_id=row["run_id"],
            worker_session_id=row["worker_session_id"],
            sequence_no=int(row["sequence_no"]),
            tool_call_id=row["tool_call_id"],
            idempotency_key=row["idempotency_key"],
            request_sha256=row["request_sha256"],
            direction=DisclosureDirection(row["direction"]),
            source_ref=row["source_ref"],
            representation_id=row["representation_id"],
            selector=DisclosureSelector.from_json(row["selector_json"]),
            content_sha256=row["content_sha256"],
            byte_length=int(row["byte_length"]),
            recipient=row["recipient"],
            provider_id=row["provider_id"],
            model_id=row["model_id"],
            authorization_ref=row["authorization_ref"],
            purpose=row["purpose"],
            reservation_id=row["reservation_id"],
            redaction_epoch=int(row["redaction_epoch"]),
            derivation_ref=row["derivation_ref"],
            input_manifest_sha256=row["input_manifest_sha256"],
            state=DisclosureState(row["state"]),
            send_attempted=bool(row["send_attempted"]),
            source_acknowledgement=SourceAcknowledgementState(
                row["source_acknowledgement"]
            ),
            source_ack_error_code=row["source_ack_error_code"],
            created_at=row["created_at"],
            possibly_sent_at=row["possibly_sent_at"],
            sent_at=row["sent_at"],
            reconciled_at=row["reconciled_at"],
        )

    @staticmethod
    def _output(row: sqlite3.Row) -> OutputManifestBinding:
        return OutputManifestBinding(
            id=row["id"],
            run_id=row["run_id"],
            output_ref=row["output_ref"],
            idempotency_key=row["idempotency_key"],
            manifest_sha256=row["manifest_sha256"],
            entry_count=int(row["entry_count"]),
            through_sequence=int(row["through_sequence"]),
            created_at=row["created_at"],
        )

    def create_run(self, *, run_id: str, worker_session_id: str) -> DisclosureRun:
        require_source_foundation_writable("agent_execution.create_run")
        run_id = _required_text(run_id, "run_id", maximum=256)
        worker_session_id = _required_text(
            worker_session_id,
            "worker_session_id",
            maximum=256,
        )
        if not is_safe_session_id(run_id) or not is_safe_session_id(worker_session_id):
            raise DisclosureValidationError(
                "run and worker session must be safe execution identities"
            )
        now = _now_utc()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM agent_execution_disclosure_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is not None:
                    run = self._run(row)
                    if run.worker_session_id != worker_session_id:
                        raise DisclosureRunConflict(
                            "run_id is already bound to another worker session"
                        )
                    conn.execute("COMMIT")
                    return run
                conn.execute(
                    "INSERT INTO agent_execution_disclosure_runs "
                    "(run_id, worker_session_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (run_id, worker_session_id, now, now),
                )
                row = conn.execute(
                    "SELECT * FROM agent_execution_disclosure_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        assert row is not None
        return self._run(row)

    def get_run(self, run_id: str) -> DisclosureRun | None:
        run_id = _required_text(run_id, "run_id", maximum=256)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_execution_disclosure_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._run(row) if row is not None else None

    def find_idempotent(
        self,
        *,
        run_id: str,
        idempotency_key: str,
        request_sha256: str,
    ) -> DisclosureEntry | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_execution_disclosure_entries "
                "WHERE run_id = ? AND idempotency_key = ?",
                (run_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        entry = self._entry(row)
        if entry.request_sha256 != request_sha256:
            raise DisclosureIdempotencyConflict(
                "idempotency_key was already used for another disclosure"
            )
        return entry

    def get_by_idempotency(
        self,
        *,
        run_id: str,
        idempotency_key: str,
    ) -> DisclosureEntry | None:
        """Look up an existing entry before recomputing dynamic derivation state."""

        run_id = _required_text(run_id, "run_id", maximum=256)
        idempotency_key = _required_text(
            idempotency_key,
            "idempotency_key",
            maximum=256,
        )
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_execution_disclosure_entries "
                "WHERE run_id = ? AND idempotency_key = ?",
                (run_id, idempotency_key),
            ).fetchone()
        return self._entry(row) if row is not None else None

    def insert_preflight(
        self,
        preflight: DisclosurePreflight,
        reservation: SourceDisclosureReservation,
    ) -> DisclosureEntry:
        require_source_foundation_writable("agent_execution.reserve_disclosure")
        now = _now_utc()
        entry_id = uuid.uuid4().hex
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM agent_execution_disclosure_entries "
                    "WHERE run_id = ? AND idempotency_key = ?",
                    (preflight.run_id, preflight.idempotency_key),
                ).fetchone()
                if existing is not None:
                    entry = self._entry(existing)
                    if entry.request_sha256 != preflight.request_sha256:
                        raise DisclosureIdempotencyConflict(
                            "idempotency_key was already used for another disclosure"
                        )
                    conn.execute("COMMIT")
                    return entry

                run = conn.execute(
                    "SELECT worker_session_id FROM agent_execution_disclosure_runs "
                    "WHERE run_id = ?",
                    (preflight.run_id,),
                ).fetchone()
                if run is not None and run["worker_session_id"] != preflight.worker_session_id:
                    raise DisclosureRunConflict(
                        "run_id is already bound to another worker session"
                    )
                if run is None:
                    conn.execute(
                        "INSERT INTO agent_execution_disclosure_runs "
                        "(run_id, worker_session_id, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (preflight.run_id, preflight.worker_session_id, now, now),
                    )

                sequence_no = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(sequence_no), 0) + 1 "
                        "FROM agent_execution_disclosure_entries WHERE run_id = ?",
                        (preflight.run_id,),
                    ).fetchone()[0]
                )
                conn.execute(
                    """
                    INSERT INTO agent_execution_disclosure_entries (
                        id, run_id, worker_session_id, sequence_no,
                        tool_call_id, idempotency_key, request_sha256, direction,
                        source_ref, representation_id, selector_json,
                        content_sha256, byte_length, recipient, provider_id,
                        model_id, authorization_ref, purpose, reservation_id,
                        redaction_epoch, derivation_ref, input_manifest_sha256,
                        state, send_attempted, source_acknowledgement,
                        created_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, 'not_sent', 0, 'pending', ?
                    )
                    """,
                    (
                        entry_id,
                        preflight.run_id,
                        preflight.worker_session_id,
                        sequence_no,
                        preflight.tool_call_id,
                        preflight.idempotency_key,
                        preflight.request_sha256,
                        preflight.direction.value,
                        preflight.source_ref,
                        preflight.representation_id,
                        _canonical_json(preflight.selector.to_dict()),
                        preflight.content_sha256,
                        preflight.byte_length,
                        preflight.recipient,
                        preflight.provider_id,
                        preflight.model_id,
                        preflight.authorization_ref,
                        preflight.purpose,
                        reservation.reservation_id,
                        reservation.redaction_epoch,
                        preflight.derivation_ref,
                        preflight.input_manifest_sha256,
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE agent_execution_disclosure_runs SET updated_at = ? "
                    "WHERE run_id = ?",
                    (now, preflight.run_id),
                )
                row = conn.execute(
                    "SELECT * FROM agent_execution_disclosure_entries WHERE id = ?",
                    (entry_id,),
                ).fetchone()
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        assert row is not None
        return self._entry(row)

    def get_entry(self, entry_id: str) -> DisclosureEntry:
        entry_id = _required_text(entry_id, "entry_id")
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_execution_disclosure_entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
        if row is None:
            raise DisclosureValidationError("disclosure entry was not found")
        return self._entry(row)

    def list_entries(self, run_id: str) -> tuple[DisclosureEntry, ...]:
        run_id = _required_text(run_id, "run_id", maximum=256)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_execution_disclosure_entries "
                "WHERE run_id = ? ORDER BY sequence_no",
                (run_id,),
            ).fetchall()
        return tuple(self._entry(row) for row in rows)

    def begin_send(self, entry_id: str) -> DisclosureEntry:
        """Write ``possibly_sent`` and grant exactly one transport attempt."""

        require_source_foundation_writable("agent_execution.begin_send")

        now = _now_utc()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM agent_execution_disclosure_entries WHERE id = ?",
                    (entry_id,),
                ).fetchone()
                if row is None:
                    raise DisclosureValidationError("disclosure entry was not found")
                entry = self._entry(row)
                if entry.send_attempted:
                    raise DisclosureReplayBlocked(
                        "this disclosure already crossed its write-ahead boundary"
                    )
                if entry.state is not DisclosureState.NOT_SENT:
                    raise DisclosureStateConflict(
                        "only a not_sent disclosure can begin a send"
                    )
                conn.execute(
                    "UPDATE agent_execution_disclosure_entries SET "
                    "state = 'possibly_sent', send_attempted = 1, "
                    "possibly_sent_at = ? WHERE id = ?",
                    (now, entry_id),
                )
                updated = conn.execute(
                    "SELECT * FROM agent_execution_disclosure_entries WHERE id = ?",
                    (entry_id,),
                ).fetchone()
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        assert updated is not None
        return self._entry(updated)

    def mark_sent(self, entry_id: str, *, reconciled: bool = False) -> DisclosureEntry:
        require_source_foundation_writable("agent_execution.mark_sent")
        now = _now_utc()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM agent_execution_disclosure_entries WHERE id = ?",
                    (entry_id,),
                ).fetchone()
                if row is None:
                    raise DisclosureValidationError("disclosure entry was not found")
                entry = self._entry(row)
                if entry.state is DisclosureState.SENT:
                    conn.execute("COMMIT")
                    return entry
                if (
                    entry.state is not DisclosureState.POSSIBLY_SENT
                    or not entry.send_attempted
                ):
                    raise DisclosureStateConflict(
                        "only a write-ahead possibly_sent disclosure can become sent"
                    )
                conn.execute(
                    "UPDATE agent_execution_disclosure_entries SET "
                    "state = 'sent', sent_at = ?, reconciled_at = ? WHERE id = ?",
                    (now, now if reconciled else None, entry_id),
                )
                updated = conn.execute(
                    "SELECT * FROM agent_execution_disclosure_entries WHERE id = ?",
                    (entry_id,),
                ).fetchone()
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        assert updated is not None
        return self._entry(updated)

    def reconcile_not_sent(self, entry_id: str) -> DisclosureEntry:
        require_source_foundation_writable("agent_execution.reconcile_not_sent")
        now = _now_utc()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM agent_execution_disclosure_entries WHERE id = ?",
                    (entry_id,),
                ).fetchone()
                if row is None:
                    raise DisclosureValidationError("disclosure entry was not found")
                entry = self._entry(row)
                if (
                    entry.state is DisclosureState.NOT_SENT
                    and entry.send_attempted
                    and entry.reconciled_at is not None
                ):
                    conn.execute("COMMIT")
                    return entry
                if (
                    entry.state is not DisclosureState.POSSIBLY_SENT
                    or not entry.send_attempted
                ):
                    raise DisclosureStateConflict(
                        "only an ambiguous possibly_sent disclosure can be proven not sent"
                    )
                conn.execute(
                    "UPDATE agent_execution_disclosure_entries SET "
                    "state = 'not_sent', reconciled_at = ? WHERE id = ?",
                    (now, entry_id),
                )
                updated = conn.execute(
                    "SELECT * FROM agent_execution_disclosure_entries WHERE id = ?",
                    (entry_id,),
                ).fetchone()
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        assert updated is not None
        return self._entry(updated)

    def mark_acknowledged(self, entry_id: str) -> DisclosureEntry:
        require_source_foundation_writable("agent_execution.acknowledge")
        with self._connection() as conn:
            conn.execute(
                "UPDATE agent_execution_disclosure_entries SET "
                "source_acknowledgement = 'acknowledged', "
                "source_ack_error_code = NULL WHERE id = ?",
                (entry_id,),
            )
        return self.get_entry(entry_id)

    def mark_ack_failed(self, entry_id: str) -> DisclosureEntry:
        require_source_foundation_writable("agent_execution.acknowledgement_failed")
        with self._connection() as conn:
            conn.execute(
                "UPDATE agent_execution_disclosure_entries SET "
                "source_acknowledgement = 'pending', "
                "source_ack_error_code = 'source_ack_failed' WHERE id = ?",
                (entry_id,),
            )
        return self.get_entry(entry_id)

    @staticmethod
    def _manifest_item(entry: DisclosureEntry) -> dict[str, object]:
        # State and acknowledgement are deliberately excluded: the digest binds
        # immutable source/recipient intent and remains stable when a successful
        # transport is acknowledged after the output is produced.
        return {
            "sequence_no": entry.sequence_no,
            "direction": entry.direction.value,
            "worker_session_id": entry.worker_session_id,
            "tool_call_id": entry.tool_call_id,
            "source_ref": entry.source_ref,
            "representation_id": entry.representation_id,
            "selector": entry.selector.to_dict(),
            "content_sha256": entry.content_sha256,
            "byte_length": entry.byte_length,
            "recipient": entry.recipient,
            "provider_id": entry.provider_id,
            "model_id": entry.model_id,
            "authorization_ref": entry.authorization_ref,
            "purpose": entry.purpose,
            "reservation_id": entry.reservation_id,
            "redaction_epoch": entry.redaction_epoch,
            "derivation_ref": entry.derivation_ref,
            "input_manifest_sha256": entry.input_manifest_sha256,
        }

    def manifest_digest(
        self,
        run_id: str,
        *,
        direction: DisclosureDirection | None = None,
        through_sequence: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> ManifestDigest:
        run_id = _required_text(run_id, "run_id", maximum=256)
        if direction is not None and not isinstance(direction, DisclosureDirection):
            direction = DisclosureDirection(direction)
        if through_sequence is not None and through_sequence < 0:
            raise DisclosureValidationError("through_sequence must not be negative")
        owned = conn is None
        active = conn or self._connect()
        try:
            clauses = ["run_id = ?", "state IN ('possibly_sent', 'sent')"]
            params: list[object] = [run_id]
            if direction is not None:
                clauses.append("direction = ?")
                params.append(direction.value)
            if through_sequence is not None:
                clauses.append("sequence_no <= ?")
                params.append(through_sequence)
            rows = active.execute(
                "SELECT * FROM agent_execution_disclosure_entries WHERE "
                + " AND ".join(clauses)
                + " ORDER BY sequence_no",
                params,
            ).fetchall()
            entries = [self._entry(row) for row in rows]
            payload = {
                "schema": "agent-execution-disclosure-manifest/v1",
                "run_id": run_id,
                "direction": direction.value if direction else None,
                "entries": [self._manifest_item(entry) for entry in entries],
            }
            return ManifestDigest(
                run_id=run_id,
                manifest_sha256=_sha256_json(payload),
                entry_count=len(entries),
                through_sequence=max((entry.sequence_no for entry in entries), default=0),
                direction=direction,
            )
        finally:
            if owned:
                active.close()

    def input_manifest_digest(self, run_id: str) -> ManifestDigest:
        return self.manifest_digest(
            run_id,
            direction=DisclosureDirection.INBOUND_TO_MODEL,
        )

    def bind_output_manifest(
        self,
        *,
        run_id: str,
        output_ref: str,
        idempotency_key: str,
    ) -> OutputManifestBinding:
        require_source_foundation_writable("agent_execution.bind_output")
        run_id = _required_text(run_id, "run_id", maximum=256)
        output_ref = _required_text(output_ref, "output_ref", maximum=512)
        idempotency_key = _required_text(
            idempotency_key,
            "idempotency_key",
            maximum=256,
        )
        request_sha256 = _sha256_json(
            {
                "run_id": run_id,
                "output_ref": output_ref,
                "idempotency_key": idempotency_key,
            }
        )
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM agent_execution_disclosure_outputs "
                    "WHERE run_id = ? AND (idempotency_key = ? OR output_ref = ?)",
                    (run_id, idempotency_key, output_ref),
                ).fetchone()
                if existing is not None:
                    if existing["request_sha256"] != request_sha256:
                        raise DisclosureIdempotencyConflict(
                            "output binding identity was already used differently"
                        )
                    binding = self._output(existing)
                    conn.execute("COMMIT")
                    return binding
                run = conn.execute(
                    "SELECT 1 FROM agent_execution_disclosure_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if run is None:
                    raise DisclosureValidationError("disclosure run was not found")
                digest = self.manifest_digest(run_id, conn=conn)
                binding_id = uuid.uuid4().hex
                created_at = _now_utc()
                conn.execute(
                    "INSERT INTO agent_execution_disclosure_outputs "
                    "(id, run_id, output_ref, idempotency_key, request_sha256, "
                    "manifest_sha256, entry_count, through_sequence, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        binding_id,
                        run_id,
                        output_ref,
                        idempotency_key,
                        request_sha256,
                        digest.manifest_sha256,
                        digest.entry_count,
                        digest.through_sequence,
                        created_at,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM agent_execution_disclosure_outputs WHERE id = ?",
                    (binding_id,),
                ).fetchone()
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        assert row is not None
        return self._output(row)

    def list_recovery(self, *, run_id: str | None = None) -> tuple[RecoveryItem, ...]:
        params: list[object] = []
        run_clause = ""
        if run_id is not None:
            run_id = _required_text(run_id, "run_id", maximum=256)
            run_clause = "AND run_id = ?"
            params.append(run_id)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_execution_disclosure_entries WHERE "
                "send_attempted = 1 AND (state = 'possibly_sent' OR "
                "source_acknowledgement = 'pending') "
                f"{run_clause} ORDER BY created_at, run_id, sequence_no",
                params,
            ).fetchall()
        items: list[RecoveryItem] = []
        for row in rows:
            entry = self._entry(row)
            reason = (
                "ambiguous_send"
                if entry.state is DisclosureState.POSSIBLY_SENT
                else "source_ack_pending"
            )
            items.append(RecoveryItem(entry=entry, reason=reason))
        return tuple(items)


class DisclosureGateway:
    """Coordinates Sources reservations with Agent Execution send state."""

    def __init__(
        self,
        store: DisclosureManifestStore,
        sources: SourcesDisclosureAdapter,
    ) -> None:
        self.store = store
        self.sources = sources

    def preflight(self, preflight: DisclosurePreflight) -> DisclosureEntry:
        if (
            preflight.direction is DisclosureDirection.OUTBOUND_TO_PROVIDER
            and preflight.input_manifest_sha256 is None
        ):
            raise DisclosureValidationError(
                "outbound_to_provider requires input_manifest_sha256"
            )
        existing = self.store.find_idempotent(
            run_id=preflight.run_id,
            idempotency_key=preflight.idempotency_key,
            request_sha256=preflight.request_sha256,
        )
        if existing is not None:
            return existing
        try:
            reservation = self.sources.reserve_disclosure(
                preflight,
                reservation_idempotency_key=(
                    f"agent-disclosure-reserve:{preflight.run_id}:"
                    f"{preflight.idempotency_key}"
                ),
            )
        except DisclosureError:
            raise
        except Exception as exc:
            raise DisclosureSourceError(
                "Sources could not reserve this disclosure"
            ) from exc
        if (
            reservation.content_sha256 != preflight.content_sha256
            or reservation.byte_length != preflight.byte_length
        ):
            raise DisclosureReservationMismatch(
                "Sources resolved a different content boundary"
            )
        return self.store.insert_preflight(preflight, reservation)

    def mark_possibly_sent(self, entry_id: str) -> DisclosureEntry:
        return self.store.begin_send(entry_id)

    def _acknowledge(self, entry: DisclosureEntry) -> DisclosureEntry:
        try:
            self.sources.acknowledge_disclosure(
                reservation_id=entry.reservation_id,
                manifest_entry_id=entry.id,
                outcome=entry.state,
                acknowledgement_idempotency_key=(
                    f"agent-disclosure-ack:{entry.id}:{entry.state.value}"
                ),
            )
        except Exception as exc:
            self.store.mark_ack_failed(entry.id)
            raise DisclosureSourceError(
                "Sources could not acknowledge this disclosure outcome"
            ) from exc
        return self.store.mark_acknowledged(entry.id)

    def mark_sent(self, entry_id: str) -> DisclosureEntry:
        return self._acknowledge(self.store.mark_sent(entry_id))

    def reconcile(
        self,
        entry_id: str,
        *,
        proven_outcome: DisclosureState,
    ) -> DisclosureEntry:
        """Record a transport-proven outcome without replaying source bytes."""

        if not isinstance(proven_outcome, DisclosureState):
            try:
                proven_outcome = DisclosureState(proven_outcome)
            except (TypeError, ValueError) as exc:
                raise DisclosureValidationError("proven_outcome is invalid") from exc
        if proven_outcome is DisclosureState.POSSIBLY_SENT:
            raise DisclosureValidationError(
                "reconciliation requires a proven sent or not_sent outcome"
            )
        entry = self.store.get_entry(entry_id)
        if entry.state is DisclosureState.POSSIBLY_SENT:
            if proven_outcome is DisclosureState.SENT:
                entry = self.store.mark_sent(entry_id, reconciled=True)
            else:
                entry = self.store.reconcile_not_sent(entry_id)
        elif entry.state is not proven_outcome:
            raise DisclosureStateConflict(
                "the recorded disclosure outcome conflicts with reconciliation"
            )
        if entry.source_acknowledgement is SourceAcknowledgementState.ACKNOWLEDGED:
            return entry
        return self._acknowledge(entry)

    def reconcile_acknowledgement(self, entry_id: str) -> DisclosureEntry:
        """Retry only a Sources acknowledgement; never retry the transport."""

        entry = self.store.get_entry(entry_id)
        if entry.source_acknowledgement is SourceAcknowledgementState.ACKNOWLEDGED:
            return entry
        if entry.state is DisclosureState.POSSIBLY_SENT:
            raise DisclosureStateConflict(
                "an ambiguous send must be reconciled before acknowledgement"
            )
        if not entry.send_attempted:
            raise DisclosureStateConflict(
                "a disclosure with no send attempt has no outcome to acknowledge"
            )
        return self._acknowledge(entry)

    def execute_handoff(
        self,
        preflight: DisclosurePreflight,
        handoff: Callable[[], _T],
    ) -> tuple[_T, DisclosureEntry]:
        """Execute one irreversible handoff behind the write-ahead boundary.

        ``handoff`` closes over the separately resolved bytes.  Neither this
        method nor the manifest accepts or persists those bytes.
        """

        entry = self.preflight(preflight)
        self.mark_possibly_sent(entry.id)
        result = handoff()
        return result, self.mark_sent(entry.id)


class SourceBoundRun:
    """Reusable one-run facade for source-aware processors.

    Journal smart processing, Truth analysis, and other domain processors can
    share this API without taking ownership of Agent Execution's SQLite state.
    Construction creates (or idempotently reopens) the run-owned manifest.
    ``prepare_inbound_handoff`` resolves/reserves exactly one SourceRef and
    persists ``possibly_sent``; the caller then performs its model handoff and
    calls ``mark_sent``.  No method accepts source content.
    """

    def __init__(
        self,
        gateway: DisclosureGateway,
        *,
        run_id: str,
        worker_session_id: str,
        recipient: str,
        provider_id: str,
        model_id: str,
        authorization_ref: str,
        purpose: str,
    ) -> None:
        self.gateway = gateway
        self.run_id = _required_text(run_id, "run_id", maximum=256)
        self.worker_session_id = _required_text(
            worker_session_id,
            "worker_session_id",
            maximum=256,
        )
        self.recipient = _required_text(recipient, "recipient")
        self.provider_id = _required_text(provider_id, "provider_id", maximum=256)
        self.model_id = _required_text(model_id, "model_id", maximum=256)
        self.authorization_ref = _required_text(
            authorization_ref,
            "authorization_ref",
        )
        self.purpose = _required_text(purpose, "purpose", maximum=128)
        self.manifest = gateway.store.create_run(
            run_id=self.run_id,
            worker_session_id=self.worker_session_id,
        )

    def reserve_inbound_source(
        self,
        *,
        tool_call_id: str,
        idempotency_key: str,
        source_ref: str,
        representation_id: str,
        selector: DisclosureSelector,
        content_sha256: str,
        byte_length: int,
        derivation_ref: str | None = None,
    ) -> DisclosureEntry:
        return self.gateway.preflight(
            DisclosurePreflight(
                run_id=self.run_id,
                worker_session_id=self.worker_session_id,
                tool_call_id=tool_call_id,
                idempotency_key=idempotency_key,
                direction=DisclosureDirection.INBOUND_TO_MODEL,
                source_ref=source_ref,
                representation_id=representation_id,
                selector=selector,
                content_sha256=content_sha256,
                byte_length=byte_length,
                recipient=self.recipient,
                provider_id=self.provider_id,
                model_id=self.model_id,
                authorization_ref=self.authorization_ref,
                purpose=self.purpose,
                derivation_ref=derivation_ref,
            )
        )

    def prepare_inbound_handoff(
        self,
        *,
        tool_call_id: str,
        idempotency_key: str,
        source_ref: str,
        representation_id: str,
        selector: DisclosureSelector,
        content_sha256: str,
        byte_length: int,
        derivation_ref: str | None = None,
    ) -> DisclosureEntry:
        entry = self.reserve_inbound_source(
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
            source_ref=source_ref,
            representation_id=representation_id,
            selector=selector,
            content_sha256=content_sha256,
            byte_length=byte_length,
            derivation_ref=derivation_ref,
        )
        return self.gateway.mark_possibly_sent(entry.id)

    def mark_sent(self, entry_id: str) -> DisclosureEntry:
        return self.gateway.mark_sent(entry_id)

    def execute_resolved_inbound(
        self,
        *,
        tool_call_id: str,
        idempotency_key: str,
        source_ref: str,
        representation_id: str,
        selector: DisclosureSelector,
        content_sha256: str,
        byte_length: int,
        resolve_content: Callable[[], bytes],
        handoff: Callable[[bytes], _T],
        derivation_ref: str | None = None,
    ) -> tuple[_T, DisclosureEntry]:
        """Resolve locally, then release exact verified bytes after write-ahead.

        ``resolve_content`` is the domain's authorized Sources read.  Its bytes
        are integrity-checked in memory and passed directly to ``handoff``;
        neither the manifest store nor any manifest request receives them.
        """

        entry = self.reserve_inbound_source(
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
            source_ref=source_ref,
            representation_id=representation_id,
            selector=selector,
            content_sha256=content_sha256,
            byte_length=byte_length,
            derivation_ref=derivation_ref,
        )
        content = resolve_content()
        if not isinstance(content, bytes):
            raise DisclosureReservationMismatch(
                "the Sources resolver did not return exact bytes"
            )
        if hashlib.sha256(content).hexdigest() != entry.content_sha256 or len(
            content
        ) != entry.byte_length:
            raise DisclosureReservationMismatch(
                "the resolved bytes changed after disclosure reservation"
            )
        self.gateway.mark_possibly_sent(entry.id)
        result = handoff(content)
        return result, self.gateway.mark_sent(entry.id)

    def bind_output(
        self,
        *,
        output_ref: str,
        idempotency_key: str,
    ) -> OutputManifestBinding:
        return self.gateway.store.bind_output_manifest(
            run_id=self.run_id,
            output_ref=output_ref,
            idempotency_key=idempotency_key,
        )

    def digest(self) -> ManifestDigest:
        return self.gateway.store.manifest_digest(self.run_id)


# Small, JSON-shaped wrappers keep MCP/HTTP handlers from reimplementing the
# state machine.  They intentionally require an injected gateway instead of a
# module-global database or Sources singleton.
def preflight_source_disclosure(
    gateway: DisclosureGateway,
    preflight: DisclosurePreflight,
) -> dict[str, object]:
    return gateway.preflight(preflight).to_dict()


def mark_source_disclosure_possibly_sent(
    gateway: DisclosureGateway,
    entry_id: str,
) -> dict[str, object]:
    return gateway.mark_possibly_sent(entry_id).to_dict()


def mark_source_disclosure_sent(
    gateway: DisclosureGateway,
    entry_id: str,
) -> dict[str, object]:
    return gateway.mark_sent(entry_id).to_dict()


def reconcile_source_disclosure(
    gateway: DisclosureGateway,
    entry_id: str,
    *,
    proven_outcome: DisclosureState,
) -> dict[str, object]:
    return gateway.reconcile(
        entry_id,
        proven_outcome=proven_outcome,
    ).to_dict()


def candidate_manifest_digest(
    store: DisclosureManifestStore,
    *,
    run_id: str,
) -> dict[str, object]:
    """Return the complete ordered manifest digest for a candidate/output."""

    return store.manifest_digest(run_id).to_dict()


def create_source_bound_run(
    gateway: DisclosureGateway,
    **kwargs: str,
) -> SourceBoundRun:
    """Gateway-friendly factory used by domain processors."""

    return SourceBoundRun(gateway, **kwargs)


def outbound_preflight_with_current_inputs(
    store: DisclosureManifestStore,
    preflight: DisclosurePreflight,
) -> DisclosurePreflight:
    """Bind an outbound provider call to the run's current inbound manifest."""

    if preflight.direction is not DisclosureDirection.OUTBOUND_TO_PROVIDER:
        raise DisclosureValidationError(
            "only outbound_to_provider may bind the current input manifest"
        )
    digest = store.input_manifest_digest(preflight.run_id)
    return replace(preflight, input_manifest_sha256=digest.manifest_sha256)

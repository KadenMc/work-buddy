"""Invariant-enforcing append layer for one targeted truth store."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from work_buddy.artifacts.io import atomic_write_bytes
from work_buddy.storage.migrations import SchemaVersionTooNew
from work_buddy.truth.anchors import (
    CompositeSelector,
    reanchor,
    serialize_selector,
)
from work_buddy.truth.contracts import (
    Actor,
    GestureError,
    InvariantViolation,
    StorePaths,
    StoreVersionError,
    TERMINAL_STATUSES,
    VALID_STATUSES,
    validate_agent_producer_meta,
)
from work_buddy.truth.fingerprints import compute_target_fingerprint
from work_buddy.truth.identity import (
    canonical_claim_payload,
    canonical_json,
    claim_sha256,
    new_id,
    parse_truth_uri,
    sha256_bytes,
    sha256_text,
    utc_now,
)
from work_buddy.truth.migrations import (
    REDACTED_SELECTOR_JSON,
    current_version,
    migrate,
)
from work_buddy.truth.profiles import (
    StoreProfile,
    dump_profile,
    load_profile,
    normalize_store_id,
    validate_new_claim,
    validate_profile,
)


DEFAULT_INLINE_CONTENT_BYTES = 64 * 1024
SQLITE_TIMEOUT_SECONDS = 10.0
SQLITE_BUSY_TIMEOUT_MS = 10_000

EVIDENCE_KINDS = frozenset(
    {"document", "web", "chat", "utterance", "artifact", "import"}
)
AUTHORSHIP_KINDS = frozenset(
    {
        "user_authored",
        "user_curated",
        "agent_authored",
        "mixed",
        "unattested",
        "external",
        "external_quarantined",
    }
)
SPAN_AUTHOR_KINDS = frozenset({"human", "agent_run", "unknown"})
ACQUISITION_METHODS = frozenset(
    {"fetch", "paste", "import", "said_in_chat", "file_read"}
)
SUPERSESSION_REASONS = frozenset(
    {
        "updated",
        "corrected",
        "refined",
        "valid_time_closed",
        "source_retracted",
        "preference_changed",
    }
)
RESERVED_PRODUCER_KEYS = frozenset(
    {"model", "harness", "surface", "session_id", "call_id"}
)
# Co-work document surface (K2). Proposal lifecycle statuses and the decision
# verb vocabulary recorded in proposal_status_events.decision. The decision set
# is the SHIPPED gesture-kind names plus dismiss (the UI verb that consumes a
# reject_plain gesture on a flag), matching the export v3 decision enum.
PROPOSAL_STATUSES = frozenset({"open", "applied", "closed", "expired"})
PROPOSAL_DECISIONS = frozenset(
    {
        "confirm",
        "edit_confirm",
        "reject_plain",
        "reject_as_false",
        "reject_as_preference",
        "redirect",
        "defer",
        "endorse",
        "dismiss",
    }
)
DOC_EVENT_KINDS = frozenset(
    {
        "registered",
        "imported",
        "materialized",
        "drift_detected",
        "reimported",
        "retired",
        "session_opened",
        "session_closed",
    }
)
DOCUMENT_CLASSES = frozenset({"co_authored", "generated"})
EXPRESSION_ROLES = frozenset({"quote", "paraphrase", "summary", "instantiation"})
LINK_TARGETS: Mapping[str, frozenset[str]] = {
    "supports_span": frozenset({"evidence_span"}),
    "about_entity": frozenset({"entity"}),
    "supersedes": frozenset({"claim"}),
    "conflicts_with": frozenset({"claim"}),
    "refutes": frozenset({"claim"}),
    "cites_external": frozenset({"external_uri"}),
    "relates_to": frozenset({"claim", "entity", "external_uri"}),
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECORD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_BLOB_CLEANUP_DIRNAME = "pending-blob-deletions"
_REDACTION_RECOVERY_DIRNAME = "pending-redaction-recoveries"


class PostCommitHookError(InvariantViolation):
    """A ledger commit succeeded but its rebuildable follow-up failed."""


class AcquisitionOrigin(str, Enum):
    """Explicit acquisition context used to derive durable evidence trust."""

    USER_INPUT = "user_input"
    HUMAN_CURATED = "human_curated"
    AGENT_GENERATED = "agent_generated"
    MIXED_TRANSCRIPT = "mixed_transcript"
    PREEXISTING = "preexisting"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class StoreInfo:
    store_id: str
    profile: str
    schema_version: int
    title: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    id: str
    kind: str
    source_locator: str
    content_sha256: str
    content: str | None
    content_path: str | None
    media_type: str | None
    acquired_at: str
    acquired_by_kind: str
    acquired_by_ref: str | None
    acquisition_method: str
    trust_class: str
    derived_from_store: str | None
    meta_json: str | None
    redacted_at: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class EvidenceSpanRecord:
    id: str
    evidence_id: str
    selector_json: str
    quote_exact: str | None
    span_sha256: str
    author_kind: str | None
    author_ref: str | None
    redacted_at: str | None
    created_at: str
    created_by_kind: str
    created_by_ref: str | None


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    id: str
    proposition: str
    canonical_sha256: str
    claim_kind: str
    structured_json: str | None
    scope: str
    valid_from: str | None
    valid_to: str | None
    confidence_extraction: float | None
    meta_json: str | None
    redacted_at: str | None
    created_at: str
    created_by_kind: str
    created_by_ref: str | None


@dataclass(frozen=True, slots=True)
class ClaimLinkRecord:
    id: str
    from_claim_id: str
    link_type: str
    to_kind: str
    to_ref: str
    role_json: str | None
    target_fingerprint: str | None
    fingerprint_reviewed_at: str | None
    created_at: str
    created_by_kind: str
    created_by_ref: str | None


@dataclass(frozen=True, slots=True)
class LinkRetractionRecord:
    link_id: str
    at: str
    actor_kind: str
    actor_ref: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class PremiseRef:
    kind: str
    ref: str

    def __post_init__(self) -> None:
        if self.kind not in {"local", "uri"}:
            raise ValueError("premise kind must be local or uri")
        if not isinstance(self.ref, str) or not self.ref.strip():
            raise ValueError("premise ref must be a nonempty string")


@dataclass(frozen=True, slots=True)
class DerivationRecord:
    id: str
    claim_id: str
    method: str
    producer_kind: str
    producer_ref: str | None
    confidence: float | None
    rationale: str | None
    created_at: str
    premises: tuple[PremiseRef, ...] = ()


@dataclass(frozen=True, slots=True)
class StatusEventRecord:
    seq: int
    id: str
    claim_id: str
    status: str
    at: str
    actor_kind: str
    actor_ref: str | None
    basis_kind: str
    basis_ref: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class GestureRecord:
    id: str
    at: str
    surface: str
    actor_ref: str
    kind: str
    subject_ref: str
    payload_sha256: str
    payload_excerpt: str
    context_sha256: str | None
    expires_at: str | None
    consumed_at: str | None


@dataclass(frozen=True, slots=True)
class ClaimWriteResult:
    claim: ClaimRecord
    created: bool


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    id: str
    path: str
    title: str | None
    document_class: str
    content_sha256: str
    ydoc_snapshot_sha256: str | None
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    meta_json: str | None


@dataclass(frozen=True, slots=True)
class DocumentSpanRecord:
    id: str
    document_id: str
    selector_json: str
    quote_exact: str | None
    span_sha256: str
    author_kind: str | None
    author_ref: str | None
    created_at: str
    created_by_kind: str
    created_by_ref: str | None


@dataclass(frozen=True, slots=True)
class ExpressionRecord:
    id: str
    document_span_id: str
    claim_ref_kind: str
    claim_ref: str
    role: str
    claim_canonical_sha256: str
    span_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    meta_json: str | None


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    id: str
    document_id: str
    base_content_sha256: str
    selector_json: str
    quote_exact: str | None
    span_sha256: str
    replacement: str | None
    rationale: str | None
    tldr: str | None
    claim_refs_json: str | None
    canonical_sha256: str
    dedup_key: str
    expires_at: str | None
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    meta_json: str | None
    redacted_at: str | None


@dataclass(frozen=True, slots=True)
class ProposalStatusEventRecord:
    seq: int
    id: str
    proposal_id: str
    status: str
    decision: str | None
    at: str
    actor_kind: str
    actor_ref: str | None
    basis_kind: str
    basis_ref: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class DocEventRecord:
    id: str
    document_id: str
    kind: str
    at: str
    actor_kind: str
    actor_ref: str | None
    content_sha256: str | None
    ydoc_snapshot_sha256: str | None
    detail: str | None


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvariantViolation(f"{label} must be a nonempty string")
    return value.strip()


def _json_object(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _valid_digest(value: str, label: str = "SHA-256") -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value.strip().lower()):
        raise InvariantViolation(f"{label} must be a 64-character hexadecimal digest")
    return value.strip().lower()


def _valid_record_id(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise InvariantViolation(f"{label} must be a 32-character hexadecimal id")
    normalized = value.strip().lower()
    if _RECORD_ID_RE.fullmatch(normalized) is None:
        raise InvariantViolation(f"{label} must be a 32-character hexadecimal id")
    return normalized


def _record_id(value: str | None, label: str) -> str:
    return new_id() if value is None else _valid_record_id(value, label)


def _validate_confidence(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvariantViolation(f"{label} must be a number from 0 to 1")
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > 1:
        raise InvariantViolation(f"{label} must be a finite number from 0 to 1")
    return result


def _parse_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise InvariantViolation(f"{label} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvariantViolation(f"{label} must include a UTC offset")
    return parsed


def _timestamp(value: str | None, label: str) -> str:
    result = utc_now() if value is None else value
    _parse_time(result, label)
    return result


def _valid_time(value: str | None, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvariantViolation(f"{label} must be an ISO 8601 date or timestamp")
    normalized = value.strip()
    try:
        if "T" not in normalized and " " not in normalized:
            day = date.fromisoformat(normalized)
            return datetime.combine(day, time.min, tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvariantViolation(
            f"{label} must be an ISO 8601 date or timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvariantViolation(f"{label} timestamps must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _source_locator(value: str) -> str:
    locator = _require_text(value, "source_locator")
    parsed = urlparse(locator)
    if not parsed.scheme:
        raise InvariantViolation("source_locator must use a named URI scheme")
    return locator


def _assign_trust_class(
    *,
    actor: Actor,
    acquisition_method: str,
    origin: AcquisitionOrigin | str | None,
    external_reviewed: bool,
) -> str:
    if not isinstance(external_reviewed, bool):
        raise InvariantViolation("external_reviewed must be true or false")
    if origin is None:
        if acquisition_method == "fetch":
            selected = AcquisitionOrigin.EXTERNAL
        elif acquisition_method in {"file_read", "import"}:
            selected = AcquisitionOrigin.PREEXISTING
        elif acquisition_method == "said_in_chat":
            selected = {
                "human": AcquisitionOrigin.USER_INPUT,
                "agent_run": AcquisitionOrigin.AGENT_GENERATED,
                "system": AcquisitionOrigin.MIXED_TRANSCRIPT,
            }[actor.kind]
        elif actor.kind == "human":
            selected = AcquisitionOrigin.USER_INPUT
        elif actor.kind == "agent_run":
            selected = AcquisitionOrigin.AGENT_GENERATED
        else:
            selected = AcquisitionOrigin.PREEXISTING
    else:
        try:
            selected = AcquisitionOrigin(origin)
        except (TypeError, ValueError) as exc:
            raise InvariantViolation(
                f"unsupported acquisition origin {origin!r}"
            ) from exc

    if (
        selected
        in {
            AcquisitionOrigin.USER_INPUT,
            AcquisitionOrigin.HUMAN_CURATED,
        }
        and actor.kind != "human"
    ):
        raise InvariantViolation(
            f"{selected.value} evidence must originate from a human surface"
        )
    if external_reviewed and selected is not AcquisitionOrigin.EXTERNAL:
        raise InvariantViolation("external_reviewed applies only to external evidence")
    if external_reviewed and actor.kind == "agent_run":
        raise InvariantViolation("agents cannot clear external evidence quarantine")

    if selected is AcquisitionOrigin.USER_INPUT:
        return "user_authored"
    if selected is AcquisitionOrigin.HUMAN_CURATED:
        return "user_curated"
    if selected is AcquisitionOrigin.AGENT_GENERATED:
        return "agent_authored"
    if selected is AcquisitionOrigin.MIXED_TRANSCRIPT:
        return "mixed"
    if selected is AcquisitionOrigin.PREEXISTING:
        return "unattested"
    return "external" if external_reviewed else "external_quarantined"


def _validate_agent(actor: Actor) -> None:
    if actor.kind == "agent_run":
        validate_agent_producer_meta(actor.meta)


def _merge_actor_meta(
    actor: Actor,
    meta: Mapping[str, Any] | None,
) -> str | None:
    data = _json_object(meta)
    if actor.kind == "agent_run":
        validate_agent_producer_meta(actor.meta)
        for key in RESERVED_PRODUCER_KEYS:
            if key not in actor.meta:
                continue
            if key in data and data[key] != actor.meta[key]:
                raise InvariantViolation(
                    f"caller meta conflicts with authoritative actor field {key!r}"
                )
            data[key] = actor.meta[key]
    return _stable_json(data) if data else None


def _row(record_type: type[Any], row: sqlite3.Row | None) -> Any | None:
    return None if row is None else record_type(**dict(row))


class TruthStore:
    """A stateless handle to one append-only truth sidecar."""

    def __init__(
        self,
        paths: StorePaths,
        *,
        inline_content_bytes: int = DEFAULT_INLINE_CONTENT_BYTES,
        on_commit: Callable[["TruthStore"], None] | None = None,
    ) -> None:
        if inline_content_bytes < 0:
            raise ValueError("inline_content_bytes cannot be negative")
        self._paths = paths
        self._inline_content_bytes = inline_content_bytes
        # Optional observer only. The profile's committed export is a built-in
        # post-commit action, so observers must not call export_store again.
        self._on_commit = on_commit

    @classmethod
    def create(
        cls,
        root: str | Path,
        profile: StoreProfile | Mapping[str, Any],
        *,
        inline_content_bytes: int = DEFAULT_INLINE_CONTENT_BYTES,
        on_commit: Callable[["TruthStore"], None] | None = None,
    ) -> "TruthStore":
        validated = validate_profile(profile)
        paths = StorePaths.from_root(root)
        paths.sidecar.mkdir(parents=True, exist_ok=True)
        store = cls(
            paths,
            inline_content_bytes=inline_content_bytes,
            on_commit=on_commit,
        )
        # Refuse a future schema before applying any persistent connection
        # tuning.  In particular, PRAGMA journal_mode mutates the database
        # even when no application rows are written.
        conn = store._open_connection(configure_storage=False)
        changed = False
        try:
            starting_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            migrate(conn, paths.db)
            changed = int(conn.execute("PRAGMA user_version").fetchone()[0]) != (
                starting_version
            )
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute("SELECT * FROM store_info").fetchall()
                if len(rows) > 1:
                    raise InvariantViolation("store_info must contain exactly one row")
                if rows:
                    info = StoreInfo(**dict(rows[0]))
                    if (
                        info.store_id != validated.store_id
                        or info.profile != validated.profile
                    ):
                        raise InvariantViolation(
                            "existing truth store identity does not match profile"
                        )
                    if paths.config.exists():
                        existing = load_profile(paths.config)
                        if (
                            existing.store_id != validated.store_id
                            or existing.profile != validated.profile
                        ):
                            raise InvariantViolation(
                                "existing store.yaml identity does not match store_info"
                            )
                    else:
                        dump_profile(validated, paths.config)
                else:
                    if paths.config.exists():
                        existing = load_profile(paths.config)
                        if (
                            existing.store_id != validated.store_id
                            or existing.profile != validated.profile
                        ):
                            raise InvariantViolation(
                                "existing store.yaml identity does not match requested store"
                            )
                    else:
                        dump_profile(validated, paths.config)
                    conn.execute(
                        "INSERT INTO store_info "
                        "(store_id, profile, schema_version, title, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            validated.store_id,
                            validated.profile,
                            current_version(conn),
                            validated.title,
                            utc_now(),
                        ),
                    )
                    changed = True
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            store._configure_storage(conn)
        except SchemaVersionTooNew as exc:
            raise StoreVersionError(str(exc)) from exc
        finally:
            conn.close()
        # Auxiliary paths are part of a successfully opened store.  Creating
        # them before migration preflight would mutate an otherwise untouched
        # future-version store that this engine must refuse.
        paths.blobs.mkdir(parents=True, exist_ok=True)
        paths.export_dir.mkdir(parents=True, exist_ok=True)
        if changed:
            store._run_on_commit()
        return cls.open(
            paths.sidecar,
            inline_content_bytes=inline_content_bytes,
            on_commit=on_commit,
        )

    @classmethod
    def open(
        cls,
        root: str | Path,
        *,
        inline_content_bytes: int = DEFAULT_INLINE_CONTENT_BYTES,
        on_commit: Callable[["TruthStore"], None] | None = None,
    ) -> "TruthStore":
        paths = StorePaths.from_root(root)
        if not paths.config.is_file():
            raise InvariantViolation(f"truth profile does not exist: {paths.config}")
        if not paths.db.is_file():
            raise InvariantViolation(f"truth database does not exist: {paths.db}")
        profile = load_profile(paths.config)
        store = cls(
            paths,
            inline_content_bytes=inline_content_bytes,
            on_commit=on_commit,
        )
        # Version and identity checks must precede persistent PRAGMAs so an
        # older engine leaves a future store byte-for-byte untouched.
        conn = store._open_connection(configure_storage=False)
        migrated = False
        try:
            try:
                starting_version = int(
                    conn.execute("PRAGMA user_version").fetchone()[0]
                )
                version = migrate(conn, paths.db)
                migrated = version != starting_version
            except SchemaVersionTooNew as exc:
                raise StoreVersionError(str(exc)) from exc
            rows = conn.execute("SELECT * FROM store_info").fetchall()
            if len(rows) != 1:
                raise InvariantViolation("store_info must contain exactly one row")
            info = StoreInfo(**dict(rows[0]))
            if (
                info.store_id != profile.store_id
                or info.profile != profile.profile
                or info.title != profile.title
            ):
                raise InvariantViolation(
                    "store_info identity does not match store.yaml"
                )
            if info.schema_version != version:
                raise StoreVersionError(
                    "store_info schema version does not match SQLite user_version"
                )
            store._configure_storage(conn)
        finally:
            conn.close()
        # Redaction removes the old rebuildable export before commit and leaves
        # a content-free recovery marker until post-commit publication succeeds.
        # Rebuild that projection before returning an interrupted store.  Blob
        # cleanup is attempted even if projection recovery reports a hook error,
        # so one failed observer cannot strand sensitive bytes indefinitely.
        try:
            if migrated:
                # Migration is itself a committed state change and retains its
                # normal hook even if another recovery caller just cleared a
                # redaction marker.
                store._run_on_commit()
            else:
                store.recover_pending_redactions()
        except Exception:
            store.recover_pending_blob_cleanups()
            raise
        store.recover_pending_blob_cleanups()
        return store

    @property
    def paths(self) -> StorePaths:
        return self._paths

    @property
    def store_id(self) -> str:
        return self.profile.store_id

    @property
    def profile(self) -> StoreProfile:
        return load_profile(self._paths.config)

    @staticmethod
    def _configure_storage(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")

    def _open_connection(
        self,
        *,
        configure_storage: bool = True,
    ) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._paths.db),
            timeout=SQLITE_TIMEOUT_SECONDS,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        if configure_storage:
            self._configure_storage(conn)
        return conn

    def connect(self) -> sqlite3.Connection:
        """Return a configured caller-owned connection."""
        return self._open_connection()

    def _validate_connection_target(self, conn: sqlite3.Connection) -> None:
        try:
            rows = conn.execute("PRAGMA database_list").fetchall()
        except sqlite3.Error as exc:
            raise InvariantViolation(
                "supplied SQLite connection is not usable"
            ) from exc
        main = next((row for row in rows if row[1] == "main"), None)
        if main is None or not main[2]:
            raise InvariantViolation(
                "supplied connection has no file-backed main database"
            )
        actual = Path(str(main[2])).resolve()
        expected = self._paths.db.resolve()
        if actual != expected:
            raise InvariantViolation(
                "supplied connection belongs to a different truth store"
            )

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._open_connection()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write_transaction(
        self,
        conn: sqlite3.Connection | None = None,
    ) -> Iterator[sqlite3.Connection]:
        """Own one write transaction, or compose inside a supplied one.

        Only the store-owned outer context can run post-commit export and
        observer actions. A supplied connection leaves commit ownership, and
        any required post-commit work, with its caller.
        """
        if conn is not None:
            self._validate_connection_target(conn)
            if not conn.in_transaction:
                raise InvariantViolation(
                    "a supplied connection must already own a transaction"
                )
            yield conn
            return

        owned = self._open_connection()
        committed = False
        created_redaction_recovery = False
        try:
            owned.execute("BEGIN IMMEDIATE")
            recovery_names_before = {
                path.name for path in self._pending_redaction_recovery_paths()
            }
            yield owned
            created_redaction_recovery = any(
                path.name not in recovery_names_before
                for path in self._pending_redaction_recovery_paths()
            )
            owned.execute("COMMIT")
            committed = True
        except Exception:
            if owned.in_transaction:
                owned.execute("ROLLBACK")
            raise
        finally:
            owned.close()
        if committed:
            self._run_on_commit(required=not created_redaction_recovery)

    _write_transaction = write_transaction

    def _writer_barrier(self) -> None:
        """Wait for any filesystem-visible, in-flight writer to settle."""

        barrier = self._open_connection()
        try:
            barrier.execute("BEGIN IMMEDIATE")
            barrier.execute("COMMIT")
        except Exception:
            if barrier.in_transaction:
                barrier.execute("ROLLBACK")
            raise
        finally:
            barrier.close()

    def _run_on_commit(self, *, required: bool = True) -> None:
        """Publish the required recovery export, then notify the observer."""

        initial_recoveries = self._pending_redaction_recovery_paths()
        if initial_recoveries:
            # A marker is published inside the redaction transaction, before
            # COMMIT.  Its filesystem visibility therefore cannot prove that
            # the corresponding database state is readable yet.  Wait for the
            # writer, then retain only markers that still need recovery.
            self._writer_barrier()
            current = {
                path.name: path
                for path in self._pending_redaction_recovery_paths()
            }
            redaction_recoveries = tuple(
                current[path.name]
                for path in initial_recoveries
                if path.name in current
            )
        else:
            redaction_recoveries = ()
        if not required and not redaction_recoveries:
            # Another post-commit caller completed this redaction while we
            # waited.  Its observer ran after the same writer barrier, so a
            # second recovery notification would be redundant.
            return
        if not required and not self._committed_redaction_recovery_paths(
            redaction_recoveries
        ):
            # The transaction that published these markers rolled back after
            # removing the rebuildable export.  Restore that projection, but do
            # not report a commit that never happened.
            self._publish_recovery_export()
            self._clear_redaction_recovery_paths(redaction_recoveries)
            return
        self._publish_recovery_export()
        try:
            if self._on_commit is not None:
                self._on_commit(self)
        except Exception as exc:
            raise PostCommitHookError(
                "truth ledger commit succeeded but the post-commit hook failed"
            ) from exc
        try:
            self._clear_redaction_recovery_paths(redaction_recoveries)
        except Exception as exc:
            raise PostCommitHookError(
                "truth ledger commit succeeded but its redaction recovery "
                "marker could not be cleared"
            ) from exc

    def _publish_recovery_export(self) -> None:
        """Publish the configured recovery export with privacy-safe failure."""

        try:
            if self.profile.export_committed:
                from work_buddy.truth.export import export_store

                export_store(self)
        except Exception as exc:
            # The old export describes pre-commit state and can retain content
            # that a sanctioned redaction just destroyed.  Profile loading is
            # part of this guarded phase: when policy cannot be read, removing
            # a rebuildable export is the privacy-safe default.
            try:
                self.paths.claims_export.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                raise PostCommitHookError(
                    "truth ledger commit succeeded but the required recovery "
                    "export failed and its stale predecessor could not be removed"
                ) from cleanup_exc
            raise PostCommitHookError(
                "truth ledger commit succeeded but the post-commit hook failed; "
                "the stale recovery export was removed"
            ) from exc

    @staticmethod
    def _require_transaction(conn: sqlite3.Connection) -> None:
        if not conn.in_transaction:
            raise InvariantViolation("locked truth operation requires a transaction")

    def get_evidence(
        self,
        evidence_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> EvidenceRecord | None:
        if conn is not None:
            return self._get_evidence_locked(conn, evidence_id)
        with self._read_connection() as read_conn:
            return self._get_evidence_locked(read_conn, evidence_id)

    def get_span(
        self,
        span_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> EvidenceSpanRecord | None:
        if conn is not None:
            return self._get_span_locked(conn, span_id)
        with self._read_connection() as read_conn:
            return self._get_span_locked(read_conn, span_id)

    def get_claim(
        self,
        claim_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> ClaimRecord | None:
        if conn is not None:
            return self._get_claim_locked(conn, claim_id)
        with self._read_connection() as read_conn:
            return self._get_claim_locked(read_conn, claim_id)

    def get_link(
        self,
        link_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> ClaimLinkRecord | None:
        if conn is not None:
            return self._get_link_locked(conn, link_id)
        with self._read_connection() as read_conn:
            return self._get_link_locked(read_conn, link_id)

    def get_derivation(
        self,
        derivation_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> DerivationRecord | None:
        if conn is not None:
            return self._get_derivation_locked(conn, derivation_id)
        with self._read_connection() as read_conn:
            return self._get_derivation_locked(read_conn, derivation_id)

    def _get_evidence_locked(
        self,
        conn: sqlite3.Connection,
        evidence_id: str,
    ) -> EvidenceRecord | None:
        return _row(
            EvidenceRecord,
            conn.execute(
                "SELECT * FROM evidence WHERE id = ?", (evidence_id,)
            ).fetchone(),
        )

    def _get_span_locked(
        self,
        conn: sqlite3.Connection,
        span_id: str,
    ) -> EvidenceSpanRecord | None:
        return _row(
            EvidenceSpanRecord,
            conn.execute(
                "SELECT * FROM evidence_spans WHERE id = ?", (span_id,)
            ).fetchone(),
        )

    def _get_claim_locked(
        self,
        conn: sqlite3.Connection,
        claim_id: str,
    ) -> ClaimRecord | None:
        return _row(
            ClaimRecord,
            conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone(),
        )

    def _get_link_locked(
        self,
        conn: sqlite3.Connection,
        link_id: str,
    ) -> ClaimLinkRecord | None:
        return _row(
            ClaimLinkRecord,
            conn.execute(
                "SELECT * FROM claim_links WHERE id = ?", (link_id,)
            ).fetchone(),
        )

    def _get_derivation_locked(
        self,
        conn: sqlite3.Connection,
        derivation_id: str,
    ) -> DerivationRecord | None:
        raw = conn.execute(
            "SELECT * FROM derivations WHERE id = ?", (derivation_id,)
        ).fetchone()
        if raw is None:
            return None
        premises = tuple(
            PremiseRef(kind=row["premise_kind"], ref=row["premise_ref"])
            for row in conn.execute(
                "SELECT premise_kind, premise_ref FROM derivation_premises "
                "WHERE derivation_id = ? ORDER BY premise_ref",
                (derivation_id,),
            )
        )
        return DerivationRecord(**dict(raw), premises=premises)

    def _latest_status_locked(
        self,
        conn: sqlite3.Connection,
        claim_id: str,
        *,
        include_overlay: bool = True,
    ) -> StatusEventRecord | None:
        where = "claim_id = ?"
        params: list[Any] = [claim_id]
        if not include_overlay:
            where += " AND status != 'needs_review'"
        raw = conn.execute(
            f"SELECT * FROM claim_status_events WHERE {where} "
            "ORDER BY seq DESC LIMIT 1",
            params,
        ).fetchone()
        return _row(StatusEventRecord, raw)

    def _get_gesture_locked(
        self,
        conn: sqlite3.Connection,
        gesture_id: str,
    ) -> GestureRecord | None:
        return _row(
            GestureRecord,
            conn.execute(
                "SELECT * FROM gestures WHERE id = ?", (gesture_id,)
            ).fetchone(),
        )

    def _insert_ledger_record_locked(
        self,
        conn: sqlite3.Connection,
        record_type: str,
        record_key: str,
        *,
        seq: int | None = None,
    ) -> int:
        """Append one item to the store-wide durable insertion order."""
        self._require_transaction(conn)
        kind = _require_text(record_type, "record_type")
        key = _require_text(record_key, "record_key")
        if seq is None:
            cursor = conn.execute(
                "INSERT INTO ledger_records (record_type, record_key) VALUES (?, ?)",
                (kind, key),
            )
            return int(cursor.lastrowid)
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
            raise InvariantViolation("ledger seq must be a positive integer")
        conn.execute(
            "INSERT INTO ledger_records (seq, record_type, record_key) "
            "VALUES (?, ?, ?)",
            (seq, kind, key),
        )
        return seq

    def _insert_evidence_locked(
        self,
        conn: sqlite3.Connection,
        record: EvidenceRecord,
    ) -> EvidenceRecord:
        self._require_transaction(conn)
        conn.execute(
            "INSERT INTO evidence "
            "(id, kind, source_locator, content_sha256, content, content_path, "
            "media_type, acquired_at, acquired_by_kind, acquired_by_ref, "
            "acquisition_method, trust_class, derived_from_store, meta_json, "
            "redacted_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(record.__dict__.values())
            if hasattr(record, "__dict__")
            else (
                record.id,
                record.kind,
                record.source_locator,
                record.content_sha256,
                record.content,
                record.content_path,
                record.media_type,
                record.acquired_at,
                record.acquired_by_kind,
                record.acquired_by_ref,
                record.acquisition_method,
                record.trust_class,
                record.derived_from_store,
                record.meta_json,
                record.redacted_at,
                record.created_at,
            ),
        )
        self._insert_ledger_record_locked(conn, "evidence", record.id)
        return record

    def _insert_span_locked(
        self,
        conn: sqlite3.Connection,
        record: EvidenceSpanRecord,
    ) -> EvidenceSpanRecord:
        self._require_transaction(conn)
        conn.execute(
            "INSERT INTO evidence_spans "
            "(id, evidence_id, selector_json, quote_exact, span_sha256, "
            "author_kind, author_ref, redacted_at, created_at, "
            "created_by_kind, created_by_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.evidence_id,
                record.selector_json,
                record.quote_exact,
                record.span_sha256,
                record.author_kind,
                record.author_ref,
                record.redacted_at,
                record.created_at,
                record.created_by_kind,
                record.created_by_ref,
            ),
        )
        self._insert_ledger_record_locked(conn, "evidence_span", record.id)
        return record

    def _insert_claim_locked(
        self,
        conn: sqlite3.Connection,
        record: ClaimRecord,
    ) -> ClaimRecord:
        self._require_transaction(conn)
        conn.execute(
            "INSERT INTO claims "
            "(id, proposition, canonical_sha256, claim_kind, structured_json, "
            "scope, valid_from, valid_to, confidence_extraction, meta_json, "
            "redacted_at, created_at, created_by_kind, created_by_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.proposition,
                record.canonical_sha256,
                record.claim_kind,
                record.structured_json,
                record.scope,
                record.valid_from,
                record.valid_to,
                record.confidence_extraction,
                record.meta_json,
                record.redacted_at,
                record.created_at,
                record.created_by_kind,
                record.created_by_ref,
            ),
        )
        self._insert_ledger_record_locked(conn, "claim", record.id)
        return record

    def _insert_link_locked(
        self,
        conn: sqlite3.Connection,
        record: ClaimLinkRecord,
    ) -> ClaimLinkRecord:
        self._require_transaction(conn)
        conn.execute(
            "INSERT INTO claim_links "
            "(id, from_claim_id, link_type, to_kind, to_ref, role_json, "
            "target_fingerprint, fingerprint_reviewed_at, created_at, "
            "created_by_kind, created_by_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.from_claim_id,
                record.link_type,
                record.to_kind,
                record.to_ref,
                record.role_json,
                record.target_fingerprint,
                record.fingerprint_reviewed_at,
                record.created_at,
                record.created_by_kind,
                record.created_by_ref,
            ),
        )
        self._insert_ledger_record_locked(conn, "claim_link", record.id)
        return record

    def _insert_derivation_locked(
        self,
        conn: sqlite3.Connection,
        record: DerivationRecord,
    ) -> DerivationRecord:
        self._require_transaction(conn)
        conn.execute(
            "INSERT INTO derivations "
            "(id, claim_id, method, producer_kind, producer_ref, confidence, "
            "rationale, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.claim_id,
                record.method,
                record.producer_kind,
                record.producer_ref,
                record.confidence,
                record.rationale,
                record.created_at,
            ),
        )
        self._insert_ledger_record_locked(conn, "derivation", record.id)
        for premise in record.premises:
            conn.execute(
                "INSERT INTO derivation_premises "
                "(derivation_id, premise_kind, premise_ref) VALUES (?, ?, ?)",
                (record.id, premise.kind, premise.ref),
            )
            self._insert_ledger_record_locked(
                conn,
                "derivation_premise",
                canonical_json(
                    {
                        "derivation_id": record.id,
                        "premise_ref": premise.ref,
                    }
                ),
            )
        return record

    def _insert_status_event_locked(
        self,
        conn: sqlite3.Connection,
        *,
        claim_id: str,
        status: str,
        actor: Actor,
        basis_kind: str,
        basis_ref: str | None = None,
        note: str | None = None,
        event_id: str | None = None,
        at: str | None = None,
    ) -> StatusEventRecord:
        self._require_transaction(conn)
        if status not in VALID_STATUSES:
            raise InvariantViolation(f"invalid truth status {status!r}")
        if self._get_claim_locked(conn, claim_id) is None:
            raise InvariantViolation(f"claim does not exist: {claim_id}")
        identifier = _record_id(event_id, "status event id")
        timestamp = _timestamp(at, "status event at")
        basis = _require_text(basis_kind, "basis_kind")
        cursor = conn.execute(
            "INSERT INTO claim_status_events "
            "(id, claim_id, status, at, actor_kind, actor_ref, basis_kind, "
            "basis_ref, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identifier,
                claim_id,
                status,
                timestamp,
                actor.kind,
                actor.ref,
                basis,
                basis_ref,
                note,
            ),
        )
        self._insert_ledger_record_locked(
            conn,
            "claim_status_event",
            identifier,
        )
        return StatusEventRecord(
            seq=int(cursor.lastrowid),
            id=identifier,
            claim_id=claim_id,
            status=status,
            at=timestamp,
            actor_kind=actor.kind,
            actor_ref=actor.ref,
            basis_kind=basis,
            basis_ref=basis_ref,
            note=note,
        )

    def _insert_gesture_locked(
        self,
        conn: sqlite3.Connection,
        record: GestureRecord,
    ) -> GestureRecord:
        self._require_transaction(conn)
        conn.execute(
            "INSERT INTO gestures "
            "(id, at, surface, actor_ref, kind, subject_ref, payload_sha256, "
            "payload_excerpt, context_sha256, expires_at, consumed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.at,
                record.surface,
                record.actor_ref,
                record.kind,
                record.subject_ref,
                record.payload_sha256,
                record.payload_excerpt,
                record.context_sha256,
                record.expires_at,
                record.consumed_at,
            ),
        )
        self._insert_ledger_record_locked(conn, "gesture", record.id)
        return record

    def _consume_gesture_locked(
        self,
        conn: sqlite3.Connection,
        gesture_id: str,
        consumed_at: str | None = None,
    ) -> GestureRecord:
        self._require_transaction(conn)
        gesture = self._get_gesture_locked(conn, gesture_id)
        if gesture is None:
            raise GestureError(f"gesture does not exist: {gesture_id}")
        if gesture.consumed_at is not None:
            raise GestureError("gesture has already been consumed")
        at = consumed_at or utc_now()
        conn.execute(
            "UPDATE gestures SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
            (at, gesture_id),
        )
        refreshed = self._get_gesture_locked(conn, gesture_id)
        assert refreshed is not None
        return refreshed

    def resolve_blob_path(self, content_path: str | Path) -> Path:
        """Resolve one canonical blob reference without permitting traversal."""
        if not isinstance(content_path, (str, Path)):
            raise InvariantViolation("content_path must be a relative blob path")
        relative = Path(content_path)
        if relative.is_absolute() or len(relative.parts) != 2:
            raise InvariantViolation("content_path must have the form blobs/<sha256>")
        if relative.parts[0] != "blobs":
            raise InvariantViolation("content_path must have the form blobs/<sha256>")
        _valid_digest(relative.parts[1], "blob filename")
        blob_root = self._paths.blobs.resolve()
        resolved = (self._paths.sidecar / relative).resolve()
        if resolved.parent != blob_root:
            raise InvariantViolation("content_path resolves outside the blob directory")
        return resolved

    def _store_blob_bytes(self, digest: str, data: bytes) -> tuple[str, bool]:
        expected = _valid_digest(digest, "content_sha256")
        if sha256_bytes(data) != expected:
            raise InvariantViolation("blob bytes do not match content_sha256")
        relative = f"blobs/{expected}"
        path = self.resolve_blob_path(relative)
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise InvariantViolation(
                    f"could not read existing blob {path}"
                ) from exc
            if sha256_bytes(existing) != expected:
                raise InvariantViolation(
                    f"existing content-addressed blob is corrupt: {path}"
                )
            return relative, False
        try:
            atomic_write_bytes(path, data)
            written = path.read_bytes()
        except OSError as exc:
            path.unlink(missing_ok=True)
            raise InvariantViolation(f"could not write evidence blob {path}") from exc
        if sha256_bytes(written) != expected:
            path.unlink(missing_ok=True)
            raise InvariantViolation(
                f"written evidence blob failed verification: {path}"
            )
        return relative, True

    def _remove_unreferenced_blob(self, digest: str) -> None:
        """Clean a blob created by a failed store-owned capture."""
        cleanup = self._open_connection()
        try:
            cleanup.execute("BEGIN IMMEDIATE")
            count = cleanup.execute(
                "SELECT COUNT(*) FROM evidence WHERE content_sha256 = ? "
                "AND content_path IS NOT NULL",
                (digest,),
            ).fetchone()[0]
            if int(count) == 0:
                self.resolve_blob_path(f"blobs/{digest}").unlink(missing_ok=True)
            cleanup.execute("COMMIT")
        except Exception:
            if cleanup.in_transaction:
                cleanup.execute("ROLLBACK")
            raise
        finally:
            cleanup.close()

    def _blob_cleanup_intent_path(self, digest: str) -> Path:
        """Return the digest-only durable intent path for one blob cleanup."""

        normalized = _valid_digest(digest, "content_sha256")
        return self._paths.sidecar / _BLOB_CLEANUP_DIRNAME / normalized

    def _queue_blob_cleanup_locked(
        self,
        conn: sqlite3.Connection,
        digest: str,
    ) -> Path:
        """Durably request post-commit deletion while the redaction is locked.

        The empty, SHA-256-named marker is intentionally created before the
        database commit.  If the transaction rolls back or another live
        reference is added before recovery, the reference check in
        ``_finish_blob_cleanup`` cancels the stale request without deleting the
        blob.  The marker contains neither evidence content nor its locator.
        """

        self._require_transaction(conn)
        intent = self._blob_cleanup_intent_path(digest)
        atomic_write_bytes(intent, b"")
        return intent

    def _redaction_recovery_intent_path(self, event_id: str) -> Path:
        """Return the content-free recovery marker for one redaction event."""

        normalized = _valid_record_id(event_id, "redaction event id")
        return self._paths.sidecar / _REDACTION_RECOVERY_DIRNAME / normalized

    def _pending_redaction_recovery_paths(self) -> tuple[Path, ...]:
        """Snapshot complete redaction recovery markers in stable order."""

        recovery_dir = self._paths.sidecar / _REDACTION_RECOVERY_DIRNAME
        if not recovery_dir.is_dir():
            return ()
        try:
            candidates = sorted(
                recovery_dir.iterdir(),
                key=lambda path: path.name,
            )
        except FileNotFoundError:
            # A concurrent recovery cleared its last marker and removed the
            # operational directory after our existence check.
            return ()
        return tuple(
            candidate
            for candidate in candidates
            if candidate.is_file()
            and _RECORD_ID_RE.fullmatch(candidate.name) is not None
        )

    def _committed_redaction_recovery_paths(
        self,
        paths: Sequence[Path],
    ) -> tuple[Path, ...]:
        """Return recovery markers backed by committed redaction events."""

        if not paths:
            return ()
        committed: list[Path] = []
        with self._read_connection() as conn:
            for path in paths:
                row = conn.execute(
                    "SELECT 1 FROM redaction_events WHERE id = ?",
                    (path.name,),
                ).fetchone()
                if row is not None:
                    committed.append(path)
        return tuple(committed)

    def _queue_redaction_recovery_locked(
        self,
        conn: sqlite3.Connection,
        event_id: str,
    ) -> Path:
        """Make a redaction's post-commit projection work crash-recoverable.

        The empty marker contains only the random redaction-event identifier.
        It is published before the old recovery export is removed, both while
        the database writer lock is held.  A rollback may therefore leave a
        missing rebuildable export, but a successful commit can never leave a
        pre-redaction export containing the destroyed payload.
        """

        self._require_transaction(conn)
        intent = self._redaction_recovery_intent_path(event_id)
        atomic_write_bytes(intent, b"")
        try:
            self._paths.claims_export.unlink(missing_ok=True)
        except OSError as exc:
            raise InvariantViolation(
                "could not remove the pre-redaction recovery export"
            ) from exc
        return intent

    def _clear_redaction_recovery_paths(self, paths: Sequence[Path]) -> None:
        """Clear only the marker snapshot covered by a successful export."""

        if not paths:
            return
        recovery_dir = (
            self._paths.sidecar / _REDACTION_RECOVERY_DIRNAME
        ).resolve()
        cleanup = self._open_connection()
        try:
            # Wait out a redaction that may have published its marker before
            # committing or rolling back.  New markers use different random
            # event ids and are not part of this snapshot.
            cleanup.execute("BEGIN IMMEDIATE")
            for path in paths:
                resolved = path.resolve()
                if resolved.parent != recovery_dir:
                    raise InvariantViolation(
                        "redaction recovery marker resolves outside its directory"
                    )
                _valid_record_id(resolved.name, "redaction recovery marker")
                resolved.unlink(missing_ok=True)
            cleanup.execute("COMMIT")
        except Exception:
            if cleanup.in_transaction:
                cleanup.execute("ROLLBACK")
            raise
        finally:
            cleanup.close()
        try:
            recovery_dir.rmdir()
        except OSError:
            pass

    def recover_pending_redactions(self) -> tuple[str, ...]:
        """Rebuild post-redaction projections and clear covered markers."""

        pending = self._pending_redaction_recovery_paths()
        if not pending:
            return ()
        self._run_on_commit(required=False)
        return tuple(path.name for path in pending if not path.exists())

    def _finish_blob_cleanup(self, digest: str) -> bool:
        """Finish one durable deletion intent under a fresh reference check.

        Filesystem deletion happens while ``BEGIN IMMEDIATE`` excludes captures
        and redactions that could change the blob refcount.  The intent is
        removed only after unlink succeeds.  Therefore interruption before
        unlink leaves both files for retry, while interruption after unlink
        leaves an idempotent marker whose retry observes a missing blob.
        """

        normalized = _valid_digest(digest, "content_sha256")
        intent = self._blob_cleanup_intent_path(normalized)
        cleanup_dir = intent.parent
        cleanup = self._open_connection()
        deleted = False
        try:
            cleanup.execute("BEGIN IMMEDIATE")
            if not intent.is_file():
                cleanup.execute("COMMIT")
                return False
            count = cleanup.execute(
                "SELECT COUNT(*) FROM evidence WHERE content_sha256 = ? "
                "AND content_path IS NOT NULL",
                (normalized,),
            ).fetchone()[0]
            if int(count) == 0:
                blob = self.resolve_blob_path(f"blobs/{normalized}")
                existed = blob.exists()
                blob.unlink(missing_ok=True)
                deleted = existed and not blob.exists()
            # A live reference makes this intent stale.  The future redaction
            # of that reference will enqueue a new request if deletion is then
            # permitted.
            intent.unlink(missing_ok=True)
            cleanup.execute("COMMIT")
        except Exception:
            if cleanup.in_transaction:
                cleanup.execute("ROLLBACK")
            raise
        finally:
            cleanup.close()
        try:
            cleanup_dir.rmdir()
        except OSError:
            # Another intent (or an interrupted atomic-write temp file) still
            # owns the directory.  A later open will scan it again.
            pass
        return deleted

    def recover_pending_blob_cleanups(self) -> tuple[str, ...]:
        """Retry every complete digest-only blob deletion intent."""

        cleanup_dir = self._paths.sidecar / _BLOB_CLEANUP_DIRNAME
        if not cleanup_dir.is_dir():
            return ()
        try:
            candidates = sorted(
                cleanup_dir.iterdir(),
                key=lambda path: path.name,
            )
        except FileNotFoundError:
            return ()
        recovered: list[str] = []
        for candidate in candidates:
            if not candidate.is_file() or _SHA256_RE.fullmatch(candidate.name) is None:
                continue
            digest = candidate.name
            self._finish_blob_cleanup(digest)
            recovered.append(digest)
        try:
            cleanup_dir.rmdir()
        except OSError:
            pass
        return tuple(recovered)

    def _read_evidence_bytes_locked(
        self,
        conn: sqlite3.Connection,
        evidence: EvidenceRecord,
    ) -> bytes | None:
        current = self._get_evidence_locked(conn, evidence.id)
        if current is None:
            raise InvariantViolation(f"evidence does not exist: {evidence.id}")
        if current.redacted_at is not None:
            return None
        if current.content is not None and current.content_path is not None:
            raise InvariantViolation("evidence cannot contain inline and blob content")
        if current.content is not None:
            data = current.content.encode("utf-8")
        elif current.content_path is not None:
            path = self.resolve_blob_path(current.content_path)
            if path.name != current.content_sha256:
                raise InvariantViolation(
                    "evidence content_path does not match content_sha256"
                )
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise InvariantViolation(
                    f"could not read evidence blob {path}"
                ) from exc
        else:
            return None
        if sha256_bytes(data) != current.content_sha256:
            raise InvariantViolation(f"evidence content hash mismatch for {current.id}")
        return data

    def read_evidence_bytes(
        self,
        evidence: str | EvidenceRecord,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bytes | None:
        """Read and verify captured bytes, or return None for hash-only content."""
        if conn is not None:
            record = (
                evidence
                if isinstance(evidence, EvidenceRecord)
                else self._get_evidence_locked(conn, evidence)
            )
            if record is None:
                raise InvariantViolation(f"evidence does not exist: {evidence}")
            return self._read_evidence_bytes_locked(conn, record)
        with self._read_connection() as read_conn:
            record = (
                evidence
                if isinstance(evidence, EvidenceRecord)
                else self._get_evidence_locked(read_conn, evidence)
            )
            if record is None:
                raise InvariantViolation(f"evidence does not exist: {evidence}")
            return self._read_evidence_bytes_locked(read_conn, record)

    def read_evidence_text(
        self,
        evidence: str | EvidenceRecord,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> str | None:
        """Read verified evidence as UTF-8 text."""
        data = self.read_evidence_bytes(evidence, conn=conn)
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvariantViolation("evidence bytes are not valid UTF-8 text") from exc

    def blob_reference_count(
        self,
        digest: str,
        *,
        live_only: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Count evidence rows that still reference one content blob."""
        normalized = _valid_digest(digest, "content_sha256")
        sql = (
            "SELECT COUNT(*) FROM evidence WHERE content_sha256 = ? "
            "AND content_path IS NOT NULL"
        )
        if live_only:
            sql += " AND redacted_at IS NULL"
        if conn is not None:
            return int(conn.execute(sql, (normalized,)).fetchone()[0])
        with self._read_connection() as read_conn:
            return int(read_conn.execute(sql, (normalized,)).fetchone()[0])

    def capture_evidence(
        self,
        *,
        kind: str,
        source_locator: str,
        actor: Actor,
        acquisition_method: str,
        content: str | bytes | bytearray | memoryview | None = None,
        content_sha256: str | None = None,
        media_type: str | None = None,
        acquired_at: str | None = None,
        origin: AcquisitionOrigin | str | None = None,
        external_reviewed: bool = False,
        derived_from_store: str | None = None,
        meta: Mapping[str, Any] | None = None,
        record_id: str | None = None,
        created_at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> EvidenceRecord:
        """Capture immutable evidence with engine-assigned trust provenance."""
        evidence_kind = _require_text(kind, "kind")
        if evidence_kind not in EVIDENCE_KINDS:
            raise InvariantViolation(f"unsupported evidence kind {evidence_kind!r}")
        method = _require_text(acquisition_method, "acquisition_method")
        if method not in ACQUISITION_METHODS:
            raise InvariantViolation(f"unsupported acquisition_method {method!r}")
        locator = _source_locator(source_locator)
        _validate_agent(actor)
        identifier = _record_id(record_id, "evidence id")
        created = _timestamp(created_at, "created_at")
        acquired = _timestamp(acquired_at or created, "acquired_at")
        if _parse_time(acquired, "acquired_at") > _parse_time(created, "created_at"):
            raise InvariantViolation("acquired_at cannot be later than created_at")
        media = None if media_type is None else _require_text(media_type, "media_type")
        derived = (
            None
            if derived_from_store is None
            else normalize_store_id(derived_from_store)
        )
        assigned_trust = _assign_trust_class(
            actor=actor,
            acquisition_method=method,
            origin=origin,
            external_reviewed=external_reviewed,
        )

        inline: str | None = None
        blob_data: bytes | None = None
        if isinstance(content, str):
            raw = content.encode("utf-8")
            if len(raw) <= self._inline_content_bytes:
                inline = content
            else:
                blob_data = raw
        elif isinstance(content, (bytes, bytearray, memoryview)):
            raw = bytes(content)
            blob_data = raw
        elif content is None:
            raw = None
        else:
            raise InvariantViolation("content must be text, bytes, or None")

        if raw is None:
            if content_sha256 is None:
                raise InvariantViolation("hash-only evidence requires content_sha256")
            digest = _valid_digest(content_sha256, "content_sha256")
        else:
            computed = sha256_bytes(raw)
            if content_sha256 is not None:
                supplied = _valid_digest(content_sha256, "content_sha256")
                if supplied != computed:
                    raise InvariantViolation(
                        "supplied content_sha256 does not match content"
                    )
            digest = computed

        record = EvidenceRecord(
            id=identifier,
            kind=evidence_kind,
            source_locator=locator,
            content_sha256=digest,
            content=inline,
            content_path=(f"blobs/{digest}" if blob_data is not None else None),
            media_type=media,
            acquired_at=acquired,
            acquired_by_kind=actor.kind,
            acquired_by_ref=actor.ref,
            acquisition_method=method,
            trust_class=assigned_trust,
            derived_from_store=derived,
            meta_json=_merge_actor_meta(actor, meta),
            redacted_at=None,
            created_at=created,
        )
        if blob_data is not None and conn is not None:
            blob_path = self.resolve_blob_path(f"blobs/{digest}")
            if not blob_path.exists():
                raise InvariantViolation(
                    "new blob-backed evidence cannot be captured inside a "
                    "caller-owned transaction"
                )
        blob_created = False
        try:
            with self.write_transaction(conn) as write_conn:
                if blob_data is not None:
                    _, blob_created = self._store_blob_bytes(digest, blob_data)
                return self._insert_evidence_locked(write_conn, record)
        except Exception:
            if conn is None and blob_created:
                self._remove_unreferenced_blob(digest)
            raise

    def mark_span(
        self,
        *,
        evidence_id: str,
        selector: CompositeSelector,
        actor: Actor,
        author_kind: str | None = None,
        author_ref: str | None = None,
        snapshot_text: str | bytes | bytearray | memoryview | None = None,
        record_id: str | None = None,
        created_at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> EvidenceSpanRecord:
        """Resolve and append one immutable evidence span."""
        if not isinstance(selector, CompositeSelector):
            raise InvariantViolation("selector must be a CompositeSelector")
        _validate_agent(actor)
        evidence_ref = _valid_record_id(evidence_id, "evidence_id")
        identifier = _record_id(record_id, "span id")
        created = _timestamp(created_at, "created_at")
        with self.write_transaction(conn) as write_conn:
            evidence = self._get_evidence_locked(write_conn, evidence_ref)
            if evidence is None:
                raise InvariantViolation(f"evidence does not exist: {evidence_ref}")
            if evidence.redacted_at is not None:
                raise InvariantViolation("cannot mark a redacted evidence record")
            text = self.read_evidence_text(evidence, conn=write_conn)
            if snapshot_text is not None:
                if isinstance(snapshot_text, str):
                    snapshot_bytes = snapshot_text.encode("utf-8")
                    supplied_text = snapshot_text
                elif isinstance(snapshot_text, (bytes, bytearray, memoryview)):
                    snapshot_bytes = bytes(snapshot_text)
                    try:
                        supplied_text = snapshot_bytes.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise InvariantViolation(
                            "snapshot_text bytes are not valid UTF-8"
                        ) from exc
                else:
                    raise InvariantViolation("snapshot_text must be text or bytes")
                if sha256_bytes(snapshot_bytes) != evidence.content_sha256:
                    raise InvariantViolation(
                        "snapshot_text does not match evidence content_sha256"
                    )
                if text is not None and text != supplied_text:
                    raise InvariantViolation(
                        "snapshot_text does not match stored evidence content"
                    )
                text = supplied_text
            if text is None:
                raise InvariantViolation(
                    "hash-only evidence requires matching snapshot_text"
                )
            anchor = reanchor(
                text,
                selector,
                expected_snapshot_sha256=evidence.content_sha256,
            )

            resolved_author = author_kind
            resolved_ref = author_ref
            if resolved_author is None:
                if evidence.trust_class == "mixed":
                    raise InvariantViolation(
                        "mixed evidence requires explicit span author_kind"
                    )
                if evidence.trust_class == "user_authored":
                    resolved_author = "human"
                    resolved_ref = resolved_ref or evidence.acquired_by_ref
                elif evidence.trust_class == "agent_authored":
                    resolved_author = "agent_run"
                    if evidence.acquired_by_kind == "agent_run":
                        resolved_ref = resolved_ref or evidence.acquired_by_ref
                else:
                    resolved_author = "unknown"
            if resolved_author not in SPAN_AUTHOR_KINDS:
                raise InvariantViolation(
                    f"unsupported span author_kind {resolved_author!r}"
                )
            if actor.kind == "agent_run" and resolved_author == "human":
                raise InvariantViolation(
                    "agent callers cannot assert human span authorship"
                )
            if resolved_author == "agent_run" and not str(resolved_ref or "").strip():
                raise InvariantViolation(
                    "agent_run span authorship requires author_ref"
                )
            if resolved_author == "unknown" and resolved_ref is not None:
                raise InvariantViolation(
                    "unknown span authorship cannot carry author_ref"
                )

            resolved_selector = CompositeSelector(
                exact=anchor.exact,
                prefix=selector.prefix,
                suffix=selector.suffix,
                start=anchor.start,
                end=anchor.end,
            )
            record = EvidenceSpanRecord(
                id=identifier,
                evidence_id=evidence_ref,
                selector_json=serialize_selector(resolved_selector),
                quote_exact=anchor.exact,
                span_sha256=sha256_text(anchor.exact),
                author_kind=resolved_author,
                author_ref=resolved_ref,
                redacted_at=None,
                created_at=created,
                created_by_kind=actor.kind,
                created_by_ref=actor.ref,
            )
            return self._insert_span_locked(write_conn, record)

    def _find_live_claim_locked(
        self,
        conn: sqlite3.Connection,
        canonical_digest: str,
    ) -> ClaimRecord | None:
        rows = conn.execute(
            "SELECT * FROM claims WHERE canonical_sha256 = ? "
            "AND redacted_at IS NULL ORDER BY created_at, id",
            (canonical_digest,),
        ).fetchall()
        matches: list[ClaimRecord] = []
        for row in rows:
            claim = ClaimRecord(**dict(row))
            base = self._latest_status_locked(
                conn,
                claim.id,
                include_overlay=False,
            )
            if base is None or base.status not in TERMINAL_STATUSES:
                matches.append(claim)
        if len(matches) > 1:
            raise InvariantViolation(
                "canonical claim hash resolves to multiple live claims"
            )
        return matches[0] if matches else None

    def propose_claim(
        self,
        *,
        proposition: str,
        claim_kind: str,
        actor: Actor,
        structured: Mapping[str, Any] | str | None = None,
        scope: str = "store",
        valid_from: str | None = None,
        valid_to: str | None = None,
        confidence_extraction: float | None = None,
        meta: Mapping[str, Any] | None = None,
        record_id: str | None = None,
        created_at: str | None = None,
        status_event_id: str | None = None,
        status_at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> ClaimWriteResult:
        """Propose a profile-valid claim or return its live canonical match."""
        _validate_agent(actor)
        start = _valid_time(valid_from, "valid_from")
        end = _valid_time(valid_to, "valid_to")
        if start is not None and end is not None and end < start:
            raise InvariantViolation("valid_to cannot be earlier than valid_from")
        try:
            validate_new_claim(
                self.profile,
                claim_kind=claim_kind,
                structured=structured,
            )
            payload = canonical_claim_payload(
                proposition=proposition,
                claim_kind=claim_kind,
                structured=structured,
                scope=scope,
                valid_from=valid_from,
                valid_to=valid_to,
            )
            digest = claim_sha256(
                proposition=proposition,
                claim_kind=claim_kind,
                structured=structured,
                scope=scope,
                valid_from=valid_from,
                valid_to=valid_to,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InvariantViolation(f"claim payload is invalid: {exc}") from exc

        identifier = _record_id(record_id, "claim id")
        event_identifier = _record_id(status_event_id, "status event id")
        created = _timestamp(created_at, "created_at")
        proposed_at = _timestamp(status_at or created, "status_at")
        if _parse_time(proposed_at, "status_at") < _parse_time(
            created,
            "created_at",
        ):
            raise InvariantViolation("status_at cannot be earlier than created_at")
        confidence = _validate_confidence(
            confidence_extraction,
            "confidence_extraction",
        )
        record = ClaimRecord(
            id=identifier,
            proposition=str(payload["proposition"]),
            canonical_sha256=digest,
            claim_kind=str(payload["claim_kind"]),
            structured_json=(
                None
                if payload["structured_json"] is None
                else canonical_json(payload["structured_json"])
            ),
            scope=str(payload["scope"]),
            valid_from=valid_from,
            valid_to=valid_to,
            confidence_extraction=confidence,
            meta_json=_merge_actor_meta(actor, meta),
            redacted_at=None,
            created_at=created,
            created_by_kind=actor.kind,
            created_by_ref=actor.ref,
        )
        with self.write_transaction(conn) as write_conn:
            existing = self._find_live_claim_locked(write_conn, digest)
            if existing is not None:
                return ClaimWriteResult(claim=existing, created=False)
            self._insert_claim_locked(write_conn, record)
            self._insert_status_event_locked(
                write_conn,
                claim_id=record.id,
                status="proposed",
                actor=actor,
                basis_kind="rule",
                basis_ref=record.id,
                event_id=event_identifier,
                at=proposed_at,
            )
            return ClaimWriteResult(claim=record, created=True)

    def _validate_link_target_locked(
        self,
        conn: sqlite3.Connection,
        *,
        from_claim_id: str,
        link_type: str,
        to_kind: str,
        to_ref: str,
    ) -> str:
        if self._get_claim_locked(conn, from_claim_id) is None:
            raise InvariantViolation(f"claim does not exist: {from_claim_id}")
        if to_kind == "claim":
            target = _valid_record_id(to_ref, "claim target")
            if self._get_claim_locked(conn, target) is None:
                raise InvariantViolation(f"target claim does not exist: {target}")
            if link_type == "supersedes" and target == from_claim_id:
                raise InvariantViolation("a claim cannot supersede itself")
            return target
        if to_kind == "evidence_span":
            target = _valid_record_id(to_ref, "evidence span target")
            span = self._get_span_locked(conn, target)
            if span is None:
                raise InvariantViolation(f"evidence span does not exist: {target}")
            evidence = self._get_evidence_locked(conn, span.evidence_id)
            if span.redacted_at is not None or evidence is None or evidence.redacted_at:
                raise InvariantViolation("cannot link new support to redacted evidence")
            return target
        target = _require_text(to_ref, "to_ref")
        if to_kind == "external_uri" and not urlparse(target).scheme:
            raise InvariantViolation("external_uri targets must use a named URI scheme")
        return target

    def add_link(
        self,
        *,
        from_claim_id: str,
        link_type: str,
        to_kind: str,
        to_ref: str,
        actor: Actor,
        role: Mapping[str, Any] | None = None,
        target_content: Any = None,
        fingerprint_reviewed_at: str | None = None,
        record_id: str | None = None,
        created_at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> ClaimLinkRecord:
        """Append a typed claim link after validating its target matrix."""
        relation = _require_text(link_type, "link_type")
        target_kind = _require_text(to_kind, "to_kind")
        allowed = LINK_TARGETS.get(relation)
        if allowed is None:
            raise InvariantViolation(f"unsupported link_type {relation!r}")
        if target_kind not in allowed:
            raise InvariantViolation(
                f"{relation} cannot target {target_kind}. Expected {sorted(allowed)}"
            )
        _validate_agent(actor)
        source = _valid_record_id(from_claim_id, "from_claim_id")
        identifier = _record_id(record_id, "link id")
        created = _timestamp(created_at, "created_at")
        role_data = _json_object(role)
        if relation == "supersedes":
            reason = role_data.get("supersession_reason")
            if reason not in SUPERSESSION_REASONS:
                raise InvariantViolation(
                    "supersedes role requires a supported supersession_reason"
                )
        if relation in {"about_entity", "cites_external"}:
            if target_content is None:
                if fingerprint_reviewed_at is not None:
                    raise InvariantViolation(
                        "fingerprint_reviewed_at requires target_content"
                    )
                fingerprint = None
                reviewed = None
            else:
                try:
                    fingerprint = compute_target_fingerprint(
                        relation,
                        target_content,
                    )
                except (TypeError, ValueError) as exc:
                    raise InvariantViolation(str(exc)) from exc
                reviewed = _timestamp(
                    fingerprint_reviewed_at or created,
                    "fingerprint_reviewed_at",
                )
        else:
            if target_content is not None or fingerprint_reviewed_at is not None:
                raise InvariantViolation(
                    "immutable link targets do not accept fingerprint content"
                )
            fingerprint = None
            reviewed = None

        with self.write_transaction(conn) as write_conn:
            target = self._validate_link_target_locked(
                write_conn,
                from_claim_id=source,
                link_type=relation,
                to_kind=target_kind,
                to_ref=to_ref,
            )
            record = ClaimLinkRecord(
                id=identifier,
                from_claim_id=source,
                link_type=relation,
                to_kind=target_kind,
                to_ref=target,
                role_json=canonical_json(role_data) if role_data else None,
                target_fingerprint=fingerprint,
                fingerprint_reviewed_at=reviewed,
                created_at=created,
                created_by_kind=actor.kind,
                created_by_ref=actor.ref,
            )
            return self._insert_link_locked(write_conn, record)

    def get_link_retraction(
        self,
        link_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> LinkRetractionRecord | None:
        sql = "SELECT * FROM link_retractions WHERE link_id = ?"
        if conn is not None:
            return _row(LinkRetractionRecord, conn.execute(sql, (link_id,)).fetchone())
        with self._read_connection() as read_conn:
            return _row(
                LinkRetractionRecord,
                read_conn.execute(sql, (link_id,)).fetchone(),
            )

    def retract_link(
        self,
        *,
        link_id: str,
        actor: Actor,
        reason: str | None = None,
        at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> LinkRetractionRecord:
        """Append the sole retraction companion for one claim link."""
        identifier = _valid_record_id(link_id, "link_id")
        _validate_agent(actor)
        timestamp = _timestamp(at, "link retraction at")
        normalized_reason = None if reason is None else _require_text(reason, "reason")
        with self.write_transaction(conn) as write_conn:
            link = self._get_link_locked(write_conn, identifier)
            if link is None:
                raise InvariantViolation(f"claim link does not exist: {identifier}")
            if _parse_time(timestamp, "link retraction at") < _parse_time(
                link.created_at,
                "link created_at",
            ):
                raise InvariantViolation("link retraction cannot predate its link")
            existing = self.get_link_retraction(identifier, conn=write_conn)
            if existing is not None:
                return existing
            if link.link_type == "supersedes" and link.to_kind == "claim":
                predecessor_status = self._latest_status_locked(
                    write_conn,
                    link.to_ref,
                    include_overlay=False,
                )
                if (
                    predecessor_status is not None
                    and predecessor_status.status == "superseded"
                    and predecessor_status.basis_kind == "claim_link"
                    and predecessor_status.basis_ref == identifier
                ):
                    raise InvariantViolation(
                        "cannot retract the supersedes link that authorizes a "
                        "claim's current superseded status"
                    )
            if link.link_type == "conflicts_with" and link.to_kind == "claim":
                challenged_status = self._latest_status_locked(
                    write_conn,
                    link.to_ref,
                    include_overlay=False,
                )
                if (
                    challenged_status is not None
                    and challenged_status.status == "challenged"
                    and challenged_status.basis_kind == "conflict_link"
                    and challenged_status.basis_ref == identifier
                ):
                    raise InvariantViolation(
                        "cannot retract the conflict link that authorizes a "
                        "claim's current challenged status"
                    )
            record = LinkRetractionRecord(
                link_id=identifier,
                at=timestamp,
                actor_kind=actor.kind,
                actor_ref=actor.ref,
                reason=normalized_reason,
            )
            write_conn.execute(
                "INSERT INTO link_retractions "
                "(link_id, at, actor_kind, actor_ref, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    record.link_id,
                    record.at,
                    record.actor_kind,
                    record.actor_ref,
                    record.reason,
                ),
            )
            self._insert_ledger_record_locked(
                write_conn,
                "link_retraction",
                record.link_id,
            )
            return record

    @staticmethod
    def _coerce_premise(value: Any) -> PremiseRef:
        if isinstance(value, PremiseRef):
            return value
        if isinstance(value, str):
            kind = "uri" if value.strip().startswith("wb-truth://") else "local"
            return PremiseRef(kind=kind, ref=value.strip())
        if isinstance(value, Mapping):
            try:
                return PremiseRef(kind=value["kind"], ref=value["ref"])
            except (KeyError, TypeError, ValueError) as exc:
                raise InvariantViolation(
                    "premise mappings require valid kind and ref fields"
                ) from exc
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            if len(value) == 2:
                try:
                    return PremiseRef(kind=value[0], ref=value[1])
                except (TypeError, ValueError) as exc:
                    raise InvariantViolation("invalid premise pair") from exc
        raise InvariantViolation(
            "premises must be PremiseRef values, refs, mappings, or pairs"
        )

    def add_derivation(
        self,
        *,
        claim_id: str,
        method: str,
        premises: Sequence[Any],
        actor: Actor,
        confidence: float | None = None,
        rationale: str | None = None,
        record_id: str | None = None,
        created_at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> DerivationRecord:
        """Append a reified derivation and its ordered-independent premises."""
        conclusion = _valid_record_id(claim_id, "claim_id")
        derivation_method = _require_text(method, "method")
        _validate_agent(actor)
        items = tuple(self._coerce_premise(item) for item in premises)
        if not items:
            raise InvariantViolation("a derivation requires at least one premise")
        if len({item.ref for item in items}) != len(items):
            raise InvariantViolation("derivation premises must be unique")
        identifier = _record_id(record_id, "derivation id")
        created = _timestamp(created_at, "created_at")
        confidence_value = _validate_confidence(confidence, "confidence")
        rationale_value = (
            None if rationale is None else _require_text(rationale, "rationale")
        )

        with self.write_transaction(conn) as write_conn:
            if self._get_claim_locked(write_conn, conclusion) is None:
                raise InvariantViolation(f"claim does not exist: {conclusion}")
            normalized: list[PremiseRef] = []
            for item in items:
                if item.kind == "local":
                    ref = _valid_record_id(item.ref, "local premise ref")
                    if ref == conclusion:
                        raise InvariantViolation(
                            "a derivation cannot use its conclusion as a premise"
                        )
                    if self._get_claim_locked(write_conn, ref) is None:
                        raise InvariantViolation(f"premise claim does not exist: {ref}")
                    normalized.append(PremiseRef(kind="local", ref=ref))
                    continue
                try:
                    parsed = parse_truth_uri(item.ref)
                except ValueError as exc:
                    raise InvariantViolation(
                        "URI premise is not a valid truth URI"
                    ) from exc
                if parsed.kind != "claim":
                    raise InvariantViolation("URI premises must reference claims")
                normalized.append(PremiseRef(kind="uri", ref=parsed.uri))

            record = DerivationRecord(
                id=identifier,
                claim_id=conclusion,
                method=derivation_method,
                producer_kind=actor.kind,
                producer_ref=actor.ref,
                confidence=confidence_value,
                rationale=rationale_value,
                created_at=created,
                premises=tuple(normalized),
            )
            return self._insert_derivation_locked(write_conn, record)

    # ------------------------------------------------------------------
    # Co-work document surface durable seam (K2, additive).
    #
    # These helpers mirror the generic _insert_*_locked + ledger-record
    # pattern above. The document engine modules (documents.py, proposals.py,
    # expressions.py, ydoc_store.py) own policy and compose these inside a
    # store.write_transaction(conn). Every base-table insert appends one
    # ledger record so the export walk over ledger_records finds the row.
    # ------------------------------------------------------------------

    def _insert_document_locked(
        self,
        conn: sqlite3.Connection,
        record: DocumentRecord,
    ) -> DocumentRecord:
        self._require_transaction(conn)
        conn.execute(
            "INSERT INTO documents "
            "(id, path, title, document_class, content_sha256, "
            "ydoc_snapshot_sha256, created_at, created_by_kind, created_by_ref, "
            "meta_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.path,
                record.title,
                record.document_class,
                record.content_sha256,
                record.ydoc_snapshot_sha256,
                record.created_at,
                record.created_by_kind,
                record.created_by_ref,
                record.meta_json,
            ),
        )
        self._insert_ledger_record_locked(conn, "document", record.id)
        return record

    def _get_document_locked(
        self,
        conn: sqlite3.Connection,
        document_id: str,
    ) -> DocumentRecord | None:
        return _row(
            DocumentRecord,
            conn.execute(
                "SELECT * FROM documents WHERE id = ?", (document_id,)
            ).fetchone(),
        )

    def _get_document_by_path_locked(
        self,
        conn: sqlite3.Connection,
        path: str,
    ) -> DocumentRecord | None:
        return _row(
            DocumentRecord,
            conn.execute(
                "SELECT * FROM documents WHERE path = ?", (path,)
            ).fetchone(),
        )

    def _advance_document_pointers_locked(
        self,
        conn: sqlite3.Connection,
        *,
        document_id: str,
        content_sha256: str | None = None,
        ydoc_snapshot_sha256: str | None = None,
    ) -> DocumentRecord:
        """Advance the latest content/snapshot pointers (narrow carve-out UPDATE).

        Every advance is separately audited by an appended doc_event, so the
        pointer UPDATE is a cache rather than a new ledger record (the
        store_info monotonic-bump precedent).
        """
        self._require_transaction(conn)
        current = self._get_document_locked(conn, document_id)
        if current is None:
            raise InvariantViolation(f"document does not exist: {document_id}")
        new_content = current.content_sha256 if content_sha256 is None else (
            _valid_digest(content_sha256, "content_sha256")
        )
        new_snapshot = (
            current.ydoc_snapshot_sha256
            if ydoc_snapshot_sha256 is None
            else _valid_digest(ydoc_snapshot_sha256, "ydoc_snapshot_sha256")
        )
        conn.execute(
            "UPDATE documents SET content_sha256 = ?, ydoc_snapshot_sha256 = ? "
            "WHERE id = ?",
            (new_content, new_snapshot, document_id),
        )
        refreshed = self._get_document_locked(conn, document_id)
        assert refreshed is not None
        return refreshed

    def _insert_document_span_locked(
        self,
        conn: sqlite3.Connection,
        record: DocumentSpanRecord,
    ) -> DocumentSpanRecord:
        self._require_transaction(conn)
        conn.execute(
            "INSERT INTO document_spans "
            "(id, document_id, selector_json, quote_exact, span_sha256, "
            "author_kind, author_ref, created_at, created_by_kind, "
            "created_by_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.document_id,
                record.selector_json,
                record.quote_exact,
                record.span_sha256,
                record.author_kind,
                record.author_ref,
                record.created_at,
                record.created_by_kind,
                record.created_by_ref,
            ),
        )
        self._insert_ledger_record_locked(conn, "document_span", record.id)
        return record

    def _get_document_span_locked(
        self,
        conn: sqlite3.Connection,
        span_id: str,
    ) -> DocumentSpanRecord | None:
        return _row(
            DocumentSpanRecord,
            conn.execute(
                "SELECT * FROM document_spans WHERE id = ?", (span_id,)
            ).fetchone(),
        )

    def _find_document_span_locked(
        self,
        conn: sqlite3.Connection,
        *,
        document_id: str,
        span_sha256: str,
    ) -> DocumentSpanRecord | None:
        return _row(
            DocumentSpanRecord,
            conn.execute(
                "SELECT * FROM document_spans WHERE document_id = ? "
                "AND span_sha256 = ? ORDER BY created_at, id LIMIT 1",
                (document_id, span_sha256),
            ).fetchone(),
        )

    def _insert_expression_locked(
        self,
        conn: sqlite3.Connection,
        record: ExpressionRecord,
    ) -> ExpressionRecord:
        self._require_transaction(conn)
        conn.execute(
            "INSERT INTO expressions "
            "(id, document_span_id, claim_ref_kind, claim_ref, role, "
            "claim_canonical_sha256, span_sha256, created_at, created_by_kind, "
            "created_by_ref, meta_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.document_span_id,
                record.claim_ref_kind,
                record.claim_ref,
                record.role,
                record.claim_canonical_sha256,
                record.span_sha256,
                record.created_at,
                record.created_by_kind,
                record.created_by_ref,
                record.meta_json,
            ),
        )
        self._insert_ledger_record_locked(conn, "expression", record.id)
        return record

    def _insert_proposal_locked(
        self,
        conn: sqlite3.Connection,
        record: ProposalRecord,
    ) -> ProposalRecord:
        self._require_transaction(conn)
        conn.execute(
            "INSERT INTO proposals "
            "(id, document_id, base_content_sha256, selector_json, quote_exact, "
            "span_sha256, replacement, rationale, tldr, claim_refs_json, "
            "canonical_sha256, dedup_key, expires_at, created_at, "
            "created_by_kind, created_by_ref, meta_json, redacted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.document_id,
                record.base_content_sha256,
                record.selector_json,
                record.quote_exact,
                record.span_sha256,
                record.replacement,
                record.rationale,
                record.tldr,
                record.claim_refs_json,
                record.canonical_sha256,
                record.dedup_key,
                record.expires_at,
                record.created_at,
                record.created_by_kind,
                record.created_by_ref,
                record.meta_json,
                record.redacted_at,
            ),
        )
        self._insert_ledger_record_locked(conn, "proposal", record.id)
        return record

    def _get_proposal_locked(
        self,
        conn: sqlite3.Connection,
        proposal_id: str,
    ) -> ProposalRecord | None:
        return _row(
            ProposalRecord,
            conn.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone(),
        )

    def _redact_proposal_content_locked(
        self,
        conn: sqlite3.Connection,
        *,
        proposal_id: str,
        at: str,
    ) -> ProposalRecord:
        """Null out proposal content while retaining ids/hashes/dedup_key.

        This is the proposals redaction carve-out UPDATE (the claim-redaction
        shape). It satisfies the proposals_append_only_update carve-out trigger:
        content fields null out, selector_json becomes the REDACTED marker, and
        every id/hash/dedup_key/meta_json is preserved so gesture bindings and
        suppression memory survive (I10).
        """
        self._require_transaction(conn)
        current = self._get_proposal_locked(conn, proposal_id)
        if current is None:
            raise InvariantViolation(f"proposal does not exist: {proposal_id}")
        if current.redacted_at is not None:
            return current
        redacted_at = _timestamp(at, "proposal redacted_at")
        conn.execute(
            "UPDATE proposals SET redacted_at = ?, quote_exact = NULL, "
            "replacement = NULL, rationale = NULL, tldr = NULL, "
            "claim_refs_json = NULL, selector_json = ? WHERE id = ?",
            (redacted_at, REDACTED_SELECTOR_JSON, proposal_id),
        )
        refreshed = self._get_proposal_locked(conn, proposal_id)
        assert refreshed is not None
        return refreshed

    def _insert_proposal_status_event_locked(
        self,
        conn: sqlite3.Connection,
        *,
        proposal_id: str,
        status: str,
        decision: str | None,
        actor: Actor,
        basis_kind: str,
        basis_ref: str | None = None,
        note: str | None = None,
        event_id: str | None = None,
        at: str | None = None,
    ) -> ProposalStatusEventRecord:
        self._require_transaction(conn)
        if status not in PROPOSAL_STATUSES:
            raise InvariantViolation(f"invalid proposal status {status!r}")
        if decision is not None and decision not in PROPOSAL_DECISIONS:
            raise InvariantViolation(f"invalid proposal decision {decision!r}")
        if self._get_proposal_locked(conn, proposal_id) is None:
            raise InvariantViolation(f"proposal does not exist: {proposal_id}")
        identifier = _record_id(event_id, "proposal status event id")
        timestamp = _timestamp(at, "proposal status event at")
        basis = _require_text(basis_kind, "basis_kind")
        cursor = conn.execute(
            "INSERT INTO proposal_status_events "
            "(id, proposal_id, status, decision, at, actor_kind, actor_ref, "
            "basis_kind, basis_ref, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identifier,
                proposal_id,
                status,
                decision,
                timestamp,
                actor.kind,
                actor.ref,
                basis,
                basis_ref,
                note,
            ),
        )
        self._insert_ledger_record_locked(
            conn,
            "proposal_status_event",
            identifier,
        )
        return ProposalStatusEventRecord(
            seq=int(cursor.lastrowid),
            id=identifier,
            proposal_id=proposal_id,
            status=status,
            decision=decision,
            at=timestamp,
            actor_kind=actor.kind,
            actor_ref=actor.ref,
            basis_kind=basis,
            basis_ref=basis_ref,
            note=note,
        )

    def _latest_proposal_status_locked(
        self,
        conn: sqlite3.Connection,
        proposal_id: str,
    ) -> ProposalStatusEventRecord | None:
        return _row(
            ProposalStatusEventRecord,
            conn.execute(
                "SELECT * FROM proposal_status_events WHERE proposal_id = ? "
                "ORDER BY seq DESC LIMIT 1",
                (proposal_id,),
            ).fetchone(),
        )

    def _insert_doc_event_locked(
        self,
        conn: sqlite3.Connection,
        record: DocEventRecord,
    ) -> DocEventRecord:
        self._require_transaction(conn)
        conn.execute(
            "INSERT INTO doc_events "
            "(id, document_id, kind, at, actor_kind, actor_ref, content_sha256, "
            "ydoc_snapshot_sha256, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.document_id,
                record.kind,
                record.at,
                record.actor_kind,
                record.actor_ref,
                record.content_sha256,
                record.ydoc_snapshot_sha256,
                record.detail,
            ),
        )
        self._insert_ledger_record_locked(conn, "doc_event", record.id)
        return record

    def _document_events_locked(
        self,
        conn: sqlite3.Connection,
        document_id: str,
    ) -> tuple[DocEventRecord, ...]:
        """Return every doc_event for one document in rowid insertion order."""
        return tuple(
            DocEventRecord(**dict(row))
            for row in conn.execute(
                "SELECT * FROM doc_events WHERE document_id = ? ORDER BY rowid",
                (document_id,),
            )
        )

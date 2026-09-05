"""SQLite authority for Journal captures, entries, and domain effects."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping

from work_buddy.backups.source_foundation_restore import (
    require_source_foundation_writable,
    source_foundation_read_only,
)
from work_buddy.journal_capture.models import (
    CaptureMode,
    CaptureTarget,
    EffectState,
    JournalCapture,
    JournalCaptureError,
    JournalCaptureConflict,
    JournalCutoverPaused,
    JournalDayComposition,
    JournalDayModule,
    JournalDocumentBinding,
    JournalModuleDocumentBinding,
    JournalDocumentUsageTransition,
    JournalEffect,
    JournalEntry,
    JournalFieldValue,
    JournalMigrationComparison,
    JournalMigrationRecord,
    JournalMigrationState,
    JournalModuleInstanceVersion,
    JournalNativeItem,
    JournalProfileRevision,
    JournalSearchEvent,
    JournalValueDisposition,
    JournalValueKind,
    ProcessingState,
    ProjectionState,
)
from work_buddy.journal_capture.migrations import JOURNAL_MIGRATIONS
from work_buddy.installed_authority import require_domain_store_open
from work_buddy.paths import resolve


_SCHEMA_VERSION = JOURNAL_MIGRATIONS.target_version


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_json(value: str | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else None


def _local_date_from_day_id(day_id: str) -> str:
    """Recover the stable date component from legacy and canonical day IDs."""

    prefix = "journal-day:"
    candidate = day_id[len(prefix) :] if day_id.startswith(prefix) else day_id
    return candidate[:10]


class JournalCaptureStore:
    """Domain store kept separate from Sources and legacy Markdown.

    Cross-database delivery is idempotent by ``source_effect_id`` and
    ``submission_id``.  No exact source bytes are placed in the capture row;
    domain composition is stored only after Journal accepts the command.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        self.path = (
            Path(path).expanduser().resolve()
            if path is not None
            else resolve("db/journal-capture").expanduser().resolve()
        )
        self.read_only = bool(read_only)
        # This check happens before schema preflight or initialization.  Once
        # the independent installation latch is sealed, a missing/replaced
        # Journal database must never be recreated as compatibility state.
        require_domain_store_open("journal", self.path)
        if self.path.is_file():
            self._preflight_existing_schema()
        if self.read_only or source_foundation_read_only():
            if not self.path.is_file():
                raise JournalCaptureError(
                    "journal_capture_state_missing_during_restore_reconciliation"
                )
            self._validate_existing()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def _preflight_existing_schema(self) -> None:
        """Reject future schemas before WAL, DDL, or migration history writes."""

        try:
            with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as conn:
                user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                legacy_version: int | None = None
                if "journal_meta" in tables:
                    row = conn.execute(
                        "SELECT value FROM journal_meta WHERE key='schema_version'"
                    ).fetchone()
                    if row is not None:
                        try:
                            legacy_version = int(row[0])
                        except (TypeError, ValueError) as exc:
                            raise JournalCaptureError(
                                "unsupported_journal_capture_schema"
                            ) from exc
        except JournalCaptureError:
            raise
        except sqlite3.Error as exc:
            raise JournalCaptureError("unsupported_journal_capture_schema") from exc
        if user_version > _SCHEMA_VERSION:
            raise JournalCaptureError("unsupported_journal_capture_schema")
        # Only versions 1..7 ever used the informal journal_meta marker with
        # user_version=0.  A larger marker is future state this process must
        # not baseline-stamp or modify.
        if user_version == 0 and legacy_version is not None and legacy_version > 7:
            raise JournalCaptureError("unsupported_journal_capture_schema")

    def _connect(self) -> sqlite3.Connection:
        read_only = self.read_only or source_foundation_read_only()
        conn = sqlite3.connect(
            f"file:{self.path}?mode=ro" if read_only else self.path,
            timeout=10.0,
            uri=read_only,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        if read_only:
            conn.execute("PRAGMA query_only = ON")
        else:
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise JournalCaptureError("journal_capture_store_is_read_only")
        require_source_foundation_writable("journal_capture.write")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            JOURNAL_MIGRATIONS.run(conn)

    def _validate_existing(self) -> None:
        try:
            with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchall()
                user_version = int(
                    conn.execute("PRAGMA user_version").fetchone()[0]
                )
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                version = (
                    conn.execute(
                        "SELECT value FROM journal_meta WHERE key='schema_version'"
                    ).fetchone()
                    if "journal_meta" in tables
                    else None
                )
        except sqlite3.Error as exc:
            raise JournalCaptureError(
                "journal_capture_state_invalid_during_restore_reconciliation"
            ) from exc
        try:
            schema_version = None if version is None else int(version[0])
        except (TypeError, ValueError):
            schema_version = None
        # Historical Journal schemas used only the informal meta marker and
        # therefore have ``user_version=0``. Native migrations keep both
        # markers equal. Read-only operators must be able to inspect any
        # coherent supported version without silently upgrading it; malformed,
        # future, or integrity-failing state remains fenced.
        legacy_supported = (
            user_version == 0
            and schema_version is not None
            and 1 <= schema_version <= 7
        )
        native_supported = (
            1 <= user_version <= _SCHEMA_VERSION
            and schema_version == user_version
        )
        if integrity != [("ok",)] or not (legacy_supported or native_supported):
            raise JournalCaptureError(
                "journal_capture_state_invalid_during_restore_reconciliation"
            )

    def create_capture(
        self,
        *,
        client_mutation_id: str,
        request_sha256: str,
        source_ref: str,
        representation_id: str,
        submission_id: str,
        command_id: str,
        source_effect_id: str,
        source_usage_id: str | None = None,
        day_id: str,
        requested_target: CaptureTarget,
        mode: CaptureMode,
        input_mode: str,
        stated_at: str | None,
        submitted_at: str,
        authorization_fingerprint: str,
        authorization_expires_at: str | None = None,
        postseal_drain_batch_id: str | None = None,
    ) -> JournalCapture:
        now = _utc_now()
        capture_id = uuid.uuid4().hex
        processing = (
            ProcessingState.PENDING
            if mode is CaptureMode.SMART or requested_target is CaptureTarget.AUTO
            else ProcessingState.NOT_REQUESTED
        )
        effect_types = ["materialize"]
        if requested_target is CaptureTarget.AUTO:
            effect_types.insert(0, "auto_route")
        elif mode is CaptureMode.SMART:
            effect_types.append("smart_annotate")
        with self.transaction() as conn:
            if postseal_drain_batch_id is not None:
                self._require_postseal_drain_effect(
                    conn,
                    batch_mutation_id=postseal_drain_batch_id,
                    effect_id=source_effect_id,
                )
            prior = conn.execute(
                "SELECT * FROM journal_captures WHERE client_mutation_id=?",
                (client_mutation_id,),
            ).fetchone()
            if prior is not None:
                if prior["request_sha256"] != request_sha256:
                    raise JournalCaptureConflict(
                        "That capture key was already used for different input."
                    )
                if postseal_drain_batch_id is not None:
                    self._record_postseal_drain_capture(
                        conn,
                        batch_mutation_id=postseal_drain_batch_id,
                        effect_id=source_effect_id,
                        capture_id=str(prior["capture_id"]),
                        request_sha256=request_sha256,
                        created_at=now,
                    )
                return self._capture(prior)
            by_submission = conn.execute(
                "SELECT * FROM journal_captures WHERE submission_id=? OR source_effect_id=?",
                (submission_id, source_effect_id),
            ).fetchone()
            if by_submission is not None:
                if by_submission["request_sha256"] != request_sha256:
                    raise JournalCaptureConflict(
                        "The source command is already bound to a different capture."
                    )
                if postseal_drain_batch_id is not None:
                    self._record_postseal_drain_capture(
                        conn,
                        batch_mutation_id=postseal_drain_batch_id,
                        effect_id=source_effect_id,
                        capture_id=str(by_submission["capture_id"]),
                        request_sha256=request_sha256,
                        created_at=now,
                    )
                return self._capture(by_submission)
            gate = conn.execute(
                "SELECT state FROM journal_cutover_gate WHERE singleton=1"
            ).fetchone()
            if gate is None or (
                str(gate["state"]) != "open"
                and postseal_drain_batch_id is None
            ):
                raise JournalCutoverPaused()
            conn.execute(
                """
                INSERT INTO journal_captures(
                    capture_id,client_mutation_id,request_sha256,source_ref,
                    representation_id,submission_id,command_id,source_effect_id,source_usage_id,
                    day_id,requested_target,mode,input_mode,stated_at,submitted_at,
                    processing_status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    capture_id,
                    client_mutation_id,
                    request_sha256,
                    source_ref,
                    representation_id,
                    submission_id,
                    command_id,
                    source_effect_id,
                    source_usage_id,
                    day_id,
                    requested_target.value,
                    mode.value,
                    input_mode,
                    stated_at,
                    submitted_at,
                    processing.value,
                    now,
                    now,
                ),
            )
            for effect_type in effect_types:
                effect_id = (
                    source_effect_id
                    if effect_type == "materialize" and len(effect_types) == 1
                    else hashlib.sha256(
                        f"{source_effect_id}:{effect_type}".encode("utf-8")
                    ).hexdigest()[:32]
                )
                conn.execute(
                    """
                    INSERT INTO journal_effects(
                        effect_id,capture_id,effect_type,state,
                        authorization_fingerprint,authorization_expires_at,
                        created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        effect_id,
                        capture_id,
                        effect_type,
                        EffectState.PENDING.value,
                        authorization_fingerprint,
                        authorization_expires_at,
                        now,
                        now,
                    ),
                )
            if postseal_drain_batch_id is not None:
                self._record_postseal_drain_capture(
                    conn,
                    batch_mutation_id=postseal_drain_batch_id,
                    effect_id=source_effect_id,
                    capture_id=capture_id,
                    request_sha256=request_sha256,
                    created_at=now,
                )
            row = conn.execute(
                "SELECT * FROM journal_captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
            assert row is not None
            return self._capture(row)

    @staticmethod
    def _require_postseal_drain_effect(
        conn: sqlite3.Connection,
        *,
        batch_mutation_id: str,
        effect_id: str,
    ) -> None:
        row = conn.execute(
            "SELECT batch.cohort_id,authority.mode,authority.activated_cohort_id,"
            "gate.state AS gate_state,gate.cohort_id AS gate_cohort_id,"
            "maintenance.state AS maintenance_state,"
            "maintenance.cohort_id AS maintenance_cohort_id,"
            "receipt.batch_mutation_id AS completed_batch "
            "FROM journal_cutover_source_drain_effects AS effect "
            "JOIN journal_cutover_source_drain_batches AS batch "
            "ON batch.mutation_id=effect.batch_mutation_id "
            "CROSS JOIN journal_authority_control AS authority "
            "CROSS JOIN journal_cutover_gate AS gate "
            "CROSS JOIN cutover_maintenance AS maintenance "
            "LEFT JOIN journal_cutover_source_drain_receipts AS receipt "
            "ON receipt.batch_mutation_id=batch.mutation_id "
            "WHERE effect.batch_mutation_id=? AND effect.effect_id=? "
            "AND authority.singleton=1 AND gate.singleton=1 "
            "AND maintenance.singleton=1",
            (batch_mutation_id, effect_id),
        ).fetchone()
        if (
            row is None
            or row["completed_batch"] is not None
            or str(row["mode"]) != "database_only"
            or str(row["activated_cohort_id"] or "") != str(row["cohort_id"])
            or str(row["gate_state"]) != "paused"
            or str(row["gate_cohort_id"] or "") != str(row["cohort_id"])
            or str(row["maintenance_state"]) != "postseal_pending"
            or str(row["maintenance_cohort_id"] or "") != str(row["cohort_id"])
        ):
            raise JournalCutoverPaused()

    @staticmethod
    def _record_postseal_drain_capture(
        conn: sqlite3.Connection,
        *,
        batch_mutation_id: str,
        effect_id: str,
        capture_id: str,
        request_sha256: str,
        created_at: str,
    ) -> None:
        row = conn.execute(
            "SELECT rowid,source_effect_id,request_sha256 FROM journal_captures "
            "WHERE capture_id=?",
            (capture_id,),
        ).fetchone()
        if (
            row is None
            or str(row["source_effect_id"]) != effect_id
            or str(row["request_sha256"]) != request_sha256
        ):
            raise JournalCaptureConflict(
                "The controlled Journal capture does not match its Source command."
            )
        prior = conn.execute(
            "SELECT * FROM journal_cutover_source_drain_captures "
            "WHERE batch_mutation_id=? AND effect_id=?",
            (batch_mutation_id, effect_id),
        ).fetchone()
        expected = (capture_id, int(row["rowid"]), request_sha256)
        if prior is not None:
            observed = (
                str(prior["capture_id"]),
                int(prior["capture_rowid"]),
                str(prior["capture_request_sha256"]),
            )
            if observed != expected:
                raise JournalCaptureConflict(
                    "The controlled Journal capture receipt conflicts."
                )
            return
        conn.execute(
            "INSERT INTO journal_cutover_source_drain_captures("
            "batch_mutation_id,effect_id,capture_id,capture_rowid,"
            "capture_request_sha256,created_at) VALUES(?,?,?,?,?,?)",
            (
                batch_mutation_id,
                effect_id,
                capture_id,
                int(row["rowid"]),
                request_sha256,
                created_at,
            ),
        )

    def get_capture(self, capture_id: str) -> JournalCapture | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
        return None if row is None else self._capture(row)

    def get_capture_by_mutation(self, client_mutation_id: str) -> JournalCapture | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_captures WHERE client_mutation_id=?",
                (client_mutation_id,),
            ).fetchone()
        return None if row is None else self._capture(row)

    def get_capture_by_source_effect(self, source_effect_id: str) -> JournalCapture | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_captures WHERE source_effect_id=?",
                (source_effect_id,),
            ).fetchone()
        return None if row is None else self._capture(row)

    def list_captures(self, day_id: str, *, limit: int = 20) -> list[JournalCapture]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM journal_captures
                   WHERE day_id=? ORDER BY submitted_at DESC LIMIT ?""",
                (day_id, max(1, min(limit, 100))),
            ).fetchall()
        return [self._capture(row) for row in rows]

    @staticmethod
    def _ensure_native_entry_bridge(
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> None:
        """Bridge legacy entry identity without copying its authoritative prose."""

        lifecycle = "current" if row["resolution_state"] == "open" else "resolved"
        conn.execute(
            """
            INSERT OR IGNORE INTO journal_items(
                item_id,local_date,item_kind,authority_kind,legacy_entry_id,
                interaction_behavior_id,interaction_behavior_version,privacy_class,
                search_mode,source_ref,lifecycle,current_revision,created_at,updated_at
            ) VALUES(?,?,?,?,?,'human_value',1,'private','lexical_dense',?,?,?,?,?)
            """,
            (
                row["entry_id"],
                _local_date_from_day_id(str(row["day_id"])),
                "record" if row["entry_kind"] == "log" else "running_note",
                "legacy_entry",
                row["entry_id"],
                row["source_ref"],
                lifecycle,
                int(row["version"]),
                row["created_at"],
                row["updated_at"],
            ),
        )
        event_id = "jso_" + hashlib.sha256(
            f"item\x00{row['entry_id']}\x00{row['version']}\x00upsert".encode("utf-8")
        ).hexdigest()[:32]
        composition = conn.execute(
            """
            SELECT s.composition_digest
            FROM journal_day_composition_snapshots AS s
            JOIN journal_days AS d ON d.day_id=s.day_id
            WHERE d.local_date=?
            """,
            (_local_date_from_day_id(str(row["day_id"])),),
        ).fetchone()
        conn.execute(
            """
            INSERT OR IGNORE INTO journal_search_outbox(
                event_id,aggregate_type,aggregate_id,aggregate_revision,event_kind,
                content_sha256,composition_digest,search_recipe_version,privacy_class,
                committed_at
            ) VALUES(?,'item',?,?,'upsert',?,?,1,'private',?)
            """,
            (
                event_id,
                row["entry_id"],
                str(row["version"]),
                row["content_sha256"],
                composition[0] if composition is not None else None,
                row["updated_at"],
            ),
        )

    def ensure_entry(
        self,
        *,
        capture_id: str,
        entry_kind: CaptureTarget,
        markdown: str,
        content_sha256: str,
        projection_marker: str,
        created_at: str,
    ) -> JournalEntry:
        if entry_kind is CaptureTarget.AUTO:
            raise ValueError("auto is not an entry kind")
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM journal_entries WHERE capture_id=?", (capture_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["entry_kind"] != entry_kind.value
                    or existing["content_sha256"] != content_sha256
                ):
                    raise JournalCaptureConflict(
                        "The capture is already materialized differently."
                    )
                self._ensure_native_entry_bridge(conn, existing)
                return self._entry(existing)
            capture = conn.execute(
                "SELECT * FROM journal_captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
            if capture is None:
                raise KeyError("journal_capture_not_found")
            entry_id = hashlib.sha256(f"journal-entry:{capture_id}".encode()).hexdigest()[:32]
            now = _utc_now()
            conn.execute(
                """
                INSERT INTO journal_entries(
                    entry_id,capture_id,day_id,entry_kind,source_ref,
                    content_sha256,markdown,created_at,updated_at,
                    processing_status,projection_marker
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    entry_id,
                    capture_id,
                    capture["day_id"],
                    entry_kind.value,
                    capture["source_ref"],
                    content_sha256,
                    markdown,
                    created_at,
                    now,
                    capture["processing_status"],
                    projection_marker,
                ),
            )
            conn.execute(
                """UPDATE journal_captures
                   SET resolved_target=?,entry_id=?,revision=revision+1,updated_at=?
                   WHERE capture_id=?""",
                (entry_kind.value, entry_id, now, capture_id),
            )
            row = conn.execute(
                "SELECT * FROM journal_entries WHERE entry_id=?", (entry_id,)
            ).fetchone()
            assert row is not None
            self._ensure_native_entry_bridge(conn, row)
            return self._entry(row)

    def get_entry(self, entry_id: str) -> JournalEntry | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_entries WHERE entry_id=?", (entry_id,)
            ).fetchone()
        return None if row is None else self._entry(row)

    def get_document_binding(self, entry_id: str) -> JournalDocumentBinding | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
        return None if row is None else self._document_binding(row)

    def get_module_document_binding(
        self,
        *,
        local_date: str,
        module_instance_id: str,
        module_instance_version: int,
    ) -> JournalModuleDocumentBinding | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_module_document_bindings "
                "WHERE local_date=? AND module_instance_id=? "
                "AND module_instance_version=?",
                (local_date, module_instance_id, module_instance_version),
            ).fetchone()
        return None if row is None else self._module_document_binding(row)

    def record_module_document_binding(
        self,
        *,
        local_date: str,
        module_instance_id: str,
        module_instance_version: int,
        domain_entity_id: str,
        binding_id: str,
        store_id: str,
        document_id: str,
        role: str,
        cowork_href: str,
        content_authority_epoch: int,
    ) -> JournalModuleDocumentBinding:
        """Record a deterministic Co-work target without duplicating its body."""

        now = _utc_now()
        with self.transaction() as conn:
            prior = conn.execute(
                "SELECT * FROM journal_module_document_bindings "
                "WHERE local_date=? AND module_instance_id=? "
                "AND module_instance_version=?",
                (local_date, module_instance_id, module_instance_version),
            ).fetchone()
            immutable = (
                domain_entity_id,
                binding_id,
                store_id,
                document_id,
                role,
            )
            if prior is not None and tuple(
                prior[key] for key in (
                    "domain_entity_id", "binding_id", "store_id", "document_id", "role"
                )
            ) != immutable:
                raise JournalCaptureConflict(
                    "That Journal document section is already bound elsewhere."
                )
            if prior is None:
                conn.execute(
                    "INSERT INTO journal_module_document_bindings "
                    "(local_date,module_instance_id,module_instance_version,domain_entity_id,binding_id,"
                    "store_id,document_id,role,cowork_href,content_authority_epoch,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        local_date,
                        module_instance_id,
                        module_instance_version,
                        domain_entity_id,
                        binding_id,
                        store_id,
                        document_id,
                        role,
                        cowork_href,
                        content_authority_epoch,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE journal_module_document_bindings SET cowork_href=?,"
                    "content_authority_epoch=?,updated_at=? WHERE local_date=? "
                    "AND module_instance_id=? AND module_instance_version=?",
                    (
                        cowork_href,
                        content_authority_epoch,
                        now,
                        local_date,
                        module_instance_id,
                        module_instance_version,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM journal_module_document_bindings "
                "WHERE local_date=? AND module_instance_id=? "
                "AND module_instance_version=?",
                (local_date, module_instance_id, module_instance_version),
            ).fetchone()
            assert row is not None
            return self._module_document_binding(row)

    def list_document_bindings(self) -> tuple[JournalDocumentBinding, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM journal_document_bindings "
                "WHERE state != 'retired' ORDER BY entry_id"
            ).fetchall()
        return tuple(self._document_binding(row) for row in rows)

    def get_document_binding_by_source_consumer(
        self, source_consumer_id: str
    ) -> JournalDocumentBinding | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE source_consumer_id=?",
                (source_consumer_id,),
            ).fetchone()
        return None if row is None else self._document_binding(row)

    def record_document_binding(
        self,
        *,
        entry_id: str,
        binding_id: str,
        store_id: str,
        document_id: str,
        change_id: str,
        source_consumer_id: str,
        source_usage_id: str,
        source_use_kind: str = "exact_insertion",
        source_disclosure_kind: str = "exact_readable_copy",
        source_redaction_policy: str = "scrub",
        cowork_href: str,
        content_authority_epoch: int,
        entry_version: int,
        inspection: Mapping[str, Any],
        state: str = "current",
    ) -> JournalDocumentBinding:
        """Persist the Journal-side navigation/authority mirror idempotently."""

        encoded = _json(inspection)
        assert encoded is not None
        now = _utc_now()
        with self.transaction() as conn:
            entry = conn.execute(
                "SELECT entry_id FROM journal_entries WHERE entry_id=?", (entry_id,)
            ).fetchone()
            if entry is None:
                raise KeyError("journal_entry_not_found")
            prior = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            if prior is not None:
                immutable = (
                    prior["binding_id"],
                    prior["store_id"],
                    prior["document_id"],
                    prior["source_consumer_id"],
                    prior["source_usage_id"],
                    prior["source_use_kind"],
                    prior["source_disclosure_kind"],
                    prior["source_redaction_policy"],
                )
                if immutable != (
                    binding_id,
                    store_id,
                    document_id,
                    source_consumer_id,
                    source_usage_id,
                    source_use_kind,
                    source_disclosure_kind,
                    source_redaction_policy,
                ):
                    raise JournalCaptureConflict(
                        "That Running Note is already bound to another document."
                    )
                conn.execute(
                    "UPDATE journal_document_bindings SET change_id=?,cowork_href=?,"
                    "content_authority_epoch=?,entry_version=?,inspection_json=?,"
                    "state=?,updated_at=? WHERE entry_id=?",
                    (
                        change_id,
                        cowork_href,
                        content_authority_epoch,
                        entry_version,
                        encoded,
                        state,
                        now,
                        entry_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO journal_document_bindings "
                    "(entry_id,binding_id,store_id,document_id,change_id,"
                    "source_consumer_id,source_usage_id,source_use_kind,"
                    "source_disclosure_kind,source_redaction_policy,"
                    "source_maintenance_state,source_maintenance_json,cowork_href,"
                    "content_authority_epoch,entry_version,inspection_json,state,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,"
                    "'clean','{}',?,?,?,?,?,?,?)",
                    (
                        entry_id,
                        binding_id,
                        store_id,
                        document_id,
                        change_id,
                        source_consumer_id,
                        source_usage_id,
                        source_use_kind,
                        source_disclosure_kind,
                        source_redaction_policy,
                        cowork_href,
                        content_authority_epoch,
                        entry_version,
                        encoded,
                        state,
                        now,
                        now,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE entry_id=?", (entry_id,)
            ).fetchone()
            assert row is not None
            return self._document_binding(row)

    def transition_document_source_usage(
        self,
        *,
        entry_id: str,
        binding_id: str,
        change_id: str,
        expected_prior_usage_id: str,
        next_usage_id: str,
        next_use_kind: str,
        next_disclosure_kind: str,
        next_redaction_policy: str,
    ) -> tuple[JournalDocumentBinding, JournalDocumentUsageTransition]:
        """Atomically publish a new active dependency and its recovery receipt."""

        transition_id = hashlib.sha256(
            (
                "journal-document-source-transition/v1\0"
                + binding_id
                + "\0"
                + change_id
            ).encode("utf-8")
        ).hexdigest()[:32]
        now = _utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            if row is None or row["binding_id"] != binding_id:
                raise JournalCaptureConflict(
                    "That Running Note source dependency is no longer current."
                )
            prior = conn.execute(
                "SELECT * FROM journal_document_usage_transitions WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            immutable = (
                transition_id,
                entry_id,
                binding_id,
                change_id,
                expected_prior_usage_id,
                next_usage_id,
                next_use_kind,
                next_disclosure_kind,
                next_redaction_policy,
            )
            if prior is not None:
                actual = (
                    prior["transition_id"],
                    prior["entry_id"],
                    prior["binding_id"],
                    prior["change_id"],
                    prior["prior_usage_id"],
                    prior["next_usage_id"],
                    prior["next_use_kind"],
                    prior["next_disclosure_kind"],
                    prior["next_redaction_policy"],
                )
                if actual != immutable:
                    raise JournalCaptureConflict(
                        "That Running Note source dependency changed concurrently."
                    )
            active_usage = str(row["source_usage_id"])
            if active_usage not in {expected_prior_usage_id, next_usage_id}:
                raise JournalCaptureConflict(
                    "That Running Note source dependency changed concurrently."
                )
            if prior is None:
                conn.execute(
                    "INSERT INTO journal_document_usage_transitions "
                    "(transition_id,entry_id,binding_id,change_id,prior_usage_id,"
                    "next_usage_id,next_use_kind,next_disclosure_kind,"
                    "next_redaction_policy,state,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'mirror_updated',?,?)",
                    (*immutable, now, now),
                )
            if active_usage == expected_prior_usage_id:
                conn.execute(
                    "UPDATE journal_document_bindings SET source_usage_id=?,"
                    "source_use_kind=?,source_disclosure_kind=?,source_redaction_policy=?,"
                    "source_maintenance_state='clean',source_maintenance_json='{}',"
                    "updated_at=? WHERE entry_id=?",
                    (
                        next_usage_id,
                        next_use_kind,
                        next_disclosure_kind,
                        next_redaction_policy,
                        now,
                        entry_id,
                    ),
                )
            binding = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE entry_id=?", (entry_id,)
            ).fetchone()
            transition = conn.execute(
                "SELECT * FROM journal_document_usage_transitions WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            assert binding is not None and transition is not None
            return self._document_binding(binding), self._usage_transition(transition)

    def complete_document_source_usage_transition(
        self, transition_id: str
    ) -> JournalDocumentUsageTransition:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_document_usage_transitions WHERE transition_id=?",
                (transition_id,),
            ).fetchone()
            if row is None:
                raise KeyError("journal_document_usage_transition_not_found")
            if row["state"] != "complete":
                conn.execute(
                    "UPDATE journal_document_usage_transitions SET state='complete',"
                    "updated_at=? WHERE transition_id=?",
                    (_utc_now(), transition_id),
                )
            updated = conn.execute(
                "SELECT * FROM journal_document_usage_transitions WHERE transition_id=?",
                (transition_id,),
            ).fetchone()
            assert updated is not None
            return self._usage_transition(updated)

    def get_document_source_usage_transition(
        self, entry_id: str
    ) -> JournalDocumentUsageTransition | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_document_usage_transitions WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
        return None if row is None else self._usage_transition(row)

    def mark_document_source_review_required(
        self,
        entry_id: str,
        *,
        details: Mapping[str, Any],
    ) -> JournalDocumentBinding:
        """Persist content-free attention without resolving the Source effect."""

        encoded = _json(details)
        assert encoded is not None
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT entry_id FROM journal_document_bindings WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            if row is None:
                raise KeyError("journal_document_binding_not_found")
            conn.execute(
                "UPDATE journal_document_bindings SET "
                "source_maintenance_state='review_required',"
                "source_maintenance_json=?,updated_at=? WHERE entry_id=?",
                (encoded, _utc_now(), entry_id),
            )
            updated = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE entry_id=?", (entry_id,)
            ).fetchone()
            assert updated is not None
            return self._document_binding(updated)

    def retire_document_binding(self, entry_id: str) -> JournalDocumentBinding | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE entry_id=?", (entry_id,)
            ).fetchone()
            if row is None:
                return None
            if row["state"] != "retired":
                conn.execute(
                    "UPDATE journal_document_bindings SET state='retired',updated_at=? "
                    "WHERE entry_id=?",
                    (_utc_now(), entry_id),
                )
            updated = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE entry_id=?", (entry_id,)
            ).fetchone()
            assert updated is not None
            return self._document_binding(updated)

    def list_running_notes(self, day_id: str) -> list[JournalEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM journal_entries
                   WHERE day_id=? AND entry_kind='running_notes'
                   AND resolution_state NOT IN ('deleted','redacted')
                   ORDER BY created_at DESC""",
                (day_id,),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def list_log_entries(self, day_id: str) -> list[JournalEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM journal_entries
                   WHERE day_id=? AND entry_kind='log'
                   AND resolution_state NOT IN ('deleted','redacted')
                   ORDER BY created_at""",
                (day_id,),
            ).fetchall()
        return [self._entry(row) for row in rows]

    # ------------------------------------------------------------------
    # Journal prose migration.  Canonical authority/epoch lives in the
    # document-kernel binding; these rows are a content-free recovery mirror.
    # ------------------------------------------------------------------

    def record_migration_selection(
        self,
        *,
        entity_kind: str,
        entity_id: str,
        day_id: str,
        marker_id: str,
        selection_start: int | None,
        selection_end: int | None,
        selected_file_sha256: str,
        selected_section_sha256: str,
    ) -> JournalMigrationRecord:
        if entity_kind not in {"running_note", "logical_day_log"}:
            raise ValueError("invalid Journal migration entity kind")
        if (selection_start is None) != (selection_end is None):
            raise ValueError("Journal selection boundaries must be paired")
        now = _utc_now()
        with self.transaction() as conn:
            if entity_kind == "running_note":
                prior_selection = conn.execute(
                    "SELECT * FROM journal_content_migrations WHERE entity_kind=? "
                    "AND day_id=? AND selection_start IS ? AND selection_end IS ? "
                    "AND selected_section_sha256=?",
                    (
                        entity_kind,
                        day_id,
                        selection_start,
                        selection_end,
                        selected_section_sha256,
                    ),
                ).fetchone()
                if prior_selection is not None:
                    return self._migration(prior_selection)
            row = conn.execute(
                "SELECT * FROM journal_content_migrations WHERE entity_kind=? "
                "AND entity_id=?",
                (entity_kind, entity_id),
            ).fetchone()
            immutable = (
                day_id,
                marker_id,
                selection_start,
                selection_end,
                selected_file_sha256,
                selected_section_sha256,
            )
            if row is None:
                conn.execute(
                    "INSERT INTO journal_content_migrations "
                    "(entity_kind,entity_id,day_id,marker_id,selection_start,"
                    "selection_end,selected_file_sha256,selected_section_sha256,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        entity_kind,
                        entity_id,
                        *immutable,
                        now,
                        now,
                    ),
                )
            else:
                actual = (
                    str(row["day_id"]),
                    str(row["marker_id"]),
                    row["selection_start"],
                    row["selection_end"],
                    str(row["selected_file_sha256"]),
                    str(row["selected_section_sha256"]),
                )
                if actual != immutable:
                    raise JournalCaptureConflict(
                        "That Journal selection changed after identity assignment."
                    )
            refreshed = conn.execute(
                "SELECT * FROM journal_content_migrations WHERE entity_kind=? "
                "AND entity_id=?",
                (entity_kind, entity_id),
            ).fetchone()
            assert refreshed is not None
            return self._migration(refreshed)

    def record_migration_shadow(
        self,
        *,
        entity_kind: str,
        entity_id: str,
        source_ref: str,
        representation_id: str,
        source_content_sha256: str,
        binding_id: str,
        store_id: str,
        document_id: str,
        byte_parity: bool,
        normalized_parity: bool,
        structural_parity: bool,
        operation_id: str,
    ) -> JournalMigrationRecord:
        comparison = (
            JournalMigrationComparison.PARITY
            if byte_parity or normalized_parity or structural_parity
            else JournalMigrationComparison.MISMATCH
        )
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_content_migrations WHERE entity_kind=? "
                "AND entity_id=?",
                (entity_kind, entity_id),
            ).fetchone()
            if row is None:
                raise KeyError("journal_migration_selection_not_found")
            if row["source_content_sha256"] not in {None, source_content_sha256}:
                raise JournalCaptureConflict(
                    "That Journal shadow import no longer matches its selection."
                )
            conn.execute(
                "UPDATE journal_content_migrations SET source_ref=?,"
                "representation_id=?,source_content_sha256=?,binding_id=?,store_id=?,"
                "document_id=?,comparison_state=?,byte_parity=?,normalized_parity=?,"
                "structural_parity=?,"
                "mirrored_state='shadow_imported',operation_id=?,error_code=NULL,"
                "updated_at=? WHERE entity_kind=? AND entity_id=?",
                (
                    source_ref,
                    representation_id,
                    source_content_sha256,
                    binding_id,
                    store_id,
                    document_id,
                    comparison.value,
                    int(byte_parity),
                    int(normalized_parity),
                    int(structural_parity),
                    operation_id,
                    _utc_now(),
                    entity_kind,
                    entity_id,
                ),
            )
            refreshed = conn.execute(
                "SELECT * FROM journal_content_migrations WHERE entity_kind=? "
                "AND entity_id=?",
                (entity_kind, entity_id),
            ).fetchone()
            assert refreshed is not None
            return self._migration(refreshed)

    def mirror_migration_authority(
        self,
        *,
        entity_kind: str,
        entity_id: str,
        state: JournalMigrationState,
        authority_epoch: int,
        rollback_deadline: str | None,
        projection_state: str,
        divergence_source_ref: str | None = None,
        operation_id: str | None = None,
        error_code: str | None = None,
    ) -> JournalMigrationRecord:
        if projection_state not in {
            "none",
            "pending",
            "committed",
            "paused_diverged",
            "failed",
        }:
            raise ValueError("invalid Journal migration projection state")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT mirrored_authority_epoch FROM journal_content_migrations "
                "WHERE entity_kind=? AND entity_id=?",
                (entity_kind, entity_id),
            ).fetchone()
            if row is None:
                raise KeyError("journal_migration_not_found")
            if authority_epoch < int(row["mirrored_authority_epoch"]):
                raise JournalCaptureConflict("The Journal authority epoch moved backwards.")
            conn.execute(
                "UPDATE journal_content_migrations SET mirrored_state=?,"
                "mirrored_authority_epoch=?,rollback_deadline=?,projection_state=?,"
                "divergence_source_ref=?,operation_id=COALESCE(?,operation_id),"
                "error_code=?,updated_at=? WHERE entity_kind=? AND entity_id=?",
                (
                    state.value,
                    authority_epoch,
                    rollback_deadline,
                    projection_state,
                    divergence_source_ref,
                    operation_id,
                    error_code,
                    _utc_now(),
                    entity_kind,
                    entity_id,
                ),
            )
            refreshed = conn.execute(
                "SELECT * FROM journal_content_migrations WHERE entity_kind=? "
                "AND entity_id=?",
                (entity_kind, entity_id),
            ).fetchone()
            assert refreshed is not None
            return self._migration(refreshed)

    def get_migration(
        self, entity_kind: str, entity_id: str
    ) -> JournalMigrationRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_content_migrations WHERE entity_kind=? "
                "AND entity_id=?",
                (entity_kind, entity_id),
            ).fetchone()
        return None if row is None else self._migration(row)

    def migrations_for_day(self, day_id: str) -> tuple[JournalMigrationRecord, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM journal_content_migrations WHERE day_id=? "
                "ORDER BY entity_kind,entity_id",
                (day_id,),
            ).fetchall()
        return tuple(self._migration(row) for row in rows)

    def list_migrations(self) -> tuple[JournalMigrationRecord, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM journal_content_migrations "
                "ORDER BY day_id,entity_kind,entity_id"
            ).fetchall()
        return tuple(self._migration(row) for row in rows)

    def begin_migration_operation(
        self,
        *,
        action: str,
        entity_kind: str,
        entity_id: str,
        idempotency_key: str,
        request_sha256: str,
    ) -> tuple[str, str]:
        operation_id = hashlib.sha256(
            f"journal-migration\0{idempotency_key}".encode("utf-8")
        ).hexdigest()[:32]
        now = _utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_migration_operations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO journal_migration_operations VALUES(?,?,?,?,?,?,'prepared',NULL,?,?)",
                    (
                        operation_id,
                        idempotency_key,
                        action,
                        entity_kind,
                        entity_id,
                        request_sha256,
                        now,
                        now,
                    ),
                )
                return operation_id, "prepared"
            if (
                row["operation_id"] != operation_id
                or row["action"] != action
                or row["entity_kind"] != entity_kind
                or row["entity_id"] != entity_id
                or row["request_sha256"] != request_sha256
            ):
                raise JournalCaptureConflict("Journal migration idempotency conflict.")
            return str(row["operation_id"]), str(row["state"])

    def advance_migration_operation(
        self, operation_id: str, *, state: str, error_code: str | None = None
    ) -> None:
        if state not in {
            "prepared",
            "document_committed",
            "epoch_committed",
            "projection_committed",
            "completed",
            "recoverable",
            "paused_diverged",
        }:
            raise ValueError("invalid Journal migration operation state")
        with self.transaction() as conn:
            found = conn.execute(
                "SELECT operation_id FROM journal_migration_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if found is None:
                raise KeyError("journal_migration_operation_not_found")
            conn.execute(
                "UPDATE journal_migration_operations SET state=?,error_code=?,updated_at=? "
                "WHERE operation_id=?",
                (state, error_code, _utc_now(), operation_id),
            )

    def recoverable_migration_operations(self) -> tuple[Mapping[str, Any], ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM journal_migration_operations WHERE state NOT IN "
                "('completed','paused_diverged') ORDER BY created_at,operation_id"
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def record_exit_evidence(
        self,
        *,
        inventory_sha256: str,
        callsite_inventory_sha256: str,
        authority_summary: Mapping[str, Any],
    ) -> str:
        payload = _json(authority_summary)
        assert payload is not None
        receipt_id = hashlib.sha256(
            (
                "journal-exit-evidence/v1\0"
                + inventory_sha256
                + "\0"
                + callsite_inventory_sha256
                + "\0"
                + payload
            ).encode("utf-8")
        ).hexdigest()[:32]
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO journal_exit_evidence VALUES(?,?,?,?,?)",
                (
                    receipt_id,
                    inventory_sha256,
                    callsite_inventory_sha256,
                    payload,
                    _utc_now(),
                ),
            )
        return receipt_id

    def latest_exit_evidence(
        self,
        *,
        expected_inventory_sha256: str | None = None,
        expected_callsite_inventory_sha256: str | None = None,
    ) -> Mapping[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_exit_evidence ORDER BY created_at DESC,receipt_id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        if (
            expected_inventory_sha256 is not None
            and row["inventory_sha256"] != expected_inventory_sha256
        ):
            return None
        if (
            expected_callsite_inventory_sha256 is not None
            and row["callsite_inventory_sha256"]
            != expected_callsite_inventory_sha256
        ):
            return None
        return {
            "receipt_id": str(row["receipt_id"]),
            "inventory_sha256": str(row["inventory_sha256"]),
            "callsite_inventory_sha256": str(row["callsite_inventory_sha256"]),
            "authority_summary": json.loads(str(row["authority_summary_json"])),
            "created_at": str(row["created_at"]),
        }

    def mark_projection_prepared(
        self, entry_id: str, *, base_sha256: str
    ) -> JournalEntry:
        return self._update_projection(
            entry_id,
            state=ProjectionState.PREPARED,
            base_sha256=base_sha256,
            result_sha256=None,
        )

    def mark_projection_committed(
        self, entry_id: str, *, base_sha256: str, result_sha256: str
    ) -> JournalEntry:
        entry = self._update_projection(
            entry_id,
            state=ProjectionState.COMMITTED,
            base_sha256=base_sha256,
            result_sha256=result_sha256,
        )
        self.finish_effect(entry.capture_id, "materialize", succeeded=True)
        return entry

    def mark_projection_failed(self, entry_id: str, *, error_code: str) -> JournalEntry:
        entry = self._update_projection(
            entry_id,
            state=ProjectionState.FAILED,
            base_sha256=None,
            result_sha256=None,
        )
        self.finish_effect(
            entry.capture_id,
            "materialize",
            succeeded=False,
            error_code=error_code,
        )
        return entry

    def _update_projection(
        self,
        entry_id: str,
        *,
        state: ProjectionState,
        base_sha256: str | None,
        result_sha256: str | None,
    ) -> JournalEntry:
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM journal_entries WHERE entry_id=?", (entry_id,)
            ).fetchone()
            if existing is None:
                raise KeyError("journal_entry_not_found")
            conn.execute(
                """UPDATE journal_entries SET projection_state=?,
                   projection_base_sha256=COALESCE(?,projection_base_sha256),
                   projection_result_sha256=COALESCE(?,projection_result_sha256),
                   updated_at=? WHERE entry_id=?""",
                (state.value, base_sha256, result_sha256, _utc_now(), entry_id),
            )
            row = conn.execute(
                "SELECT * FROM journal_entries WHERE entry_id=?", (entry_id,)
            ).fetchone()
            assert row is not None
            return self._entry(row)

    def lease_effect(
        self,
        effect_id: str,
        *,
        owner: str,
        lease_seconds: int = 60,
    ) -> JournalEffect | None:
        now = datetime.now(UTC)
        now_s = now.isoformat()
        expires = (now + timedelta(seconds=max(5, lease_seconds))).isoformat()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_effects WHERE effect_id=?", (effect_id,)
            ).fetchone()
            if row is None or row["state"] == EffectState.SUCCEEDED.value:
                return None
            if row["error_code"] == "journal_proposal_source_withdrawn":
                return None
            if (
                row["authorization_expires_at"] is not None
                and row["authorization_expires_at"] <= now_s
            ):
                conn.execute(
                    """UPDATE journal_effects SET state='paused',
                       error_code='journal_authorization_expired',
                       lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                       WHERE effect_id=?""",
                    (now_s, effect_id),
                )
                return None
            if row["state"] == EffectState.PAUSED.value:
                return None
            if (
                row["state"] == EffectState.RUNNING.value
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] > now_s
            ):
                if row["lease_owner"] != owner:
                    return None
                return self._effect(row)
            conn.execute(
                """UPDATE journal_effects SET state='running',attempts=attempts+1,
                   lease_owner=?,lease_expires_at=?,error_code=NULL,updated_at=?
                   WHERE effect_id=?""",
                (owner, expires, now_s, effect_id),
            )
            updated = conn.execute(
                "SELECT * FROM journal_effects WHERE effect_id=?", (effect_id,)
            ).fetchone()
            assert updated is not None
            return self._effect(updated)

    def claim_smart_processing_request(
        self,
        *,
        request_id: str,
        capture_id: str,
        effect_id: str,
        worker_id: str,
        provider_id: str,
        model_id: str,
        provider_label: str,
        model_label: str,
        smart_disclosure_sha256: str,
        lease_seconds: int = 900,
    ) -> Mapping[str, Any] | None:
        """Atomically pin and lease one account-backed Smart attempt.

        A provider/model pair is copied from the already validated execution
        selection.  An existing live attempt wins; an expired attempt is
        terminal and can only be replaced by this fresh explicit start.
        """

        if (
            not request_id.startswith("jspr_")
            or len(request_id) != 37
            or not worker_id
            or not provider_id
            or not model_id
            or not provider_label
            or not model_label
            or len(smart_disclosure_sha256) != 64
            or lease_seconds < 30
            or lease_seconds > 3600
        ):
            raise ValueError("invalid Journal Smart worker binding")
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        lease_token = secrets.token_urlsafe(32)
        token_sha256 = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        with self.transaction() as conn:
            capture = conn.execute(
                "SELECT * FROM journal_captures WHERE capture_id=?",
                (capture_id,),
            ).fetchone()
            effect = conn.execute(
                "SELECT * FROM journal_effects WHERE effect_id=?",
                (effect_id,),
            ).fetchone()
            if (
                capture is None
                or capture["mode"] != CaptureMode.SMART.value
                or effect is None
                or effect["capture_id"] != capture_id
                or effect["effect_type"] not in {"auto_route", "smart_annotate"}
            ):
                raise JournalCaptureConflict(
                    "That Smart processing request is unavailable."
                )
            if capture["processing_status"] == ProcessingState.SUCCEEDED.value:
                return None
            payload = _decode_json(effect["payload_json"]) or {}
            if payload.get("smart_disclosure_sha256") != smart_disclosure_sha256:
                raise JournalCaptureConflict(
                    "The Smart provider changed. Review the current disclosure and retry."
                )
            if (
                effect["authorization_expires_at"] is not None
                and effect["authorization_expires_at"] <= now
            ):
                conn.execute(
                    "UPDATE journal_effects SET state='paused',"
                    "error_code='journal_authorization_expired',lease_owner=NULL,"
                    "lease_expires_at=NULL,updated_at=? WHERE effect_id=?",
                    (now, effect_id),
                )
                return None
            conn.execute(
                "UPDATE journal_smart_processing_requests SET status='failed',"
                "lease_owner=NULL,lease_token_sha256=NULL,lease_expires_at=NULL,"
                "error_code='smart_worker_lease_expired',completed_at=?,updated_at=? "
                "WHERE capture_id=? AND status='leased' AND lease_expires_at<=?",
                (now, now, capture_id, now),
            )
            active = conn.execute(
                "SELECT request_id FROM journal_smart_processing_requests "
                "WHERE capture_id=? AND status='leased'",
                (capture_id,),
            ).fetchone()
            if active is not None:
                return None
            attempt = int(
                conn.execute(
                    "SELECT COALESCE(MAX(attempt_number),0)+1 "
                    "FROM journal_smart_processing_requests WHERE capture_id=?",
                    (capture_id,),
                ).fetchone()[0]
            )
            conn.execute(
                "INSERT INTO journal_smart_processing_requests("
                "request_id,capture_id,effect_id,attempt_number,provider_id,"
                "model_id,provider_label,model_label,smart_disclosure_sha256,"
                "status,lease_owner,lease_token_sha256,lease_expires_at,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'leased',?,?,?,?,?)",
                (
                    request_id,
                    capture_id,
                    effect_id,
                    attempt,
                    provider_id,
                    model_id,
                    provider_label,
                    model_label,
                    smart_disclosure_sha256,
                    worker_id,
                    token_sha256,
                    expires,
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE journal_effects SET state='running',attempts=attempts+1,"
                "lease_owner=?,lease_expires_at=?,error_code=NULL,updated_at=? "
                "WHERE effect_id=?",
                (worker_id, expires, now, effect_id),
            )
            conn.execute(
                "UPDATE journal_captures SET processing_status='running',"
                "processing_error_code=NULL,revision=revision+1,updated_at=? "
                "WHERE capture_id=?",
                (now, capture_id),
            )
            conn.execute(
                "UPDATE journal_entries SET processing_status='running',"
                "processing_error_code=NULL,updated_at=? WHERE capture_id=?",
                (now, capture_id),
            )
        return {
            "requestId": request_id,
            "captureId": capture_id,
            "effectId": effect_id,
            "attemptNumber": attempt,
            "providerId": provider_id,
            "modelId": model_id,
            "providerLabel": provider_label,
            "modelLabel": model_label,
            "smartDisclosureSha256": smart_disclosure_sha256,
            "leaseOwner": worker_id,
            "leaseToken": lease_token,
            "leaseExpiresAt": expires,
            "authorizationFingerprint": str(effect["authorization_fingerprint"]),
        }

    def get_smart_processing_request(
        self, request_id: str
    ) -> Mapping[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_smart_processing_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def get_active_smart_processing_request(
        self, capture_id: str
    ) -> Mapping[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_smart_processing_requests "
                "WHERE capture_id=? AND status='leased' "
                "ORDER BY attempt_number DESC LIMIT 1",
                (capture_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def validate_smart_processing_worker_lease(
        self,
        *,
        request_id: str,
        lease_token: str,
        worker_id: str,
    ) -> Mapping[str, Any]:
        """Validate the secret, worker identity, effect, and authorization."""

        now = _utc_now()
        token_sha256 = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT request.*,effect.authorization_fingerprint,"
                "effect.authorization_expires_at,effect.state AS effect_state,"
                "effect.lease_owner AS effect_lease_owner,"
                "effect.lease_expires_at AS effect_lease_expires_at "
                "FROM journal_smart_processing_requests AS request "
                "JOIN journal_effects AS effect ON effect.effect_id=request.effect_id "
                "WHERE request.request_id=?",
                (request_id,),
            ).fetchone()
        if (
            row is None
            or row["status"] != "leased"
            or row["lease_owner"] != worker_id
            or row["lease_token_sha256"] != token_sha256
            or row["lease_expires_at"] <= now
            or row["effect_state"] != EffectState.RUNNING.value
            or row["effect_lease_owner"] != worker_id
            or row["effect_lease_expires_at"] <= now
            or (
                row["authorization_expires_at"] is not None
                and row["authorization_expires_at"] <= now
            )
        ):
            raise JournalCaptureConflict(
                "That Smart processing lease is unavailable."
            )
        return dict(row)

    def record_smart_processing_input_manifest(
        self,
        *,
        request_id: str,
        lease_token: str,
        worker_id: str,
        manifest_sha256: str,
    ) -> None:
        self.validate_smart_processing_worker_lease(
            request_id=request_id,
            lease_token=lease_token,
            worker_id=worker_id,
        )
        with self.transaction() as conn:
            changed = conn.execute(
                "UPDATE journal_smart_processing_requests SET "
                "input_manifest_sha256=COALESCE(input_manifest_sha256,?),"
                "updated_at=? WHERE request_id=? AND status='leased' "
                "AND lease_owner=? AND lease_token_sha256=? "
                "AND (input_manifest_sha256 IS NULL OR input_manifest_sha256=?)",
                (
                    manifest_sha256,
                    _utc_now(),
                    request_id,
                    worker_id,
                    hashlib.sha256(lease_token.encode("utf-8")).hexdigest(),
                    manifest_sha256,
                ),
            ).rowcount
        if changed != 1:
            raise JournalCaptureConflict(
                "That Smart processing input changed during delivery."
            )

    def fail_smart_processing_request(
        self,
        *,
        request_id: str,
        lease_token: str,
        worker_id: str,
        error_code: str,
    ) -> None:
        now = _utc_now()
        token_sha256 = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_smart_processing_requests "
                "WHERE request_id=? AND status='leased' AND lease_owner=? "
                "AND lease_token_sha256=? AND lease_expires_at>?",
                (request_id, worker_id, token_sha256, now),
            ).fetchone()
            if row is None:
                raise JournalCaptureConflict(
                    "That Smart processing lease is unavailable."
                )
            conn.execute(
                "UPDATE journal_smart_processing_requests SET status='failed',"
                "lease_owner=NULL,lease_token_sha256=NULL,lease_expires_at=NULL,"
                "error_code=?,completed_at=?,updated_at=? WHERE request_id=?",
                (error_code[:128], now, now, request_id),
            )
            conn.execute(
                "UPDATE journal_effects SET state='failed',lease_owner=NULL,"
                "lease_expires_at=NULL,error_code=?,updated_at=? "
                "WHERE effect_id=? AND lease_owner=?",
                (error_code[:128], now, row["effect_id"], worker_id),
            )
            conn.execute(
                "UPDATE journal_captures SET processing_status='failed',"
                "processing_error_code=?,revision=revision+1,updated_at=? "
                "WHERE capture_id=?",
                (error_code[:128], now, row["capture_id"]),
            )
            conn.execute(
                "UPDATE journal_entries SET processing_status='failed',"
                "processing_error_code=?,updated_at=? WHERE capture_id=?",
                (error_code[:128], now, row["capture_id"]),
            )

    def reauthorize_effect(
        self,
        capture_id: str,
        effect_type: str,
        *,
        authorization_fingerprint: str,
        authorization_expires_at: str | None,
    ) -> JournalEffect:
        """Bind a fresh explicit authorization to one unsettled effect.

        Successful work is immutable. A still-live lease cannot be stolen by
        a retry gesture; an expired lease may be safely returned to pending.
        """

        now = _utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_effects WHERE capture_id=? AND effect_type=?",
                (capture_id, effect_type),
            ).fetchone()
            if row is None:
                raise KeyError("journal_effect_not_found")
            if row["error_code"] == "journal_proposal_source_withdrawn":
                raise JournalCaptureConflict("This source was removed; its pending proposal cannot be resumed.")
            if row["state"] == EffectState.SUCCEEDED.value:
                return self._effect(row)
            if (
                row["state"] == EffectState.RUNNING.value
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] > now
            ):
                raise JournalCaptureConflict(
                    "That Journal action is already running."
                )
            conn.execute(
                """UPDATE journal_effects SET state='pending',
                   authorization_fingerprint=?,authorization_expires_at=?,
                   lease_owner=NULL,lease_expires_at=NULL,error_code=NULL,updated_at=?
                   WHERE effect_id=?""",
                (
                    authorization_fingerprint,
                    authorization_expires_at,
                    now,
                    row["effect_id"],
                ),
            )
            updated = conn.execute(
                "SELECT * FROM journal_effects WHERE effect_id=?", (row["effect_id"],)
            ).fetchone()
            assert updated is not None
            return self._effect(updated)

    def bind_smart_disclosure(self, capture_id: str, disclosure_sha256: str, *, retry: bool = False) -> None:
        """Pin the human-reviewed boundary before a source reaches the model."""

        encoded = _json({"smart_disclosure_sha256": disclosure_sha256})
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_effects WHERE capture_id=? AND effect_type IN ('auto_route','smart_annotate')",
                (capture_id,),
            ).fetchone()
            if row is None:
                raise KeyError("journal_effect_not_found")
            if row["state"] == "succeeded":
                return
            if row["state"] == "running" and row["lease_expires_at"] and row["lease_expires_at"] > _utc_now():
                raise JournalCaptureConflict("That Journal action is already running.")
            if not retry and row["payload_json"] is not None and row["payload_json"] != encoded:
                raise JournalCaptureConflict("The Smart disclosure changed; review it before retrying.")
            conn.execute("UPDATE journal_effects SET payload_json=? WHERE effect_id=?", (encoded, row["effect_id"]))

    def effects_for_capture(self, capture_id: str) -> list[JournalEffect]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM journal_effects WHERE capture_id=? ORDER BY created_at,effect_type",
                (capture_id,),
            ).fetchall()
        return [self._effect(row) for row in rows]

    def pending_effects(self, *, limit: int = 20, effect_type: str | None = None) -> list[JournalEffect]:
        now = _utc_now()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM journal_effects
                   WHERE (? IS NULL OR effect_type=?) AND (
                      state IN ('pending','failed')
                      OR (state='running' AND lease_expires_at <= ?))
                   ORDER BY updated_at LIMIT ?""",
                (effect_type, effect_type, now, max(1, min(limit, 100))),
            ).fetchall()
        return [self._effect(row) for row in rows]

    def finish_effect(
        self,
        capture_id: str,
        effect_type: str,
        *,
        succeeded: bool,
        error_code: str | None = None,
        result: Mapping[str, Any] | None = None,
        lease_owner: str | None = None,
    ) -> None:
        now = _utc_now()
        with self.transaction() as conn:
            conn.execute(
                """UPDATE journal_effects SET state=?,error_code=?,result_json=COALESCE(?,result_json),
                   lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE capture_id=? AND effect_type=?
                   AND (error_code IS NULL OR error_code!='journal_proposal_source_withdrawn')
                   AND (? IS NULL OR lease_owner=?)""",
                (
                    EffectState.SUCCEEDED.value if succeeded else EffectState.FAILED.value,
                    error_code,
                    _json(result),
                    now,
                    capture_id,
                    effect_type,
                    lease_owner,
                    lease_owner,
                ),
            )

    def proposal_effect_for_delivery(self, effect_id: str, *, owner: str) -> JournalEffect | None:
        """Read the current owned proposal immediately before cross-store ingress.

        A lease returned earlier is not authorization to revive a proposal that
        source maintenance has since canceled, or another worker now owns.
        """

        now = _utc_now()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM journal_effects WHERE effect_id=? AND effect_type='task_proposal'
                   AND state='running' AND lease_owner=? AND lease_expires_at>?
                   AND (authorization_expires_at IS NULL OR authorization_expires_at>?)
                   AND (error_code IS NULL OR error_code!='journal_proposal_source_withdrawn')""",
                (effect_id, owner, now, now),
            ).fetchone()
        return None if row is None else self._effect(row)

    def enqueue_proposal(
        self,
        capture_id: str,
        payload: Mapping[str, Any],
        *,
        authorization_fingerprint: str,
        authorization_expires_at: str | None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """A delivery command, not a second proposal authority."""

        if conn is None:
            with self.transaction() as transaction:
                self.enqueue_proposal(
                    capture_id, payload,
                    authorization_fingerprint=authorization_fingerprint,
                    authorization_expires_at=authorization_expires_at,
                    conn=transaction,
                )
            return
        encoded = _json(payload)
        prior = conn.execute(
            "SELECT payload_json FROM journal_effects WHERE capture_id=? AND effect_type='task_proposal'",
            (capture_id,),
        ).fetchone()
        if prior is not None:
            if prior["payload_json"] != encoded:
                raise JournalCaptureConflict("That capture already has a different proposal follow-up.")
            return
        now = _utc_now()
        conn.execute(
            """INSERT INTO journal_effects(
                effect_id,capture_id,effect_type,state,payload_json,
                authorization_fingerprint,authorization_expires_at,created_at,updated_at
            ) VALUES(?,?,'task_proposal','pending',?,?,?,?,?)""",
            (hashlib.sha256(f"{capture_id}:task_proposal:v1".encode()).hexdigest()[:32],
             capture_id, encoded, authorization_fingerprint, authorization_expires_at, now, now),
        )
        conn.execute(
            "UPDATE journal_captures SET revision=revision+1,updated_at=? WHERE capture_id=?",
            (now, capture_id),
        )

    def settle_smart(
        self,
        capture_id: str,
        *,
        effect_type: str,
        annotation: Mapping[str, Any],
        resolved_target: CaptureTarget,
        proposal_payload: Mapping[str, Any] | None = None,
        worker_request_id: str | None = None,
        worker_lease_token: str | None = None,
        worker_session_id: str | None = None,
        input_manifest_sha256: str | None = None,
        result_sha256: str | None = None,
    ) -> JournalCapture:
        """Commit the model result and optional delivery command atomically.

        Once this commits, a retry cannot redisclose the source to reconstruct a
        proposal, even if materialization or cross-store delivery subsequently fails.
        """

        now = _utc_now()
        worker_values = (
            worker_request_id,
            worker_lease_token,
            worker_session_id,
            input_manifest_sha256,
            result_sha256,
        )
        if any(value is not None for value in worker_values) and any(
            value is None for value in worker_values
        ):
            raise ValueError("The Smart worker completion binding is incomplete.")
        with self.transaction() as conn:
            effect = conn.execute(
                "SELECT * FROM journal_effects WHERE capture_id=? AND effect_type=?",
                (capture_id, effect_type),
            ).fetchone()
            if effect is None:
                raise KeyError("journal_effect_not_found")
            if worker_request_id is not None:
                request = conn.execute(
                    "SELECT * FROM journal_smart_processing_requests "
                    "WHERE request_id=?",
                    (worker_request_id,),
                ).fetchone()
                assert worker_lease_token is not None
                assert worker_session_id is not None
                assert input_manifest_sha256 is not None
                assert result_sha256 is not None
                token_sha256 = hashlib.sha256(
                    worker_lease_token.encode("utf-8")
                ).hexdigest()
                if request is not None and request["status"] == "succeeded":
                    if (
                        request["capture_id"] == capture_id
                        and request["effect_id"] == effect["effect_id"]
                        and request["input_manifest_sha256"]
                        == input_manifest_sha256
                        and request["result_sha256"] == result_sha256
                    ):
                        row = conn.execute(
                            "SELECT * FROM journal_captures WHERE capture_id=?",
                            (capture_id,),
                        ).fetchone()
                        assert row is not None
                        return self._capture(row)
                    raise JournalCaptureConflict(
                        "That Smart completion was already used for another result."
                    )
                if (
                    request is None
                    or request["capture_id"] != capture_id
                    or request["effect_id"] != effect["effect_id"]
                    or request["status"] != "leased"
                    or request["lease_owner"] != worker_session_id
                    or request["lease_token_sha256"] != token_sha256
                    or request["lease_expires_at"] <= now
                    or request["input_manifest_sha256"]
                    != input_manifest_sha256
                    or effect["state"] != EffectState.RUNNING.value
                    or effect["lease_owner"] != worker_session_id
                    or effect["lease_expires_at"] <= now
                    or (
                        effect["authorization_expires_at"] is not None
                        and effect["authorization_expires_at"] <= now
                    )
                ):
                    raise JournalCaptureConflict(
                        "That Smart processing lease is unavailable."
                    )
            if proposal_payload is not None:
                self.enqueue_proposal(
                    capture_id, proposal_payload,
                    authorization_fingerprint=effect["authorization_fingerprint"],
                    authorization_expires_at=effect["authorization_expires_at"], conn=conn,
                )
            if worker_request_id is not None:
                conn.execute(
                    "UPDATE journal_smart_processing_requests SET "
                    "status='succeeded',lease_owner=NULL,"
                    "lease_token_sha256=NULL,lease_expires_at=NULL,"
                    "input_manifest_sha256=?,result_sha256=?,error_code=NULL,"
                    "completed_at=?,updated_at=? WHERE request_id=?",
                    (
                        input_manifest_sha256,
                        result_sha256,
                        now,
                        now,
                        worker_request_id,
                    ),
                )
            conn.execute(
                """UPDATE journal_effects SET state='succeeded',error_code=NULL,
                   lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE capture_id=? AND effect_type=?""", (now, capture_id, effect_type),
            )
            conn.execute(
                """UPDATE journal_captures SET processing_status='succeeded',annotation_json=?,
                   processing_error_code=NULL,resolved_target=?,revision=revision+1,updated_at=?
                   WHERE capture_id=?""", (_json(annotation), resolved_target.value, now, capture_id),
            )
            conn.execute(
                """UPDATE journal_entries SET processing_status='succeeded',annotation_json=?,
                   processing_error_code=NULL,updated_at=? WHERE capture_id=?""",
                (_json(annotation), now, capture_id),
            )
            row = conn.execute("SELECT * FROM journal_captures WHERE capture_id=?", (capture_id,)).fetchone()
            assert row is not None
            return self._capture(row)

    def proposal_resolution_effects(self, *, limit: int = 100) -> list[JournalEffect]:
        """A bounded, oldest-checked-first batch of delivered proposal receipts."""

        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM journal_effects WHERE effect_type='task_proposal'
                   AND state='succeeded' AND json_extract(result_json,'$.resolution_synced') IS NULL
                   ORDER BY updated_at,effect_id LIMIT ?""",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [self._effect(row) for row in rows]

    def record_proposal_resolution(
        self, capture_id: str, *, thread_id: str,
        terminal_status: str | None = None, realization: Mapping[str, Any] | None = None,
    ) -> None:
        """Checkpoint delivery reconciliation, never author proposal lifecycle.

        Only a verified Threads realization may close an open intention. The
        terminal receipt and note resolution commit together so replay is safe.
        """

        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_effects WHERE capture_id=? AND effect_type='task_proposal' AND state='succeeded'",
                (capture_id,),
            ).fetchone()
            if row is None:
                return
            result = json.loads(row["result_json"] or "{}")
            if result.get("thread_id") != thread_id or result.get("resolution_synced"):
                return
            now = _utc_now()
            if terminal_status in {"realized", "rejected"}:
                result["resolution_synced"] = terminal_status
            if terminal_status == "realized" and realization is not None:
                result["realization"] = dict(realization)
                changed = conn.execute(
                    """UPDATE journal_entries SET resolution_state='routed_to_task',
                       version=version+1,updated_at=? WHERE capture_id=? AND resolution_state='open'
                       AND entry_kind='running_notes'""", (now, capture_id),
                ).rowcount
                if changed:
                    conn.execute(
                        "UPDATE journal_captures SET revision=revision+1,updated_at=? WHERE capture_id=?",
                        (now, capture_id),
                    )
            conn.execute("UPDATE journal_effects SET result_json=?,updated_at=? WHERE effect_id=?",
                         (_json(result), now, row["effect_id"]))

    def set_processing(
        self,
        capture_id: str,
        *,
        status: ProcessingState,
        annotation: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        resolved_target: CaptureTarget | None = None,
    ) -> JournalCapture:
        now = _utc_now()
        with self.transaction() as conn:
            conn.execute(
                """UPDATE journal_captures SET processing_status=?,
                   annotation_json=?,processing_error_code=?,
                   resolved_target=COALESCE(?,resolved_target),
                   revision=revision+1,updated_at=? WHERE capture_id=?""",
                (
                    status.value,
                    _json(annotation),
                    error_code,
                    resolved_target.value if resolved_target is not None else None,
                    now,
                    capture_id,
                ),
            )
            conn.execute(
                """UPDATE journal_entries SET processing_status=?,annotation_json=?,
                   processing_error_code=?,updated_at=? WHERE capture_id=?""",
                (status.value, _json(annotation), error_code, now, capture_id),
            )
            row = conn.execute(
                "SELECT * FROM journal_captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
            if row is None:
                raise KeyError("journal_capture_not_found")
            return self._capture(row)

    def pause_source_proposals(self, *, source_effect_id: str, source_ref: str) -> None:
        """Cancel undelivered derivatives before a redaction projection can stall.

        A delivered Thread is an independently retained derivative. This narrow
        guard only erases Journal's unsent parameters and prevents a later drain
        or retry gesture from authoring a new Thread from removed source text.
        """

        with self.transaction() as conn:
            capture = conn.execute(
                "SELECT capture_id,source_ref FROM journal_captures WHERE source_effect_id=?",
                (source_effect_id,),
            ).fetchone()
            if capture is None:
                return
            if capture["source_ref"] != source_ref:
                raise JournalCaptureConflict("The source removal does not match the Journal capture.")
            now = _utc_now()
            changed = conn.execute(
                """UPDATE journal_effects SET state='paused',payload_json=NULL,
                   error_code='journal_proposal_source_withdrawn',lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE capture_id=? AND effect_type='task_proposal' AND state!='succeeded'
                   AND (error_code IS NULL OR error_code!='journal_proposal_source_withdrawn')""",
                (now, capture["capture_id"]),
            ).rowcount
            if changed:
                conn.execute("UPDATE journal_captures SET revision=revision+1,updated_at=? WHERE capture_id=?",
                             (now, capture["capture_id"]))

    def mark_source_redacted(
        self,
        *,
        source_effect_id: str,
        source_usage_id: str,
        source_ref: str,
        redaction_event_id: str,
        redaction_epoch: int,
        result_sha256: str,
        projection_state: str = "committed",
    ) -> JournalEntry | None:
        """Scrub a managed source copy and persist a content-free receipt.

        The Markdown adapter removes the readable block before this method is
        called.  Replays are idempotent by the source redaction event and usage
        IDs; no prior readable text is copied into the receipt.
        """

        if projection_state not in {"committed", "paused_diverged"}:
            raise ValueError("invalid_journal_redaction_projection_state")
        now = _utc_now()
        with self.transaction() as conn:
            prior = conn.execute(
                "SELECT * FROM journal_source_redactions WHERE redaction_event_id=?",
                (redaction_event_id,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["source_effect_id"] != source_effect_id
                    or prior["source_usage_id"] != source_usage_id
                    or prior["source_ref"] != source_ref
                    or int(prior["redaction_epoch"]) != redaction_epoch
                    or prior["result_sha256"] != result_sha256
                ):
                    raise JournalCaptureConflict(
                        "That source redaction is already bound differently."
                    )
                if prior["entry_id"] is None:
                    return None
                row = conn.execute(
                    "SELECT * FROM journal_entries WHERE entry_id=?",
                    (prior["entry_id"],),
                ).fetchone()
                return None if row is None else self._entry(row)

            capture = conn.execute(
                "SELECT * FROM journal_captures WHERE source_effect_id=?",
                (source_effect_id,),
            ).fetchone()
            if capture is not None:
                if capture["source_ref"] != source_ref:
                    raise JournalCaptureConflict(
                        "The source redaction does not match the Journal capture."
                    )
                bound_usage = capture["source_usage_id"]
                if bound_usage is not None and bound_usage != source_usage_id:
                    raise JournalCaptureConflict(
                        "The source redaction does not match the managed copy."
                    )
            entry = None
            native = None
            if capture is not None and capture["entry_id"] is not None:
                entry = conn.execute(
                    "SELECT * FROM journal_entries WHERE entry_id=?",
                    (capture["entry_id"],),
                ).fetchone()
                if entry is None:
                    raise KeyError("journal_entry_not_found")
                conn.execute(
                    "UPDATE journal_entries SET markdown='[redacted]', "
                    "resolution_state='redacted', projection_state=?, "
                    "projection_result_sha256=?, version=version+1, updated_at=? "
                    "WHERE entry_id=?",
                    (projection_state, result_sha256, now, entry["entry_id"]),
                )
                conn.execute(
                    "UPDATE journal_captures SET revision=revision+1,updated_at=? "
                    "WHERE capture_id=?",
                    (now, capture["capture_id"]),
                )
            elif capture is not None:
                native = conn.execute(
                    """
                    SELECT binding.item_id,item.current_revision,item.source_ref,
                           item.local_date,item.privacy_class
                    FROM journal_native_capture_bindings AS binding
                    JOIN journal_items AS item ON item.item_id=binding.item_id
                    WHERE binding.capture_id=?
                    """,
                    (capture["capture_id"],),
                ).fetchone()
                if native is not None:
                    if str(native["source_ref"]) != source_ref:
                        raise JournalCaptureConflict(
                            "The native Journal item does not match the removed source."
                        )
                    original_revision = int(native["current_revision"])
                    scrubbed_revision = original_revision + 1
                    redacted_text = "[redacted]"
                    redacted_sha = hashlib.sha256(
                        redacted_text.encode("utf-8")
                    ).hexdigest()
                    conn.execute(
                        """
                        INSERT INTO journal_native_redactions(
                            redaction_event_id,capture_id,item_id,source_ref,
                            redaction_epoch,original_revision,state,created_at
                        ) VALUES(?,?,?,?,?,?,'scrubbing',?)
                        """,
                        (
                            redaction_event_id,
                            capture["capture_id"],
                            native["item_id"],
                            source_ref,
                            redaction_epoch,
                            original_revision,
                            now,
                        ),
                    )
                    # Privacy redaction is the sole exception to immutable
                    # revision prose: every readable historical copy is
                    # overwritten while a fail-closed scrubbing receipt exists.
                    conn.execute(
                        "UPDATE journal_item_revisions SET plain_value=?,"
                        "content_sha256=?,lifecycle='tombstoned' WHERE item_id=?",
                        (redacted_text, redacted_sha, native["item_id"]),
                    )
                    actor_json = json.dumps(
                        {
                            "kind": "source_redaction",
                            "redactionEventId": redaction_event_id,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    conn.execute(
                        """
                        INSERT INTO journal_item_revisions(
                            item_id,revision,authority_kind,plain_value,
                            content_sha256,lifecycle,actor_json,source_ref,
                            authorship,review_state,intent_id,created_at
                        ) VALUES(?,?,'native_plain',?,?,'tombstoned',?,?,'unknown',
                            'unknown',?,?)
                        """,
                        (
                            native["item_id"],
                            scrubbed_revision,
                            redacted_text,
                            redacted_sha,
                            actor_json,
                            source_ref,
                            f"source-redaction:{redaction_event_id}",
                            now,
                        ),
                    )
                    conn.execute(
                        "UPDATE journal_items SET current_plain_value=?,"
                        "current_content_sha256=?,lifecycle='tombstoned',"
                        "current_revision=?,updated_at=? WHERE item_id=?",
                        (
                            redacted_text,
                            redacted_sha,
                            scrubbed_revision,
                            now,
                            native["item_id"],
                        ),
                    )
                    event_id = hashlib.sha256(
                        (
                            "journal-search-delete:"
                            f"{native['item_id']}:{scrubbed_revision}"
                        ).encode("utf-8")
                    ).hexdigest()[:32]
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO journal_search_outbox(
                            event_id,aggregate_type,aggregate_id,aggregate_revision,
                            event_kind,content_sha256,search_recipe_version,
                            privacy_class,committed_at
                        ) VALUES(?,'item',?,?,'delete',?,1,?,?)
                        """,
                        (
                            f"jso_{event_id}",
                            native["item_id"],
                            str(scrubbed_revision),
                            redacted_sha,
                            native["privacy_class"],
                            now,
                        ),
                    )
                    conn.execute(
                        "UPDATE journal_native_redactions SET state='committed',"
                        "scrubbed_revision=?,result_sha256=?,completed_at=? "
                        "WHERE redaction_event_id=? AND state='scrubbing'",
                        (
                            scrubbed_revision,
                            result_sha256,
                            now,
                            redaction_event_id,
                        ),
                    )
                    conn.execute(
                        "UPDATE journal_captures SET revision=revision+1,updated_at=? "
                        "WHERE capture_id=?",
                        (now, capture["capture_id"]),
                    )

            conn.execute(
                "INSERT INTO journal_source_redactions "
                "(redaction_event_id,source_effect_id,source_usage_id,source_ref,"
                "capture_id,entry_id,redaction_epoch,result_sha256,completed_at,"
                "native_item_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    redaction_event_id,
                    source_effect_id,
                    source_usage_id,
                    source_ref,
                    capture["capture_id"] if capture is not None else None,
                    entry["entry_id"] if entry is not None else None,
                    redaction_epoch,
                    result_sha256,
                    now,
                    native["item_id"] if native is not None else None,
                ),
            )
            if entry is None:
                return None
            updated = conn.execute(
                "SELECT * FROM journal_entries WHERE entry_id=?",
                (entry["entry_id"],),
            ).fetchone()
            assert updated is not None
            return self._entry(updated)

    def get_import_source_dependency(
        self,
        *,
        source_usage_id: str,
        source_usage_consumer_id: str,
        source_ref: str,
    ) -> Mapping[str, Any] | None:
        """Resolve a staged-history file from its deterministic Source usage."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT cohort_id,file_id,source_ref,source_usage_id,"
                "source_usage_consumer_id,source_usage_state,state "
                "FROM journal_import_files WHERE source_usage_consumer_id=?",
                (source_usage_consumer_id,),
            ).fetchone()
        if row is None:
            return None
        if row["source_usage_id"] is not None and str(row["source_usage_id"]) != source_usage_id:
            raise JournalCaptureConflict(
                "The import Source redaction does not match its managed copy."
            )
        if row["source_ref"] is not None and str(row["source_ref"]) != source_ref:
            raise JournalCaptureConflict(
                "The import Source redaction does not match its retained Source."
            )
        return {
            "cohort_id": str(row["cohort_id"]),
            "file_id": str(row["file_id"]),
            "source_ref": None if row["source_ref"] is None else str(row["source_ref"]),
            "source_usage_id": (
                None if row["source_usage_id"] is None else str(row["source_usage_id"])
            ),
            "source_usage_consumer_id": str(row["source_usage_consumer_id"]),
            "source_usage_state": str(row["source_usage_state"]),
            "state": str(row["state"]),
        }

    def mark_import_source_redacted(
        self,
        *,
        cohort_id: str,
        file_id: str,
        source_usage_id: str,
        source_usage_consumer_id: str,
        source_ref: str,
        redaction_event_id: str,
        redaction_epoch: int,
        result_sha256: str,
    ) -> int:
        """Scrub every native current/history copy derived from one import file.

        This transaction is the Journal-side durable boundary.  A Source
        usage is released only after it commits; replay is keyed by the Source
        redaction event and contains no original prose.
        """

        redacted_text = "[redacted]"
        redacted_sha = hashlib.sha256(redacted_text.encode("utf-8")).hexdigest()
        redacted_value_json = '{"redacted":true}'
        redacted_value_sha = hashlib.sha256(
            redacted_value_json.encode("utf-8")
        ).hexdigest()
        now = _utc_now()
        with self.transaction() as conn:
            file_row = conn.execute(
                "SELECT * FROM journal_import_files WHERE cohort_id=? AND file_id=?",
                (cohort_id, file_id),
            ).fetchone()
            if file_row is None:
                raise KeyError("journal_import_file_not_found")
            if str(file_row["source_usage_consumer_id"]) != source_usage_consumer_id:
                raise JournalCaptureConflict(
                    "The import Source redaction consumer does not match."
                )
            if file_row["source_usage_id"] is not None and str(
                file_row["source_usage_id"]
            ) != source_usage_id:
                raise JournalCaptureConflict(
                    "The import Source redaction usage does not match."
                )
            if file_row["source_ref"] is not None and str(file_row["source_ref"]) != source_ref:
                raise JournalCaptureConflict(
                    "The import Source redaction Source does not match."
                )
            prior = conn.execute(
                "SELECT * FROM journal_import_source_redactions "
                "WHERE redaction_event_id=?",
                (redaction_event_id,),
            ).fetchone()
            if prior is not None:
                if (
                    str(prior["cohort_id"]) != cohort_id
                    or str(prior["file_id"]) != file_id
                    or str(prior["source_usage_id"]) != source_usage_id
                    or str(prior["source_usage_consumer_id"])
                    != source_usage_consumer_id
                    or str(prior["source_ref"]) != source_ref
                    or int(prior["redaction_epoch"]) != redaction_epoch
                    or str(prior["result_sha256"]) != result_sha256
                ):
                    raise JournalCaptureConflict(
                        "That import Source redaction is already bound differently."
                    )
                return int(prior["scrubbed_item_count"])

            conn.execute(
                "INSERT INTO journal_import_source_redactions("
                "redaction_event_id,cohort_id,file_id,source_usage_id,"
                "source_usage_consumer_id,source_ref,redaction_epoch,state,created_at) "
                "VALUES(?,?,?,?,?,?,?,'scrubbing',?)",
                (
                    redaction_event_id,
                    cohort_id,
                    file_id,
                    source_usage_id,
                    source_usage_consumer_id,
                    source_ref,
                    redaction_epoch,
                    now,
                ),
            )
            items = conn.execute(
                "SELECT item.item_id,item.current_revision,item.privacy_class "
                "FROM journal_import_spans AS span "
                "JOIN journal_items AS item ON item.item_id=span.item_id "
                "WHERE span.cohort_id=? AND span.file_id=? AND span.materialize=1 "
                "ORDER BY item.item_id",
                (cohort_id, file_id),
            ).fetchall()
            actor_json = json.dumps(
                {
                    "kind": "source_redaction",
                    "redactionEventId": redaction_event_id,
                    "importCohortId": cohort_id,
                    "importFileId": file_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in items:
                item_id = str(item["item_id"])
                original_revision = int(item["current_revision"])
                scrubbed_revision = original_revision + 1
                conn.execute(
                    "UPDATE journal_item_revisions SET plain_value=?,"
                    "content_sha256=?,lifecycle='tombstoned' WHERE item_id=?",
                    (redacted_text, redacted_sha, item_id),
                )
                conn.execute(
                    "INSERT INTO journal_item_revisions("
                    "item_id,revision,authority_kind,plain_value,content_sha256,"
                    "lifecycle,actor_json,source_ref,authorship,review_state,"
                    "intent_id,created_at) "
                    "VALUES(?,?,'native_plain',?,?,'tombstoned',?,?,'unknown',"
                    "'unknown',?,?)",
                    (
                        item_id,
                        scrubbed_revision,
                        redacted_text,
                        redacted_sha,
                        actor_json,
                        source_ref,
                        f"source-redaction:{redaction_event_id}:{item_id}",
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE journal_items SET current_plain_value=?,"
                    "current_content_sha256=?,lifecycle='tombstoned',"
                    "current_revision=?,updated_at=? WHERE item_id=?",
                    (redacted_text, redacted_sha, scrubbed_revision, now, item_id),
                )
                event_id = "jso_" + hashlib.sha256(
                    (
                        "journal-import-search-delete:"
                        f"{redaction_event_id}:{item_id}:{scrubbed_revision}"
                    ).encode("utf-8")
                ).hexdigest()[:32]
                conn.execute(
                    "INSERT OR IGNORE INTO journal_search_outbox("
                    "event_id,aggregate_type,aggregate_id,aggregate_revision,"
                    "event_kind,content_sha256,search_recipe_version,privacy_class,"
                    "committed_at) VALUES(?,'item',?,?,'delete',?,1,?,?)",
                    (
                        event_id,
                        item_id,
                        str(scrubbed_revision),
                        redacted_sha,
                        item["privacy_class"],
                        now,
                    ),
                )
            field_values = conn.execute(
                "SELECT value.value_id,value.current_revision,definition.privacy_class "
                "FROM journal_import_typed_observations AS observation "
                "JOIN journal_field_values AS value ON value.value_id=observation.value_id "
                "JOIN journal_field_definition_versions AS definition "
                "ON definition.field_id=value.field_id "
                "AND definition.definition_version=value.field_definition_version "
                "WHERE observation.cohort_id=? AND observation.file_id=? "
                "AND observation.state='materialized' ORDER BY value.value_id",
                (cohort_id, file_id),
            ).fetchall()
            for field_value in field_values:
                value_id = str(field_value["value_id"])
                imported_revision = conn.execute(
                    "SELECT 1 FROM journal_field_value_revisions "
                    "WHERE value_id=? AND revision=1",
                    (value_id,),
                ).fetchone()
                if imported_revision is None:
                    raise JournalCaptureConflict(
                        "The imported typed observation revision is unavailable."
                    )
                conn.execute(
                    "UPDATE journal_field_value_revisions SET value_json=?,"
                    "value_sha256=? WHERE value_id=? AND revision=1",
                    (redacted_value_json, redacted_value_sha, value_id),
                )
                if int(field_value["current_revision"]) != 1:
                    continue
                scrubbed_revision = 2
                conn.execute(
                    "UPDATE journal_field_values SET disposition='missing',"
                    "text_value=NULL,number_value=NULL,boolean_value=NULL,"
                    "temporal_value=NULL,duration_seconds=NULL,option_value=NULL,"
                    "collection_present=0,lifecycle='tombstoned',current_revision=?,"
                    "updated_at=? WHERE value_id=? AND current_revision=1",
                    (scrubbed_revision, now, value_id),
                )
                conn.execute(
                    "DELETE FROM journal_field_value_options WHERE value_id=?",
                    (value_id,),
                )
                conn.execute(
                    "DELETE FROM journal_field_value_references WHERE value_id=?",
                    (value_id,),
                )
                conn.execute(
                    "INSERT INTO journal_field_value_revisions("
                    "value_id,revision,value_json,value_sha256,actor_json,source_ref,"
                    "intent_id,created_at,authorship,review_state) "
                    "VALUES(?,2,?,?,?,?,?,?,'unknown','unknown')",
                    (
                        value_id,
                        redacted_value_json,
                        redacted_value_sha,
                        actor_json,
                        source_ref,
                        f"source-redaction:{redaction_event_id}:{value_id}",
                        now,
                    ),
                )
                event_id = "jso_" + hashlib.sha256(
                    (
                        "journal-import-field-search-delete:"
                        f"{redaction_event_id}:{value_id}:{scrubbed_revision}"
                    ).encode("utf-8")
                ).hexdigest()[:32]
                conn.execute(
                    "INSERT OR IGNORE INTO journal_search_outbox("
                    "event_id,aggregate_type,aggregate_id,aggregate_revision,"
                    "event_kind,content_sha256,search_recipe_version,privacy_class,"
                    "committed_at) VALUES(?,'field_value',?,?,'delete',?,1,?,?)",
                    (
                        event_id,
                        value_id,
                        str(scrubbed_revision),
                        redacted_value_sha,
                        field_value["privacy_class"],
                        now,
                    ),
                )
            conn.execute(
                "UPDATE journal_import_source_redactions SET state='committed',"
                "scrubbed_item_count=?,scrubbed_field_value_count=?,"
                "result_sha256=?,completed_at=? "
                "WHERE redaction_event_id=? AND state='scrubbing'",
                (
                    len(items),
                    len(field_values),
                    result_sha256,
                    now,
                    redaction_event_id,
                ),
            )
            conn.execute(
                "UPDATE journal_import_files SET source_ref=COALESCE(source_ref,?),"
                "source_usage_id=COALESCE(source_usage_id,?),"
                "source_usage_state='redaction_committed' "
                "WHERE cohort_id=? AND file_id=?",
                (source_ref, source_usage_id, cohort_id, file_id),
            )
            return len(items)

    def mark_import_source_usage_released(
        self,
        *,
        cohort_id: str,
        file_id: str,
        source_usage_id: str,
    ) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT source_usage_id,source_usage_state FROM journal_import_files "
                "WHERE cohort_id=? AND file_id=?",
                (cohort_id, file_id),
            ).fetchone()
            if row is None or str(row["source_usage_id"]) != source_usage_id:
                raise JournalCaptureConflict(
                    "The released import Source usage does not match."
                )
            if str(row["source_usage_state"]) == "released":
                return
            if str(row["source_usage_state"]) != "redaction_committed":
                raise JournalCaptureConflict(
                    "The import Source usage cannot be released before scrubbing."
                )
            conn.execute(
                "UPDATE journal_import_files SET source_usage_state='released' "
                "WHERE cohort_id=? AND file_id=? AND source_usage_id=?",
                (cohort_id, file_id, source_usage_id),
            )

    def get_item_revision_source_dependency(
        self,
        *,
        source_usage_id: str,
        source_usage_consumer_id: str,
        source_ref: str,
    ) -> Mapping[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_item_revision_source_dependencies "
                "WHERE source_usage_consumer_id=?",
                (source_usage_consumer_id,),
            ).fetchone()
        if row is None:
            return None
        if str(row["source_ref"]) != source_ref or (
            row["source_usage_id"] is not None
            and str(row["source_usage_id"]) != source_usage_id
        ):
            raise JournalCaptureConflict(
                "The Journal item revision Source dependency does not match."
            )
        return {
            "dependency_id": str(row["dependency_id"]),
            "item_id": str(row["item_id"]),
            "item_revision": (
                None if row["item_revision"] is None else int(row["item_revision"])
            ),
            "state": str(row["state"]),
        }

    def mark_item_revision_source_redacted(
        self,
        *,
        dependency_id: str,
        source_usage_id: str,
        source_usage_consumer_id: str,
        source_ref: str,
        redaction_event_id: str,
        redaction_epoch: int,
        result_sha256: str,
    ) -> str:
        redacted_text = "[redacted]"
        redacted_sha = hashlib.sha256(redacted_text.encode("utf-8")).hexdigest()
        now = _utc_now()
        with self.transaction() as conn:
            dependency = conn.execute(
                "SELECT * FROM journal_item_revision_source_dependencies "
                "WHERE dependency_id=?",
                (dependency_id,),
            ).fetchone()
            if dependency is None or (
                str(dependency["source_usage_consumer_id"])
                != source_usage_consumer_id
                or str(dependency["source_ref"]) != source_ref
                or str(dependency["source_usage_id"]) != source_usage_id
                or dependency["item_revision"] is None
            ):
                raise JournalCaptureConflict(
                    "The Journal item revision redaction dependency does not match."
                )
            prior = conn.execute(
                "SELECT * FROM journal_item_revision_source_redactions "
                "WHERE redaction_event_id=?",
                (redaction_event_id,),
            ).fetchone()
            if prior is not None:
                if (
                    str(prior["dependency_id"]) != dependency_id
                    or str(prior["result_sha256"]) != result_sha256
                ):
                    raise JournalCaptureConflict(
                        "That Journal item revision redaction is already bound differently."
                    )
                return str(prior["item_id"])
            item_id = str(dependency["item_id"])
            item_revision = int(dependency["item_revision"])
            item = conn.execute(
                "SELECT * FROM journal_items WHERE item_id=?",
                (item_id,),
            ).fetchone()
            revision = conn.execute(
                "SELECT * FROM journal_item_revisions WHERE item_id=? AND revision=?",
                (item_id, item_revision),
            ).fetchone()
            if item is None or revision is None or str(revision["source_ref"]) != source_ref:
                raise JournalCaptureConflict(
                    "The Journal item revision no longer matches its Source."
                )
            conn.execute(
                "INSERT INTO journal_item_revision_source_redactions("
                "redaction_event_id,dependency_id,source_usage_id,"
                "source_usage_consumer_id,source_ref,item_id,item_revision,"
                "redaction_epoch,state,created_at) "
                "VALUES(?,?,?,?,?,?,?,?, 'scrubbing',?)",
                (
                    redaction_event_id,
                    dependency_id,
                    source_usage_id,
                    source_usage_consumer_id,
                    source_ref,
                    item_id,
                    item_revision,
                    redaction_epoch,
                    now,
                ),
            )
            conn.execute(
                "UPDATE journal_item_revisions SET plain_value=?,content_sha256=?,"
                "lifecycle='tombstoned' WHERE item_id=? AND revision=?",
                (redacted_text, redacted_sha, item_id, item_revision),
            )
            scrubbed_current_revision = None
            if int(item["current_revision"]) == item_revision:
                scrubbed_current_revision = item_revision + 1
                actor_json = json.dumps(
                    {
                        "kind": "source_redaction",
                        "redactionEventId": redaction_event_id,
                        "sourceDependencyId": dependency_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                conn.execute(
                    "INSERT INTO journal_item_revisions("
                    "item_id,revision,authority_kind,plain_value,content_sha256,"
                    "lifecycle,actor_json,source_ref,authorship,review_state,"
                    "intent_id,created_at) VALUES(?,?,?,?,?,'tombstoned',?,?,"
                    "'unknown','unknown',?,?)",
                    (
                        item_id,
                        scrubbed_current_revision,
                        item["authority_kind"],
                        redacted_text,
                        redacted_sha,
                        actor_json,
                        source_ref,
                        f"source-redaction:{redaction_event_id}",
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE journal_items SET current_plain_value=?,"
                    "current_content_sha256=?,lifecycle='tombstoned',"
                    "current_revision=?,updated_at=? WHERE item_id=?",
                    (
                        redacted_text,
                        redacted_sha,
                        scrubbed_current_revision,
                        now,
                        item_id,
                    ),
                )
                event_id = "jso_" + hashlib.sha256(
                    f"journal-item-revision-redaction:{redaction_event_id}".encode()
                ).hexdigest()[:32]
                conn.execute(
                    "INSERT OR IGNORE INTO journal_search_outbox("
                    "event_id,aggregate_type,aggregate_id,aggregate_revision,"
                    "event_kind,content_sha256,search_recipe_version,privacy_class,"
                    "committed_at) VALUES(?,'item',?,?,'delete',?,1,?,?)",
                    (
                        event_id,
                        item_id,
                        str(scrubbed_current_revision),
                        redacted_sha,
                        item["privacy_class"],
                        now,
                    ),
                )
            conn.execute(
                "UPDATE journal_item_revision_source_redactions SET "
                "state='committed',scrubbed_current_revision=?,result_sha256=?,"
                "completed_at=? WHERE redaction_event_id=?",
                (
                    scrubbed_current_revision,
                    result_sha256,
                    now,
                    redaction_event_id,
                ),
            )
            conn.execute(
                "UPDATE journal_item_revision_source_dependencies SET "
                "state='redaction_committed',updated_at=? WHERE dependency_id=?",
                (now, dependency_id),
            )
        return item_id

    def mark_item_revision_source_usage_released(
        self,
        *,
        dependency_id: str,
        source_usage_id: str,
    ) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT source_usage_id,state FROM "
                "journal_item_revision_source_dependencies WHERE dependency_id=?",
                (dependency_id,),
            ).fetchone()
            if row is None or str(row["source_usage_id"]) != source_usage_id:
                raise JournalCaptureConflict(
                    "The released Journal item revision Source use does not match."
                )
            if str(row["state"]) == "released":
                return
            if str(row["state"]) != "redaction_committed":
                raise JournalCaptureConflict(
                    "The Journal item revision cannot release Source use before scrubbing."
                )
            conn.execute(
                "UPDATE journal_item_revision_source_dependencies SET "
                "state='released',updated_at=? WHERE dependency_id=?",
                (_utc_now(), dependency_id),
            )

    def get_native_item_source_dependency(
        self,
        *,
        source_usage_id: str,
        source_usage_consumer_id: str,
        source_ref: str,
    ) -> Mapping[str, Any] | None:
        """Resolve a generic native-item dependency for Source maintenance."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_native_source_dependencies "
                "WHERE source_usage_consumer_id=?",
                (source_usage_consumer_id,),
            ).fetchone()
        if row is None:
            return None
        if str(row["source_ref"]) != source_ref:
            raise JournalCaptureConflict(
                "The native item Source redaction does not match its Source."
            )
        if row["source_usage_id"] is not None and str(
            row["source_usage_id"]
        ) != source_usage_id:
            raise JournalCaptureConflict(
                "The native item Source redaction does not match its managed copy."
            )
        return {
            "dependency_id": str(row["dependency_id"]),
            "item_id": None if row["item_id"] is None else str(row["item_id"]),
            "state": str(row["state"]),
        }

    def mark_native_item_source_redacted(
        self,
        *,
        dependency_id: str,
        source_usage_id: str,
        source_usage_consumer_id: str,
        source_ref: str,
        redaction_event_id: str,
        redaction_epoch: int,
        result_sha256: str,
    ) -> str | None:
        """Scrub a generic Source-backed item, including every revision."""

        redacted_text = "[redacted]"
        redacted_sha = hashlib.sha256(redacted_text.encode("utf-8")).hexdigest()
        now = _utc_now()
        with self.transaction() as conn:
            dependency = conn.execute(
                "SELECT * FROM journal_native_source_dependencies "
                "WHERE dependency_id=?",
                (dependency_id,),
            ).fetchone()
            if dependency is None:
                raise KeyError("journal_native_source_dependency_not_found")
            if (
                str(dependency["source_usage_consumer_id"])
                != source_usage_consumer_id
                or str(dependency["source_ref"]) != source_ref
                or (
                    dependency["source_usage_id"] is not None
                    and str(dependency["source_usage_id"]) != source_usage_id
                )
            ):
                raise JournalCaptureConflict(
                    "The native item Source redaction dependency does not match."
                )
            prior = conn.execute(
                "SELECT * FROM journal_native_source_redactions "
                "WHERE redaction_event_id=?",
                (redaction_event_id,),
            ).fetchone()
            if prior is not None:
                if (
                    str(prior["dependency_id"]) != dependency_id
                    or str(prior["source_usage_id"]) != source_usage_id
                    or str(prior["source_usage_consumer_id"])
                    != source_usage_consumer_id
                    or str(prior["source_ref"]) != source_ref
                    or int(prior["redaction_epoch"]) != redaction_epoch
                    or str(prior["result_sha256"]) != result_sha256
                ):
                    raise JournalCaptureConflict(
                        "That native item Source redaction is already bound differently."
                    )
                return None if prior["item_id"] is None else str(prior["item_id"])

            item_id = None if dependency["item_id"] is None else str(dependency["item_id"])
            conn.execute(
                "INSERT INTO journal_native_source_redactions("
                "redaction_event_id,dependency_id,source_usage_id,"
                "source_usage_consumer_id,source_ref,item_id,redaction_epoch,state,"
                "created_at) VALUES(?,?,?,?,?,?,?,'scrubbing',?)",
                (
                    redaction_event_id,
                    dependency_id,
                    source_usage_id,
                    source_usage_consumer_id,
                    source_ref,
                    item_id,
                    redaction_epoch,
                    now,
                ),
            )
            scrubbed_revision = None
            if item_id is not None:
                item = conn.execute(
                    "SELECT current_revision,privacy_class,source_ref "
                    "FROM journal_items WHERE item_id=?",
                    (item_id,),
                ).fetchone()
                if item is None or str(item["source_ref"]) != source_ref:
                    raise JournalCaptureConflict(
                        "The native item does not match its Source dependency."
                    )
                scrubbed_revision = int(item["current_revision"]) + 1
                conn.execute(
                    "UPDATE journal_item_revisions SET plain_value=?,"
                    "content_sha256=?,lifecycle='tombstoned' WHERE item_id=?",
                    (redacted_text, redacted_sha, item_id),
                )
                actor_json = json.dumps(
                    {
                        "kind": "source_redaction",
                        "redactionEventId": redaction_event_id,
                        "sourceDependencyId": dependency_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                conn.execute(
                    "INSERT INTO journal_item_revisions("
                    "item_id,revision,authority_kind,plain_value,content_sha256,"
                    "lifecycle,actor_json,source_ref,authorship,review_state,"
                    "intent_id,created_at) "
                    "VALUES(?,?,'native_plain',?,?,'tombstoned',?,?,'unknown',"
                    "'unknown',?,?)",
                    (
                        item_id,
                        scrubbed_revision,
                        redacted_text,
                        redacted_sha,
                        actor_json,
                        source_ref,
                        f"source-redaction:{redaction_event_id}",
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE journal_items SET current_plain_value=?,"
                    "current_content_sha256=?,lifecycle='tombstoned',"
                    "current_revision=?,updated_at=? WHERE item_id=?",
                    (redacted_text, redacted_sha, scrubbed_revision, now, item_id),
                )
                event_id = "jso_" + hashlib.sha256(
                    (
                        "journal-native-source-search-delete:"
                        f"{redaction_event_id}:{item_id}:{scrubbed_revision}"
                    ).encode("utf-8")
                ).hexdigest()[:32]
                conn.execute(
                    "INSERT OR IGNORE INTO journal_search_outbox("
                    "event_id,aggregate_type,aggregate_id,aggregate_revision,"
                    "event_kind,content_sha256,search_recipe_version,privacy_class,"
                    "committed_at) VALUES(?,'item',?,?,'delete',?,1,?,?)",
                    (
                        event_id,
                        item_id,
                        str(scrubbed_revision),
                        redacted_sha,
                        item["privacy_class"],
                        now,
                    ),
                )
            conn.execute(
                "UPDATE journal_native_source_redactions SET state='committed',"
                "scrubbed_revision=?,result_sha256=?,completed_at=? "
                "WHERE redaction_event_id=? AND state='scrubbing'",
                (scrubbed_revision, result_sha256, now, redaction_event_id),
            )
            conn.execute(
                "UPDATE journal_native_source_dependencies SET "
                "source_usage_id=COALESCE(source_usage_id,?),"
                "state='redaction_committed',updated_at=? WHERE dependency_id=?",
                (source_usage_id, now, dependency_id),
            )
            return item_id

    def mark_native_item_source_usage_released(
        self,
        *,
        dependency_id: str,
        source_usage_id: str,
    ) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT source_usage_id,state FROM journal_native_source_dependencies "
                "WHERE dependency_id=?",
                (dependency_id,),
            ).fetchone()
            if row is None or str(row["source_usage_id"]) != source_usage_id:
                raise JournalCaptureConflict(
                    "The released native item Source usage does not match."
                )
            if str(row["state"]) == "released":
                return
            if str(row["state"]) != "redaction_committed":
                raise JournalCaptureConflict(
                    "The native item Source use cannot be released before scrubbing."
                )
            conn.execute(
                "UPDATE journal_native_source_dependencies SET state='released',"
                "updated_at=? WHERE dependency_id=? AND source_usage_id=?",
                (_utc_now(), dependency_id, source_usage_id),
            )

    def get_prompt_source_dependency(
        self,
        *,
        source_usage_id: str,
        source_usage_consumer_id: str,
        source_ref: str,
    ) -> Mapping[str, Any] | None:
        with self._connect() as conn:
            input_row = conn.execute(
                "SELECT * FROM journal_prompt_input_source_dependencies "
                "WHERE source_usage_consumer_id=?",
                (source_usage_consumer_id,),
            ).fetchone()
            result_row = conn.execute(
                "SELECT * FROM journal_prompt_result_source_dependencies "
                "WHERE source_usage_consumer_id=?",
                (source_usage_consumer_id,),
            ).fetchone()
        row = input_row or result_row
        if row is None:
            return None
        if str(row["source_ref"]) != source_ref or (
            row["source_usage_id"] is not None
            and str(row["source_usage_id"]) != source_usage_id
        ):
            raise JournalCaptureConflict(
                "The Journal prompt Source dependency does not match."
            )
        return {
            "dependency_kind": "input" if input_row is not None else "result",
            "dependency_id": str(row["dependency_id"]),
            "interaction_id": str(row["interaction_id"]),
            "variant_id": (
                None
                if input_row is not None or row["variant_id"] is None
                else str(row["variant_id"])
            ),
            "state": str(row["state"]),
        }

    def mark_prompt_source_redacted(
        self,
        *,
        dependency_kind: str,
        dependency_id: str,
        source_usage_id: str,
        source_usage_consumer_id: str,
        source_ref: str,
        redaction_event_id: str,
        redaction_epoch: int,
        result_sha256: str,
    ) -> str:
        if dependency_kind not in {"input", "result"}:
            raise JournalCaptureConflict("The Journal prompt Source kind is invalid.")
        table = (
            "journal_prompt_input_source_dependencies"
            if dependency_kind == "input"
            else "journal_prompt_result_source_dependencies"
        )
        now = _utc_now()
        redacted = "[redacted]"
        redacted_sha = hashlib.sha256(redacted.encode()).hexdigest()
        with self.transaction() as conn:
            dependency = conn.execute(
                f"SELECT * FROM {table} WHERE dependency_id=?",
                (dependency_id,),
            ).fetchone()
            if dependency is None or (
                str(dependency["source_usage_id"]) != source_usage_id
                or str(dependency["source_usage_consumer_id"])
                != source_usage_consumer_id
                or str(dependency["source_ref"]) != source_ref
            ):
                raise JournalCaptureConflict(
                    "The Journal prompt redaction dependency does not match."
                )
            prior = conn.execute(
                "SELECT * FROM journal_prompt_source_redactions "
                "WHERE redaction_event_id=?",
                (redaction_event_id,),
            ).fetchone()
            if prior is not None:
                if (
                    str(prior["dependency_id"]) != dependency_id
                    or str(prior["result_sha256"]) != result_sha256
                ):
                    raise JournalCaptureConflict(
                        "That Journal prompt redaction is already bound differently."
                    )
                return str(prior["interaction_id"])
            interaction_id = str(dependency["interaction_id"])
            variant_id = (
                None
                if dependency_kind == "input"
                else str(dependency["variant_id"])
            )
            conn.execute(
                "INSERT INTO journal_prompt_source_redactions("
                "redaction_event_id,dependency_kind,dependency_id,source_usage_id,"
                "source_usage_consumer_id,source_ref,interaction_id,variant_id,"
                "redaction_epoch,state,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'scrubbing',?)",
                (
                    redaction_event_id,
                    dependency_kind,
                    dependency_id,
                    source_usage_id,
                    source_usage_consumer_id,
                    source_ref,
                    interaction_id,
                    variant_id,
                    redaction_epoch,
                    now,
                ),
            )
            if dependency_kind == "input":
                interaction = conn.execute(
                    "SELECT current_revision FROM journal_prompt_interactions "
                    "WHERE interaction_id=?",
                    (interaction_id,),
                ).fetchone()
                if interaction is None:
                    raise JournalCaptureConflict(
                        "The Journal prompt interaction is unavailable."
                    )
                conn.execute(
                    "UPDATE journal_prompt_interactions SET input_text=?,"
                    "input_sha256=?,lifecycle='tombstoned',current_revision=?,"
                    "updated_at=? WHERE interaction_id=?",
                    (
                        redacted,
                        redacted_sha,
                        int(interaction["current_revision"]) + 1,
                        now,
                        interaction_id,
                    ),
                )
                conn.execute(
                    "UPDATE journal_prompt_generation_requests SET "
                    "status='canceled',error_code='prompt_input_redacted',"
                    "completed_at=?,updated_at=? WHERE interaction_id=? "
                    "AND status IN ('pending','leased')",
                    (now, now, interaction_id),
                )
            else:
                assert variant_id is not None
                conn.execute(
                    "UPDATE journal_prompt_result_variants SET result_text=?,"
                    "result_content_sha256=?,lifecycle='archived',updated_at=? "
                    "WHERE variant_id=?",
                    (redacted, redacted_sha, now, variant_id),
                )
                event_id = "jso_" + hashlib.sha256(
                    f"journal-prompt-result-redaction:{redaction_event_id}".encode()
                ).hexdigest()[:32]
                conn.execute(
                    "INSERT OR IGNORE INTO journal_search_outbox("
                    "event_id,aggregate_type,aggregate_id,aggregate_revision,"
                    "event_kind,content_sha256,search_recipe_version,privacy_class,"
                    "committed_at) VALUES(?,'prompt_result',?,'redacted','delete',"
                    "?,1,'private',?)",
                    (event_id, variant_id, redacted_sha, now),
                )
            conn.execute(
                f"UPDATE {table} SET state='redaction_committed',updated_at=? "
                "WHERE dependency_id=?",
                (now, dependency_id),
            )
            conn.execute(
                "UPDATE journal_prompt_source_redactions SET state='committed',"
                "result_sha256=?,completed_at=? WHERE redaction_event_id=?",
                (result_sha256, now, redaction_event_id),
            )
        return interaction_id

    def mark_prompt_source_usage_released(
        self,
        *,
        dependency_kind: str,
        dependency_id: str,
        source_usage_id: str,
    ) -> None:
        table = (
            "journal_prompt_input_source_dependencies"
            if dependency_kind == "input"
            else "journal_prompt_result_source_dependencies"
        )
        with self.transaction() as conn:
            row = conn.execute(
                f"SELECT source_usage_id,state FROM {table} WHERE dependency_id=?",
                (dependency_id,),
            ).fetchone()
            if row is None or str(row["source_usage_id"]) != source_usage_id:
                raise JournalCaptureConflict(
                    "The released Journal prompt Source use does not match."
                )
            if str(row["state"]) == "released":
                return
            if str(row["state"]) != "redaction_committed":
                raise JournalCaptureConflict(
                    "The Journal prompt Source use cannot release before scrubbing."
                )
            conn.execute(
                f"UPDATE {table} SET state='released',updated_at=? "
                "WHERE dependency_id=?",
                (_utc_now(), dependency_id),
            )

    def get_field_value_source_dependency(
        self,
        *,
        source_usage_id: str,
        source_usage_consumer_id: str,
        source_ref: str,
    ) -> Mapping[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_field_source_dependencies "
                "WHERE source_usage_consumer_id=?",
                (source_usage_consumer_id,),
            ).fetchone()
        if row is None:
            return None
        if str(row["source_ref"]) != source_ref or (
            row["source_usage_id"] is not None
            and str(row["source_usage_id"]) != source_usage_id
        ):
            raise JournalCaptureConflict(
                "The Journal field Source redaction dependency does not match."
            )
        return {
            "dependency_id": str(row["dependency_id"]),
            "value_id": str(row["value_id"]),
            "value_revision": (
                None if row["value_revision"] is None else int(row["value_revision"])
            ),
            "state": str(row["state"]),
        }

    def mark_field_value_source_redacted(
        self,
        *,
        dependency_id: str,
        source_usage_id: str,
        source_usage_consumer_id: str,
        source_ref: str,
        redaction_event_id: str,
        redaction_epoch: int,
        result_sha256: str,
    ) -> tuple[str, int | None]:
        """Scrub the exact typed revision and tombstone it when current."""

        redacted = {"redacted": True}
        redacted_json = json.dumps(
            redacted, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        redacted_sha = hashlib.sha256(redacted_json.encode("utf-8")).hexdigest()
        now = _utc_now()
        with self.transaction() as conn:
            dependency = conn.execute(
                "SELECT * FROM journal_field_source_dependencies "
                "WHERE dependency_id=?",
                (dependency_id,),
            ).fetchone()
            if dependency is None:
                raise KeyError("journal_field_source_dependency_not_found")
            if (
                str(dependency["source_usage_consumer_id"])
                != source_usage_consumer_id
                or str(dependency["source_ref"]) != source_ref
                or (
                    dependency["source_usage_id"] is not None
                    and str(dependency["source_usage_id"]) != source_usage_id
                )
            ):
                raise JournalCaptureConflict(
                    "The Journal field Source redaction dependency does not match."
                )
            prior = conn.execute(
                "SELECT * FROM journal_field_source_redactions "
                "WHERE redaction_event_id=?",
                (redaction_event_id,),
            ).fetchone()
            if prior is not None:
                if (
                    str(prior["dependency_id"]) != dependency_id
                    or str(prior["source_usage_id"]) != source_usage_id
                    or str(prior["source_usage_consumer_id"])
                    != source_usage_consumer_id
                    or str(prior["source_ref"]) != source_ref
                    or int(prior["redaction_epoch"]) != redaction_epoch
                    or str(prior["result_sha256"]) != result_sha256
                ):
                    raise JournalCaptureConflict(
                        "That Journal field Source redaction is bound differently."
                    )
                return (
                    str(prior["value_id"]),
                    None
                    if prior["scrubbed_revision"] is None
                    else int(prior["scrubbed_revision"]),
                )

            value_id = str(dependency["value_id"])
            value_revision = (
                None
                if dependency["value_revision"] is None
                else int(dependency["value_revision"])
            )
            conn.execute(
                "INSERT INTO journal_field_source_redactions("
                "redaction_event_id,dependency_id,source_usage_id,"
                "source_usage_consumer_id,source_ref,value_id,value_revision,"
                "redaction_epoch,state,created_at) "
                "VALUES(?,?,?,?,?,?,?,?, 'scrubbing',?)",
                (
                    redaction_event_id,
                    dependency_id,
                    source_usage_id,
                    source_usage_consumer_id,
                    source_ref,
                    value_id,
                    value_revision,
                    redaction_epoch,
                    now,
                ),
            )
            scrubbed_revision = value_revision
            if value_revision is not None:
                revision = conn.execute(
                    "SELECT 1 FROM journal_field_value_revisions "
                    "WHERE value_id=? AND revision=?",
                    (value_id, value_revision),
                ).fetchone()
                if revision is None:
                    raise JournalCaptureConflict(
                        "The Journal field revision dependency is unavailable."
                    )
                conn.execute(
                    "UPDATE journal_field_value_revisions SET value_json=?,"
                    "value_sha256=? WHERE value_id=? AND revision=?",
                    (redacted_json, redacted_sha, value_id, value_revision),
                )
                current = conn.execute(
                    "SELECT value.*,definition.privacy_class AS field_privacy_class "
                    "FROM journal_field_values AS value "
                    "JOIN journal_field_definition_versions AS definition "
                    "ON definition.field_id=value.field_id "
                    "AND definition.definition_version=value.field_definition_version "
                    "WHERE value.value_id=?",
                    (value_id,),
                ).fetchone()
                if current is not None and int(current["current_revision"]) == value_revision:
                    scrubbed_revision = value_revision + 1
                    conn.execute(
                        "UPDATE journal_field_values SET disposition='missing',"
                        "text_value=NULL,number_value=NULL,boolean_value=NULL,"
                        "temporal_value=NULL,duration_seconds=NULL,option_value=NULL,"
                        "collection_present=0,lifecycle='tombstoned',"
                        "current_revision=?,updated_at=? WHERE value_id=?",
                        (scrubbed_revision, now, value_id),
                    )
                    conn.execute(
                        "DELETE FROM journal_field_value_options WHERE value_id=?",
                        (value_id,),
                    )
                    conn.execute(
                        "DELETE FROM journal_field_value_references WHERE value_id=?",
                        (value_id,),
                    )
                    actor_json = json.dumps(
                        {
                            "kind": "source_redaction",
                            "redactionEventId": redaction_event_id,
                            "sourceDependencyId": dependency_id,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    conn.execute(
                        "INSERT INTO journal_field_value_revisions("
                        "value_id,revision,value_json,value_sha256,actor_json,"
                        "source_ref,intent_id,created_at,authorship,review_state) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            value_id,
                            scrubbed_revision,
                            redacted_json,
                            redacted_sha,
                            actor_json,
                            source_ref,
                            f"source-redaction:{redaction_event_id}",
                            now,
                            "unknown",
                            "unknown",
                        ),
                    )
                    event_id = "jso_" + hashlib.sha256(
                        (
                            "journal-field-source-search-delete:"
                            f"{redaction_event_id}:{value_id}:{scrubbed_revision}"
                        ).encode("utf-8")
                    ).hexdigest()[:32]
                    conn.execute(
                        "INSERT OR IGNORE INTO journal_search_outbox("
                        "event_id,aggregate_type,aggregate_id,aggregate_revision,"
                        "event_kind,content_sha256,search_recipe_version,privacy_class,"
                        "committed_at) VALUES(?,'field_value',?,?,'delete',?,1,?,?)",
                        (
                            event_id,
                            value_id,
                            str(scrubbed_revision),
                            redacted_sha,
                            current["field_privacy_class"],
                            now,
                        ),
                    )
            conn.execute(
                "UPDATE journal_field_source_redactions SET state='committed',"
                "scrubbed_revision=?,result_sha256=?,completed_at=? "
                "WHERE redaction_event_id=? AND state='scrubbing'",
                (scrubbed_revision, result_sha256, now, redaction_event_id),
            )
            conn.execute(
                "UPDATE journal_field_source_dependencies SET "
                "source_usage_id=COALESCE(source_usage_id,?),"
                "state='redaction_committed',updated_at=? WHERE dependency_id=?",
                (source_usage_id, now, dependency_id),
            )
            return value_id, scrubbed_revision

    def mark_field_value_source_usage_released(
        self,
        *,
        dependency_id: str,
        source_usage_id: str,
    ) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT source_usage_id,state FROM journal_field_source_dependencies "
                "WHERE dependency_id=?",
                (dependency_id,),
            ).fetchone()
            if row is None or str(row["source_usage_id"]) != source_usage_id:
                raise JournalCaptureConflict(
                    "The released Journal field Source usage does not match."
                )
            if str(row["state"]) == "released":
                return
            if str(row["state"]) != "redaction_committed":
                raise JournalCaptureConflict(
                    "The Journal field Source use cannot release before scrubbing."
                )
            conn.execute(
                "UPDATE journal_field_source_dependencies SET state='released',"
                "updated_at=? WHERE dependency_id=? AND source_usage_id=?",
                (_utc_now(), dependency_id, source_usage_id),
            )

    def record_mutation(
        self,
        *,
        client_mutation_id: str,
        request_sha256: str,
        result: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        encoded = _json(result)
        assert encoded is not None
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_mutations WHERE client_mutation_id=?",
                (client_mutation_id,),
            ).fetchone()
            if row is not None:
                if row["request_sha256"] != request_sha256:
                    raise JournalCaptureConflict(
                        "That mutation key was already used for a different change."
                    )
                decoded = json.loads(row["result_json"])
                return decoded
            conn.execute(
                "INSERT INTO journal_mutations VALUES(?,?,?,?)",
                (client_mutation_id, request_sha256, encoded, _utc_now()),
            )
        return result

    @staticmethod
    def _capture(row: sqlite3.Row) -> JournalCapture:
        return JournalCapture(
            capture_id=row["capture_id"],
            client_mutation_id=row["client_mutation_id"],
            request_sha256=row["request_sha256"],
            source_ref=row["source_ref"],
            representation_id=row["representation_id"],
            submission_id=row["submission_id"],
            command_id=row["command_id"],
            source_effect_id=row["source_effect_id"],
            source_usage_id=row["source_usage_id"],
            day_id=row["day_id"],
            requested_target=CaptureTarget(row["requested_target"]),
            resolved_target=(
                CaptureTarget(row["resolved_target"])
                if row["resolved_target"] is not None
                else None
            ),
            mode=CaptureMode(row["mode"]),
            input_mode=row["input_mode"],
            stated_at=row["stated_at"],
            submitted_at=row["submitted_at"],
            persistence_status=row["persistence_status"],
            processing_status=ProcessingState(row["processing_status"]),
            processing_error_code=row["processing_error_code"],
            annotation=_decode_json(row["annotation_json"]),
            entry_id=row["entry_id"],
            revision=int(row["revision"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _entry(row: sqlite3.Row) -> JournalEntry:
        return JournalEntry(
            entry_id=row["entry_id"],
            capture_id=row["capture_id"],
            day_id=row["day_id"],
            entry_kind=CaptureTarget(row["entry_kind"]),
            source_ref=row["source_ref"],
            content_sha256=row["content_sha256"],
            markdown=row["markdown"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=int(row["version"]),
            resolution_state=row["resolution_state"],
            processing_status=ProcessingState(row["processing_status"]),
            annotation=_decode_json(row["annotation_json"]),
            processing_error_code=row["processing_error_code"],
            projection_state=ProjectionState(row["projection_state"]),
            projection_marker=row["projection_marker"],
            projection_base_sha256=row["projection_base_sha256"],
            projection_result_sha256=row["projection_result_sha256"],
        )

    @staticmethod
    def _effect(row: sqlite3.Row) -> JournalEffect:
        return JournalEffect(
            effect_id=row["effect_id"],
            capture_id=row["capture_id"],
            effect_type=row["effect_type"],
            state=EffectState(row["state"]),
            attempts=int(row["attempts"]),
            authorization_fingerprint=row["authorization_fingerprint"],
            authorization_expires_at=row["authorization_expires_at"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            error_code=row["error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            payload=_decode_json(row["payload_json"]),
            result=_decode_json(row["result_json"]),
        )

    @staticmethod
    def _document_binding(row: sqlite3.Row) -> JournalDocumentBinding:
        inspection = _decode_json(row["inspection_json"])
        if inspection is None:
            raise RuntimeError("invalid_journal_document_binding")
        return JournalDocumentBinding(
            entry_id=row["entry_id"],
            binding_id=row["binding_id"],
            store_id=row["store_id"],
            document_id=row["document_id"],
            change_id=row["change_id"],
            source_consumer_id=row["source_consumer_id"],
            source_usage_id=row["source_usage_id"],
            source_use_kind=row["source_use_kind"],
            source_disclosure_kind=row["source_disclosure_kind"],
            source_redaction_policy=row["source_redaction_policy"],
            source_maintenance_state=row["source_maintenance_state"],
            source_maintenance=_decode_json(row["source_maintenance_json"]) or {},
            cowork_href=row["cowork_href"],
            content_authority_epoch=int(row["content_authority_epoch"]),
            entry_version=int(row["entry_version"]),
            inspection=inspection,
            state=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _module_document_binding(row: sqlite3.Row) -> JournalModuleDocumentBinding:
        return JournalModuleDocumentBinding(**dict(row))

    @staticmethod
    def _usage_transition(row: sqlite3.Row) -> JournalDocumentUsageTransition:
        return JournalDocumentUsageTransition(**dict(row))

    @staticmethod
    def _migration(row: sqlite3.Row) -> JournalMigrationRecord:
        return JournalMigrationRecord(
            entity_kind=str(row["entity_kind"]),
            entity_id=str(row["entity_id"]),
            day_id=str(row["day_id"]),
            marker_id=str(row["marker_id"]),
            selection_start=row["selection_start"],
            selection_end=row["selection_end"],
            selected_file_sha256=row["selected_file_sha256"],
            selected_section_sha256=row["selected_section_sha256"],
            source_ref=row["source_ref"],
            representation_id=row["representation_id"],
            source_content_sha256=row["source_content_sha256"],
            binding_id=row["binding_id"],
            store_id=row["store_id"],
            document_id=row["document_id"],
            comparison_state=JournalMigrationComparison(row["comparison_state"]),
            byte_parity=(
                None if row["byte_parity"] is None else bool(row["byte_parity"])
            ),
            normalized_parity=(
                None
                if row["normalized_parity"] is None
                else bool(row["normalized_parity"])
            ),
            structural_parity=(
                None
                if row["structural_parity"] is None
                else bool(row["structural_parity"])
            ),
            rollback_deadline=row["rollback_deadline"],
            mirrored_state=JournalMigrationState(row["mirrored_state"]),
            mirrored_authority_epoch=int(row["mirrored_authority_epoch"]),
            projection_state=str(row["projection_state"]),
            divergence_source_ref=row["divergence_source_ref"],
            operation_id=row["operation_id"],
            error_code=row["error_code"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

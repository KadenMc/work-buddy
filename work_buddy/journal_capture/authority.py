"""Explicit authority seal for the Journal database-only cutover."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping

from work_buddy.cutover_maintenance import (
    CutoverMaintenanceError,
    IsolatedRehearsalAuthorization,
    mark_postseal_pending,
    pause_cutover_maintenance,
    prior_postseal_release_evidence,
    release_postseal_maintenance,
    require_isolated_rehearsal_path,
    resume_preseal_maintenance,
)
from work_buddy.journal_capture.models import (
    CaptureTarget,
    JournalCapture,
    JournalCaptureConflict,
    JournalCaptureError,
    JournalCutoverPaused,
)
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.installed_authority import (
    InstalledAuthorityError,
    confirm_domain_seal,
    mark_domain_released,
    prepare_domain_seal,
    require_domain_store_open,
)
from work_buddy.sources.store import SourceStore


_REDACTED_TEXT = "[redacted]"
_REDACTED_SHA256 = hashlib.sha256(_REDACTED_TEXT.encode("utf-8")).hexdigest()
_LEGACY_MARKDOWN_GUARD_TARGETS: ContextVar[tuple[str, ...]] = ContextVar(
    "journal_legacy_markdown_guard_targets",
    default=(),
)


class JournalAuthorityStateError(JournalCaptureError):
    code = "journal_authority_state_invalid"


class JournalAuthorityFenced(JournalCaptureError):
    code = "journal_authority_recovery_fenced"

    def __init__(self) -> None:
        super().__init__(
            "Journal writes are paused while authority recovery is in progress.",
            retryable=True,
        )


@dataclass(frozen=True, slots=True)
class JournalAuthorityState:
    mode: str
    authority_revision: int
    activated_cohort_id: str | None
    prior_mode: str | None
    first_native_capture_id: str | None
    first_native_item_id: str | None
    first_native_write_at: str | None
    fence_code: str | None
    fenced_at: str | None
    unfinished_materializations: int
    cutover_gate_state: str
    cutover_gate_revision: int
    cutover_cohort_id: str | None
    cutover_inventory_sha256: str | None
    cutover_paused_at: str | None
    cutover_released_at: str | None
    cutover_evidence_sha256: str | None
    capture_row_count: int | None
    capture_row_high_water: int | None
    entry_row_count: int | None
    entry_row_high_water: int | None

    @property
    def reversible_to_legacy(self) -> bool:
        return False


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _path_sha(path: str | Path) -> str:
    normalized = str(Path(path).expanduser().resolve()).replace("\\", "/").casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _result_sha(result_json: str) -> str:
    return hashlib.sha256(result_json.encode("utf-8")).hexdigest()


def existing_authority_mode(path: str | Path | None = None) -> str:
    """Read the durable Journal authority without creating or migrating it.

    Compatibility writers call this at the last shared file-write boundary.
    A missing/pre-authority database means the old compatibility epoch.  An
    unreadable or malformed control row fails closed so a damaged database can
    never silently re-enable Markdown writes.
    """

    from work_buddy.paths import resolve

    target = (
        Path(path).expanduser().resolve()
        if path is not None
        else resolve("db/journal-capture").expanduser().resolve()
    )
    try:
        require_domain_store_open("journal", target)
    except InstalledAuthorityError as exc:
        raise JournalAuthorityStateError(
            "Journal's installed authority latch cannot prove the native database; "
            "legacy file writes are fenced."
        ) from exc
    if not target.is_file():
        return "legacy_compatibility"
    try:
        with sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True) as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "journal_authority_control" not in tables:
                return "legacy_compatibility"
            row = conn.execute(
                "SELECT mode FROM journal_authority_control WHERE singleton=1"
            ).fetchone()
            gate = (
                conn.execute(
                    "SELECT state FROM journal_cutover_gate WHERE singleton=1"
                ).fetchone()
                if "journal_cutover_gate" in tables
                else None
            )
            maintenance = (
                conn.execute(
                    "SELECT state FROM cutover_maintenance WHERE singleton=1"
                ).fetchone()
                if "cutover_maintenance" in tables
                else None
            )
    except sqlite3.Error as exc:
        raise JournalAuthorityStateError(
            "Journal authority is unavailable; legacy file writes are fenced."
        ) from exc
    if row is None or str(row[0]) not in {
        "legacy_compatibility",
        "database_only",
        "recovery_fenced",
    }:
        raise JournalAuthorityStateError(
            "Journal authority is invalid; legacy file writes are fenced."
        )
    mode = str(row[0])
    if gate is not None:
        gate_state = str(gate[0])
        if gate_state not in {"open", "paused"}:
            raise JournalAuthorityStateError(
                "Journal cutover gate is invalid; legacy file writes are fenced."
            )
        if mode == "legacy_compatibility" and gate_state == "paused":
            return "cutover_paused"
    if maintenance is not None:
        maintenance_state = str(maintenance[0])
        if maintenance_state not in {
            "open",
            "preseal_fenced",
            "postseal_pending",
            "recovery",
        }:
            raise JournalAuthorityStateError(
                "Journal cutover maintenance is invalid; legacy file writes are fenced."
            )
        if mode == "legacy_compatibility" and maintenance_state != "open":
            return "cutover_paused"
    return mode


def require_legacy_markdown_write(path: str | Path | None = None) -> None:
    """Reject every daily-file mutation after the database authority seal."""

    mode = existing_authority_mode(path)
    if mode != "legacy_compatibility":
        raise JournalAuthorityStateError(
            f"Journal Markdown writes are fenced while authority is {mode}."
        )


@contextmanager
def legacy_markdown_write_guard(
    path: str | Path | None = None,
) -> Iterator[None]:
    """Hold the Journal database writer lock for one compatibility file write.

    The lock closes the check/write race at cutover: a pause waits for an
    in-flight guarded write to finish, while a writer arriving after the pause
    observes the durable gate before it can touch the file.  Missing historical
    stores keep compatibility behavior without being created as a side effect.
    """

    from work_buddy.paths import resolve

    target = (
        Path(path).expanduser().resolve()
        if path is not None
        else resolve("db/journal-capture").expanduser().resolve()
    )
    target_identity = str(target).replace("\\", "/").casefold()
    held_targets = _LEGACY_MARKDOWN_GUARD_TARGETS.get()
    if target_identity in held_targets:
        # Compatibility adapters can reach the shared vault writer, which
        # applies this same final-boundary guard.  The outer transaction still
        # owns the SQLite writer lock, so reacquiring it here would deadlock.
        yield
        return
    try:
        installed = require_domain_store_open("journal", target)
    except InstalledAuthorityError as exc:
        raise JournalAuthorityStateError(
            "Journal's installed authority latch cannot prove the native database; "
            "legacy file writes are fenced."
        ) from exc
    if installed is not None:
        raise JournalAuthorityStateError(
            "Journal Markdown writes are fenced and retired under installed authority."
        )
    if not target.is_file():
        token = _LEGACY_MARKDOWN_GUARD_TARGETS.set(
            (*held_targets, target_identity)
        )
        try:
            require_legacy_markdown_write(target)
            yield
            return
        finally:
            _LEGACY_MARKDOWN_GUARD_TARGETS.reset(token)
    conn: sqlite3.Connection | None = None
    token = None
    try:
        conn = sqlite3.connect(
            f"file:{target.as_posix()}?mode=rw",
            uri=True,
            timeout=10.0,
        )
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            installed = require_domain_store_open("journal", target)
        except InstalledAuthorityError as exc:
            raise JournalAuthorityStateError(
                "Journal's installed authority latch cannot prove the native database; "
                "legacy file writes are fenced."
            ) from exc
        if installed is not None:
            raise JournalAuthorityStateError(
                "Journal Markdown writes are fenced and retired under installed authority."
            )
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "journal_authority_control" in tables:
            authority = conn.execute(
                "SELECT mode FROM journal_authority_control WHERE singleton=1"
            ).fetchone()
            if authority is None or str(authority[0]) != "legacy_compatibility":
                mode = "invalid" if authority is None else str(authority[0])
                raise JournalAuthorityStateError(
                    f"Journal Markdown writes are fenced while authority is {mode}."
                )
        if "journal_cutover_gate" in tables:
            gate = conn.execute(
                "SELECT state FROM journal_cutover_gate WHERE singleton=1"
            ).fetchone()
            if gate is None or str(gate[0]) != "open":
                state = "invalid" if gate is None else str(gate[0])
                raise JournalAuthorityStateError(
                    f"Journal Markdown writes are fenced while cutover gate is {state}."
                )
        if "cutover_maintenance" in tables:
            maintenance = conn.execute(
                "SELECT state FROM cutover_maintenance WHERE singleton=1"
            ).fetchone()
            if maintenance is None or str(maintenance[0]) != "open":
                state = "invalid" if maintenance is None else str(maintenance[0])
                raise JournalAuthorityStateError(
                    "Journal Markdown writes are fenced while cutover maintenance "
                    f"is {state}."
                )
        token = _LEGACY_MARKDOWN_GUARD_TARGETS.set(
            (*held_targets, target_identity)
        )
        yield
        conn.commit()
    except sqlite3.Error as exc:
        if conn is not None:
            conn.rollback()
        raise JournalAuthorityStateError(
            "Journal authority is unavailable; legacy file writes are fenced."
        ) from exc
    except BaseException:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if token is not None:
            _LEGACY_MARKDOWN_GUARD_TARGETS.reset(token)
        if conn is not None:
            conn.close()


class JournalAuthorityCoordinator:
    """Coordinate cutover, rollback, recovery fencing, and native publication.

    Import sealing and authority activation are deliberately separate calls.
    Activation requires a sealed cohort and a quiet legacy materialization
    queue.  The first native-only item is recorded in the same transaction as
    the item and effect receipt; after that point rollback is rejected.
    """

    def __init__(self, store: JournalCaptureStore) -> None:
        self.store = store

    def state(self) -> JournalAuthorityState:
        with self.store._connect() as conn:
            return self._state(conn)

    def capture_mode(self) -> str:
        with self.store._connect() as conn:
            state = self._state(conn)
            if state.mode == "recovery_fenced":
                raise JournalAuthorityFenced()
            if state.cutover_gate_state != "open":
                raise JournalCutoverPaused()
            return state.mode

    def pause_legacy_ingress(
        self,
        *,
        cohort_id: str,
        client_mutation_id: str,
        actor: Mapping[str, Any],
    ) -> JournalAuthorityState:
        """Fence capture and Markdown writers before final cohort verification.

        ``BEGIN IMMEDIATE`` shares the same SQLite writer barrier held by each
        guarded Markdown write.  When this returns, no guarded file write is in
        flight, new capture inserts are trigger-fenced, and the exact capture /
        legacy-entry high-water is durable for activation-time verification.
        """

        actor_sha = _sha({"actor": dict(actor)})
        request_sha = _sha(
            {
                "schema": "wb.journal-cutover-gate/v1",
                "operation": "pause_legacy_ingress",
                "cohortId": cohort_id,
                "actorSha256": actor_sha,
            }
        )
        now = _now()
        with self.store.transaction() as conn:
            if self._mutation_replay(conn, client_mutation_id, request_sha):
                return self._state(conn)
            control = self._control(conn)
            if str(control["mode"]) != "legacy_compatibility":
                raise JournalAuthorityStateError(
                    "legacy ingress can be paused only from compatibility authority"
                )
            cohort = conn.execute(
                "SELECT state,inventory_sha256 FROM journal_import_cohorts "
                "WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if cohort is None or str(cohort["state"]) not in {
                "prepared",
                "staging",
                "verified",
            }:
                raise JournalAuthorityStateError(
                    "legacy ingress pause requires an unsealed import cohort"
                )
            try:
                pause_cutover_maintenance(
                    conn,
                    domain="journal",
                    cohort_id=cohort_id,
                    inventory_sha256=str(cohort["inventory_sha256"]),
                    mutation_id=client_mutation_id,
                    actor_sha256=actor_sha,
                    at=now,
                )
            except CutoverMaintenanceError as exc:
                raise JournalAuthorityStateError(str(exc)) from exc
            gate = self._cutover_gate(conn)
            if str(gate["state"]) != "open":
                raise JournalAuthorityStateError("Journal cutover ingress is already paused")
            unfinished = self._unfinished_materializations(conn)
            if unfinished:
                raise JournalAuthorityStateError(
                    "legacy Journal materialization must be quiet before ingress pause"
                )
            capture_count, capture_high_water = self._row_high_water(
                conn, "journal_captures"
            )
            entry_count, entry_high_water = self._row_high_water(
                conn, "journal_entries"
            )
            revision = int(gate["gate_revision"]) + 1
            cursor = conn.execute(
                "UPDATE journal_cutover_gate SET state='paused',gate_revision=?,"
                "cohort_id=?,request_sha256=?,capture_row_count=?,"
                "capture_row_high_water=?,entry_row_count=?,entry_row_high_water=?,"
                "paused_at=?,updated_at=? WHERE singleton=1 AND state='open' "
                "AND gate_revision=?",
                (
                    revision,
                    cohort_id,
                    request_sha,
                    capture_count,
                    capture_high_water,
                    entry_count,
                    entry_high_water,
                    now,
                    now,
                    gate["gate_revision"],
                ),
            )
            if cursor.rowcount != 1:
                raise JournalCaptureConflict("Journal cutover gate changed concurrently.")
            self._gate_transition(
                conn,
                revision=revision,
                kind="pause",
                from_state="open",
                to_state="paused",
                cohort_id=cohort_id,
                request_sha=request_sha,
                actor=actor,
                created_at=now,
                capture_count=capture_count,
                capture_high_water=capture_high_water,
                entry_count=entry_count,
                entry_high_water=entry_high_water,
            )
            self._record_mutation(
                conn,
                client_mutation_id,
                request_sha,
                {
                    "state": "paused",
                    "gateRevision": revision,
                    "cohortId": cohort_id,
                    "captureRowCount": capture_count,
                    "captureRowHighWater": capture_high_water,
                    "entryRowCount": entry_count,
                    "entryRowHighWater": entry_high_water,
                },
                now,
            )
            return self._state(conn)

    def resume_legacy_ingress(
        self,
        *,
        cohort_id: str,
        client_mutation_id: str,
        actor: Mapping[str, Any],
    ) -> JournalAuthorityState:
        """Release a pre-seal pause after an aborted/rejected cohort."""

        actor_sha = _sha({"actor": dict(actor)})
        request_sha = _sha(
            {
                "schema": "wb.journal-cutover-gate/v1",
                "operation": "resume_legacy_ingress",
                "cohortId": cohort_id,
                "actorSha256": actor_sha,
            }
        )
        now = _now()
        with self.store.transaction() as conn:
            if self._mutation_replay(conn, client_mutation_id, request_sha):
                return self._state(conn)
            control = self._control(conn)
            if str(control["mode"]) != "legacy_compatibility":
                raise JournalAuthorityStateError(
                    "legacy ingress can resume only under compatibility authority"
                )
            gate = self._cutover_gate(conn)
            if str(gate["state"]) != "paused" or str(gate["cohort_id"]) != cohort_id:
                raise JournalAuthorityStateError(
                    "the requested cohort does not own the cutover ingress pause"
                )
            cohort = conn.execute(
                "SELECT state FROM journal_import_cohorts WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if cohort is None or str(cohort["state"]) == "sealed":
                raise JournalAuthorityStateError(
                    "sealed Journal imports cannot reopen legacy ingress"
                )
            self._assert_cutover_high_water(conn, gate)
            try:
                resume_preseal_maintenance(
                    conn,
                    domain="journal",
                    cohort_id=cohort_id,
                    mutation_id=client_mutation_id,
                    actor_sha256=actor_sha,
                    at=now,
                )
            except CutoverMaintenanceError as exc:
                raise JournalAuthorityStateError(str(exc)) from exc
            revision = int(gate["gate_revision"]) + 1
            self._open_cutover_gate(conn, revision=revision, now=now)
            self._gate_transition(
                conn,
                revision=revision,
                kind="resume",
                from_state="paused",
                to_state="open",
                cohort_id=cohort_id,
                request_sha=request_sha,
                actor=actor,
                created_at=now,
            )
            self._record_mutation(
                conn,
                client_mutation_id,
                request_sha,
                {"state": "open", "gateRevision": revision},
                now,
            )
            return self._state(conn)

    def activate_database_only(
        self,
        *,
        cohort_id: str,
        client_mutation_id: str,
        actor: Mapping[str, Any],
    ) -> JournalAuthorityState:
        actor_sha = _sha({"actor": dict(actor)})
        request_sha = _sha(
            {
                "schema": "wb.journal-authority-cutover/v1",
                "operation": "activate_database_only",
                "cohortId": cohort_id,
                "actorSha256": actor_sha,
            }
        )
        now = _now()
        with self.store.transaction() as conn:
            if self._mutation_replay(conn, client_mutation_id, request_sha):
                state = self._state(conn)
                prepare_domain_seal(
                    "journal", self.store.path, cohort_id=cohort_id
                )
                confirm_domain_seal(
                    "journal", self.store.path, cohort_id=cohort_id
                )
                return state
            control = self._control(conn)
            if str(control["mode"]) != "legacy_compatibility":
                raise JournalAuthorityStateError(
                    "database-only authority can be activated only from compatibility mode"
                )
            gate = self._cutover_gate(conn)
            if (
                str(gate["state"]) != "paused"
                or str(gate["cohort_id"]) != cohort_id
            ):
                raise JournalAuthorityStateError(
                    "database-only authority requires the cohort's durable ingress pause"
                )
            self._assert_cutover_high_water(conn, gate)
            cohort = conn.execute(
                "SELECT state,inventory_sha256 FROM journal_import_cohorts "
                "WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            verified = conn.execute(
                "SELECT 1 FROM journal_import_state_transitions "
                "WHERE cohort_id=? AND to_state='verified'",
                (cohort_id,),
            ).fetchone()
            if cohort is None or str(cohort["state"]) != "sealed" or verified is None:
                raise JournalAuthorityStateError(
                    "database-only authority requires a verified and sealed import cohort"
                )
            unacknowledged_source = conn.execute(
                "SELECT 1 FROM journal_import_files WHERE cohort_id=? "
                "AND source_usage_state!='acknowledged' LIMIT 1",
                (cohort_id,),
            ).fetchone()
            if unacknowledged_source is not None:
                raise JournalAuthorityStateError(
                    "database-only authority requires acknowledged import Source dependencies"
                )
            unfinished = self._unfinished_materializations(conn)
            if unfinished:
                raise JournalAuthorityStateError(
                    "legacy Journal materialization must be quiet before authority cutover"
                )
            inventory_sha = str(cohort["inventory_sha256"])
            try:
                mark_postseal_pending(
                    conn,
                    domain="journal",
                    cohort_id=cohort_id,
                    inventory_sha256=inventory_sha,
                    at=now,
                )
            except CutoverMaintenanceError as exc:
                raise JournalAuthorityStateError(str(exc)) from exc
            revision = int(control["authority_revision"]) + 1
            prepare_domain_seal(
                "journal", self.store.path, cohort_id=cohort_id
            )
            cursor = conn.execute(
                "UPDATE journal_authority_control SET mode='database_only',"
                "authority_revision=?,activated_cohort_id=?,updated_at=? "
                "WHERE singleton=1 AND mode='legacy_compatibility' AND authority_revision=?",
                (revision, cohort_id, now, control["authority_revision"]),
            )
            if cursor.rowcount != 1:
                raise JournalCaptureConflict("Journal authority changed concurrently.")
            gate_revision = int(gate["gate_revision"]) + 1
            gate_cursor = conn.execute(
                "UPDATE journal_cutover_gate SET gate_revision=?,request_sha256=?,"
                "updated_at=? WHERE singleton=1 AND state='paused' "
                "AND gate_revision=? AND cohort_id=?",
                (
                    gate_revision,
                    request_sha,
                    now,
                    gate["gate_revision"],
                    cohort_id,
                ),
            )
            if gate_cursor.rowcount != 1:
                raise JournalCaptureConflict("Journal cutover gate changed concurrently.")
            self._gate_transition(
                conn,
                revision=gate_revision,
                kind="activate",
                from_state="paused",
                to_state="paused",
                cohort_id=cohort_id,
                request_sha=request_sha,
                actor=actor,
                created_at=now,
                capture_count=int(gate["capture_row_count"]),
                capture_high_water=int(gate["capture_row_high_water"]),
                entry_count=int(gate["entry_row_count"]),
                entry_high_water=int(gate["entry_row_high_water"]),
            )
            self._set_domain_mode(conn, "database_only", now)
            self._transition(
                conn,
                revision=revision,
                kind="activate",
                from_mode="legacy_compatibility",
                to_mode="database_only",
                request_sha=request_sha,
                actor=actor,
                created_at=now,
                cohort_id=cohort_id,
            )
            self._record_mutation(
                conn,
                client_mutation_id,
                request_sha,
                {
                    "mode": "database_only",
                    "authorityRevision": revision,
                    "gateRevision": gate_revision,
                },
                now,
            )
            state = self._state(conn)
        confirm_domain_seal("journal", self.store.path, cohort_id=cohort_id)
        return state

    def bind_postseal_source_drain(
        self,
        *,
        sources: SourceStore,
        cohort_id: str,
        client_mutation_id: str,
        actor: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Freeze the exact Source commands one privileged drain may consume.

        The Sources writer barrier is released before dispatch begins.  This is
        intentional: dispatch leases and acknowledges through separate Sources
        connections, so retaining the barrier would self-deadlock SQLite.
        """

        actor_sha = _sha({"actor": dict(actor)})
        source_path_sha = _path_sha(sources.paths.db)
        now = _now()
        with sources.write_transaction() as source_conn:
            with self.store.transaction() as conn:
                prior = conn.execute(
                    "SELECT * FROM journal_cutover_source_drain_batches "
                    "WHERE mutation_id=?",
                    (client_mutation_id,),
                ).fetchone()
                if prior is not None:
                    if (
                        str(prior["cohort_id"]) != cohort_id
                        or str(prior["actor_sha256"]) != actor_sha
                        or str(prior["source_authority_id"]) != sources.authority_id
                        or str(prior["source_db_path_sha256"]) != source_path_sha
                    ):
                        raise JournalCaptureConflict(
                            "That Journal drain key was used for another request."
                        )
                    return self._source_drain_result(conn, prior)

                control, gate, maintenance = self._postseal_drain_state(
                    conn, cohort_id=cohort_id
                )
                del control, maintenance
                unfinished = conn.execute(
                    "SELECT batch.mutation_id FROM journal_cutover_source_drain_batches AS batch "
                    "LEFT JOIN journal_cutover_source_drain_receipts AS receipt "
                    "ON receipt.batch_mutation_id=batch.mutation_id "
                    "WHERE batch.cohort_id=? AND receipt.batch_mutation_id IS NULL LIMIT 1",
                    (cohort_id,),
                ).fetchone()
                if unfinished is not None:
                    raise JournalAuthorityStateError(
                        "The prior Journal Source drain must finish before another is bound."
                    )
                projection = self._controlled_drain_projection(
                    conn, gate=gate, cohort_id=cohort_id
                )
                source = self._source_effect_snapshot(source_conn)
                previous = conn.execute(
                    "SELECT receipt.result_sha256 FROM journal_cutover_source_drain_receipts AS receipt "
                    "JOIN journal_cutover_source_drain_batches AS batch "
                    "ON batch.mutation_id=receipt.batch_mutation_id "
                    "WHERE batch.cohort_id=? ORDER BY batch.source_effect_max_rowid DESC,"
                    "batch.created_at DESC LIMIT 1",
                    (cohort_id,),
                ).fetchone()
                request = {
                    "schema": "wb.journal-postseal-source-drain/v1",
                    "operation": "bind",
                    "cohortId": cohort_id,
                    "actorSha256": actor_sha,
                    "sourceAuthorityId": sources.authority_id,
                    "sourceDbPathSha256": source_path_sha,
                    "sourceEffectCount": source["effectCount"],
                    "sourceEffectMaxRowid": source["effectMaxRowid"],
                    "sourceBaselineSetSha256": source["baselineSetSha256"],
                    "boundEffectSetSha256": source["unresolvedSetSha256"],
                    "baselineCaptureCount": projection["captureCount"],
                    "baselineCaptureMaxRowid": projection["captureMaxRowid"],
                    "previousDrainResultSha256": (
                        None if previous is None else str(previous[0])
                    ),
                }
                request_sha = _sha(request)
                conn.execute(
                    "INSERT INTO journal_cutover_source_drain_batches("
                    "mutation_id,request_sha256,cohort_id,actor_sha256,"
                    "source_authority_id,source_db_path_sha256,source_effect_count,"
                    "source_effect_max_rowid,source_baseline_set_sha256,"
                    "bound_effect_count,bound_effect_set_sha256,baseline_capture_count,"
                    "baseline_capture_max_rowid,previous_drain_result_sha256,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        client_mutation_id,
                        request_sha,
                        cohort_id,
                        actor_sha,
                        sources.authority_id,
                        source_path_sha,
                        source["effectCount"],
                        source["effectMaxRowid"],
                        source["baselineSetSha256"],
                        len(source["unresolved"]),
                        source["unresolvedSetSha256"],
                        projection["captureCount"],
                        projection["captureMaxRowid"],
                        None if previous is None else str(previous[0]),
                        now,
                    ),
                )
                for ordinal, effect in enumerate(source["unresolved"]):
                    conn.execute(
                        "INSERT INTO journal_cutover_source_drain_effects("
                        "batch_mutation_id,ordinal,source_rowid,effect_id,payload_sha256) "
                        "VALUES(?,?,?,?,?)",
                        (
                            client_mutation_id,
                            ordinal,
                            effect["rowId"],
                            effect["effectId"],
                            effect["payloadSha256"],
                        ),
                    )
                row = conn.execute(
                    "SELECT * FROM journal_cutover_source_drain_batches WHERE mutation_id=?",
                    (client_mutation_id,),
                ).fetchone()
                assert row is not None
                return self._source_drain_result(conn, row)

    def source_drain_effect_ids(
        self,
        *,
        cohort_id: str,
        client_mutation_id: str,
    ) -> tuple[str, ...]:
        """Return only the immutable effect IDs bound to one active drain."""

        with self.store._connect() as conn:
            self._postseal_drain_state(conn, cohort_id=cohort_id)
            batch = conn.execute(
                "SELECT cohort_id FROM journal_cutover_source_drain_batches "
                "WHERE mutation_id=?",
                (client_mutation_id,),
            ).fetchone()
            if batch is None or str(batch["cohort_id"]) != cohort_id:
                raise JournalAuthorityStateError("Journal Source drain is unavailable.")
            rows = conn.execute(
                "SELECT effect_id FROM journal_cutover_source_drain_effects "
                "WHERE batch_mutation_id=? ORDER BY ordinal",
                (client_mutation_id,),
            ).fetchall()
            return tuple(str(row[0]) for row in rows)

    def finalize_postseal_source_drain(
        self,
        *,
        sources: SourceStore,
        cohort_id: str,
        client_mutation_id: str,
        actor: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Certify exact Journal rows and Source acknowledgements for a batch."""

        actor_sha = _sha({"actor": dict(actor)})
        source_path_sha = _path_sha(sources.paths.db)
        with sources.write_transaction() as source_conn:
            with self.store.transaction() as conn:
                _control, gate, _maintenance = self._postseal_drain_state(
                    conn, cohort_id=cohort_id
                )
                batch = conn.execute(
                    "SELECT * FROM journal_cutover_source_drain_batches "
                    "WHERE mutation_id=?",
                    (client_mutation_id,),
                ).fetchone()
                if batch is None:
                    raise JournalAuthorityStateError("Journal Source drain is unavailable.")
                if (
                    str(batch["cohort_id"]) != cohort_id
                    or str(batch["actor_sha256"]) != actor_sha
                    or str(batch["source_authority_id"]) != sources.authority_id
                    or str(batch["source_db_path_sha256"]) != source_path_sha
                ):
                    raise JournalCaptureConflict(
                        "That Journal drain key was used for another request."
                    )
                prior = conn.execute(
                    "SELECT * FROM journal_cutover_source_drain_receipts "
                    "WHERE batch_mutation_id=?",
                    (client_mutation_id,),
                ).fetchone()
                if prior is not None:
                    result_json = str(prior["result_json"])
                    if _result_sha(result_json) != str(prior["result_sha256"]):
                        raise JournalAuthorityStateError(
                            "The Journal Source drain receipt changed after commit."
                        )
                    value = json.loads(result_json)
                    if not isinstance(value, dict):
                        raise JournalAuthorityStateError(
                            "The Journal Source drain receipt is invalid."
                        )
                    return value

                source = self._source_effect_snapshot(
                    source_conn,
                    max_rowid=int(batch["source_effect_max_rowid"]),
                )
                if (
                    source["effectCount"] != int(batch["source_effect_count"])
                    or source["baselineSetSha256"]
                    != str(batch["source_baseline_set_sha256"])
                ):
                    raise JournalAuthorityStateError(
                        "The bound Journal Source baseline changed during drain."
                    )
                bound = conn.execute(
                    "SELECT * FROM journal_cutover_source_drain_effects "
                    "WHERE batch_mutation_id=? ORDER BY ordinal",
                    (client_mutation_id,),
                ).fetchall()
                acknowledgements: list[dict[str, Any]] = []
                captures: list[dict[str, Any]] = []
                for effect in bound:
                    source_row = next(
                        (
                            row
                            for row in source["rows"]
                            if row["effectId"] == str(effect["effect_id"])
                        ),
                        None,
                    )
                    if (
                        source_row is None
                        or source_row["rowId"] != int(effect["source_rowid"])
                        or source_row["payloadSha256"]
                        != str(effect["payload_sha256"])
                        or source_row["status"] != "succeeded"
                        or source_row["receiptId"] is None
                    ):
                        raise JournalAuthorityStateError(
                            "The bound Journal Source drain is not fully acknowledged."
                        )
                    capture = conn.execute(
                        "SELECT receipt.*,capture.source_effect_id,capture.request_sha256 "
                        "FROM journal_cutover_source_drain_captures AS receipt "
                        "JOIN journal_captures AS capture ON capture.capture_id=receipt.capture_id "
                        "WHERE receipt.batch_mutation_id=? AND receipt.effect_id=?",
                        (client_mutation_id, effect["effect_id"]),
                    ).fetchone()
                    expected_result = (
                        None
                        if capture is None
                        else f"journal-capture:{capture['capture_id']}"
                    )
                    if (
                        capture is None
                        or str(capture["source_effect_id"]) != str(effect["effect_id"])
                        or str(capture["request_sha256"])
                        != str(capture["capture_request_sha256"])
                        or source_row["resultRef"] != expected_result
                    ):
                        raise JournalAuthorityStateError(
                            "The bound Journal Source result does not match its capture."
                        )
                    acknowledgements.append(
                        {
                            "effectId": str(effect["effect_id"]),
                            "receiptId": source_row["receiptId"],
                            "resultSha256": source_row["resultSha256"],
                        }
                    )
                    captures.append(
                        {
                            "effectId": str(effect["effect_id"]),
                            "captureId": str(capture["capture_id"]),
                            "captureRowid": int(capture["capture_rowid"]),
                            "requestSha256": str(capture["capture_request_sha256"]),
                        }
                    )
                if source["unresolved"]:
                    raise JournalAuthorityStateError(
                        "Journal Source effects remain unresolved through the bound high-water."
                    )
                projection = self._controlled_drain_projection(
                    conn,
                    gate=gate,
                    cohort_id=cohort_id,
                    allow_unfinalized_batch_id=client_mutation_id,
                )
                result = {
                    "schema": "wb.journal-postseal-source-drain-result/v1",
                    "cohortId": cohort_id,
                    "mutationId": client_mutation_id,
                    "sourceEffectCount": int(batch["source_effect_count"]),
                    "sourceEffectMaxRowid": int(batch["source_effect_max_rowid"]),
                    "sourceEffectSetSha256": str(batch["source_baseline_set_sha256"]),
                    "boundEffectSetSha256": str(batch["bound_effect_set_sha256"]),
                    "sourceAckSetSha256": _sha(acknowledgements),
                    "captureSetSha256": _sha(captures),
                    "controlledDeltaSha256": projection["controlledDeltaSha256"],
                    "postCaptureCount": projection["captureCount"],
                    "postCaptureMaxRowid": projection["captureMaxRowid"],
                    "postEntryCount": projection["entryCount"],
                    "postEntryMaxRowid": projection["entryMaxRowid"],
                    "completedAt": _now(),
                }
                result_json = _canonical(result)
                conn.execute(
                    "INSERT INTO journal_cutover_source_drain_receipts("
                    "batch_mutation_id,request_sha256,source_ack_set_sha256,"
                    "capture_set_sha256,post_capture_count,post_capture_max_rowid,"
                    "post_entry_count,post_entry_max_rowid,result_json,result_sha256,"
                    "completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        client_mutation_id,
                        str(batch["request_sha256"]),
                        result["sourceAckSetSha256"],
                        result["captureSetSha256"],
                        result["postCaptureCount"],
                        result["postCaptureMaxRowid"],
                        result["postEntryCount"],
                        result["postEntryMaxRowid"],
                        result_json,
                        _result_sha(result_json),
                        result["completedAt"],
                    ),
                )
                return result

    def release_postseal_ingress(
        self,
        *,
        cohort_id: str,
        client_mutation_id: str,
        actor: Mapping[str, Any],
        source_drain_mutation_id: str,
        sources: SourceStore | None = None,
        checkpoint_evidence_path: str | Path | None = None,
        search_evidence_path: str | Path | None = None,
        detachment_evidence_path: str | Path | None = None,
        rehearsal_evidence_sha256s: Mapping[str, str] | None = None,
        allow_unvalidated_rehearsal: bool = False,
        rehearsal_authorization: IsolatedRehearsalAuthorization | None = None,
    ) -> JournalAuthorityState:
        """Open native ingress after the held authority passes release evidence.

        Configured releases accept receipt paths and re-certify live state.
        Digest-only evidence is restricted to an explicitly acknowledged
        database under the operating system's temporary directory.
        """

        actor_sha = _sha({"actor": dict(actor)})
        if sources is None:
            from work_buddy.paths import resolve

            sources = SourceStore.open(resolve("stores/sources"))
        source_path_sha = _path_sha(sources.paths.db)
        configured_paths = {
            "databaseCheckpoint": checkpoint_evidence_path,
            "search": search_evidence_path,
            "detachment": detachment_evidence_path,
        }
        if allow_unvalidated_rehearsal:
            require_isolated_rehearsal_path(
                self.store.path,
                domain="journal",
                authorization=rehearsal_authorization,
            )
            if any(value is not None for value in configured_paths.values()):
                raise JournalAuthorityStateError(
                    "Journal rehearsal release cannot mix receipt paths and digests"
                )
            if rehearsal_evidence_sha256s is None or set(
                rehearsal_evidence_sha256s
            ) != {"databaseCheckpoint", "search", "detachment"}:
                raise JournalAuthorityStateError(
                    "Journal rehearsal release evidence is incomplete"
                )
            path_sha256s: dict[str, str] | None = None
        else:
            if (
                rehearsal_evidence_sha256s is not None
                or rehearsal_authorization is not None
                or any(value is None for value in configured_paths.values())
            ):
                raise JournalAuthorityStateError(
                    "Journal configured postseal evidence receipt paths are required"
                )
            path_sha256s = {
                key: _path_sha(value)  # type: ignore[arg-type]
                for key, value in configured_paths.items()
            }

        now = _now()
        with sources.write_transaction() as source_conn, self.store.transaction() as conn:
            prior = conn.execute(
                "SELECT * FROM journal_cutover_release_receipts WHERE mutation_id=?",
                (client_mutation_id,),
            ).fetchone()
            if prior is not None:
                replay_drain = conn.execute(
                    "SELECT source_authority_id,source_db_path_sha256 "
                    "FROM journal_cutover_source_drain_batches WHERE mutation_id=?",
                    (source_drain_mutation_id,),
                ).fetchone()
                if (
                    str(prior["cohort_id"]) != cohort_id
                    or str(prior["actor_sha256"]) != actor_sha
                    or str(prior["source_drain_mutation_id"] or "")
                    != source_drain_mutation_id
                    or replay_drain is None
                    or str(replay_drain["source_authority_id"])
                    != sources.authority_id
                    or str(replay_drain["source_db_path_sha256"])
                    != source_path_sha
                ):
                    raise JournalCaptureConflict(
                        "That Journal release key was used for another request."
                    )
                stored_paths = (
                    None
                    if prior["checkpoint_path_sha256"] is None
                    else {
                        "databaseCheckpoint": str(
                            prior["checkpoint_path_sha256"]
                        ),
                        "search": str(prior["search_path_sha256"]),
                        "detachment": str(prior["detachment_path_sha256"]),
                    }
                )
                if stored_paths != path_sha256s:
                    raise JournalCaptureConflict(
                        "That Journal release key was used with different evidence paths."
                    )
                stored_evidence = {
                    "databaseCheckpoint": str(
                        prior["database_checkpoint_sha256"]
                    ),
                    "search": str(prior["search_sha256"]),
                    "detachment": str(prior["detachment_sha256"]),
                    "authorityHead": str(prior["authority_head_sha256"]),
                }
                if allow_unvalidated_rehearsal:
                    supplied = dict(rehearsal_evidence_sha256s or {})
                else:
                    from work_buddy.cutover_release import (
                        hash_supplied_postseal_evidence,
                    )

                    supplied = hash_supplied_postseal_evidence(
                        checkpoint_evidence_path=checkpoint_evidence_path,  # type: ignore[arg-type]
                        search_evidence_path=search_evidence_path,  # type: ignore[arg-type]
                        detachment_evidence_path=detachment_evidence_path,  # type: ignore[arg-type]
                    )
                if any(
                    supplied.get(key) != stored_evidence[key]
                    for key in ("databaseCheckpoint", "search", "detachment")
                ):
                    raise JournalCaptureConflict(
                        "That Journal release key was used with different evidence."
                    )
                request = self._postseal_release_request(
                    cohort_id=cohort_id,
                    inventory_sha256=str(prior["inventory_sha256"]),
                    actor_sha256=actor_sha,
                    evidence_sha256s=stored_evidence,
                    high_water_sha256=str(prior["high_water_sha256"]),
                    source_drain_mutation_id=source_drain_mutation_id,
                    source_drain_result_sha256=str(
                        prior["source_drain_result_sha256"]
                    ),
                    source_effect_set_sha256=str(
                        prior["source_effect_set_sha256"]
                    ),
                    source_effect_max_rowid=int(prior["source_effect_max_rowid"]),
                    evidence_path_sha256s=path_sha256s,
                    rehearsal=allow_unvalidated_rehearsal,
                )
                if _sha(request) != str(prior["request_sha256"]):
                    raise JournalCaptureConflict(
                        "That Journal release receipt does not match its request."
                    )
                result_json = str(prior["result_json"])
                if _result_sha(result_json) != str(prior["result_sha256"]):
                    raise JournalAuthorityStateError(
                        "The Journal release receipt changed after commit."
                    )
                try:
                    release_postseal_maintenance(
                        conn,
                        domain="journal",
                        cohort_id=cohort_id,
                        mutation_id=client_mutation_id,
                        actor_sha256=actor_sha,
                        evidence_sha256s=stored_evidence,
                    )
                except CutoverMaintenanceError as exc:
                    raise JournalAuthorityStateError(str(exc)) from exc
                mark_domain_released(
                    "journal", self.store.path, cohort_id=cohort_id
                )
                return self._state(conn)

            control = self._control(conn)
            if (
                str(control["mode"]) != "database_only"
                or str(control["activated_cohort_id"] or "") != cohort_id
            ):
                raise JournalAuthorityStateError(
                    "Journal postseal maintenance is unavailable"
                )
            gate = self._cutover_gate(conn)
            maintenance = self._maintenance(conn)
            if (
                str(gate["state"]) != "paused"
                or str(gate["cohort_id"] or "") != cohort_id
                or str(maintenance["state"]) != "postseal_pending"
                or str(maintenance["cohort_id"] or "") != cohort_id
            ):
                raise JournalAuthorityStateError(
                    "Journal postseal maintenance is not releasable"
                )
            cohort = conn.execute(
                "SELECT state,inventory_sha256 FROM journal_import_cohorts "
                "WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if (
                cohort is None
                or str(cohort["state"]) != "sealed"
                or str(cohort["inventory_sha256"])
                != str(maintenance["inventory_sha256"])
            ):
                raise JournalAuthorityStateError(
                    "Journal release inventory does not match the sealed cohort"
                )
            drain = self._validate_release_source_drain(
                conn,
                source_conn=source_conn,
                sources=sources,
                source_path_sha256=source_path_sha,
                cohort_id=cohort_id,
                source_drain_mutation_id=source_drain_mutation_id,
                gate=gate,
            )
            high_water = self._cutover_high_water(gate)
            high_water_sha = _sha(high_water)
            prior_evidence = prior_postseal_release_evidence(
                conn, mutation_id=client_mutation_id
            )
            if prior_evidence is not None:
                raise JournalAuthorityStateError(
                    "Journal release receipts disagree across authority ledgers"
                )
            if allow_unvalidated_rehearsal:
                evidence = dict(rehearsal_evidence_sha256s or {})
                evidence["authorityHead"] = hashlib.sha256(
                    self.store.path.read_bytes()
                ).hexdigest()
            else:
                from work_buddy.cutover_release import (
                    validate_configured_postseal_evidence,
                )

                evidence = validate_configured_postseal_evidence(
                    domain="journal",
                    authority_db_path=self.store.path,
                    checkpoint_evidence_path=checkpoint_evidence_path,  # type: ignore[arg-type]
                    search_evidence_path=search_evidence_path,  # type: ignore[arg-type]
                    detachment_evidence_path=detachment_evidence_path,  # type: ignore[arg-type]
                )
            request = self._postseal_release_request(
                cohort_id=cohort_id,
                inventory_sha256=str(cohort["inventory_sha256"]),
                actor_sha256=actor_sha,
                evidence_sha256s=evidence,
                high_water_sha256=high_water_sha,
                source_drain_mutation_id=source_drain_mutation_id,
                source_drain_result_sha256=drain["resultSha256"],
                source_effect_set_sha256=drain["sourceEffectSetSha256"],
                source_effect_max_rowid=drain["sourceEffectMaxRowid"],
                evidence_path_sha256s=path_sha256s,
                rehearsal=allow_unvalidated_rehearsal,
            )
            request_sha = _sha(request)
            if self._mutation_replay(conn, client_mutation_id, request_sha):
                raise JournalAuthorityStateError(
                    "Journal release mutation is missing its release receipt"
                )
            try:
                released = release_postseal_maintenance(
                    conn,
                    domain="journal",
                    cohort_id=cohort_id,
                    mutation_id=client_mutation_id,
                    actor_sha256=actor_sha,
                    evidence_sha256s=evidence,
                    at=now,
                )
            except CutoverMaintenanceError as exc:
                raise JournalAuthorityStateError(str(exc)) from exc
            gate_revision = int(gate["gate_revision"]) + 1
            self._open_cutover_gate(conn, revision=gate_revision, now=now)
            self._gate_transition(
                conn,
                revision=gate_revision,
                kind="release",
                from_state="paused",
                to_state="open",
                cohort_id=cohort_id,
                request_sha=request_sha,
                actor=actor,
                created_at=now,
                capture_count=int(gate["capture_row_count"]),
                capture_high_water=int(gate["capture_row_high_water"]),
                entry_count=int(gate["entry_row_count"]),
                entry_high_water=int(gate["entry_row_high_water"]),
            )
            result = {
                "schema": "wb.journal-postseal-release/v1",
                "domain": "journal",
                "cohortId": cohort_id,
                "inventorySha256": str(cohort["inventory_sha256"]),
                "mutationId": client_mutation_id,
                "actorSha256": actor_sha,
                "state": "open",
                "postsealEvidenceSha256": str(
                    released["postsealEvidenceSha256"]
                ),
                "evidenceSha256s": dict(released["evidenceSha256s"]),
                "highWater": high_water,
                "highWaterSha256": high_water_sha,
                "controlledDeltaSha256": drain["controlledDeltaSha256"],
                "sourceDrainMutationId": source_drain_mutation_id,
                "sourceDrainResultSha256": drain["resultSha256"],
                "sourceEffectSetSha256": drain["sourceEffectSetSha256"],
                "sourceEffectMaxRowid": drain["sourceEffectMaxRowid"],
                "releasedAt": str(released["releasedAt"]),
            }
            result_json = _canonical(result)
            conn.execute(
                "INSERT INTO journal_cutover_release_receipts("
                "mutation_id,request_sha256,domain,cohort_id,inventory_sha256,"
                "actor_sha256,evidence_sha256,database_checkpoint_sha256,"
                "search_sha256,detachment_sha256,authority_head_sha256,"
                "high_water_sha256,checkpoint_path_sha256,search_path_sha256,"
                "detachment_path_sha256,released_at,result_json,result_sha256,"
                "created_at,source_drain_mutation_id,source_drain_result_sha256,"
                "source_effect_set_sha256,source_effect_max_rowid) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    client_mutation_id,
                    request_sha,
                    "journal",
                    cohort_id,
                    str(cohort["inventory_sha256"]),
                    actor_sha,
                    str(released["postsealEvidenceSha256"]),
                    evidence["databaseCheckpoint"],
                    evidence["search"],
                    evidence["detachment"],
                    evidence["authorityHead"],
                    high_water_sha,
                    None if path_sha256s is None else path_sha256s["databaseCheckpoint"],
                    None if path_sha256s is None else path_sha256s["search"],
                    None if path_sha256s is None else path_sha256s["detachment"],
                    str(released["releasedAt"]),
                    result_json,
                    _result_sha(result_json),
                    now,
                    source_drain_mutation_id,
                    drain["resultSha256"],
                    drain["sourceEffectSetSha256"],
                    drain["sourceEffectMaxRowid"],
                ),
            )
            self._record_mutation(
                conn,
                client_mutation_id,
                request_sha,
                result,
                now,
            )
            state = self._state(conn)
        mark_domain_released("journal", self.store.path, cohort_id=cohort_id)
        return state

    def rollback_to_legacy(
        self,
        *,
        client_mutation_id: str,
        actor: Mapping[str, Any],
    ) -> JournalAuthorityState:
        del client_mutation_id, actor
        with self.store.transaction() as conn:
            control = self._control(conn)
            if str(control["mode"]) != "database_only":
                raise JournalAuthorityStateError(
                    "Journal authority is not in database-only mode"
                )
            raise JournalAuthorityStateError(
                "sealed Journal authority is roll-forward only; release postseal "
                "maintenance after certifying native search"
            )

    def fence_recovery(
        self,
        *,
        fence_code: str,
        client_mutation_id: str,
        actor: Mapping[str, Any],
    ) -> JournalAuthorityState:
        if not fence_code or len(fence_code) > 128:
            raise JournalAuthorityStateError("a bounded recovery fence code is required")
        request_sha = _sha(
            {
                "schema": "wb.journal-authority-cutover/v1",
                "operation": "fence_recovery",
                "fenceCode": fence_code,
            }
        )
        now = _now()
        with self.store.transaction() as conn:
            if self._mutation_replay(conn, client_mutation_id, request_sha):
                return self._state(conn)
            control = self._control(conn)
            prior = str(control["mode"])
            if prior == "recovery_fenced":
                raise JournalAuthorityStateError("Journal authority is already fenced")
            revision = int(control["authority_revision"]) + 1
            conn.execute(
                "UPDATE journal_authority_control SET mode='recovery_fenced',"
                "authority_revision=?,prior_mode=?,fence_code=?,fenced_at=?,updated_at=? "
                "WHERE singleton=1",
                (revision, prior, fence_code, now, now),
            )
            self._set_domain_mode(conn, "recovery_fenced", now)
            self._transition(
                conn,
                revision=revision,
                kind="fence",
                from_mode=prior,
                to_mode="recovery_fenced",
                request_sha=request_sha,
                actor=actor,
                created_at=now,
            )
            self._record_mutation(
                conn,
                client_mutation_id,
                request_sha,
                {
                    "mode": "recovery_fenced",
                    "authorityRevision": revision,
                    "fenceCode": fence_code,
                },
                now,
            )
            return self._state(conn)

    def recover(
        self,
        *,
        client_mutation_id: str,
        actor: Mapping[str, Any],
    ) -> JournalAuthorityState:
        request_sha = _sha(
            {
                "schema": "wb.journal-authority-cutover/v1",
                "operation": "recover",
            }
        )
        now = _now()
        with self.store.transaction() as conn:
            if self._mutation_replay(conn, client_mutation_id, request_sha):
                return self._state(conn)
            control = self._control(conn)
            if str(control["mode"]) != "recovery_fenced" or control["prior_mode"] is None:
                raise JournalAuthorityStateError("Journal authority is not recovery-fenced")
            restored = str(control["prior_mode"])
            if str(control["fence_code"] or "") == (
                "pre_v15_import_source_dependencies_missing"
            ):
                unresolved = conn.execute(
                    "SELECT 1 FROM journal_import_files WHERE "
                    "source_usage_state!='acknowledged' LIMIT 1"
                ).fetchone()
                if unresolved is not None:
                    raise JournalAuthorityStateError(
                        "Journal recovery requires import Source dependency reconciliation"
                    )
            revision = int(control["authority_revision"]) + 1
            conn.execute(
                "UPDATE journal_authority_control SET mode=?,authority_revision=?,"
                "prior_mode=NULL,fence_code=NULL,fenced_at=NULL,updated_at=? "
                "WHERE singleton=1",
                (restored, revision, now),
            )
            self._set_domain_mode(conn, restored, now)
            self._transition(
                conn,
                revision=revision,
                kind="recover",
                from_mode="recovery_fenced",
                to_mode=restored,
                request_sha=request_sha,
                actor=actor,
                created_at=now,
            )
            self._record_mutation(
                conn,
                client_mutation_id,
                request_sha,
                {"mode": restored, "authorityRevision": revision},
                now,
            )
            return self._state(conn)

    def materialize_native_capture(
        self,
        capture: JournalCapture,
        *,
        exact_text: str,
        target: CaptureTarget,
        postseal_drain_batch_id: str | None = None,
    ) -> str:
        if target is CaptureTarget.AUTO:
            raise JournalAuthorityStateError("automatic routing must settle before publication")
        content_sha = _sha(exact_text)
        item_id = "ji_" + _sha(
            {
                "schema": "wb.journal-native-capture-item-id/v1",
                "captureId": capture.capture_id,
            }
        )[:32]
        request_sha = _sha(
            {
                "schema": "wb.journal-native-capture/v1",
                "captureId": capture.capture_id,
                "target": target.value,
                "contentSha256": content_sha,
                "sourceRef": capture.source_ref,
            }
        )
        now = _now()
        with self.store.transaction() as conn:
            control = self._control(conn)
            if str(control["mode"]) == "recovery_fenced":
                raise JournalAuthorityFenced()
            if str(control["mode"]) != "database_only":
                raise JournalAuthorityStateError(
                    "native capture publication requires database-only authority"
                )
            gate = self._cutover_gate(conn)
            maintenance = self._maintenance(conn)
            if postseal_drain_batch_id is None:
                if (
                    str(gate["state"]) != "open"
                    or str(maintenance["state"]) != "open"
                ):
                    raise JournalCutoverPaused()
            else:
                admitted = conn.execute(
                    "SELECT batch.cohort_id FROM journal_cutover_source_drain_effects AS effect "
                    "JOIN journal_cutover_source_drain_batches AS batch "
                    "ON batch.mutation_id=effect.batch_mutation_id "
                    "LEFT JOIN journal_cutover_source_drain_receipts AS receipt "
                    "ON receipt.batch_mutation_id=batch.mutation_id "
                    "WHERE effect.batch_mutation_id=? AND effect.effect_id=? "
                    "AND receipt.batch_mutation_id IS NULL",
                    (postseal_drain_batch_id, capture.source_effect_id),
                ).fetchone()
                if (
                    admitted is None
                    or str(control["activated_cohort_id"] or "")
                    != str(admitted["cohort_id"])
                    or str(gate["state"]) != "paused"
                    or str(gate["cohort_id"] or "")
                    != str(admitted["cohort_id"])
                    or str(maintenance["state"]) != "postseal_pending"
                    or str(maintenance["cohort_id"] or "")
                    != str(admitted["cohort_id"])
                ):
                    raise JournalCutoverPaused()
            bound = conn.execute(
                "SELECT * FROM journal_native_capture_bindings WHERE capture_id=?",
                (capture.capture_id,),
            ).fetchone()
            if bound is not None:
                item = conn.execute(
                    "SELECT * FROM journal_items WHERE item_id=?", (bound["item_id"],)
                ).fetchone()
                if (
                    str(bound["request_sha256"]) != request_sha
                    or item is None
                    or str(item["current_content_sha256"]) != content_sha
                    or str(bound["target"]) != target.value
                ):
                    raise JournalCaptureConflict(
                        "The Journal capture is already bound to different native content."
                    )
                return str(bound["item_id"])
            capture_row = conn.execute(
                "SELECT * FROM journal_captures WHERE capture_id=?",
                (capture.capture_id,),
            ).fetchone()
            if capture_row is None:
                raise JournalAuthorityStateError("the Journal capture is unavailable")
            if capture_row["entry_id"] is not None:
                raise JournalAuthorityStateError(
                    "a legacy-materialized capture cannot become native-only"
                )
            effect = conn.execute(
                "SELECT * FROM journal_effects WHERE capture_id=? AND effect_type='materialize'",
                (capture.capture_id,),
            ).fetchone()
            if effect is None:
                raise JournalAuthorityStateError("the materialization effect is unavailable")
            item_kind = "record" if target is CaptureTarget.LOG else "running_note"
            module_id = "simple.stream" if target is CaptureTarget.LOG else "simple.notes"
            created_at = capture.stated_at or capture.submitted_at
            actor_json = _canonical(
                {
                    "kind": "journal_capture_materializer",
                    "captureId": capture.capture_id,
                    "sourceRef": capture.source_ref,
                }
            )
            conn.execute(
                """
                INSERT INTO journal_items(
                    item_id,local_date,module_instance_id,module_instance_version,
                    item_kind,authority_kind,current_plain_value,current_content_sha256,
                    interaction_behavior_id,interaction_behavior_version,privacy_class,
                    search_mode,source_ref,lifecycle,current_revision,created_at,updated_at
                ) VALUES(?,?,?,?,?,'native_plain',?,?,'human_value',1,'private',
                    'lexical_dense',?,'current',1,?,?)
                """,
                (
                    item_id,
                    capture.day_id,
                    module_id,
                    1,
                    item_kind,
                    exact_text,
                    content_sha,
                    capture.source_ref,
                    created_at,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO journal_item_revisions(
                    item_id,revision,authority_kind,plain_value,content_sha256,lifecycle,
                    actor_json,source_ref,authorship,review_state,intent_id,created_at
                ) VALUES(?,1,'native_plain',?,?,'current',?,?,'unknown','unknown',?,?)
                """,
                (
                    item_id,
                    exact_text,
                    content_sha,
                    actor_json,
                    capture.source_ref,
                    f"journal-capture:{capture.capture_id}",
                    now,
                ),
            )
            first_write = control["first_native_capture_id"] is None
            authority_revision = int(control["authority_revision"]) + int(first_write)
            conn.execute(
                "INSERT INTO journal_native_capture_bindings("
                "capture_id,item_id,target,request_sha256,authority_revision,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (
                    capture.capture_id,
                    item_id,
                    target.value,
                    request_sha,
                    authority_revision,
                    now,
                ),
            )
            conn.execute(
                "UPDATE journal_captures SET resolved_target=?,revision=revision+1,updated_at=? "
                "WHERE capture_id=?",
                (target.value, now, capture.capture_id),
            )
            composition = conn.execute(
                """
                SELECT s.composition_digest FROM journal_day_composition_snapshots AS s
                JOIN journal_days AS d ON d.day_id=s.day_id WHERE d.local_date=?
                """,
                (capture.day_id,),
            ).fetchone()
            event_id = "jso_" + _sha(
                {
                    "aggregate_type": "item",
                    "aggregate_id": item_id,
                    "aggregate_revision": "1",
                    "event_kind": "upsert",
                }
            )[:32]
            conn.execute(
                """
                INSERT INTO journal_search_outbox(
                    event_id,aggregate_type,aggregate_id,aggregate_revision,event_kind,
                    content_sha256,composition_digest,search_recipe_version,
                    privacy_class,committed_at
                ) VALUES(?,'item',?,'1','upsert',?,?,1,'private',?)
                """,
                (
                    event_id,
                    item_id,
                    content_sha,
                    composition[0] if composition is not None else None,
                    now,
                ),
            )
            conn.execute(
                "UPDATE journal_effects SET state='succeeded',error_code=NULL,"
                "result_json=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE effect_id=?",
                (
                    _canonical(
                        {
                            "nativeItemId": item_id,
                            "authorityRevision": authority_revision,
                        }
                    ),
                    now,
                    effect["effect_id"],
                ),
            )
            if first_write:
                cursor = conn.execute(
                    "UPDATE journal_authority_control SET authority_revision=?,"
                    "first_native_capture_id=?,first_native_item_id=?,"
                    "first_native_write_at=?,updated_at=? "
                    "WHERE singleton=1 AND mode='database_only' "
                    "AND first_native_capture_id IS NULL AND authority_revision=?",
                    (
                        authority_revision,
                        capture.capture_id,
                        item_id,
                        now,
                        now,
                        control["authority_revision"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise JournalCaptureConflict("Journal authority changed concurrently.")
                self._transition(
                    conn,
                    revision=authority_revision,
                    kind="first_native_write",
                    from_mode="database_only",
                    to_mode="database_only",
                    request_sha=request_sha,
                    actor=json.loads(actor_json),
                    created_at=now,
                    capture_id=capture.capture_id,
                    item_id=item_id,
                )
            return item_id

    def native_item_for_capture(self, capture_id: str) -> str | None:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT item_id FROM journal_native_capture_bindings WHERE capture_id=?",
                (capture_id,),
            ).fetchone()
        return None if row is None else str(row["item_id"])

    @staticmethod
    def _control(conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM journal_authority_control WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise JournalAuthorityStateError("Journal authority control is unavailable")
        return row

    @classmethod
    def _state(cls, conn: sqlite3.Connection) -> JournalAuthorityState:
        row = cls._control(conn)
        gate = cls._cutover_gate(conn)
        maintenance = cls._maintenance(conn)
        return JournalAuthorityState(
            mode=str(row["mode"]),
            authority_revision=int(row["authority_revision"]),
            activated_cohort_id=row["activated_cohort_id"],
            prior_mode=row["prior_mode"],
            first_native_capture_id=row["first_native_capture_id"],
            first_native_item_id=row["first_native_item_id"],
            first_native_write_at=row["first_native_write_at"],
            fence_code=row["fence_code"],
            fenced_at=row["fenced_at"],
            unfinished_materializations=cls._unfinished_materializations(conn),
            cutover_gate_state=str(maintenance["state"]),
            cutover_gate_revision=int(gate["gate_revision"]),
            cutover_cohort_id=maintenance["cohort_id"],
            cutover_inventory_sha256=maintenance["inventory_sha256"],
            cutover_paused_at=maintenance["paused_at"],
            cutover_released_at=maintenance["released_at"],
            cutover_evidence_sha256=maintenance["postseal_evidence_sha256"],
            capture_row_count=(
                None
                if gate["capture_row_count"] is None
                else int(gate["capture_row_count"])
            ),
            capture_row_high_water=(
                None
                if gate["capture_row_high_water"] is None
                else int(gate["capture_row_high_water"])
            ),
            entry_row_count=(
                None
                if gate["entry_row_count"] is None
                else int(gate["entry_row_count"])
            ),
            entry_row_high_water=(
                None
                if gate["entry_row_high_water"] is None
                else int(gate["entry_row_high_water"])
            ),
        )

    @staticmethod
    def _cutover_gate(conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM journal_cutover_gate WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise JournalAuthorityStateError("Journal cutover gate is unavailable")
        return row

    @staticmethod
    def _maintenance(conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM cutover_maintenance WHERE singleton=1"
        ).fetchone()
        if row is None or str(row["domain"]) != "journal":
            raise JournalAuthorityStateError(
                "Journal cutover maintenance state is unavailable"
            )
        return row

    @staticmethod
    def _row_high_water(conn: sqlite3.Connection, table: str) -> tuple[int, int]:
        if table not in {"journal_captures", "journal_entries"}:
            raise ValueError("unsupported Journal cutover high-water table")
        row = conn.execute(
            f"SELECT COUNT(*),COALESCE(MAX(rowid),0) FROM {table}"
        ).fetchone()
        return int(row[0]), int(row[1])

    @classmethod
    def _postseal_drain_state(
        cls,
        conn: sqlite3.Connection,
        *,
        cohort_id: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        control = cls._control(conn)
        gate = cls._cutover_gate(conn)
        maintenance = cls._maintenance(conn)
        if (
            str(control["mode"]) != "database_only"
            or str(control["activated_cohort_id"] or "") != cohort_id
            or str(gate["state"]) != "paused"
            or str(gate["cohort_id"] or "") != cohort_id
            or str(maintenance["state"]) != "postseal_pending"
            or str(maintenance["cohort_id"] or "") != cohort_id
        ):
            raise JournalAuthorityStateError(
                "Journal postseal Source drain is unavailable."
            )
        return control, gate, maintenance

    @staticmethod
    def _source_effect_snapshot(
        conn: sqlite3.Connection,
        *,
        max_rowid: int | None = None,
    ) -> dict[str, Any]:
        clauses = [
            "outbox.target_domain='journal'",
            "outbox.effect_type='journal.capture.materialize'",
        ]
        params: list[Any] = []
        if max_rowid is not None:
            clauses.append("outbox.rowid<=?")
            params.append(max_rowid)
        rows = conn.execute(
            "SELECT outbox.rowid AS source_rowid,outbox.effect_id,"
            "outbox.payload_sha256,outbox.status,outbox.result_ref,"
            "outbox.result_sha256,receipt.receipt_id,"
            "receipt.result_ref AS receipt_result_ref,"
            "receipt.result_sha256 AS receipt_result_sha256 "
            "FROM source_outbox AS outbox LEFT JOIN source_effect_receipts AS receipt "
            "ON receipt.effect_id=outbox.effect_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY outbox.rowid",
            params,
        ).fetchall()
        values: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for row in rows:
            value = {
                "rowId": int(row["source_rowid"]),
                "effectId": str(row["effect_id"]),
                "payloadSha256": str(row["payload_sha256"]),
                "status": str(row["status"]),
                "resultRef": row["result_ref"],
                "resultSha256": row["result_sha256"],
                "receiptId": row["receipt_id"],
            }
            values.append(value)
            if (
                value["status"] != "succeeded"
                or value["receiptId"] is None
                or row["receipt_result_ref"] != value["resultRef"]
                or row["receipt_result_sha256"] != value["resultSha256"]
            ):
                unresolved.append(
                    {
                        "rowId": value["rowId"],
                        "effectId": value["effectId"],
                        "payloadSha256": value["payloadSha256"],
                    }
                )
        baseline = [
            {
                "rowId": value["rowId"],
                "effectId": value["effectId"],
                "payloadSha256": value["payloadSha256"],
            }
            for value in values
        ]
        return {
            "effectCount": len(values),
            "effectMaxRowid": max((value["rowId"] for value in values), default=0),
            "baselineSetSha256": _sha(baseline),
            "unresolvedSetSha256": _sha(unresolved),
            "rows": values,
            "unresolved": unresolved,
        }

    @classmethod
    def _controlled_drain_projection(
        cls,
        conn: sqlite3.Connection,
        *,
        gate: sqlite3.Row,
        cohort_id: str,
        allow_unfinalized_batch_id: str | None = None,
    ) -> dict[str, Any]:
        baseline_capture_count = int(gate["capture_row_count"])
        baseline_capture_max = int(gate["capture_row_high_water"])
        baseline_entry_count = int(gate["entry_row_count"])
        baseline_entry_max = int(gate["entry_row_high_water"])
        entries = cls._row_high_water(conn, "journal_entries")
        if entries != (baseline_entry_count, baseline_entry_max):
            raise JournalAuthorityStateError(
                "Legacy Journal entries changed behind the postseal fence."
            )

        batches = conn.execute(
            "SELECT batch.*,receipt.result_sha256 AS drain_result_sha256,"
            "receipt.post_capture_count,receipt.post_capture_max_rowid "
            "FROM journal_cutover_source_drain_batches AS batch "
            "LEFT JOIN journal_cutover_source_drain_receipts AS receipt "
            "ON receipt.batch_mutation_id=batch.mutation_id "
            "WHERE batch.cohort_id=? "
            "ORDER BY batch.source_effect_max_rowid,batch.created_at,batch.mutation_id",
            (cohort_id,),
        ).fetchall()
        expected_count = baseline_capture_count
        expected_max = baseline_capture_max
        previous_result: str | None = None
        for batch in batches:
            if (
                int(batch["baseline_capture_count"]) != expected_count
                or int(batch["baseline_capture_max_rowid"]) != expected_max
                or batch["previous_drain_result_sha256"] != previous_result
            ):
                raise JournalAuthorityStateError(
                    "The Journal Source drain chain does not match its prior high-water."
                )
            if batch["drain_result_sha256"] is None:
                if str(batch["mutation_id"]) != allow_unfinalized_batch_id:
                    raise JournalAuthorityStateError(
                        "A Journal Source drain batch is not finalized."
                    )
            else:
                expected_count = int(batch["post_capture_count"])
                expected_max = int(batch["post_capture_max_rowid"])
                previous_result = str(batch["drain_result_sha256"])

        receipt_rows = conn.execute(
            "SELECT receipt.*,capture.source_effect_id,capture.request_sha256 "
            "FROM journal_cutover_source_drain_captures AS receipt "
            "JOIN journal_cutover_source_drain_batches AS batch "
            "ON batch.mutation_id=receipt.batch_mutation_id "
            "LEFT JOIN journal_captures AS capture ON capture.capture_id=receipt.capture_id "
            "WHERE batch.cohort_id=? ORDER BY receipt.capture_rowid,receipt.effect_id",
            (cohort_id,),
        ).fetchall()
        ledger_rows: list[dict[str, Any]] = []
        for row in receipt_rows:
            actual = conn.execute(
                "SELECT rowid,source_effect_id,request_sha256 FROM journal_captures "
                "WHERE capture_id=?",
                (row["capture_id"],),
            ).fetchone()
            if (
                actual is None
                or int(actual["rowid"]) != int(row["capture_rowid"])
                or str(actual["source_effect_id"]) != str(row["effect_id"])
                or str(actual["request_sha256"])
                != str(row["capture_request_sha256"])
            ):
                raise JournalAuthorityStateError(
                    "A controlled Journal capture receipt no longer matches authority."
                )
            ledger_rows.append(
                {
                    "batchMutationId": str(row["batch_mutation_id"]),
                    "effectId": str(row["effect_id"]),
                    "captureId": str(row["capture_id"]),
                    "captureRowid": int(row["capture_rowid"]),
                    "requestSha256": str(row["capture_request_sha256"]),
                }
            )
        delta_rows = [
            row for row in ledger_rows if row["captureRowid"] > baseline_capture_max
        ]
        actual_delta = conn.execute(
            "SELECT rowid,capture_id,source_effect_id,request_sha256 "
            "FROM journal_captures WHERE rowid>? ORDER BY rowid,capture_id",
            (baseline_capture_max,),
        ).fetchall()
        expected_delta = [
            (
                row["captureRowid"],
                row["captureId"],
                row["effectId"],
                row["requestSha256"],
            )
            for row in delta_rows
        ]
        observed_delta = [
            (
                int(row["rowid"]),
                str(row["capture_id"]),
                str(row["source_effect_id"]),
                str(row["request_sha256"]),
            )
            for row in actual_delta
        ]
        if observed_delta != expected_delta:
            raise JournalAuthorityStateError(
                "Journal captures contain an out-of-set postseal row."
            )
        capture_count, capture_max = cls._row_high_water(conn, "journal_captures")
        projected_count = baseline_capture_count + len(delta_rows)
        projected_max = max(
            [baseline_capture_max, *(row["captureRowid"] for row in delta_rows)]
        )
        if capture_count != projected_count or capture_max != projected_max:
            raise JournalAuthorityStateError(
                "Journal capture high-water is not explained by the controlled drain."
            )
        unexplained = conn.execute(
            "SELECT 1 FROM journal_effects AS effect "
            "JOIN journal_captures AS capture ON capture.capture_id=effect.capture_id "
            "WHERE effect.effect_type='materialize' AND effect.state!='succeeded' "
            "AND NOT (capture.requested_target='auto' AND EXISTS("
            "SELECT 1 FROM journal_cutover_source_drain_captures AS receipt "
            "WHERE receipt.capture_id=capture.capture_id)) LIMIT 1"
        ).fetchone()
        if unexplained is not None:
            raise JournalAuthorityStateError(
                "Journal materialization changed outside the controlled drain."
            )
        return {
            "captureCount": capture_count,
            "captureMaxRowid": capture_max,
            "entryCount": entries[0],
            "entryMaxRowid": entries[1],
            "controlledDeltaSha256": _sha(ledger_rows),
            "latestDrainResultSha256": previous_result,
        }

    @classmethod
    def _validate_release_source_drain(
        cls,
        conn: sqlite3.Connection,
        *,
        source_conn: sqlite3.Connection,
        sources: SourceStore,
        source_path_sha256: str,
        cohort_id: str,
        source_drain_mutation_id: str,
        gate: sqlite3.Row,
    ) -> dict[str, Any]:
        batch = conn.execute(
            "SELECT batch.*,receipt.result_json,receipt.result_sha256 "
            "FROM journal_cutover_source_drain_batches AS batch "
            "JOIN journal_cutover_source_drain_receipts AS receipt "
            "ON receipt.batch_mutation_id=batch.mutation_id "
            "WHERE batch.mutation_id=?",
            (source_drain_mutation_id,),
        ).fetchone()
        if (
            batch is None
            or str(batch["cohort_id"]) != cohort_id
            or str(batch["source_authority_id"]) != sources.authority_id
            or str(batch["source_db_path_sha256"]) != source_path_sha256
        ):
            raise JournalAuthorityStateError(
                "Journal release requires the exact finalized Source drain."
            )
        latest = conn.execute(
            "SELECT mutation_id FROM journal_cutover_source_drain_batches "
            "WHERE cohort_id=? ORDER BY source_effect_max_rowid DESC,created_at DESC,"
            "mutation_id DESC LIMIT 1",
            (cohort_id,),
        ).fetchone()
        if latest is None or str(latest[0]) != source_drain_mutation_id:
            raise JournalAuthorityStateError(
                "Journal release requires the latest Source drain batch."
            )
        source = cls._source_effect_snapshot(source_conn)
        if (
            source["effectCount"] != int(batch["source_effect_count"])
            or source["effectMaxRowid"] != int(batch["source_effect_max_rowid"])
            or source["baselineSetSha256"]
            != str(batch["source_baseline_set_sha256"])
            or source["unresolved"]
        ):
            raise JournalAuthorityStateError(
                "Journal Source ingress changed after the finalized drain."
            )
        projection = cls._controlled_drain_projection(
            conn,
            gate=gate,
            cohort_id=cohort_id,
        )
        result_json = str(batch["result_json"])
        result_sha = str(batch["result_sha256"])
        if _result_sha(result_json) != result_sha:
            raise JournalAuthorityStateError(
                "The Journal Source drain receipt changed after commit."
            )
        result = json.loads(result_json)
        if (
            not isinstance(result, dict)
            or result.get("cohortId") != cohort_id
            or result.get("mutationId") != source_drain_mutation_id
            or result.get("sourceEffectCount") != source["effectCount"]
            or result.get("sourceEffectMaxRowid") != source["effectMaxRowid"]
            or result.get("sourceEffectSetSha256")
            != source["baselineSetSha256"]
            or result.get("controlledDeltaSha256")
            != projection["controlledDeltaSha256"]
            or projection["latestDrainResultSha256"] != result_sha
        ):
            raise JournalAuthorityStateError(
                "The Journal Source drain receipt does not match current authority."
            )
        return {
            **result,
            "resultSha256": result_sha,
        }

    @staticmethod
    def _source_drain_result(
        conn: sqlite3.Connection,
        batch: sqlite3.Row,
    ) -> dict[str, Any]:
        receipt = conn.execute(
            "SELECT result_json,result_sha256 FROM journal_cutover_source_drain_receipts "
            "WHERE batch_mutation_id=?",
            (batch["mutation_id"],),
        ).fetchone()
        if receipt is not None:
            result_json = str(receipt["result_json"])
            if _result_sha(result_json) != str(receipt["result_sha256"]):
                raise JournalAuthorityStateError(
                    "The Journal Source drain receipt changed after commit."
                )
            value = json.loads(result_json)
            if isinstance(value, dict):
                return value
            raise JournalAuthorityStateError("The Journal Source drain receipt is invalid.")
        effects = conn.execute(
            "SELECT effect_id FROM journal_cutover_source_drain_effects "
            "WHERE batch_mutation_id=? ORDER BY ordinal",
            (batch["mutation_id"],),
        ).fetchall()
        return {
            "schema": "wb.journal-postseal-source-drain-bound/v1",
            "cohortId": str(batch["cohort_id"]),
            "mutationId": str(batch["mutation_id"]),
            "status": "bound",
            "sourceEffectCount": int(batch["source_effect_count"]),
            "sourceEffectMaxRowid": int(batch["source_effect_max_rowid"]),
            "boundEffectCount": int(batch["bound_effect_count"]),
            "boundEffectIds": [str(row[0]) for row in effects],
            "boundEffectSetSha256": str(batch["bound_effect_set_sha256"]),
        }

    @staticmethod
    def _cutover_high_water(gate: sqlite3.Row) -> dict[str, int]:
        return {
            "journalCaptureRowCount": int(gate["capture_row_count"]),
            "journalCaptureMaxRowid": int(gate["capture_row_high_water"]),
            "legacyEntryRowCount": int(gate["entry_row_count"]),
            "legacyEntryMaxRowid": int(gate["entry_row_high_water"]),
        }

    @staticmethod
    def _postseal_release_request(
        *,
        cohort_id: str,
        inventory_sha256: str,
        actor_sha256: str,
        evidence_sha256s: Mapping[str, str],
        high_water_sha256: str,
        source_drain_mutation_id: str,
        source_drain_result_sha256: str,
        source_effect_set_sha256: str,
        source_effect_max_rowid: int,
        evidence_path_sha256s: Mapping[str, str] | None,
        rehearsal: bool,
    ) -> dict[str, Any]:
        evidence = {
            key: str(evidence_sha256s[key])
            for key in sorted(
                {"databaseCheckpoint", "search", "detachment", "authorityHead"}
            )
        }
        return {
            "schema": "wb.journal-postseal-release-request/v1",
            "operation": "release_postseal_ingress",
            "domain": "journal",
            "cohortId": cohort_id,
            "inventorySha256": inventory_sha256,
            "actorSha256": actor_sha256,
            "evidenceSha256": _sha(evidence),
            "evidenceSha256s": evidence,
            "highWaterSha256": high_water_sha256,
            "sourceDrainMutationId": source_drain_mutation_id,
            "sourceDrainResultSha256": source_drain_result_sha256,
            "sourceEffectSetSha256": source_effect_set_sha256,
            "sourceEffectMaxRowid": source_effect_max_rowid,
            "evidenceMode": "rehearsal" if rehearsal else "configured",
            "evidencePathSha256s": (
                None
                if evidence_path_sha256s is None
                else {
                    key: str(evidence_path_sha256s[key])
                    for key in sorted(evidence_path_sha256s)
                }
            ),
        }

    @classmethod
    def _assert_cutover_high_water(
        cls,
        conn: sqlite3.Connection,
        gate: sqlite3.Row,
    ) -> None:
        capture = cls._row_high_water(conn, "journal_captures")
        entries = cls._row_high_water(conn, "journal_entries")
        expected_capture = (
            int(gate["capture_row_count"]),
            int(gate["capture_row_high_water"]),
        )
        expected_entries = (
            int(gate["entry_row_count"]),
            int(gate["entry_row_high_water"]),
        )
        if capture != expected_capture or entries != expected_entries:
            raise JournalAuthorityStateError(
                "Journal ingress changed after the durable cutover pause"
            )
        if cls._unfinished_materializations(conn):
            raise JournalAuthorityStateError(
                "Journal materialization changed after the durable cutover pause"
            )

    @staticmethod
    def _open_cutover_gate(
        conn: sqlite3.Connection,
        *,
        revision: int,
        now: str,
    ) -> None:
        cursor = conn.execute(
            "UPDATE journal_cutover_gate SET state='open',gate_revision=?,"
            "cohort_id=NULL,request_sha256=NULL,capture_row_count=NULL,"
            "capture_row_high_water=NULL,entry_row_count=NULL,"
            "entry_row_high_water=NULL,paused_at=NULL,updated_at=? "
            "WHERE singleton=1 AND state='paused'",
            (revision, now),
        )
        if cursor.rowcount != 1:
            raise JournalCaptureConflict("Journal cutover gate changed concurrently.")

    @staticmethod
    def _gate_transition(
        conn: sqlite3.Connection,
        *,
        revision: int,
        kind: str,
        from_state: str,
        to_state: str,
        cohort_id: str,
        request_sha: str,
        actor: Mapping[str, Any],
        created_at: str,
        capture_count: int | None = None,
        capture_high_water: int | None = None,
        entry_count: int | None = None,
        entry_high_water: int | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO journal_cutover_gate_transitions("
            "gate_revision,transition_kind,from_state,to_state,cohort_id,"
            "request_sha256,actor_json,capture_row_count,capture_row_high_water,"
            "entry_row_count,entry_row_high_water,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision,
                kind,
                from_state,
                to_state,
                cohort_id,
                request_sha,
                _canonical(dict(actor)),
                capture_count,
                capture_high_water,
                entry_count,
                entry_high_water,
                created_at,
            ),
        )

    @staticmethod
    def _unfinished_materializations(conn: sqlite3.Connection) -> int:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM journal_effects "
                "WHERE effect_type='materialize' AND state!='succeeded'"
            ).fetchone()[0]
        )

    @staticmethod
    def _set_domain_mode(conn: sqlite3.Connection, mode: str, now: str) -> None:
        conn.execute(
            "INSERT INTO journal_domain_state(key,value,revision,updated_at) "
            "VALUES('content_authority',?,1,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            "revision=journal_domain_state.revision+1,updated_at=excluded.updated_at",
            (mode, now),
        )

    @staticmethod
    def _transition(
        conn: sqlite3.Connection,
        *,
        revision: int,
        kind: str,
        from_mode: str,
        to_mode: str,
        request_sha: str,
        actor: Mapping[str, Any],
        created_at: str,
        cohort_id: str | None = None,
        capture_id: str | None = None,
        item_id: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO journal_authority_transitions("
            "authority_revision,transition_kind,from_mode,to_mode,cohort_id,"
            "capture_id,item_id,request_sha256,actor_json,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                revision,
                kind,
                from_mode,
                to_mode,
                cohort_id,
                capture_id,
                item_id,
                request_sha,
                _canonical(dict(actor)),
                created_at,
            ),
        )

    @staticmethod
    def _mutation_replay(
        conn: sqlite3.Connection, client_mutation_id: str, request_sha: str
    ) -> bool:
        if not client_mutation_id or len(client_mutation_id) > 256:
            raise JournalAuthorityStateError("a bounded authority mutation identity is required")
        row = conn.execute(
            "SELECT request_sha256 FROM journal_mutations WHERE client_mutation_id=?",
            (client_mutation_id,),
        ).fetchone()
        if row is None:
            return False
        if str(row["request_sha256"]) != request_sha:
            raise JournalCaptureConflict(
                "That Journal mutation key was used for another authority request."
            )
        return True

    @staticmethod
    def _record_mutation(
        conn: sqlite3.Connection,
        client_mutation_id: str,
        request_sha: str,
        result: Mapping[str, Any],
        created_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO journal_mutations("
            "client_mutation_id,request_sha256,result_json,created_at"
            ") VALUES(?,?,?,?)",
            (client_mutation_id, request_sha, _canonical(dict(result)), created_at),
        )


__all__ = [
    "existing_authority_mode",
    "JournalAuthorityCoordinator",
    "JournalAuthorityFenced",
    "JournalAuthorityState",
    "JournalAuthorityStateError",
    "JournalCutoverPaused",
    "legacy_markdown_write_guard",
    "require_legacy_markdown_write",
]

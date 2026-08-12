"""Conservative Journal prose migration onto domain-bound Co-work documents.

Markdown is authoritative until an explicitly selected content entity has an
exact Source, a parity-checked shadow document, and a future rollback window.
The document-kernel binding is the sole authority/epoch record after cutover;
the Journal database retains only content-free discovery and recovery state.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from work_buddy.backups.source_foundation_restore import (
    require_source_foundation_writable,
)
from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.document_kernel.domain_service import (
    DomainContentStoreManager,
    RunningNoteDocumentService,
)
from work_buddy.document_kernel.journal_projection import (
    FileDivergenceCapture,
    JournalProjectionAdapter,
    JournalProjectionWorker,
)
from work_buddy.document_kernel.protocol import sha256_bytes
from work_buddy.journal_capture.content_adapter import (
    JournalContentAdapter,
    _MANAGED_BLOCK_RE,
)
from work_buddy.journal_capture.models import (
    JournalCaptureConflict,
    JournalMigrationComparison,
    JournalMigrationRecord,
    JournalMigrationState,
    JournalEntry,
    JournalProjectionDiverged,
    JournalProjectionError,
)
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.sources import (
    ActorRef,
    AttributionAssertion,
    OriginRef,
    SourceRef,
    SourceStore,
    resolve_and_reserve_source,
)
from work_buddy.sources.models import canonical_sha256
from work_buddy.truth import documents


JOURNAL_MIGRATION_SCHEMA = "wb.journal-content-migration/v1"
JOURNAL_SECTION_PROVIDER_ID = "work-buddy-journal-section"

# Reviewed production inventory.  Its digest is written into exit evidence so
# future callsite additions cannot silently inherit an older certification.
JOURNAL_CONTENT_CALLSITES = (
    "work_buddy/journal.py:read_journal_state",
    "work_buddy/journal.py:append_to_journal",
    "work_buddy/journal.py:extract_sign_in",
    "work_buddy/journal.py:write_sign_in",
    "work_buddy/journal.py:persist_briefing_to_journal",
    "work_buddy/journal_capture/service.py:_materialize",
    "work_buddy/journal_backlog/extract.py:extract_running_notes",
    "work_buddy/journal_backlog/rewrite.py:rewrite_running_notes",
    "work_buddy/journal_backlog/route.py:_append_to_note_impl",
    "work_buddy/obsidian/day_planner/env.py:get_todays_plan",
    "work_buddy/obsidian/day_planner/env.py:write_plan",
    "work_buddy/health/fixers.py:_append_section",
    "work_buddy/threads/cleanup_adapters.py:_journal_note_cleanup",
    "work_buddy/collectors/obsidian_collector.py:_get_journal_entries",
    "work_buddy/collectors/obsidian_collector.py:_get_journal_stats",
    "work_buddy/collectors/obsidian_collector.py:_parse_wellness",
    "work_buddy/activity.py:infer_activity",
    "work_buddy/obsidian/vault_writer.py:vault_write",
)
CALLSITE_INVENTORY_SHA256 = canonical_sha256(JOURNAL_CONTENT_CALLSITES)


def normalized_markdown_sha256(value: bytes) -> str:
    text = value.decode("utf-8-sig")
    return sha256_bytes(text.replace("\r\n", "\n").replace("\r", "\n").encode())


def structural_markdown_sha256(value: bytes) -> str:
    """Hash the narrow Markdown equivalences introduced by the kernel.

    Exact and newline/BOM parity remain recorded separately. This comparison
    admits only representation-neutral changes made by the current parser:
    section-edge blank-line separators and CommonMark unordered-list marker
    choice. It intentionally does not collapse prose whitespace,
    emphasis, heading depth, ordering, or any visible text.
    """

    text = value.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    text = text.strip("\n")
    text = re.sub(r"(?m)^([ \t]*)[+*](?=\s)", r"\1-", text)
    return sha256_bytes(text.encode("utf-8"))


def logical_day_marker_id(day_id: str) -> str:
    return hashlib.sha256(f"journal-log/v1\0{day_id}".encode()).hexdigest()[:32]


def _migration_inventory_item(
    entity_kind: str,
    entity_id: str,
    record: JournalMigrationRecord | None,
    *,
    present: bool = True,
) -> dict[str, Any]:
    return {
        "entityKind": entity_kind,
        "entityId": entity_id,
        "present": present,
        "state": "legacy_authoritative" if record is None else record.mirrored_state.value,
        "authorityEpoch": 0 if record is None else record.mirrored_authority_epoch,
        "comparison": None if record is None else record.comparison_state.value,
        "projection": None if record is None else record.projection_state,
        "bound": bool(record and record.binding_id and record.document_id),
    }


def _exit_inventory_sha256(inventory: Mapping[str, Any]) -> str:
    """Digest migration topology without coupling evidence to normal prose edits."""

    days = []
    for item in inventory["days"]:
        days.append(
            {
                "dayId": item["dayId"],
                "logicalDayLog": item["logicalDayLog"],
                "managedRunningNotes": item["managedRunningNotes"],
                "unadmittedRunningNotes": item["unadmittedRunningNotes"],
            }
        )
    return canonical_sha256(
        {
            "schema": "wb.journal-exit-inventory/v1",
            "callsiteInventorySha256": inventory["callsiteInventorySha256"],
            "days": days,
        }
    )


def build_journal_content_inventory(
    *,
    vault_root: str | Path,
    journal_store: JournalCaptureStore | None,
    cutover_enabled: bool,
    content_stores: DomainContentStoreManager | None = None,
) -> dict[str, Any]:
    """Build a content-free migration inventory without opening the kernel."""

    root = Path(vault_root).expanduser().resolve()
    adapter = JournalContentAdapter(root)
    records = (
        {}
        if journal_store is None
        else {
            (item.entity_kind, item.entity_id): item
            for item in journal_store.list_migrations()
        }
    )
    bound = tuple(
        record
        for record in records.values()
        if record.binding_id is not None
        and record.document_id is not None
    )
    domain_store = None
    causality = None
    if bound:
        try:
            domain_store = (content_stores or DomainContentStoreManager()).open_existing(
                root
            )
            causality = DocumentCausalityStore(domain_store.paths.sidecar)
        except (OSError, RuntimeError):
            # Inventory stays observable but certification fails through the
            # per-entity authorityUnavailable fact below.
            domain_store = None
            causality = None

    def observe(
        item: dict[str, Any],
        record: JournalMigrationRecord | None,
        *,
        legacy_content: str | None,
        managed_body: str | None,
    ) -> dict[str, Any]:
        if record is None:
            return item
        if record.mirrored_state not in {
            JournalMigrationState.COWORK,
            JournalMigrationState.PAUSED_DIVERGED,
        }:
            if legacy_content is not None:
                current_bytes = legacy_content.encode("utf-8")
                current = sha256_bytes(current_bytes)
                if record.byte_parity:
                    policy_digest = current
                elif record.normalized_parity:
                    policy_digest = normalized_markdown_sha256(current_bytes)
                elif record.structural_parity:
                    policy_digest = structural_markdown_sha256(current_bytes)
                else:
                    policy_digest = current
                item["legacyContentSha256"] = policy_digest
                matches = (
                    record.source_content_sha256 is not None
                    and current == record.source_content_sha256
                )
                if (
                    not matches
                    and domain_store is not None
                    and record.document_id is not None
                ):
                    try:
                        document = documents.get_document(
                            domain_store, record.document_id
                        )
                        projected = domain_store.resolve_blob_path(
                            f"blobs/{document.content_sha256}"
                        ).read_bytes()
                    except (KeyError, OSError, RuntimeError):
                        item["authorityUnavailable"] = True
                    else:
                        matches = bool(
                            (record.byte_parity and projected == current_bytes)
                            or (
                                record.normalized_parity
                                and normalized_markdown_sha256(projected)
                                == normalized_markdown_sha256(current_bytes)
                            )
                            or (
                                record.structural_parity
                                and structural_markdown_sha256(projected)
                                == structural_markdown_sha256(current_bytes)
                            )
                        )
                elif not matches and record.binding_id is not None:
                    item["authorityUnavailable"] = True
                if not matches:
                    item["comparison"] = JournalMigrationComparison.MISMATCH.value
            return item

        item["authorityUnavailable"] = domain_store is None or causality is None
        item["observedDivergence"] = (
            record.mirrored_state is JournalMigrationState.PAUSED_DIVERGED
        )
        item["projectionLag"] = False
        if domain_store is None or causality is None or record.binding_id is None:
            return item
        binding = causality.get_binding(record.binding_id)
        if binding is None or binding.lifecycle != "current":
            item["authorityUnavailable"] = True
            return item
        if binding.content_authority != "co_work":
            # A pre-cutover external edit is deliberately paused while legacy
            # Markdown remains canonical.
            if legacy_content is not None:
                current = sha256_bytes(legacy_content.encode("utf-8"))
                item["legacyContentSha256"] = current
            item["observedDivergence"] = True
            return item
        cursor = causality.projection_cursor(binding.binding_id)
        if cursor is None or cursor.status == "paused_diverged":
            item["observedDivergence"] = True
        elif (
            managed_body is None
            or cursor.section_sha256
            != sha256_bytes(managed_body.encode("utf-8"))
        ):
            item["observedDivergence"] = True
        try:
            documents.get_document(domain_store, binding.document_id)
            version = documents.current_document_version(
                domain_store, binding.document_id
            )
        except (KeyError, OSError, RuntimeError):
            item["authorityUnavailable"] = True
        else:
            item["projectionLag"] = (
                cursor is None
                or version is None
                or cursor.document_head_sha256 != version.structured_head_sha256
            )
        return item

    days: list[dict[str, Any]] = []
    journal_dir = root / adapter.journal_dir
    paths = sorted(journal_dir.glob("????-??-??.md")) if journal_dir.is_dir() else ()
    for path in paths:
        day_id = path.stem
        snapshot = None
        try:
            snapshot = adapter.snapshot(day_id)
            body, running_start, _running_end = snapshot.section("running_note")
            log_body, _log_start, _log_end = snapshot.section("logical_day_log")
        except JournalProjectionError as exc:
            known = sorted(
                (
                    _migration_inventory_item(record.entity_kind, record.entity_id, record)
                    for record in records.values()
                    if record.day_id == day_id and record.entity_kind == "running_note"
                ),
                key=lambda entry: entry["entityId"],
            )
            days.append(
                {
                    "dayId": day_id,
                    "fileSha256": None if snapshot is None else snapshot.file_sha256,
                    "logicalDayLog": _migration_inventory_item(
                        "logical_day_log",
                        day_id,
                        records.get(("logical_day_log", day_id)),
                    ),
                    "managedRunningNotes": known,
                    "unadmittedRunningNotes": True,
                    "runningSectionStart": None,
                    "inventoryError": getattr(
                        exc, "code", "journal_content_inventory_unavailable"
                    ),
                }
            )
            continue
        managed: list[dict[str, Any]] = []
        unowned = body
        for match in reversed(tuple(_MANAGED_BLOCK_RE.finditer(body))):
            marker_id = match.group("id")
            record = records.get(("running_note", marker_id))
            legacy_content = None
            if record is not None and record.mirrored_state not in {
                JournalMigrationState.COWORK,
                JournalMigrationState.PAUSED_DIVERGED,
            }:
                try:
                    legacy_content = _exact_marked_body(match)
                except JournalProjectionDiverged:
                    legacy_content = match.group("body")
            managed.append(
                observe(
                    _migration_inventory_item("running_note", marker_id, record),
                    record,
                    legacy_content=legacy_content,
                    managed_body=match.group("body"),
                )
            )
            unowned = unowned[: match.start()] + unowned[match.end() :]
        explicit: list[dict[str, Any]] = []
        managed_ids = {entry["entityId"] for entry in managed}
        for record in records.values():
            if (
                record.day_id != day_id
                or record.entity_kind != "running_note"
                or record.marker_id in managed_ids
            ):
                continue
            legacy_content = None
            if (
                record.selection_start is not None
                and record.selection_end is not None
                and record.selection_end <= len(body)
            ):
                legacy_content = body[
                    record.selection_start : record.selection_end
                ]
            explicit.append(
                observe(
                    _migration_inventory_item(
                        "running_note", record.entity_id, record
                    ),
                    record,
                    legacy_content=legacy_content,
                    managed_body=None,
                )
            )
        log_record = records.get(("logical_day_log", day_id))
        log_matches = (
            []
            if log_record is None
            else [
                match
                for match in _MANAGED_BLOCK_RE.finditer(log_body)
                if match.group("id") == log_record.marker_id
            ]
        )
        log_managed_body = log_matches[0].group("body") if len(log_matches) == 1 else None
        if len(log_matches) == 1 and log_record is not None and (
            log_record.mirrored_state
            not in {
                JournalMigrationState.COWORK,
                JournalMigrationState.PAUSED_DIVERGED,
            }
        ):
            try:
                log_legacy_content = _exact_marked_body(log_matches[0])
            except JournalProjectionDiverged:
                log_legacy_content = log_managed_body
        else:
            log_legacy_content = log_body if not log_matches else None
        log_item = observe(
            _migration_inventory_item(
                "logical_day_log",
                day_id,
                log_record,
                present=bool(log_body.strip()),
            ),
            log_record,
            legacy_content=log_legacy_content,
            managed_body=log_managed_body,
        )
        if (
            log_record is not None
            and log_record.mirrored_state
            in {
                JournalMigrationState.COWORK,
                JournalMigrationState.PAUSED_DIVERGED,
            }
            and len(log_matches) == 1
        ):
            match = log_matches[0]
            if (log_body[: match.start()] + log_body[match.end() :]).strip():
                log_item["observedDivergence"] = True
        days.append(
            {
                "dayId": day_id,
                "fileSha256": snapshot.file_sha256,
                "logicalDayLog": log_item,
                "managedRunningNotes": sorted(
                    managed + explicit, key=lambda entry: entry["entityId"]
                ),
                "unadmittedRunningNotes": bool(_strip_running_structure(unowned)),
                "runningSectionStart": running_start,
            }
        )
    payload = {
        "schema": "wb.journal-content-inventory/v1",
        "cutoverGate": "open" if cutover_enabled else "closed",
        "callsiteInventorySha256": CALLSITE_INVENTORY_SHA256,
        "days": days,
    }
    payload["inventorySha256"] = canonical_sha256(payload)
    payload["exitInventorySha256"] = _exit_inventory_sha256(payload)
    return payload


def latest_current_exit_evidence(
    *,
    vault_root: str | Path,
    journal_store: JournalCaptureStore,
    cutover_enabled: bool,
    content_stores: DomainContentStoreManager | None = None,
) -> Mapping[str, Any] | None:
    """Return evidence only while its cohort and callsite digests are current."""

    inventory = build_journal_content_inventory(
        vault_root=vault_root,
        journal_store=journal_store,
        cutover_enabled=cutover_enabled,
        content_stores=content_stores,
    )
    return journal_store.latest_exit_evidence(
        expected_inventory_sha256=inventory["exitInventorySha256"],
        expected_callsite_inventory_sha256=CALLSITE_INVENTORY_SHA256,
    )


@dataclass(frozen=True, slots=True)
class SelectedJournalContent:
    entity_kind: str
    entity_id: str
    day_id: str
    marker_id: str
    file_sha256: str
    section_sha256: str
    content: bytes
    relative_start: int
    relative_end: int
    absolute_start: int
    absolute_end: int


class JournalMigrationService:
    """One-entity migration/recovery coordinator.

    ``cutover_enabled`` is deployment configuration and defaults false.  There
    is intentionally no public mutation that flips it at runtime.
    """

    def __init__(
        self,
        *,
        vault_root: str | Path,
        journal_store: JournalCaptureStore,
        source_store: SourceStore,
        principal: ActorRef,
        cutover_enabled: bool = False,
        kernel: DocumentKernelClient | None = None,
        stores: DomainContentStoreManager | None = None,
    ) -> None:
        self.vault_root = Path(vault_root).expanduser().resolve()
        self.journal = journal_store
        self.sources = source_store
        self.principal = principal
        self.cutover_enabled = bool(cutover_enabled)
        self.kernel = kernel or DocumentKernelClient()
        self._owns_kernel = kernel is None
        self.stores = stores or DomainContentStoreManager()
        self.adapter = JournalContentAdapter(self.vault_root)
        self.documents = RunningNoteDocumentService(
            kernel=self.kernel,
            stores=self.stores,
        )
        self.projector = JournalProjectionWorker(
            kernel=self.kernel,
            adapter=JournalProjectionAdapter(self.vault_root),
            divergence_capture=FileDivergenceCapture(
                source_store=self.sources,
                vault_root=self.vault_root,
                principal=self.principal,
            ),
        )

    def close(self) -> None:
        if self._owns_kernel:
            self.kernel.close()

    def __enter__(self) -> "JournalMigrationService":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ----- inventory and explicit identity assignment -----------------

    def inventory(self) -> dict[str, Any]:
        return build_journal_content_inventory(
            vault_root=self.vault_root,
            journal_store=self.journal,
            cutover_enabled=self.cutover_enabled,
            content_stores=self.stores,
        )

    @staticmethod
    def _inventory_item(
        entity_kind: str,
        entity_id: str,
        record: JournalMigrationRecord | None,
        *,
        present: bool = True,
    ) -> dict[str, Any]:
        return _migration_inventory_item(
            entity_kind, entity_id, record, present=present
        )

    def select_log(self, day_id: str) -> JournalMigrationRecord:
        selected = self._log_selection(day_id)
        return self._record_selection(selected)

    def select_managed_running_note(self, day_id: str, marker_id: str) -> JournalMigrationRecord:
        selected = self._managed_running_selection(day_id, marker_id)
        return self._record_selection(selected)

    def assign_running_note(
        self,
        day_id: str,
        *,
        start_line: int,
        end_line: int,
    ) -> JournalMigrationRecord:
        """Assign stable identity to an explicitly reviewed legacy line range."""

        snapshot = self.adapter.snapshot(day_id)
        body, body_start, _ = snapshot.section("running_note")
        lines = body.splitlines(keepends=True)
        if start_line < 1 or end_line < start_line or end_line > len(lines):
            raise ValueError("Running Note line selection is invalid")
        relative_start = sum(len(item) for item in lines[: start_line - 1])
        relative_end = sum(len(item) for item in lines[:end_line])
        selected_text = body[relative_start:relative_end]
        if not selected_text.strip() or "<!-- wb:journal-entry/v1 " in selected_text:
            raise ValueError("Select one unowned Running Note passage")
        digest = sha256_bytes(selected_text.encode("utf-8"))
        # The reviewed line range is a transient selector, not durable
        # identity. The store deduplicates an ambiguous retry of this exact
        # selection transactionally; the admitted entity receives an opaque ID.
        entity_id = uuid.uuid4().hex
        return self._record_selection(
            SelectedJournalContent(
                "running_note",
                entity_id,
                day_id,
                entity_id,
                snapshot.file_sha256,
                digest,
                selected_text.encode("utf-8"),
                relative_start,
                relative_end,
                body_start + relative_start,
                body_start + relative_end,
            )
        )

    def _record_selection(self, selected: SelectedJournalContent) -> JournalMigrationRecord:
        return self.journal.record_migration_selection(
            entity_kind=selected.entity_kind,
            entity_id=selected.entity_id,
            day_id=selected.day_id,
            marker_id=selected.marker_id,
            selection_start=selected.relative_start,
            selection_end=selected.relative_end,
            selected_file_sha256=selected.file_sha256,
            selected_section_sha256=selected.section_sha256,
        )

    # ----- shadow, parity, cutover, rollback ---------------------------

    def shadow_import(self, entity_kind: str, entity_id: str) -> JournalMigrationRecord:
        record = self._require_record(entity_kind, entity_id)
        selected = self._selection_from_record(record)
        operation_id, operation_state = self.journal.begin_migration_operation(
            action="shadow_import",
            entity_kind=entity_kind,
            entity_id=entity_id,
            idempotency_key=f"journal-shadow:{entity_kind}:{entity_id}:{selected.section_sha256}",
            request_sha256=selected.section_sha256,
        )
        if operation_state == "completed":
            return self._require_record(entity_kind, entity_id)
        try:
            item = self.sources.capture_source(
                content=selected.content,
                source_role="imported_file",
                tenant_scope_id=self.principal.tenant_scope_id,
                originating_surface="journal_shadow_import",
                media_type="text/markdown",
                representation_kind="raw_bytes",
                encoding="utf-8",
                origin_ref=OriginRef(
                    provider_id=JOURNAL_SECTION_PROVIDER_ID,
                    container_id=hashlib.sha256(str(self.vault_root).encode()).hexdigest(),
                    native_item_id=(
                        f"{self.adapter.journal_dir.as_posix()}/{record.day_id}.md"
                    ),
                    revision=selected.file_sha256,
                    part=entity_kind,
                    coordinates={
                        "entity_id": entity_id,
                        "start": str(selected.relative_start),
                        "end": str(selected.relative_end),
                    },
                ),
                native_revision=selected.file_sha256,
                fidelity="exact_bytes",
                namespace="journal-shadow-import",
                attributions=(
                    AttributionAssertion(
                        role="author",
                        actor=None,
                        state="unknown",
                        basis="file_origin",
                        assurance="unknown",
                    ),
                ),
            )
            representation = self.sources.get_representation(item.primary_representation_id)
            if representation is None or representation.content_sha256 != selected.section_sha256:
                raise JournalProjectionError("The Journal shadow Source could not be verified.")
            self.sources.grant_access(
                source_ref=item.source_ref,
                principal=self.principal,
                purpose="journal.migration",
                access_mode="content",
                authorization_fingerprint=canonical_sha256(
                    {
                        "schema": JOURNAL_MIGRATION_SCHEMA,
                        "operation": operation_id,
                        "source": item.source_ref.uri,
                        "representation": representation.representation_id,
                    }
                ),
                scope={"consumer_domain": "cowork_document", "use_kind": "exact_insertion"},
                content_boundary={"representation_id": representation.representation_id},
            )
            reserved = resolve_and_reserve_source(
                self.sources,
                source_ref=item.source_ref,
                representation_id=representation.representation_id,
                principal=self.principal,
                purpose="journal.migration",
                consumer_domain="cowork_document",
                consumer_id=operation_id,
                use_kind="exact_insertion",
                disclosure_kind="exact_readable_copy",
                redaction_policy="scrub",
                selector={
                    "kind": "journal_section/v1",
                    "entity_kind": entity_kind,
                    "entity_id": entity_id,
                },
                expected_digest=selected.section_sha256,
            )
            change = self.documents.materialize(
                vault_root=self.vault_root,
                entry_id=record.marker_id,
                day_id=record.day_id,
                domain_revision=selected.section_sha256,
                source_store=self.sources,
                reserved_source=reserved,
                actors={
                    "selected_by": self.principal.canonical_id,
                    "applied_by": self.principal.canonical_id,
                    "reviewed_by": self.principal.canonical_id,
                },
                idempotency_key=f"journal-shadow-document:{operation_id}",
                projection_path=(
                    f"{self.adapter.journal_dir.as_posix()}/{record.day_id}.md"
                ),
                domain_kind=entity_kind,
                role=entity_kind,
                document_path=(
                    f"journal/logs/{record.day_id}.md"
                    if entity_kind == "logical_day_log"
                    else f"journal/running-notes/{entity_id}.md"
                ),
                title="Journal Log" if entity_kind == "logical_day_log" else "Running Note",
                migration_origin="journal-shadow-import/v1",
                cutover=False,
            )
            self.journal.advance_migration_operation(operation_id, state="document_committed")
            document = documents.get_document(change.store, change.binding.document_id)
            projected = change.store.resolve_blob_path(
                f"blobs/{document.content_sha256}"
            ).read_bytes()
            byte_parity = projected == selected.content
            normalized_parity = (
                normalized_markdown_sha256(projected)
                == normalized_markdown_sha256(selected.content)
            )
            structural_parity = (
                structural_markdown_sha256(projected)
                == structural_markdown_sha256(selected.content)
            )
            result = self.journal.record_migration_shadow(
                entity_kind=entity_kind,
                entity_id=entity_id,
                source_ref=item.source_ref.uri,
                representation_id=representation.representation_id,
                source_content_sha256=selected.section_sha256,
                binding_id=change.binding.binding_id,
                store_id=change.store.store_id,
                document_id=change.binding.document_id,
                byte_parity=byte_parity,
                normalized_parity=normalized_parity,
                structural_parity=structural_parity,
                operation_id=operation_id,
            )
            self.journal.advance_migration_operation(operation_id, state="completed")
            return result
        except Exception as exc:
            self.journal.advance_migration_operation(
                operation_id,
                state="recoverable",
                error_code=getattr(exc, "code", type(exc).__name__),
            )
            raise

    def cutover(
        self,
        entity_kind: str,
        entity_id: str,
        *,
        rollback_deadline: str,
    ) -> JournalMigrationRecord:
        require_source_foundation_writable("journal_content_migration.cutover")
        if not self.cutover_enabled:
            raise JournalCaptureConflict(
                "Journal Co-work cutover is closed in deployment configuration."
            )
        record = self._require_record(entity_kind, entity_id)
        if (
            record.comparison_state is not JournalMigrationComparison.PARITY
            or record.binding_id is None
            or record.source_content_sha256 is None
        ):
            raise JournalCaptureConflict("Journal shadow parity is required before cutover.")
        deadline = _future_deadline(rollback_deadline)
        store, causality, binding = self._canonical_binding(record)
        if binding.content_authority not in {"domain", "co_work"}:
            raise JournalCaptureConflict("The Journal content authority is unavailable.")
        if record.rollback_deadline not in {None, deadline}:
            raise JournalCaptureConflict(
                "That Journal cutover already has a different rollback deadline."
            )
        request_sha = canonical_sha256(
            {
                "schema": JOURNAL_MIGRATION_SCHEMA,
                "action": "cutover",
                "entity_kind": entity_kind,
                "entity_id": entity_id,
                "source_sha256": record.source_content_sha256,
                "rollback_deadline": deadline,
            }
        )
        operation_id, operation_state = self.journal.begin_migration_operation(
            action="cutover",
            entity_kind=entity_kind,
            entity_id=entity_id,
            idempotency_key=(
                f"journal-cutover:{entity_kind}:{entity_id}:"
                f"{record.source_content_sha256}"
            ),
            request_sha256=request_sha,
        )
        if operation_state == "completed":
            return self._require_record(entity_kind, entity_id)
        try:
            selected: SelectedJournalContent | None = None
            if binding.content_authority == "domain":
                selected = self._selection_from_record(record)
                if selected.section_sha256 != record.source_content_sha256:
                    raise JournalProjectionDiverged(
                        "The Journal passage changed after parity was established."
                    )
            self.journal.mirror_migration_authority(
                entity_kind=entity_kind,
                entity_id=entity_id,
                state=record.mirrored_state,
                authority_epoch=binding.content_authority_epoch,
                rollback_deadline=deadline,
                projection_state="pending",
                operation_id=operation_id,
            )
            if binding.content_authority == "domain":
                assert selected is not None
                self.adapter.install_managed_selection(
                    day_id=record.day_id,
                    marker_id=record.marker_id,
                    expected_file_sha256=selected.file_sha256,
                    expected_section_sha256=selected.section_sha256,
                    selection_start=selected.absolute_start,
                    selection_end=selected.absolute_end,
                )
                binding = causality.cutover_to_cowork(
                    binding.binding_id,
                    domain_revision=selected.section_sha256,
                )
            self.journal.advance_migration_operation(operation_id, state="epoch_committed")
            cursor = self.projector.project(
                store,
                binding=binding,
                entry_id=record.marker_id,
                expected_initial_text=(
                    self._shadow_content(record).decode("utf-8")
                    if selected is None
                    else selected.content.decode("utf-8")
                ),
            )
            if cursor.status == "paused_diverged":
                self.journal.advance_migration_operation(
                    operation_id, state="paused_diverged", error_code="journal_projection_diverged"
                )
                return self.journal.mirror_migration_authority(
                    entity_kind=entity_kind,
                    entity_id=entity_id,
                    state=JournalMigrationState.PAUSED_DIVERGED,
                    authority_epoch=binding.content_authority_epoch,
                    rollback_deadline=deadline,
                    projection_state="paused_diverged",
                    divergence_source_ref=cursor.divergence_source_ref,
                    operation_id=operation_id,
                    error_code="journal_projection_diverged",
                )
            self.journal.advance_migration_operation(
                operation_id, state="projection_committed"
            )
            result = self.journal.mirror_migration_authority(
                entity_kind=entity_kind,
                entity_id=entity_id,
                state=JournalMigrationState.COWORK,
                authority_epoch=binding.content_authority_epoch,
                rollback_deadline=deadline,
                projection_state="committed",
                operation_id=operation_id,
            )
            self.journal.advance_migration_operation(operation_id, state="completed")
            return result
        except JournalProjectionDiverged:
            try:
                self._pause_file_divergence(
                    record,
                    binding=binding,
                    operation_id=operation_id,
                    rollback_deadline=deadline,
                )
            except Exception as pause_exc:
                self.journal.advance_migration_operation(
                    operation_id,
                    state="recoverable",
                    error_code=getattr(pause_exc, "code", type(pause_exc).__name__),
                )
            raise
        except Exception as exc:
            self.journal.advance_migration_operation(
                operation_id,
                state="recoverable",
                error_code=getattr(exc, "code", type(exc).__name__),
            )
            raise

    def reconcile(self, entity_kind: str, entity_id: str) -> JournalMigrationRecord:
        # Reconciliation may invoke the local document kernel before its first
        # receipt write. Fence that dispatch explicitly during restore repair.
        require_source_foundation_writable("journal_content_migration.reconcile")
        record = self._require_record(entity_kind, entity_id)
        if record.binding_id is None:
            return self.shadow_import(entity_kind, entity_id)
        store, causality, binding = self._canonical_binding(record)
        # A captured external file is a review boundary, not a retryable CAS
        # miss. Generic reconciliation must remain observational until an
        # explicit future import/review operation rebases that entity.
        if (
            record.mirrored_state is JournalMigrationState.PAUSED_DIVERGED
            and record.divergence_source_ref is not None
        ):
            return record
        operations = self._recoverable_operations(entity_kind, entity_id)
        if binding.content_authority == "co_work":
            cursor = self.projector.project(
                store,
                binding=binding,
                entry_id=record.marker_id,
                expected_initial_text=self._shadow_content(record).decode("utf-8"),
            )
            state = (
                JournalMigrationState.PAUSED_DIVERGED
                if cursor.status == "paused_diverged"
                else JournalMigrationState.COWORK
            )
            result = self.journal.mirror_migration_authority(
                entity_kind=entity_kind,
                entity_id=entity_id,
                state=state,
                authority_epoch=binding.content_authority_epoch,
                rollback_deadline=record.rollback_deadline,
                projection_state=cursor.status,
                divergence_source_ref=cursor.divergence_source_ref,
            )
            if cursor.status != "paused_diverged":
                self._finish_operations(operations)
            return result
        # A canonical rollback may have committed before the compatibility
        # marker cleanup. Finish that cleanup from the latest exact file base.
        snapshot = self.adapter.snapshot(record.day_id)
        self.adapter.unwrap_managed_selection(
            day_id=record.day_id,
            marker_id=record.marker_id,
            expected_file_sha256=snapshot.file_sha256,
        )
        rollback_pending = any(item["action"] == "rollback" for item in operations)
        target_state = (
            JournalMigrationState.LEGACY
            if rollback_pending or record.mirrored_state is JournalMigrationState.LEGACY
            else JournalMigrationState.SHADOW
        )
        operation_id = str(operations[-1]["operation_id"]) if operations else None
        result = self.journal.mirror_migration_authority(
            entity_kind=entity_kind,
            entity_id=entity_id,
            state=target_state,
            authority_epoch=binding.content_authority_epoch,
            rollback_deadline=None,
            projection_state="none",
            operation_id=operation_id,
        )
        self._finish_operations(
            operations,
            error_code=(
                None
                if rollback_pending
                else "cutover_reverted_before_epoch"
                if any(item["action"] == "cutover" for item in operations)
                else None
            ),
        )
        return result

    def rollback(self, entity_kind: str, entity_id: str) -> JournalMigrationRecord:
        require_source_foundation_writable("journal_content_migration.rollback")
        record = self._require_record(entity_kind, entity_id)
        store, causality, binding = self._canonical_binding(record)
        if (
            binding.content_authority == "domain"
            and record.mirrored_state is JournalMigrationState.LEGACY
        ):
            if record.operation_id is not None:
                try:
                    self.journal.advance_migration_operation(
                        record.operation_id, state="completed"
                    )
                except KeyError:
                    pass
            return record
        if record.rollback_deadline is None:
            raise JournalCaptureConflict("That Journal entity has no rollback window.")
        deadline = _future_deadline(record.rollback_deadline)
        request_sha = canonical_sha256(
            {
                "schema": JOURNAL_MIGRATION_SCHEMA,
                "action": "rollback",
                "entity_kind": entity_kind,
                "entity_id": entity_id,
                "binding_id": binding.binding_id,
                "rollback_deadline": deadline,
            }
        )
        operation_id, operation_state = self.journal.begin_migration_operation(
            action="rollback",
            entity_kind=entity_kind,
            entity_id=entity_id,
            idempotency_key=(
                f"journal-rollback:{entity_kind}:{entity_id}:{deadline}"
            ),
            request_sha256=request_sha,
        )
        if operation_state == "completed":
            return self._require_record(entity_kind, entity_id)
        try:
            if binding.content_authority == "co_work":
                cursor = self.projector.project(
                    store,
                    binding=binding,
                    entry_id=record.marker_id,
                    expected_initial_text=self._shadow_content(record).decode("utf-8"),
                )
                if cursor.status == "paused_diverged":
                    self.journal.advance_migration_operation(
                        operation_id,
                        state="paused_diverged",
                        error_code="journal_projection_diverged",
                    )
                    self.journal.mirror_migration_authority(
                        entity_kind=entity_kind,
                        entity_id=entity_id,
                        state=JournalMigrationState.PAUSED_DIVERGED,
                        authority_epoch=binding.content_authority_epoch,
                        rollback_deadline=deadline,
                        projection_state="paused_diverged",
                        divergence_source_ref=cursor.divergence_source_ref,
                        operation_id=operation_id,
                        error_code="journal_projection_diverged",
                    )
                    raise JournalProjectionDiverged(
                        "Resolve the external Journal edit before rollback."
                    )
                binding = causality.rollback_to_domain(
                    binding.binding_id,
                    domain_revision=cursor.document_head_sha256 or binding.domain_revision,
                    expected_epoch=binding.content_authority_epoch,
                )
            elif binding.content_authority != "domain":
                raise JournalCaptureConflict("The Journal content authority is unavailable.")
            self.journal.advance_migration_operation(
                operation_id, state="epoch_committed"
            )
            snapshot = self.adapter.snapshot(record.day_id)
            self.adapter.unwrap_managed_selection(
                day_id=record.day_id,
                marker_id=record.marker_id,
                expected_file_sha256=snapshot.file_sha256,
            )
            self.journal.advance_migration_operation(
                operation_id, state="projection_committed"
            )
            result = self.journal.mirror_migration_authority(
                entity_kind=entity_kind,
                entity_id=entity_id,
                state=JournalMigrationState.LEGACY,
                authority_epoch=binding.content_authority_epoch,
                rollback_deadline=None,
                projection_state="none",
                operation_id=operation_id,
            )
            self.journal.advance_migration_operation(operation_id, state="completed")
            return result
        except JournalProjectionDiverged:
            try:
                self._pause_file_divergence(
                    record,
                    binding=binding,
                    operation_id=operation_id,
                    rollback_deadline=deadline,
                )
            except Exception as pause_exc:
                self.journal.advance_migration_operation(
                    operation_id,
                    state="recoverable",
                    error_code=getattr(pause_exc, "code", type(pause_exc).__name__),
                )
            raise
        except Exception as exc:
            self.journal.advance_migration_operation(
                operation_id,
                state="recoverable",
                error_code=getattr(exc, "code", type(exc).__name__),
            )
            raise

    def append_log_capture(
        self,
        entry: JournalEntry,
        *,
        stated_at: str | None,
    ):
        """Append one structured capture through the authoritative Log doc.

        Returns ``None`` while that logical day is still Markdown-authoritative
        so the ordinary compatibility path can proceed.
        """

        record = self.journal.get_migration("logical_day_log", entry.day_id)
        if record is None or record.binding_id is None:
            return None
        store, _causality, binding = self._canonical_binding(record)
        if binding.content_authority != "co_work":
            return None
        document = documents.get_document(store, binding.document_id)
        try:
            current = store.resolve_blob_path(
                f"blobs/{document.content_sha256}"
            ).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise JournalProjectionError(
                "The authoritative Journal Log is unavailable."
            ) from exc
        block = self.adapter._render(entry, stated_at=stated_at)
        if entry.projection_marker in current:
            return self.projector.project(
                store, binding=binding, entry_id=record.marker_id
            )
        # Reuse the established chronological policy against a synthetic Log
        # section, then keep only its body as the bound document composition.
        from work_buddy.journal import _find_chronological_insertion_point

        header = "# **Log**"
        wrapped = header + current
        display_time = re.search(r"^\*\s+([^\-]+?)\s+-", block, re.MULTILINE)
        time_text = display_time.group(1).strip() if display_time else ""
        insertion = _find_chronological_insertion_point(wrapped, time_text)
        if insertion is None:
            raise JournalProjectionError("The authoritative Journal Log is malformed.")
        updated = wrapped[:insertion] + "\n" + block + wrapped[insertion:]
        updated_body = updated[len(header) :]
        changed = self._replace_authoritative(
            record,
            updated_body,
            idempotency_key=f"journal-log-capture:{entry.entry_id}:v{entry.version}",
        )
        return self.projector.project(
            changed.store,
            binding=changed.binding,
            entry_id=record.marker_id,
        )

    def append_log_entries(
        self,
        day_id: str,
        entries: list[tuple[str, str]],
    ) -> dict[str, Any] | None:
        """Compatibility bridge for the legacy update-journal workflow."""

        record = self.journal.get_migration("logical_day_log", day_id)
        if record is None or record.binding_id is None:
            return None
        store, _causality, binding = self._canonical_binding(record)
        if binding.content_authority != "co_work":
            return None
        document = documents.get_document(store, binding.document_id)
        current = store.resolve_blob_path(
            f"blobs/{document.content_sha256}"
        ).read_text(encoding="utf-8")
        from work_buddy.journal import (
            _find_chronological_insertion_point,
            _format_log_entry,
        )

        header = "# **Log**"
        wrapped = header + current
        inserted = 0
        already_present: list[str] = []
        for time_text, description in entries:
            line = _format_log_entry(time_text, description)
            if line in wrapped:
                already_present.append(time_text)
                continue
            marker_id = hashlib.sha256(
                f"journal-log-occurrence/v1\0{day_id}\0{time_text}\0{description}".encode()
            ).hexdigest()[:32]
            digest = sha256_bytes(line.encode("utf-8"))
            block = (
                f"<!-- wb:journal-entry/v1 id={marker_id} content-sha256={digest} -->\n"
                f"{line}\n<!-- /wb:journal-entry/v1 id={marker_id} -->"
            )
            insertion = _find_chronological_insertion_point(wrapped, time_text)
            if insertion is None:
                raise JournalProjectionError("The authoritative Journal Log is malformed.")
            wrapped = wrapped[:insertion] + "\n" + block + wrapped[insertion:]
            inserted += 1
        if inserted:
            change = self._replace_authoritative(
                record,
                wrapped[len(header) :],
                idempotency_key="journal-log-workflow:"
                + canonical_sha256(
                    {"day_id": day_id, "entries": entries, "base": document.content_sha256}
                ),
            )
            cursor = self.projector.project(
                change.store, binding=change.binding, entry_id=record.marker_id
            )
        else:
            cursor = self.projector.project(
                store, binding=binding, entry_id=record.marker_id
            )
        return {
            "success": True,
            "file": str(self.adapter.journal_path(day_id)),
            "entries_written": inserted,
            "already_present": already_present,
            "message": (
                f"Appended {inserted} entries through the authoritative Co-work Log."
                if inserted
                else "All entries were already present in the authoritative Co-work Log."
            ),
            "authority_epoch": binding.content_authority_epoch,
            "projection_state": cursor.status,
        }

    def _replace_authoritative(
        self,
        record: JournalMigrationRecord,
        content: str,
        *,
        idempotency_key: str,
    ):
        store, _causality, binding = self._canonical_binding(record)
        if binding.content_authority != "co_work":
            raise JournalCaptureConflict("That Journal entity is not Co-work-authoritative.")
        exact = content.encode("utf-8")
        digest = sha256_bytes(exact)
        item = self.sources.capture_source(
            content=exact,
            source_role="derived_content",
            tenant_scope_id=self.principal.tenant_scope_id,
            originating_surface="journal_content_adapter",
            media_type="text/markdown",
            representation_kind="decoded_text",
            encoding="utf-8",
            namespace="journal-source-change",
            producer=self.principal,
        )
        representation = self.sources.get_representation(item.primary_representation_id)
        if representation is None or representation.content_sha256 != digest:
            raise JournalProjectionError("The Journal change Source could not be verified.")
        operation_id = hashlib.sha256(
            f"journal-source-change\0{idempotency_key}".encode()
        ).hexdigest()[:32]
        self.sources.grant_access(
            source_ref=item.source_ref,
            principal=self.principal,
            purpose="journal.replace",
            access_mode="content",
            authorization_fingerprint=canonical_sha256(
                {
                    "schema": "wb.journal-source-change/v1",
                    "operation_id": operation_id,
                    "source_ref": item.source_ref.uri,
                    "representation_id": representation.representation_id,
                }
            ),
            scope={"consumer_domain": "cowork_document", "use_kind": "exact_insertion"},
            content_boundary={"representation_id": representation.representation_id},
        )
        reserved = resolve_and_reserve_source(
            self.sources,
            source_ref=item.source_ref,
            representation_id=representation.representation_id,
            principal=self.principal,
            purpose="journal.replace",
            consumer_domain="cowork_document",
            consumer_id=operation_id,
            use_kind="exact_insertion",
            disclosure_kind="exact_readable_copy",
            redaction_policy="scrub",
            selector={"kind": "whole_document/v1"},
            expected_digest=digest,
        )
        return self.documents.materialize(
            vault_root=self.vault_root,
            entry_id=record.marker_id,
            day_id=record.day_id,
            domain_revision=digest,
            source_store=self.sources,
            reserved_source=reserved,
            actors={
                "selected_by": self.principal.canonical_id,
                "applied_by": self.principal.canonical_id,
            },
            # The Sources redaction effect carries ``operation_id`` as its
            # consumer identity. Keep that identity in the causality intent so
            # the content-free Journal migration mirror can route cleanup
            # without retaining another usage table.
            idempotency_key=f"journal-source-document:{operation_id}",
            projection_path=(
                f"{self.adapter.journal_dir.as_posix()}/{record.day_id}.md"
            ),
            domain_kind=record.entity_kind,
            role=record.entity_kind,
            document_path=(
                f"journal/logs/{record.day_id}.md"
                if record.entity_kind == "logical_day_log"
                else f"journal/running-notes/{record.entity_id}.md"
            ),
            title="Journal Log" if record.entity_kind == "logical_day_log" else "Running Note",
            migration_origin="journal-source-change/v1",
            cutover=True,
        )

    def certify_exit(self) -> dict[str, Any]:
        """Persist evidence only when inventory has no unadmitted/mismatched prose."""

        inventory = self.inventory()
        blockers: list[str] = []
        for day in inventory["days"]:
            if day["unadmittedRunningNotes"]:
                blockers.append(f"{day['dayId']}:unadmitted_running_notes")
            candidates = [day["logicalDayLog"], *day["managedRunningNotes"]]
            for item in candidates:
                if not item["present"]:
                    continue
                if item["comparison"] != "parity":
                    blockers.append(
                        f"{day['dayId']}:{item['entityKind']}:{item['entityId']}:no_parity"
                    )
                if item["state"] not in {
                    "cowork_authoritative",
                    "legacy_authoritative",
                }:
                    blockers.append(
                        f"{day['dayId']}:{item['entityKind']}:{item['entityId']}:unsettled"
                    )
                if item["projection"] == "paused_diverged":
                    blockers.append(
                        f"{day['dayId']}:{item['entityKind']}:{item['entityId']}:diverged"
                    )
                if item.get("observedDivergence"):
                    blockers.append(
                        f"{day['dayId']}:{item['entityKind']}:{item['entityId']}:"
                        "observed_divergence"
                    )
                if item.get("projectionLag"):
                    blockers.append(
                        f"{day['dayId']}:{item['entityKind']}:{item['entityId']}:"
                        "projection_lag"
                    )
                if item.get("authorityUnavailable"):
                    blockers.append(
                        f"{day['dayId']}:{item['entityKind']}:{item['entityId']}:"
                        "authority_unavailable"
                    )
        if self.journal.recoverable_migration_operations():
            blockers.append("recoverable_operations")
        if blockers:
            raise JournalCaptureConflict(
                "Journal exit evidence is blocked: " + ", ".join(sorted(blockers)[:12])
            )
        summary = {
            "schema": "wb.journal-exit-evidence/v1",
            "days": len(inventory["days"]),
            "entities": sum(
                1 + len(day["managedRunningNotes"]) for day in inventory["days"]
            ),
            "cutoverGate": inventory["cutoverGate"],
        }
        receipt = self.journal.record_exit_evidence(
            inventory_sha256=inventory["exitInventorySha256"],
            callsite_inventory_sha256=CALLSITE_INVENTORY_SHA256,
            authority_summary=summary,
        )
        return {
            **summary,
            "receiptId": receipt,
            "inventorySha256": inventory["exitInventorySha256"],
        }

    def latest_exit_evidence(self) -> Mapping[str, Any] | None:
        return latest_current_exit_evidence(
            vault_root=self.vault_root,
            journal_store=self.journal,
            cutover_enabled=self.cutover_enabled,
            content_stores=self.stores,
        )

    # ----- low-level selection/binding helpers -------------------------

    def _recoverable_operations(
        self, entity_kind: str, entity_id: str
    ) -> list[Mapping[str, Any]]:
        return [
            item
            for item in self.journal.recoverable_migration_operations()
            if item["entity_kind"] == entity_kind and item["entity_id"] == entity_id
        ]

    def _pause_file_divergence(
        self,
        record: JournalMigrationRecord,
        *,
        binding,
        operation_id: str,
        rollback_deadline: str | None,
    ) -> JournalMigrationRecord:
        latest = self._require_record(record.entity_kind, record.entity_id)
        source_ref = (
            latest.divergence_source_ref
            if latest.mirrored_state is JournalMigrationState.PAUSED_DIVERGED
            else None
        )
        if source_ref is None:
            snapshot = self.adapter.snapshot(record.day_id)
            source_ref = self.projector.divergence_capture(
                snapshot.path,
                snapshot.file_sha256,
            )
        self.journal.advance_migration_operation(
            operation_id,
            state="paused_diverged",
            error_code="journal_projection_diverged",
        )
        return self.journal.mirror_migration_authority(
            entity_kind=record.entity_kind,
            entity_id=record.entity_id,
            state=JournalMigrationState.PAUSED_DIVERGED,
            authority_epoch=binding.content_authority_epoch,
            rollback_deadline=rollback_deadline,
            projection_state="paused_diverged",
            divergence_source_ref=source_ref,
            operation_id=operation_id,
            error_code="journal_projection_diverged",
        )

    def _shadow_content(self, record: JournalMigrationRecord) -> bytes:
        if record.source_ref is None or record.representation_id is None:
            raise JournalCaptureConflict("The Journal shadow Source is unavailable.")
        source_ref = SourceRef.parse(record.source_ref)
        with self.sources.connect() as conn:
            row = self.sources._representation_row(  # noqa: SLF001 - same domain store
                conn, source_ref, record.representation_id
            )
            content = self.sources._read_representation_row(row)  # noqa: SLF001
        if (
            record.source_content_sha256 is None
            or sha256_bytes(content) != record.source_content_sha256
        ):
            raise JournalProjectionError("The Journal shadow Source could not be verified.")
        return content

    def _finish_operations(
        self,
        operations: list[Mapping[str, Any]],
        *,
        error_code: str | None = None,
    ) -> None:
        for item in operations:
            self.journal.advance_migration_operation(
                str(item["operation_id"]),
                state="completed",
                error_code=error_code,
            )

    def _require_record(self, entity_kind: str, entity_id: str) -> JournalMigrationRecord:
        record = self.journal.get_migration(entity_kind, entity_id)
        if record is None:
            if entity_kind == "logical_day_log":
                record = self.select_log(entity_id)
            else:
                raise KeyError("journal_migration_selection_not_found")
        return record

    def _log_selection(
        self, day_id: str, *, snapshot=None
    ) -> SelectedJournalContent:
        snapshot = snapshot or self.adapter.snapshot(day_id)
        body, start, end = snapshot.section("logical_day_log")
        exact = body.encode("utf-8")
        return SelectedJournalContent(
            "logical_day_log",
            day_id,
            day_id,
            logical_day_marker_id(day_id),
            snapshot.file_sha256,
            sha256_bytes(exact),
            exact,
            0,
            len(body),
            start,
            end,
        )

    def _managed_running_selection(
        self, day_id: str, marker_id: str
    ) -> SelectedJournalContent:
        snapshot = self.adapter.snapshot(day_id)
        body, section_start, _ = snapshot.section("running_note")
        matches = [
            item for item in _MANAGED_BLOCK_RE.finditer(body) if item.group("id") == marker_id
        ]
        if len(matches) != 1:
            raise JournalProjectionDiverged("The managed Running Note is ambiguous.")
        match = matches[0]
        exact_text = _exact_marked_body(match)
        relative_start = match.start("body")
        relative_end = relative_start + len(exact_text)
        exact = exact_text.encode("utf-8")
        return SelectedJournalContent(
            "running_note",
            marker_id,
            day_id,
            marker_id,
            snapshot.file_sha256,
            sha256_bytes(exact),
            exact,
            relative_start,
            relative_end,
            section_start + relative_start,
            section_start + relative_end,
        )

    def _selection_from_record(self, record: JournalMigrationRecord) -> SelectedJournalContent:
        snapshot = self.adapter.snapshot(record.day_id)
        section_kind = (
            "logical_day_log"
            if record.entity_kind == "logical_day_log"
            else "running_note"
        )
        body, body_start, _ = snapshot.section(section_kind)
        # A pre-existing managed entry is re-located by stable marker.
        if f"id={record.marker_id} " in body:
            matches = [
                item
                for item in _MANAGED_BLOCK_RE.finditer(body)
                if item.group("id") == record.marker_id
            ]
            if len(matches) != 1:
                raise JournalProjectionDiverged(
                    "The managed Journal passage is ambiguous."
                )
            match = matches[0]
            exact_text = _exact_marked_body(match)
            exact = exact_text.encode("utf-8")
            relative_start = match.start("body")
            return SelectedJournalContent(
                record.entity_kind,
                record.entity_id,
                record.day_id,
                record.marker_id,
                snapshot.file_sha256,
                sha256_bytes(exact),
                exact,
                relative_start,
                relative_start + len(exact_text),
                body_start + relative_start,
                body_start + relative_start + len(exact_text),
            )
        if record.entity_kind == "logical_day_log":
            return self._log_selection(record.day_id, snapshot=snapshot)
        if record.selection_start is None or record.selection_end is None:
            raise JournalProjectionDiverged("The selected Running Note is unavailable.")
        if record.selection_end > len(body):
            raise JournalProjectionDiverged("The selected Running Note is unavailable.")
        content = body[record.selection_start : record.selection_end]
        exact = content.encode("utf-8")
        return SelectedJournalContent(
            record.entity_kind,
            record.entity_id,
            record.day_id,
            record.marker_id,
            snapshot.file_sha256,
            sha256_bytes(exact),
            exact,
            record.selection_start,
            record.selection_end,
            body_start + record.selection_start,
            body_start + record.selection_end,
        )

    def _canonical_binding(self, record: JournalMigrationRecord):
        if record.binding_id is None or record.store_id is None or record.document_id is None:
            raise JournalCaptureConflict("The Journal shadow binding is unavailable.")
        store = self.stores.ensure(self.vault_root)
        if store.store_id != record.store_id:
            raise JournalCaptureConflict("The Journal content store binding changed.")
        causality = DocumentCausalityStore(store.paths.sidecar)
        binding = causality.get_binding(record.binding_id)
        if (
            binding is None
            or binding.lifecycle != "current"
            or binding.document_id != record.document_id
            or binding.domain_namespace != "journal"
            or binding.domain_kind != record.entity_kind
            or binding.domain_entity_id != record.marker_id
        ):
            raise JournalCaptureConflict("The Journal content binding is not current.")
        return store, causality, binding


def _future_deadline(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("rollback_deadline must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed <= datetime.now(UTC):
        raise JournalCaptureConflict("The Journal rollback window must still be open.")
    return parsed.astimezone(UTC).isoformat()


def _strip_running_structure(value: str) -> str:
    value = re.sub(r"^%\s*RUNNING\s+END\s*$", "", value, flags=re.MULTILINE)
    value = re.sub(
        r"^\*{3}'Running Notes\s*/\s*Considerations'\s*carried over from\s+"
        r"\d{4}-\d{2}-\d{2}\*{3}\s*$",
        "",
        value,
        flags=re.MULTILINE,
    )
    return value.strip()


def _exact_marked_body(match: re.Match[str]) -> str:
    """Recover the exact pre-projection body authenticated by its marker.

    The outer boundary adds one newline only when the selected passage did not
    already end with one. Trying both byte shapes against the recorded digest
    avoids stripping a user-owned trailing newline by convention.
    """

    body = match.group("body")
    if "<!-- wb:cowork-projection/v1 " in body:
        raise JournalProjectionDiverged(
            "That Journal passage is already a Co-work compatibility projection."
        )
    expected = match.group("digest")
    candidates = [body]
    if body.endswith("\r\n"):
        candidates.append(body[:-2])
    elif body.endswith("\n"):
        candidates.append(body[:-1])
    for candidate in candidates:
        if sha256_bytes(candidate.encode("utf-8")) == expected:
            return candidate
    raise JournalProjectionDiverged(
        "The managed Journal passage no longer matches its recorded digest."
    )


__all__ = [
    "CALLSITE_INVENTORY_SHA256",
    "JOURNAL_CONTENT_CALLSITES",
    "JournalMigrationService",
    "SelectedJournalContent",
    "build_journal_content_inventory",
    "latest_current_exit_evidence",
    "logical_day_marker_id",
    "normalized_markdown_sha256",
    "structural_markdown_sha256",
]

"""Private staged import service for legacy Journal history.

This module intentionally has no configured paths, CLI, MCP registration, or
user-specific mapping.  Callers must explicitly provide the source root, an
already selected Journal store, an already selected Sources store, and a
mapping.  Prepared and staged metadata lives outside ``journal_items``;
ordinary Journal and search reads therefore see nothing until ``seal`` commits
the complete publication cohort.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from work_buddy.journal_capture.history_import import (
    PARSER_VERSION,
    LegacyJournalImportError,
    LegacyJournalInventoryEntry,
    LegacyJournalSpan,
    freeze_inventory,
    inventory_sha256,
    parse_inventory_report,
    parse_legacy_journal,
    verify_frozen_inventory,
)
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.journal_capture.domain import JournalDomainService, _schedule_membership
from work_buddy.journal_capture.typed_import import (
    JournalImportProfileMapping,
    JournalImportTypedObservation,
)
from work_buddy.journal_day import window_for_local_date
from work_buddy.sources import (
    HumanInputRequest,
    SourceRef,
    SourceStore,
    TrustedIngressContext,
    TrustedIngressService,
)


IMPORT_SCHEMA = "wb.legacy-journal-import-cohort/v1"
IMPORT_SOURCE_PURPOSE = "journal.history_import"
IMPORT_SOURCE_USE_KIND = "journal_history_import"
_ALLOWED_ITEM_KINDS = frozenset(
    {"record", "running_note", "generated_artifact", "prompt_input", "prompt_result"}
)
_ALLOWED_PRIVACY = frozenset({"private", "sensitive", "internal"})
_ALLOWED_SEARCH = frozenset(
    {"structured_only", "lexical", "dense", "lexical_dense", "excluded"}
)


class JournalImportCohortError(RuntimeError):
    """A staged import invariant was violated."""


class JournalImportCohortConflict(JournalImportCohortError):
    """An idempotency identity was reused for a different request."""


class JournalImportCohortStateError(JournalImportCohortError):
    """The requested transition is not valid from the current state."""


class JournalImportCohortDrift(JournalImportCohortError):
    """The source tree or retained Source differs from its frozen inventory."""


@dataclass(frozen=True, slots=True)
class JournalImportTarget:
    """Generic publication policy for one parser disposition.

    A private operator can build this mapping in an ignored migration
    workspace.  No person's file layout or preferences are compiled into the
    runtime service.
    """

    item_kind: str
    classification_id: str | None = None
    module_instance_id: str | None = None
    module_instance_version: int | None = None
    privacy_class: str = "private"
    search_mode: str = "lexical_dense"
    interaction_behavior_id: str = "human_value"
    interaction_behavior_version: int = 1

    def __post_init__(self) -> None:
        if self.item_kind not in _ALLOWED_ITEM_KINDS:
            raise ValueError("unsupported Journal import item kind")
        if self.privacy_class not in _ALLOWED_PRIVACY:
            raise ValueError("unsupported Journal import privacy class")
        if self.search_mode not in _ALLOWED_SEARCH:
            raise ValueError("unsupported Journal import search mode")
        if not self.interaction_behavior_id or self.interaction_behavior_version < 1:
            raise ValueError("a versioned interaction behavior is required")
        if (self.module_instance_id is None) != (self.module_instance_version is None):
            raise ValueError("module identity and version must be supplied together")
        if self.module_instance_version is not None and self.module_instance_version < 1:
            raise ValueError("module version must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "itemKind": self.item_kind,
            "classificationId": self.classification_id,
            "moduleInstanceId": self.module_instance_id,
            "moduleInstanceVersion": self.module_instance_version,
            "privacyClass": self.privacy_class,
            "searchMode": self.search_mode,
            "interactionBehaviorId": self.interaction_behavior_id,
            "interactionBehaviorVersion": self.interaction_behavior_version,
        }


@dataclass(frozen=True, slots=True)
class LegacyJournalImportMapping:
    mapping_version: str
    targets: Mapping[str, JournalImportTarget]

    def __post_init__(self) -> None:
        if not self.mapping_version or len(self.mapping_version) > 128:
            raise ValueError("a bounded mapping version is required")
        normalized = dict(sorted(self.targets.items()))
        if any(not key or len(key) > 128 for key in normalized):
            raise ValueError("invalid parser disposition mapping")
        object.__setattr__(self, "targets", normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "wb.legacy-journal-import-mapping/v1",
            "mappingVersion": self.mapping_version,
            "targets": {key: value.to_dict() for key, value in self.targets.items()},
        }

    @property
    def sha256(self) -> str:
        return _sha_json(self.to_dict())


@dataclass(frozen=True, slots=True)
class JournalImportCohort:
    cohort_id: str
    state: str
    state_revision: int
    request_sha256: str
    inventory_sha256: str
    mapping_version: str
    mapping_sha256: str
    expected_file_count: int
    expected_byte_count: int
    expected_span_count: int
    expected_item_count: int
    verified_at: str | None
    sealed_at: str | None
    aborted_at: str | None
    abort_code: str | None
    seal_sha256: str | None
    typed_mapping_sha256: str | None = None
    expected_observation_count: int = 0


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_text(value: str) -> str:
    return _sha_bytes(value.encode("utf-8"))


def _sha_json(value: Any) -> str:
    return _sha_text(_canonical(value))


def _file_id(cohort_id: str, entry: LegacyJournalInventoryEntry) -> str:
    return "jhif_" + _sha_json(
        {
            "schema": "wb.legacy-journal-import-file-id/v1",
            "cohortId": cohort_id,
            "relativePath": entry.relative_path,
            "rawSha256": entry.raw_sha256,
        }
    )[:32]


def _item_id(cohort_id: str, logical_id: str) -> str:
    return "ji_" + _sha_json(
        {
            "schema": "wb.legacy-journal-import-item-id/v1",
            "cohortId": cohort_id,
            "logicalId": logical_id,
        }
    )[:32]


def _receipt_id(cohort_id: str, kind: str, subject: str, request_sha: str) -> str:
    return "jhir_" + _sha_json(
        {
            "schema": "wb.legacy-journal-import-receipt-id/v1",
            "cohortId": cohort_id,
            "kind": kind,
            "subject": subject,
            "requestSha256": request_sha,
        }
    )[:32]


def _source_consumer_id(cohort_id: str, file_id: str) -> str:
    return f"journal-import-file:{cohort_id}:{file_id}"


class LegacyJournalImportService:
    """Coordinate prepare, stage, verify, and atomic publication.

    Both stores are mandatory constructor arguments so this service cannot
    silently resolve or mutate a configured installation.  ``source_committed``
    is an internal crash-boundary seam used by recovery tests.
    """

    def __init__(
        self,
        journal_store: JournalCaptureStore,
        source_store: SourceStore,
        *,
        source_committed: Callable[[str, str], None] | None = None,
    ) -> None:
        if journal_store.read_only:
            raise JournalImportCohortError("the Journal import store is read-only")
        self.journal = journal_store
        self.sources = source_store
        self.ingress = TrustedIngressService(source_store)
        self._source_committed = source_committed or (lambda _cohort, _file: None)

    def prepare(
        self,
        source_root: str | Path,
        *,
        mapping: LegacyJournalImportMapping,
        client_mutation_id: str,
        actor: Mapping[str, Any],
        allowlist: Sequence[str] | None = None,
    ) -> JournalImportCohort:
        if not client_mutation_id or len(client_mutation_id) > 256:
            raise JournalImportCohortError("a bounded import mutation identity is required")
        root = Path(source_root).expanduser().resolve()
        inventory = freeze_inventory(root, allowlist=allowlist)
        inventory_digest = inventory_sha256(inventory)
        request = {
            "schema": IMPORT_SCHEMA,
            "operation": "prepare",
            "inventorySha256": inventory_digest,
            "parserVersion": PARSER_VERSION,
            "mappingVersion": mapping.mapping_version,
            "mappingSha256": mapping.sha256,
        }
        request_sha = _sha_json(request)
        cohort_id = "jhic_" + request_sha[:32]
        parsed = tuple(
            parse_legacy_journal(root / entry.relative_path, root=root, cohort_id=cohort_id)
            for entry in inventory
        )
        report = parse_inventory_report(parsed)
        expected_items = sum(
            1
            for day in parsed
            for span in day.spans
            if span.reason_code is None and span.disposition in mapping.targets
        )
        now = _now()
        actor_json = _canonical(dict(actor))
        with self.journal.transaction() as conn:
            replay = conn.execute(
                "SELECT * FROM journal_import_cohorts "
                "WHERE client_mutation_id=? OR request_sha256=? OR cohort_id=?",
                (client_mutation_id, request_sha, cohort_id),
            ).fetchone()
            if replay is not None:
                if (
                    str(replay["request_sha256"]) != request_sha
                    or str(replay["inventory_sha256"]) != inventory_digest
                    or str(replay["mapping_sha256"]) != mapping.sha256
                ):
                    raise JournalImportCohortConflict(
                        "the import identity was already used for different frozen input"
                    )
                return self._cohort(replay)
            conn.execute(
                """
                INSERT INTO journal_import_cohorts(
                    cohort_id,client_mutation_id,request_sha256,inventory_sha256,
                    parser_version,mapping_version,mapping_sha256,parse_report_sha256,
                    state,state_revision,expected_file_count,expected_byte_count,
                    expected_span_count,expected_item_count,actor_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,'prepared',1,?,?,?,?,?,?,?)
                """,
                (
                    cohort_id,
                    client_mutation_id,
                    request_sha,
                    inventory_digest,
                    PARSER_VERSION,
                    mapping.mapping_version,
                    mapping.sha256,
                    str(report["reportSha256"]),
                    len(parsed),
                    sum(day.inventory.byte_length for day in parsed),
                    sum(len(day.spans) for day in parsed),
                    expected_items,
                    actor_json,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO journal_import_state_transitions("
                "cohort_id,state_revision,from_state,to_state,request_sha256,actor_json,created_at"
                ") VALUES(?,1,NULL,'prepared',?,?,?)",
                (cohort_id, request_sha, actor_json, now),
            )
            for day in parsed:
                file_id = _file_id(cohort_id, day.inventory)
                stage_request = _sha_json(
                    {
                        "schema": IMPORT_SCHEMA,
                        "operation": "stage_file",
                        "cohortId": cohort_id,
                        "fileId": file_id,
                        "rawSha256": day.inventory.raw_sha256,
                        "parseSha256": day.parse_sha256,
                    }
                )
                ingress_mutation = f"journal-history-import:{cohort_id}:{file_id}"
                source_consumer = _source_consumer_id(cohort_id, file_id)
                conn.execute(
                    """
                    INSERT INTO journal_import_files(
                        cohort_id,file_id,relative_path,local_date,byte_length,mtime_ns,
                        raw_sha256,encoding,newline,expected_parse_sha256,
                        expected_span_count,ingress_client_mutation_id,stage_request_sha256,
                        source_usage_consumer_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        cohort_id,
                        file_id,
                        day.inventory.relative_path,
                        day.inventory.local_date,
                        day.inventory.byte_length,
                        day.inventory.mtime_ns,
                        day.inventory.raw_sha256,
                        day.inventory.encoding,
                        day.inventory.newline,
                        day.parse_sha256,
                        len(day.spans),
                        ingress_mutation,
                        stage_request,
                        source_consumer,
                    ),
                )
                conn.execute(
                    "INSERT INTO journal_import_progress("
                    "cohort_id,phase,subject_id,request_sha256,state,attempts,updated_at"
                    ") VALUES(?,'stage_file',?,?,'pending',0,?)",
                    (cohort_id, file_id, stage_request, now),
                )
                for span in day.spans:
                    self._insert_prepared_span(conn, cohort_id, file_id, span, mapping)
            payload = {
                "schema": IMPORT_SCHEMA,
                "cohortId": cohort_id,
                "inventorySha256": inventory_digest,
                "parseReportSha256": report["reportSha256"],
                "fileCount": len(parsed),
                "byteCount": sum(day.inventory.byte_length for day in parsed),
                "spanCount": sum(len(day.spans) for day in parsed),
                "itemCount": expected_items,
            }
            self._insert_receipt(
                conn, cohort_id, "prepared", cohort_id, request_sha, payload, now
            )
        return self.get(cohort_id)

    def prepare_typed_mapping(
        self,
        cohort_id: str,
        mapping: JournalImportProfileMapping,
    ) -> str:
        """Bind a neutral, versioned profile/field mapping to a prepared cohort."""

        request_sha = _sha_json(
            {
                "schema": IMPORT_SCHEMA,
                "operation": "prepare_typed_mapping",
                "cohortId": cohort_id,
                "typedMappingSha256": mapping.sha256,
            }
        )
        now = _now()
        with self.journal.transaction() as conn:
            cohort = self._cohort_row(conn, cohort_id)
            if str(cohort["state"]) != "prepared":
                existing = conn.execute(
                    "SELECT mapping_sha256 FROM journal_import_profile_mappings "
                    "WHERE cohort_id=?",
                    (cohort_id,),
                ).fetchone()
                if existing is not None and str(existing[0]) == mapping.sha256:
                    return mapping.sha256
                raise JournalImportCohortStateError(
                    "a typed import mapping can only be bound while prepared"
                )
            existing = conn.execute(
                "SELECT mapping_sha256 FROM journal_import_profile_mappings "
                "WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != mapping.sha256:
                    raise JournalImportCohortConflict(
                        "the cohort already has a different typed mapping"
                    )
                return mapping.sha256
            if cohort["typed_mapping_sha256"] is not None:
                raise JournalImportCohortConflict(
                    "the cohort typed mapping digest is inconsistent"
                )
            module_type = conn.execute(
                "SELECT 1 FROM journal_module_type_revisions "
                "WHERE module_type_id=? AND module_type_version=?",
                (mapping.module_type_id, mapping.module_type_version),
            ).fetchone()
            module_behavior = conn.execute(
                "SELECT 1 FROM journal_interaction_behavior_revisions "
                "WHERE behavior_id=? AND behavior_version=?",
                (mapping.behavior_id, mapping.behavior_version),
            ).fetchone()
            if module_type is None or module_behavior is None:
                raise JournalImportCohortError(
                    "the typed mapping references an unavailable Journal contract"
                )
            for profile_module in mapping.profile_modules:
                if (
                    profile_module.module_instance_id == mapping.module_instance_id
                    and profile_module.module_instance_version
                    == mapping.module_instance_version
                ):
                    continue
                available = conn.execute(
                    "SELECT 1 FROM journal_module_instance_versions "
                    "WHERE module_instance_id=? AND instance_version=?",
                    (
                        profile_module.module_instance_id,
                        profile_module.module_instance_version,
                    ),
                ).fetchone()
                if available is None:
                    raise JournalImportCohortError(
                        "the imported profile references an unavailable Journal module"
                    )
            for item in mapping.fields:
                behavior = conn.execute(
                    "SELECT 1 FROM journal_interaction_behavior_revisions "
                    "WHERE behavior_id=? AND behavior_version=?",
                    (item.behavior_id, item.behavior_version),
                ).fetchone()
                if behavior is None:
                    raise JournalImportCohortError(
                        "a typed import field references an unavailable behavior"
                    )
                if item.function_id is not None:
                    function = conn.execute(
                        "SELECT value_kind,unit FROM journal_function_contract_revisions "
                        "WHERE function_id=? AND function_version=?",
                        (item.function_id, item.function_version),
                    ).fetchone()
                    if (
                        function is None
                        or str(function["value_kind"]) != item.value_kind
                        or (
                            function["unit"] is not None
                            and str(function["unit"]) != str(item.unit)
                        )
                    ):
                        raise JournalImportCohortError(
                            "a typed import field is incompatible with its function"
                        )
            conn.execute(
                """
                INSERT INTO journal_import_profile_mappings(
                    cohort_id,mapping_version,mapping_sha256,profile_id,
                    profile_revision,profile_name,profile_description,profile_digest,
                    module_instance_id,module_instance_version,module_type_id,
                    module_type_version,module_label,module_slot_id,module_settings_json,
                    module_settings_sha256,profile_modules_json,profile_modules_sha256,
                    day_timezone,day_boundary,boundary_policy_revision,behavior_id,
                    behavior_version,authorship,review_state,field_count,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    cohort_id,
                    mapping.mapping_version,
                    mapping.sha256,
                    mapping.profile_id,
                    mapping.profile_revision,
                    mapping.profile_name,
                    mapping.profile_description,
                    mapping.profile_digest,
                    mapping.module_instance_id,
                    mapping.module_instance_version,
                    mapping.module_type_id,
                    mapping.module_type_version,
                    mapping.module_label,
                    mapping.module_slot_id,
                    _canonical(dict(mapping.module_settings)),
                    mapping.module_settings_sha256,
                    _canonical([item.to_dict() for item in mapping.profile_modules]),
                    _sha_json([item.to_dict() for item in mapping.profile_modules]),
                    mapping.day_timezone,
                    mapping.day_boundary,
                    mapping.boundary_policy_revision,
                    mapping.behavior_id,
                    mapping.behavior_version,
                    mapping.authorship,
                    mapping.review_state,
                    len(mapping.fields),
                    now,
                ),
            )
            for ordinal, item in enumerate(mapping.fields):
                conn.execute(
                    """
                    INSERT INTO journal_import_field_mappings(
                        cohort_id,field_id,definition_version,ordinal,slot_id,owner,
                        stable_key,label,description,value_kind,unit,constraints_json,
                        value_codec_version,function_id,function_version,behavior_id,
                        behavior_version,privacy_class,search_mode,disclosure_policy_id,
                        definition_sha256
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        cohort_id,
                        item.field_id,
                        item.definition_version,
                        ordinal,
                        item.slot_id,
                        item.owner,
                        item.stable_key,
                        item.label,
                        item.description,
                        item.value_kind,
                        item.unit,
                        _canonical(dict(item.constraints)),
                        item.value_codec_version,
                        item.function_id,
                        item.function_version,
                        item.behavior_id,
                        item.behavior_version,
                        item.privacy_class,
                        item.search_mode,
                        item.disclosure_policy_id,
                        item.definition_sha256,
                    ),
                )
            conn.execute(
                "UPDATE journal_import_cohorts SET typed_mapping_sha256=?,updated_at=? "
                "WHERE cohort_id=? AND state='prepared'",
                (mapping.sha256, now, cohort_id),
            )
            self._insert_receipt(
                conn,
                cohort_id,
                "prepared",
                "typed-profile",
                request_sha,
                {
                    "schema": IMPORT_SCHEMA,
                    "cohortId": cohort_id,
                    "typedMappingSha256": mapping.sha256,
                    "profileDigest": mapping.profile_digest,
                    "fieldCount": len(mapping.fields),
                    "containsProse": False,
                },
                now,
            )
        return mapping.sha256

    def stage_typed_observations(
        self,
        cohort_id: str,
        observations: Sequence[JournalImportTypedObservation],
    ) -> int:
        """Validate exact Source evidence and stage one immutable observation set."""

        supplied = tuple(observations)
        with self.journal._connect() as conn:
            cohort = self._cohort_row(conn, cohort_id)
            if str(cohort["state"]) == "aborted":
                raise JournalImportCohortStateError(
                    "an aborted cohort cannot accept typed observations"
                )
            mapping = conn.execute(
                "SELECT * FROM journal_import_profile_mappings WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if mapping is None:
                raise JournalImportCohortStateError(
                    "the cohort has no prepared typed mapping"
                )
            field_rows = {
                str(row["field_id"]): row
                for row in conn.execute(
                    "SELECT * FROM journal_import_field_mappings "
                    "WHERE cohort_id=? ORDER BY ordinal",
                    (cohort_id,),
                ).fetchall()
            }
            files = {
                str(row["relative_path"]): row
                for row in conn.execute(
                    "SELECT * FROM journal_import_files WHERE cohort_id=?",
                    (cohort_id,),
                ).fetchall()
            }
        plans: list[dict[str, Any]] = []
        seen_slots: set[tuple[str, str]] = set()
        for observation in supplied:
            file_row = files.get(observation.relative_path)
            field_row = field_rows.get(observation.field_id)
            if file_row is None or field_row is None:
                raise JournalImportCohortError(
                    "a typed observation references unavailable frozen metadata"
                )
            if str(file_row["local_date"] or "") != observation.local_date:
                raise JournalImportCohortDrift(
                    "a typed observation date differs from its frozen file"
                )
            slot = (observation.local_date, observation.field_id)
            if slot in seen_slots:
                raise JournalImportCohortConflict(
                    "a typed import contains more than one value for a day field"
                )
            seen_slots.add(slot)
            encoded, options, references, frozen = observation.normalized_value(field_row)
            file_id = str(file_row["file_id"])
            receipt = {
                "schema": "wb.journal-import-typed-observation-receipt/v1",
                "observationId": observation.observation_id(cohort_id, file_id),
                "valueId": observation.value_id(cohort_id, file_id),
                "fileId": file_id,
                "localDate": observation.local_date,
                "fieldId": observation.field_id,
                "fieldDefinitionVersion": int(field_row["definition_version"]),
                "evidenceStartByte": observation.evidence_start_byte,
                "evidenceEndByte": observation.evidence_end_byte,
                "evidenceSha256": observation.evidence_sha256,
                "extractorReceiptSha256": observation.extractor_receipt_sha256,
                "frozenValueSha256": _sha_json(frozen),
                "containsProse": False,
            }
            plans.append(
                {
                    "observation": observation,
                    "file": file_row,
                    "field": field_row,
                    "encoded": encoded,
                    "options": options,
                    "references": references,
                    "frozen": frozen,
                    "receipt": receipt,
                    "receipt_sha256": _sha_json(receipt),
                }
            )
        plans.sort(key=lambda plan: str(plan["receipt"]["observationId"]))
        set_sha = _sha_json([plan["receipt_sha256"] for plan in plans])
        if mapping["observation_set_sha256"] is not None:
            if (
                str(mapping["observation_set_sha256"]) != set_sha
                or int(cohort["expected_observation_count"]) != len(plans)
            ):
                raise JournalImportCohortConflict(
                    "the cohort already has a different typed observation set"
                )
            return len(plans)
        if str(cohort["state"]) != "staging":
            raise JournalImportCohortStateError(
                "typed observations can only be staged with retained Sources"
            )
        for plan in plans:
            file_row = plan["file"]
            if (
                str(file_row["state"]) != "staged"
                or str(file_row["source_usage_state"]) != "acknowledged"
            ):
                raise JournalImportCohortStateError(
                    "typed observation evidence is not durably retained"
                )
            raw = self._verify_retained_source(file_row)
            observation = plan["observation"]
            start = observation.evidence_start_byte
            end = observation.evidence_end_byte
            if end > len(raw) or _sha_bytes(raw[start:end]) != observation.evidence_sha256:
                self.abort(
                    cohort_id,
                    abort_code="typed_observation_evidence_drift",
                )
                raise JournalImportCohortDrift(
                    "typed observation evidence differs from retained Source"
                )
        now = _now()
        with self.journal.transaction() as conn:
            cohort = self._cohort_row(conn, cohort_id)
            if str(cohort["state"]) != "staging":
                raise JournalImportCohortStateError(
                    "the cohort changed before typed observation staging"
                )
            current = conn.execute(
                "SELECT observation_set_sha256 FROM journal_import_profile_mappings "
                "WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if current is None:
                raise JournalImportCohortStateError("the typed mapping disappeared")
            if current[0] is not None:
                if str(current[0]) != set_sha:
                    raise JournalImportCohortConflict(
                        "the typed observation set changed concurrently"
                    )
                return len(plans)
            for plan in plans:
                observation = plan["observation"]
                receipt = plan["receipt"]
                conn.execute(
                    """
                    INSERT INTO journal_import_typed_observations(
                        cohort_id,observation_id,file_id,value_id,local_date,field_id,
                        field_definition_version,evidence_start_byte,evidence_end_byte,
                        evidence_sha256,extractor_receipt_sha256,value_json,disposition,
                        frozen_value_sha256,observed_at,stated_at,receipt_sha256,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        cohort_id,
                        receipt["observationId"],
                        receipt["fileId"],
                        receipt["valueId"],
                        observation.local_date,
                        observation.field_id,
                        receipt["fieldDefinitionVersion"],
                        observation.evidence_start_byte,
                        observation.evidence_end_byte,
                        observation.evidence_sha256,
                        observation.extractor_receipt_sha256,
                        None if observation.disposition is not None else _canonical(observation.value),
                        observation.disposition,
                        receipt["frozenValueSha256"],
                        observation.observed_at,
                        observation.stated_at,
                        plan["receipt_sha256"],
                        now,
                    ),
                )
            conn.execute(
                "UPDATE journal_import_profile_mappings SET observation_set_sha256=? "
                "WHERE cohort_id=? AND observation_set_sha256 IS NULL",
                (set_sha, cohort_id),
            )
            conn.execute(
                "UPDATE journal_import_cohorts SET expected_observation_count=?,"
                "updated_at=? WHERE cohort_id=? AND state='staging'",
                (len(plans), now, cohort_id),
            )
        return len(plans)

    def stage(
        self,
        cohort_id: str,
        source_root: str | Path,
        *,
        ingress_context: TrustedIngressContext,
    ) -> JournalImportCohort:
        root = Path(source_root).expanduser().resolve()
        cohort = self.get(cohort_id)
        if cohort.state in {"verified", "sealed"}:
            return cohort
        if cohort.state == "aborted":
            raise JournalImportCohortStateError("an aborted import cannot be staged")
        self._verify_inventory_or_abort(cohort_id, root, "stage_source_drift")
        if cohort.state == "prepared":
            request_sha = _sha_json(
                {"schema": IMPORT_SCHEMA, "operation": "start_staging", "cohortId": cohort_id}
            )
            with self.journal.transaction() as conn:
                row = self._cohort_row(conn, cohort_id)
                if str(row["state"]) == "prepared":
                    now = _now()
                    self._transition(conn, row, "staging", request_sha, now)
                    self._insert_receipt(
                        conn,
                        cohort_id,
                        "staging_started",
                        cohort_id,
                        request_sha,
                        {"schema": IMPORT_SCHEMA, "cohortId": cohort_id},
                        now,
                    )
        with self.journal._connect() as conn:
            files = conn.execute(
                "SELECT * FROM journal_import_files WHERE cohort_id=? ORDER BY relative_path",
                (cohort_id,),
            ).fetchall()
        for file_row in files:
            if str(file_row["state"]) == "staged":
                try:
                    self._verify_retained_source(file_row)
                    self._reconcile_source_dependency(file_row, ingress_context)
                except JournalImportCohortDrift:
                    self.abort(cohort_id, abort_code="stage_retained_source_drift")
                    raise
                continue
            self._stage_file(cohort_id, root, file_row, ingress_context)
        return self.get(cohort_id)

    def verify(
        self,
        cohort_id: str,
        source_root: str | Path,
        *,
        allow_quarantine: bool = False,
    ) -> JournalImportCohort:
        cohort = self.get(cohort_id)
        if cohort.state in {"verified", "sealed"}:
            return cohort
        if cohort.state != "staging":
            raise JournalImportCohortStateError("only a staged cohort can be verified")
        root = Path(source_root).expanduser().resolve()
        self._verify_inventory_or_abort(cohort_id, root, "verify_source_drift")
        try:
            with self.journal._connect() as conn:
                files = conn.execute(
                    "SELECT * FROM journal_import_files WHERE cohort_id=? ORDER BY relative_path",
                    (cohort_id,),
                ).fetchall()
                spans = conn.execute(
                    "SELECT * FROM journal_import_spans WHERE cohort_id=? "
                    "ORDER BY file_id,start_byte",
                    (cohort_id,),
                ).fetchall()
            if len(files) != cohort.expected_file_count or any(
                str(row["state"]) != "staged" for row in files
            ):
                raise JournalImportCohortDrift("not every frozen file was staged")
            if any(str(row["source_usage_state"]) != "acknowledged" for row in files):
                raise JournalImportCohortDrift(
                    "not every staged file has an acknowledged Source dependency"
                )
            if len(spans) != cohort.expected_span_count:
                raise JournalImportCohortDrift("the staged span cohort is incomplete")
            by_file: dict[str, list[sqlite3.Row]] = {}
            for span in spans:
                by_file.setdefault(str(span["file_id"]), []).append(span)
            for file_row in files:
                raw = self._verify_retained_source(file_row)
                cursor = 0
                for span in by_file.get(str(file_row["file_id"]), []):
                    if int(span["start_byte"]) != cursor:
                        raise JournalImportCohortDrift("the staged spans contain a gap")
                    end = int(span["end_byte"])
                    if _sha_bytes(raw[cursor:end]) != str(span["raw_sha256"]):
                        raise JournalImportCohortDrift("a staged span differs from its Source")
                    cursor = end
                if cursor != int(file_row["byte_length"]):
                    raise JournalImportCohortDrift("the staged spans do not cover the file")
            quarantine = [row for row in spans if row["reason_code"] is not None]
            if quarantine and not allow_quarantine:
                raise JournalImportCohortDrift("the cohort contains quarantined source bytes")
            with self.journal._connect() as conn:
                typed_mapping = conn.execute(
                    "SELECT observation_set_sha256,field_count "
                    "FROM journal_import_profile_mappings WHERE cohort_id=?",
                    (cohort_id,),
                ).fetchone()
                typed_observations = conn.execute(
                    "SELECT COUNT(*),COUNT(DISTINCT local_date || ':' || field_id),"
                    "SUM(CASE WHEN state='prepared' THEN 1 ELSE 0 END) "
                    "FROM journal_import_typed_observations WHERE cohort_id=?",
                    (cohort_id,),
                ).fetchone()
            typed_count = int(typed_observations[0] or 0)
            if typed_mapping is None:
                if cohort.typed_mapping_sha256 is not None or typed_count:
                    raise JournalImportCohortDrift(
                        "the cohort typed mapping metadata is incomplete"
                    )
            elif (
                typed_mapping["observation_set_sha256"] is None
                or typed_count != cohort.expected_observation_count
                or int(typed_observations[1] or 0) != typed_count
                or int(typed_observations[2] or 0) != typed_count
            ):
                raise JournalImportCohortDrift(
                    "the staged typed observation set is incomplete"
                )
        except JournalImportCohortDrift:
            self.abort(cohort_id, abort_code="verification_failed")
            raise
        request_sha = _sha_json(
            {
                "schema": IMPORT_SCHEMA,
                "operation": "verify",
                "cohortId": cohort_id,
                "inventorySha256": cohort.inventory_sha256,
                "fileCount": len(files),
                "spanCount": len(spans),
                "allowQuarantine": bool(allow_quarantine),
                "typedMappingSha256": cohort.typed_mapping_sha256,
                "typedObservationCount": typed_count,
            }
        )
        now = _now()
        with self.journal.transaction() as conn:
            row = self._cohort_row(conn, cohort_id)
            if str(row["state"]) == "verified":
                return self._cohort(row)
            self._transition(conn, row, "verified", request_sha, now)
            receipt_id = self._insert_receipt(
                conn,
                cohort_id,
                "verified",
                cohort_id,
                request_sha,
                {
                    "schema": IMPORT_SCHEMA,
                    "cohortId": cohort_id,
                    "inventorySha256": cohort.inventory_sha256,
                    "fileCount": len(files),
                    "spanCount": len(spans),
                    "quarantineCount": len(quarantine),
                    "typedMappingSha256": cohort.typed_mapping_sha256,
                    "typedObservationCount": typed_count,
                },
                now,
            )
            self._upsert_progress(
                conn,
                cohort_id,
                "verify",
                cohort_id,
                request_sha,
                "succeeded",
                receipt_id,
                None,
                now,
            )
        return self.get(cohort_id)

    def seal(
        self,
        cohort_id: str,
        source_root: str | Path,
    ) -> JournalImportCohort:
        cohort = self.get(cohort_id)
        if cohort.state == "sealed":
            return cohort
        if cohort.state != "verified":
            raise JournalImportCohortStateError("only a verified cohort can be sealed")
        with self.journal._connect() as conn:
            gate = conn.execute(
                "SELECT state,cohort_id FROM journal_cutover_gate WHERE singleton=1"
            ).fetchone()
        if (
            gate is None
            or str(gate["state"]) != "paused"
            or str(gate["cohort_id"]) != cohort_id
        ):
            raise JournalImportCohortStateError(
                "the cohort requires its durable ingress pause before seal"
            )
        root = Path(source_root).expanduser().resolve()
        self._verify_inventory_or_abort(cohort_id, root, "seal_source_drift")
        with self.journal._connect() as conn:
            files = conn.execute(
                "SELECT * FROM journal_import_files WHERE cohort_id=? ORDER BY relative_path",
                (cohort_id,),
            ).fetchall()
            spans = conn.execute(
                "SELECT * FROM journal_import_spans WHERE cohort_id=? AND materialize=1 "
                "ORDER BY file_id,start_byte",
                (cohort_id,),
            ).fetchall()
        try:
            raw_by_file = {
                str(file_row["file_id"]): self._verify_retained_source(file_row)
                for file_row in files
            }
        except JournalImportCohortDrift:
            self.abort(cohort_id, abort_code="seal_retained_source_drift")
            raise
        file_by_id = {str(row["file_id"]): row for row in files}
        publications: list[tuple[sqlite3.Row, sqlite3.Row, str, str]] = []
        for span in spans:
            file_row = file_by_id[str(span["file_id"])]
            start, end = int(span["start_byte"]), int(span["end_byte"])
            content = raw_by_file[str(span["file_id"])][start:end]
            if _sha_bytes(content) != str(span["raw_sha256"]):
                self.abort(cohort_id, abort_code="seal_source_mismatch")
                raise JournalImportCohortDrift("a publication span differs from its Source")
            try:
                plain_value = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                self.abort(cohort_id, abort_code="seal_encoding_failure")
                raise JournalImportCohortDrift("a publication span is not UTF-8") from exc
            publications.append((file_row, span, plain_value, _sha_text(plain_value)))
        with self.journal._connect() as conn:
            typed_mapping = conn.execute(
                "SELECT mapping_sha256,profile_digest,observation_set_sha256 "
                "FROM journal_import_profile_mappings WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            typed_receipts = [
                str(row[0])
                for row in conn.execute(
                    "SELECT receipt_sha256 FROM journal_import_typed_observations "
                    "WHERE cohort_id=? ORDER BY observation_id",
                    (cohort_id,),
                ).fetchall()
            ]
        if typed_mapping is not None and (
            typed_mapping["observation_set_sha256"] is None
            or len(typed_receipts) != cohort.expected_observation_count
        ):
            raise JournalImportCohortStateError(
                "the typed import publication is not verified"
            )
        seal_payload = {
            "schema": IMPORT_SCHEMA,
            "operation": "seal",
            "cohortId": cohort_id,
            "inventorySha256": cohort.inventory_sha256,
            "mappingSha256": cohort.mapping_sha256,
            "itemIds": [str(span["item_id"]) for _, span, _, _ in publications],
            "contentSha256": [digest for _, _, _, digest in publications],
            "typedMappingSha256": (
                None if typed_mapping is None else str(typed_mapping["mapping_sha256"])
            ),
            "typedProfileDigest": (
                None if typed_mapping is None else str(typed_mapping["profile_digest"])
            ),
            "typedObservationSetSha256": (
                None
                if typed_mapping is None
                else str(typed_mapping["observation_set_sha256"])
            ),
            "typedObservationReceiptSha256": typed_receipts,
        }
        request_sha = _sha_json(seal_payload)
        seal_sha = _sha_json({"schema": IMPORT_SCHEMA, "sealed": seal_payload})
        now = _now()
        with self.journal.transaction() as conn:
            row = self._cohort_row(conn, cohort_id)
            if str(row["state"]) == "sealed":
                if str(row["seal_sha256"]) != seal_sha:
                    raise JournalImportCohortConflict("the sealed cohort digest changed")
                return self._cohort(row)
            if str(row["state"]) != "verified":
                raise JournalImportCohortStateError("the cohort changed before seal")
            gate = conn.execute(
                "SELECT state,cohort_id FROM journal_cutover_gate WHERE singleton=1"
            ).fetchone()
            if (
                gate is None
                or str(gate["state"]) != "paused"
                or str(gate["cohort_id"]) != cohort_id
            ):
                raise JournalImportCohortStateError(
                    "the cohort ingress pause changed before seal"
                )
            dependency_gap = conn.execute(
                "SELECT 1 FROM journal_import_files WHERE cohort_id=? "
                "AND source_usage_state!='acknowledged' LIMIT 1",
                (cohort_id,),
            ).fetchone()
            if dependency_gap is not None:
                raise JournalImportCohortStateError(
                    "an import Source dependency changed before seal"
                )
            actor_json = str(row["actor_json"])
            typed_count = self._materialize_typed_import(
                conn, cohort_id=cohort_id, actor_json=actor_json, created_at=now
            )
            for file_row, span, plain_value, content_sha in publications:
                item_id = str(span["item_id"])
                source_ref = str(file_row["source_ref"])
                conn.execute(
                    """
                    INSERT INTO journal_items(
                        item_id,local_date,module_instance_id,module_instance_version,
                        item_kind,classification_id,authority_kind,current_plain_value,
                        current_content_sha256,interaction_behavior_id,
                        interaction_behavior_version,privacy_class,search_mode,source_ref,
                        lifecycle,current_revision,created_at,updated_at,import_cohort_id
                    ) VALUES(?,?,?,?,?,?,'native_plain',?,?,?,?,?,?,?,'current',1,?,?,?)
                    """,
                    (
                        item_id,
                        file_row["local_date"],
                        span["module_instance_id"],
                        span["module_instance_version"],
                        span["item_kind"],
                        span["classification_id"],
                        plain_value,
                        content_sha,
                        span["interaction_behavior_id"],
                        span["interaction_behavior_version"],
                        span["privacy_class"],
                        span["search_mode"],
                        source_ref,
                        now,
                        now,
                        cohort_id,
                    ),
                )
                intent_id = f"journal-history-import:{cohort_id}:{span['logical_id']}"
                conn.execute(
                    """
                    INSERT INTO journal_item_revisions(
                        item_id,revision,authority_kind,plain_value,content_sha256,lifecycle,
                        actor_json,source_ref,authorship,review_state,intent_id,created_at
                    ) VALUES(?,1,'native_plain',?,?,'current',?,?,'unknown','unknown',?,?)
                    """,
                    (item_id, plain_value, content_sha, actor_json, source_ref, intent_id, now),
                )
                composition = conn.execute(
                    """
                    SELECT s.composition_digest FROM journal_day_composition_snapshots AS s
                    JOIN journal_days AS d ON d.day_id=s.day_id WHERE d.local_date=?
                    """,
                    (file_row["local_date"],),
                ).fetchone()
                event_id = "jso_" + _sha_json(
                    {
                        "aggregate_type": "item",
                        "aggregate_id": item_id,
                        "aggregate_revision": "1",
                        "event_kind": "backfill",
                    }
                )[:32]
                conn.execute(
                    """
                    INSERT INTO journal_search_outbox(
                        event_id,aggregate_type,aggregate_id,aggregate_revision,event_kind,
                        content_sha256,composition_digest,search_recipe_version,
                        privacy_class,committed_at,visibility_cohort_id
                    ) VALUES(?,'item',?,'1','backfill',?,?,1,?,?,?)
                    """,
                    (
                        event_id,
                        item_id,
                        content_sha,
                        composition[0] if composition is not None else None,
                        span["privacy_class"],
                        now,
                        cohort_id,
                    ),
                )
                conn.execute(
                    "UPDATE journal_import_spans SET materialized_at=? "
                    "WHERE cohort_id=? AND logical_id=?",
                    (now, cohort_id, span["logical_id"]),
                )
            receipt_id = self._insert_receipt(
                conn,
                cohort_id,
                "sealed",
                cohort_id,
                request_sha,
                {
                    "schema": IMPORT_SCHEMA,
                    "cohortId": cohort_id,
                    "sealSha256": seal_sha,
                    "publishedItemCount": len(publications),
                    "publishedTypedObservationCount": typed_count,
                },
                now,
            )
            self._transition(conn, row, "sealed", request_sha, now, seal_sha=seal_sha)
            self._upsert_progress(
                conn, cohort_id, "seal", cohort_id, request_sha, "succeeded", receipt_id, None, now
            )
        return self.get(cohort_id)

    def _materialize_typed_import(
        self,
        conn: sqlite3.Connection,
        *,
        cohort_id: str,
        actor_json: str,
        created_at: str,
    ) -> int:
        mapping = conn.execute(
            "SELECT * FROM journal_import_profile_mappings WHERE cohort_id=?",
            (cohort_id,),
        ).fetchone()
        if mapping is None:
            return 0
        fields = conn.execute(
            "SELECT * FROM journal_import_field_mappings WHERE cohort_id=? "
            "ORDER BY ordinal",
            (cohort_id,),
        ).fetchall()
        observations = conn.execute(
            "SELECT observation.*,file.source_ref "
            "FROM journal_import_typed_observations AS observation "
            "JOIN journal_import_files AS file ON file.cohort_id=observation.cohort_id "
            "AND file.file_id=observation.file_id "
            "WHERE observation.cohort_id=? ORDER BY observation.observation_id",
            (cohort_id,),
        ).fetchall()
        if (
            len(fields) != int(mapping["field_count"])
            or len(observations)
            != int(
                conn.execute(
                    "SELECT expected_observation_count FROM journal_import_cohorts "
                    "WHERE cohort_id=?",
                    (cohort_id,),
                ).fetchone()[0]
            )
            or any(str(row["state"]) != "prepared" for row in observations)
        ):
            raise JournalImportCohortStateError(
                "the typed import changed before materialization"
            )

        for field_row in fields:
            existing = conn.execute(
                "SELECT definition_sha256,import_cohort_id "
                "FROM journal_field_definition_versions "
                "WHERE field_id=? AND definition_version=?",
                (field_row["field_id"], field_row["definition_version"]),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["definition_sha256"])
                    != str(field_row["definition_sha256"])
                    or str(existing["import_cohort_id"] or "") != cohort_id
                ):
                    raise JournalImportCohortConflict(
                        "a typed import field definition collides with existing config"
                    )
                continue
            current = int(
                conn.execute(
                    "SELECT COALESCE(MAX(definition_version),0) "
                    "FROM journal_field_definition_versions WHERE field_id=?",
                    (field_row["field_id"],),
                ).fetchone()[0]
            )
            version = int(field_row["definition_version"])
            if current != version - 1:
                raise JournalImportCohortConflict(
                    "a typed import field revision is not append-only"
                )
            conn.execute(
                """
                INSERT INTO journal_field_definition_versions(
                    field_id,definition_version,owner,stable_key,label,description,
                    value_kind,unit,constraints_json,value_codec_version,function_id,
                    function_version,behavior_id,behavior_version,privacy_class,
                    search_mode,disclosure_policy_id,definition_sha256,created_at,
                    supersedes_version,import_cohort_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    field_row["field_id"],
                    version,
                    field_row["owner"],
                    field_row["stable_key"],
                    field_row["label"],
                    field_row["description"],
                    field_row["value_kind"],
                    field_row["unit"],
                    field_row["constraints_json"],
                    field_row["value_codec_version"],
                    field_row["function_id"],
                    field_row["function_version"],
                    field_row["behavior_id"],
                    field_row["behavior_version"],
                    field_row["privacy_class"],
                    field_row["search_mode"],
                    field_row["disclosure_policy_id"],
                    field_row["definition_sha256"],
                    created_at,
                    current or None,
                    cohort_id,
                ),
            )

        module_id = str(mapping["module_instance_id"])
        module_version = int(mapping["module_instance_version"])
        existing_module = conn.execute(
            "SELECT settings_sha256,import_cohort_id FROM journal_module_instance_versions "
            "WHERE module_instance_id=? AND instance_version=?",
            (module_id, module_version),
        ).fetchone()
        if existing_module is None:
            current = int(
                conn.execute(
                    "SELECT COALESCE(MAX(instance_version),0) "
                    "FROM journal_module_instance_versions WHERE module_instance_id=?",
                    (module_id,),
                ).fetchone()[0]
            )
            if current != module_version - 1:
                raise JournalImportCohortConflict(
                    "the typed import module revision is not append-only"
                )
            conn.execute(
                """
                INSERT INTO journal_module_instance_versions(
                    module_instance_id,instance_version,module_type_id,module_type_version,
                    label,settings_schema_version,settings_json,settings_sha256,
                    behavior_id,behavior_version,schedule_kind,schedule_json,
                    reveal_policy_json,created_at,supersedes_version,import_cohort_id
                ) VALUES(?,?,?,?,?,1,?,?,?,?, 'always','{}','{}',?,?,?)
                """,
                (
                    module_id,
                    module_version,
                    mapping["module_type_id"],
                    mapping["module_type_version"],
                    mapping["module_label"],
                    mapping["module_settings_json"],
                    mapping["module_settings_sha256"],
                    mapping["behavior_id"],
                    mapping["behavior_version"],
                    created_at,
                    current or None,
                    cohort_id,
                ),
            )
            for field_row in fields:
                conn.execute(
                    "INSERT INTO journal_module_field_slots("
                    "module_instance_id,module_instance_version,slot_id,ordinal,"
                    "field_id,field_definition_version) VALUES(?,?,?,?,?,?)",
                    (
                        module_id,
                        module_version,
                        field_row["slot_id"],
                        field_row["ordinal"],
                        field_row["field_id"],
                        field_row["definition_version"],
                    ),
                )
        elif (
            str(existing_module["settings_sha256"])
            != str(mapping["module_settings_sha256"])
            or str(existing_module["import_cohort_id"] or "") != cohort_id
        ):
            raise JournalImportCohortConflict(
                "the typed import module collides with existing config"
            )

        profile_id = str(mapping["profile_id"])
        profile_revision = int(mapping["profile_revision"])
        profile_modules = json.loads(str(mapping["profile_modules_json"]))
        if (
            not isinstance(profile_modules, list)
            or not profile_modules
            or _sha_json(profile_modules) != str(mapping["profile_modules_sha256"])
        ):
            raise JournalImportCohortDrift(
                "the typed import profile composition changed before seal"
            )
        existing_profile = conn.execute(
            "SELECT profile_digest,import_cohort_id FROM journal_profile_revisions "
            "WHERE profile_id=? AND profile_revision=?",
            (profile_id, profile_revision),
        ).fetchone()
        if existing_profile is None:
            current = int(
                conn.execute(
                    "SELECT COALESCE(MAX(profile_revision),0) "
                    "FROM journal_profile_revisions WHERE profile_id=?",
                    (profile_id,),
                ).fetchone()[0]
            )
            if current != profile_revision - 1:
                raise JournalImportCohortConflict(
                    "the typed import profile revision is not append-only"
                )
            conn.execute(
                """
                INSERT INTO journal_profile_revisions(
                    profile_id,profile_revision,format_version,name,description,
                    canonical_order_json,profile_digest,created_by,created_at,
                    supersedes_revision,import_cohort_id
                ) VALUES(?,?,1,?,?,?,?,?,?,?,?)
                """,
                (
                    profile_id,
                    profile_revision,
                    mapping["profile_name"],
                    mapping["profile_description"],
                    _canonical([item["slotId"] for item in profile_modules]),
                    mapping["profile_digest"],
                    "journal-history-import",
                    created_at,
                    current or None,
                    cohort_id,
                ),
            )
            for module_ref in profile_modules:
                conn.execute(
                    "INSERT INTO journal_profile_module_slots("
                    "profile_id,profile_revision,slot_id,ordinal,module_instance_id,"
                    "module_instance_version,required) VALUES(?,?,?,?,?,?,?)",
                    (
                        profile_id,
                        profile_revision,
                        module_ref["slotId"],
                        module_ref["ordinal"],
                        module_ref["moduleInstanceId"],
                        module_ref["moduleInstanceVersion"],
                        int(bool(module_ref["required"])),
                    ),
                )
        elif (
            str(existing_profile["profile_digest"]) != str(mapping["profile_digest"])
            or str(existing_profile["import_cohort_id"] or "") != cohort_id
        ):
            raise JournalImportCohortConflict(
                "the typed import profile collides with existing config"
            )

        snapshots = self._materialize_imported_day_compositions(
            conn,
            cohort_id=cohort_id,
            mapping=mapping,
            profile_modules=profile_modules,
            actor_json=actor_json,
            created_at=created_at,
        )

        field_by_id = {str(row["field_id"]): row for row in fields}
        for observation in observations:
            field_row = field_by_id[str(observation["field_id"])]
            raw_value = (
                None
                if observation["value_json"] is None
                else json.loads(str(observation["value_json"]))
            )
            encoded, options, references, frozen = JournalDomainService._encode_field_value(
                field_row,
                value=raw_value,
                disposition=observation["disposition"],
            )
            if _sha_json(frozen) != str(observation["frozen_value_sha256"]):
                raise JournalImportCohortDrift(
                    "a typed observation value changed before seal"
                )
            value_id = str(observation["value_id"])
            source_ref = str(observation["source_ref"])
            columns = (
                encoded["disposition"],
                encoded["text_value"],
                encoded["number_value"],
                encoded["boolean_value"],
                encoded["temporal_value"],
                encoded["duration_seconds"],
                encoded["option_value"],
                encoded["collection_present"],
            )
            conn.execute(
                """
                INSERT INTO journal_field_values(
                    value_id,local_date,day_id,composition_snapshot_id,
                    composition_slot_id,module_instance_id,
                    module_instance_version,field_id,field_definition_version,
                    value_codec_version,value_kind,disposition,text_value,number_value,
                    boolean_value,temporal_value,duration_seconds,option_value,
                    collection_present,source_ref,authorship,review_state,observed_at,
                    stated_at,ingested_at,current_revision,updated_at,import_cohort_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
                """,
                (
                    value_id,
                    observation["local_date"],
                    snapshots[str(observation["local_date"])]["day_id"],
                    snapshots[str(observation["local_date"])]["snapshot_id"],
                    f"{mapping['module_slot_id']}:{field_row['slot_id']}",
                    module_id,
                    module_version,
                    field_row["field_id"],
                    field_row["definition_version"],
                    field_row["value_codec_version"],
                    field_row["value_kind"],
                    *columns,
                    source_ref,
                    mapping["authorship"],
                    mapping["review_state"],
                    observation["observed_at"],
                    observation["stated_at"],
                    created_at,
                    created_at,
                    cohort_id,
                ),
            )
            for ordinal, option in enumerate(options):
                conn.execute(
                    "INSERT INTO journal_field_value_options(value_id,ordinal,option_id) "
                    "VALUES(?,?,?)",
                    (value_id, ordinal, option),
                )
            for ordinal, reference in enumerate(references):
                conn.execute(
                    "INSERT INTO journal_field_value_references("
                    "value_id,ordinal,reference_kind,reference_id,reference_revision) "
                    "VALUES(?,?,?,?,?)",
                    (
                        value_id,
                        ordinal,
                        reference["kind"],
                        reference["id"],
                        reference.get("revision"),
                    ),
                )
            conn.execute(
                "INSERT INTO journal_field_value_revisions("
                "value_id,revision,value_json,value_sha256,actor_json,source_ref,"
                "intent_id,created_at,authorship,review_state) "
                "VALUES(?,1,?,?,?,?,?,?,?,?)",
                (
                    value_id,
                    _canonical(frozen),
                    _sha_json(frozen),
                    actor_json,
                    source_ref,
                    f"journal-history-import:{cohort_id}:{observation['observation_id']}",
                    created_at,
                    mapping["authorship"],
                    mapping["review_state"],
                ),
            )
            event_id = "jso_" + _sha_json(
                {
                    "aggregate_type": "field_value",
                    "aggregate_id": value_id,
                    "aggregate_revision": "1",
                    "event_kind": "backfill",
                }
            )[:32]
            conn.execute(
                "INSERT INTO journal_search_outbox("
                "event_id,aggregate_type,aggregate_id,aggregate_revision,event_kind,"
                "content_sha256,composition_digest,search_recipe_version,privacy_class,"
                "committed_at,visibility_cohort_id) "
                "VALUES(?,'field_value',?,'1','backfill',?,?,1,?,?,?)",
                (
                    event_id,
                    value_id,
                    _sha_json(frozen),
                    snapshots[str(observation["local_date"])]["composition_digest"],
                    field_row["privacy_class"],
                    created_at,
                    cohort_id,
                ),
            )
            conn.execute(
                "UPDATE journal_import_typed_observations SET state='materialized',"
                "materialized_at=? WHERE cohort_id=? AND observation_id=? "
                "AND state='prepared'",
                (created_at, cohort_id, observation["observation_id"]),
            )
        conn.execute(
            "UPDATE journal_import_profile_mappings SET materialized_at=? "
            "WHERE cohort_id=? AND materialized_at IS NULL",
            (created_at, cohort_id),
        )
        return len(observations)

    def _materialize_imported_day_compositions(
        self,
        conn: sqlite3.Connection,
        *,
        cohort_id: str,
        mapping: sqlite3.Row,
        profile_modules: Sequence[Mapping[str, Any]],
        actor_json: str,
        created_at: str,
    ) -> dict[str, dict[str, str]]:
        """Freeze imported dates under their explicit historical profile and policy."""

        local_dates = [
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT local_date FROM journal_import_files "
                "WHERE cohort_id=? AND local_date IS NOT NULL ORDER BY local_date",
                (cohort_id,),
            ).fetchall()
        ]
        if not local_dates:
            raise JournalImportCohortStateError(
                "a typed import needs at least one historical Journal day"
            )
        activation_request = {
            "schema": IMPORT_SCHEMA,
            "operation": "materialize_historical_profile_activation",
            "cohortId": cohort_id,
            "profileId": mapping["profile_id"],
            "profileRevision": int(mapping["profile_revision"]),
            "profileDigest": mapping["profile_digest"],
            "firstLocalDate": local_dates[0],
        }
        activation_revision = int(
            conn.execute(
                "SELECT COALESCE(MAX(activation_revision),0)+1 "
                "FROM journal_profile_activation_epochs"
            ).fetchone()[0]
        )
        conn.execute(
            "INSERT INTO journal_profile_activation_epochs("
            "activation_revision,profile_id,profile_revision,profile_digest,"
            "effective_local_date,actor_json,client_mutation_id,request_sha256,"
            "activated_at,import_cohort_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                activation_revision,
                mapping["profile_id"],
                mapping["profile_revision"],
                mapping["profile_digest"],
                local_dates[0],
                actor_json,
                f"journal-history-import:{cohort_id}:profile-activation",
                _sha_json(activation_request),
                created_at,
                cohort_id,
            ),
        )

        timezone_name = str(mapping["day_timezone"])
        boundary = str(mapping["day_boundary"])
        zone = ZoneInfo(timezone_name)
        snapshots: dict[str, dict[str, str]] = {}
        for local_date in local_dates:
            window = window_for_local_date(date.fromisoformat(local_date), zone, boundary)
            day_id = f"journal-day:{local_date}:{timezone_name}:{boundary}"
            existing_day = conn.execute(
                "SELECT * FROM journal_days WHERE local_date=?", (local_date,)
            ).fetchone()
            if existing_day is None:
                conn.execute(
                    "INSERT INTO journal_days("
                    "day_id,local_date,timezone,boundary,window_start,window_end,"
                    "boundary_policy_revision,created_at,updated_at,import_cohort_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        day_id,
                        local_date,
                        timezone_name,
                        boundary,
                        window.start.isoformat(),
                        window.end.isoformat(),
                        mapping["boundary_policy_revision"],
                        created_at,
                        created_at,
                        cohort_id,
                    ),
                )
            else:
                identity = (
                    str(existing_day["day_id"]),
                    str(existing_day["timezone"]),
                    str(existing_day["boundary"]),
                    str(existing_day["window_start"]),
                    str(existing_day["window_end"]),
                    str(existing_day["boundary_policy_revision"] or ""),
                )
                requested = (
                    day_id,
                    timezone_name,
                    boundary,
                    window.start.isoformat(),
                    window.end.isoformat(),
                    str(mapping["boundary_policy_revision"]),
                )
                if identity != requested:
                    raise JournalImportCohortConflict(
                        "an imported historical day collides with another day policy"
                    )

            modules_payload: list[dict[str, Any]] = []
            fields_payload: list[dict[str, Any]] = []
            materialized_modules: list[tuple[Mapping[str, Any], sqlite3.Row, str, Mapping[str, Any]]] = []
            materialized_fields: list[tuple[str, str, sqlite3.Row]] = []
            for module_ref in profile_modules:
                module = conn.execute(
                    "SELECT * FROM journal_module_instance_versions "
                    "WHERE module_instance_id=? AND instance_version=?",
                    (
                        module_ref["moduleInstanceId"],
                        module_ref["moduleInstanceVersion"],
                    ),
                ).fetchone()
                if module is None:
                    raise JournalImportCohortConflict(
                        "an imported profile module disappeared before seal"
                    )
                membership, evidence = _schedule_membership(
                    str(module["schedule_kind"]),
                    json.loads(str(module["schedule_json"])),
                    date.fromisoformat(local_date),
                )
                materialized_modules.append((module_ref, module, membership, evidence))
                modules_payload.append(
                    {
                        "slotId": module_ref["slotId"],
                        "ordinal": int(module_ref["ordinal"]),
                        "moduleInstanceId": module["module_instance_id"],
                        "moduleInstanceVersion": int(module["instance_version"]),
                        "moduleTypeId": module["module_type_id"],
                        "moduleTypeVersion": int(module["module_type_version"]),
                        "membership": membership,
                        "scheduleEvidence": dict(evidence),
                    }
                )
                if membership != "included":
                    continue
                slot_rows = conn.execute(
                    "SELECT * FROM journal_module_field_slots WHERE "
                    "module_instance_id=? AND module_instance_version=? "
                    "ORDER BY ordinal,slot_id",
                    (module["module_instance_id"], module["instance_version"]),
                ).fetchall()
                for field_row in slot_rows:
                    composition_slot_id = f"{module_ref['slotId']}:{field_row['slot_id']}"
                    materialized_fields.append(
                        (str(module_ref["slotId"]), composition_slot_id, field_row)
                    )
                    fields_payload.append(
                        {
                            "compositionSlotId": composition_slot_id,
                            "moduleSlotId": module_ref["slotId"],
                            "ordinal": int(field_row["ordinal"]),
                            "fieldId": field_row["field_id"],
                            "fieldDefinitionVersion": int(
                                field_row["field_definition_version"]
                            ),
                            "promptId": field_row["prompt_id"],
                            "promptVersion": (
                                None
                                if field_row["prompt_version"] is None
                                else int(field_row["prompt_version"])
                            ),
                        }
                    )
            composition_digest = _sha_json(
                {
                    "schema": "wb.journal-imported-day-composition/v1",
                    "profileDigest": mapping["profile_digest"],
                    "modules": modules_payload,
                    "fields": fields_payload,
                    "dayPolicy": {
                        "timezone": timezone_name,
                        "boundary": boundary,
                        "policyRevision": mapping["boundary_policy_revision"],
                    },
                }
            )
            snapshot_id = "jds_" + _sha_json(
                {
                    "cohortId": cohort_id,
                    "localDate": local_date,
                    "compositionDigest": composition_digest,
                }
            )[:32]
            existing_snapshot = conn.execute(
                "SELECT snapshot_id,composition_digest,import_cohort_id "
                "FROM journal_day_composition_snapshots WHERE day_id=?",
                (day_id,),
            ).fetchone()
            if existing_snapshot is not None:
                if (
                    str(existing_snapshot["composition_digest"]) != composition_digest
                    or str(existing_snapshot["import_cohort_id"] or "") != cohort_id
                ):
                    raise JournalImportCohortConflict(
                        "an imported historical day already has another composition"
                    )
                snapshots[local_date] = {
                    "day_id": day_id,
                    "snapshot_id": str(existing_snapshot["snapshot_id"]),
                    "composition_digest": composition_digest,
                }
                continue
            conn.execute(
                "INSERT INTO journal_day_composition_snapshots("
                "snapshot_id,day_id,profile_id,profile_revision,profile_digest,"
                "activation_revision,composition_digest,search_recipe_version,"
                "schedule_timezone,schedule_window_start,schedule_window_end,"
                "created_by,created_at,import_cohort_id) "
                "VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    day_id,
                    mapping["profile_id"],
                    mapping["profile_revision"],
                    mapping["profile_digest"],
                    activation_revision,
                    composition_digest,
                    timezone_name,
                    window.start.isoformat(),
                    window.end.isoformat(),
                    "journal-history-import",
                    created_at,
                    cohort_id,
                ),
            )
            for module_ref, module, membership, evidence in materialized_modules:
                conn.execute(
                    "INSERT INTO journal_day_composition_modules("
                    "snapshot_id,slot_id,ordinal,module_instance_id,"
                    "module_instance_version,module_type_id,module_type_version,"
                    "semantic_membership,schedule_kind,schedule_evidence_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id,
                        module_ref["slotId"],
                        module_ref["ordinal"],
                        module["module_instance_id"],
                        module["instance_version"],
                        module["module_type_id"],
                        module["module_type_version"],
                        membership,
                        module["schedule_kind"],
                        _canonical(dict(evidence)),
                    ),
                )
            for module_slot_id, composition_slot_id, field_row in materialized_fields:
                conn.execute(
                    "INSERT INTO journal_day_composition_fields("
                    "snapshot_id,composition_slot_id,module_slot_id,ordinal,field_id,"
                    "field_definition_version,prompt_id,prompt_version) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id,
                        composition_slot_id,
                        module_slot_id,
                        field_row["ordinal"],
                        field_row["field_id"],
                        field_row["field_definition_version"],
                        field_row["prompt_id"],
                        field_row["prompt_version"],
                    ),
                )
            snapshots[local_date] = {
                "day_id": day_id,
                "snapshot_id": snapshot_id,
                "composition_digest": composition_digest,
            }
        conn.execute(
            "UPDATE journal_import_profile_mappings SET activation_revision=?,"
            "composition_count=? WHERE cohort_id=? AND activation_revision IS NULL",
            (activation_revision, len(snapshots), cohort_id),
        )
        return snapshots

    def abort(self, cohort_id: str, *, abort_code: str) -> JournalImportCohort:
        if not abort_code or len(abort_code) > 128:
            raise JournalImportCohortError("a bounded abort code is required")
        request_sha = _sha_json(
            {
                "schema": IMPORT_SCHEMA,
                "operation": "abort",
                "cohortId": cohort_id,
                "abortCode": abort_code,
            }
        )
        now = _now()
        with self.journal.transaction() as conn:
            row = self._cohort_row(conn, cohort_id)
            state = str(row["state"])
            if state == "aborted":
                return self._cohort(row)
            if state == "sealed":
                raise JournalImportCohortStateError("a sealed import cannot be aborted")
            self._transition(conn, row, "aborted", request_sha, now, abort_code=abort_code)
            self._insert_receipt(
                conn,
                cohort_id,
                "aborted",
                cohort_id,
                request_sha,
                {"schema": IMPORT_SCHEMA, "cohortId": cohort_id, "abortCode": abort_code},
                now,
            )
            usages = conn.execute(
                "SELECT source_usage_id FROM journal_import_files "
                "WHERE cohort_id=? AND source_usage_id IS NOT NULL "
                "AND source_usage_state IN ('reserved','acknowledged')",
                (cohort_id,),
            ).fetchall()
        for usage in usages:
            usage_id = str(usage["source_usage_id"])
            self.sources.release_usage(usage_id)
            with self.journal.transaction() as conn:
                conn.execute(
                    "UPDATE journal_import_files SET source_usage_state='released' "
                    "WHERE cohort_id=? AND source_usage_id=? "
                    "AND source_usage_state IN ('reserved','acknowledged')",
                    (cohort_id, usage_id),
                )
        return self.get(cohort_id)

    def get(self, cohort_id: str) -> JournalImportCohort:
        with self.journal._connect() as conn:
            row = self._cohort_row(conn, cohort_id)
        return self._cohort(row)

    def _stage_file(
        self,
        cohort_id: str,
        root: Path,
        file_row: sqlite3.Row,
        context: TrustedIngressContext,
    ) -> None:
        file_id = str(file_row["file_id"])
        request_sha = str(file_row["stage_request_sha256"])
        try:
            parsed = parse_legacy_journal(
                root / str(file_row["relative_path"]), root=root, cohort_id=cohort_id
            )
            if parsed.parse_sha256 != str(file_row["expected_parse_sha256"]):
                raise JournalImportCohortDrift("the parser result changed after prepare")
            with self.journal._connect() as conn:
                stored_spans = conn.execute(
                    "SELECT logical_id,receipt_sha256 FROM journal_import_spans "
                    "WHERE cohort_id=? AND file_id=? ORDER BY start_byte",
                    (cohort_id, file_id),
                ).fetchall()
            expected = [
                (span.logical_id, _sha_json(span.to_receipt())) for span in parsed.spans
            ]
            if expected != [
                (str(row["logical_id"]), str(row["receipt_sha256"])) for row in stored_spans
            ]:
                raise JournalImportCohortDrift("the prepared span receipt changed")
            raw = (root / str(file_row["relative_path"])).read_bytes()
            if (
                len(raw) != int(file_row["byte_length"])
                or _sha_bytes(raw) != str(file_row["raw_sha256"])
            ):
                raise JournalImportCohortDrift(
                    "the source file changed between parse and retention"
                )
            committed = self.ingress.commit_human_input(
                context,
                HumanInputRequest(
                    exact_content=raw,
                    client_mutation_id=str(file_row["ingress_client_mutation_id"]),
                    input_mode="import",
                    media_type="text/markdown",
                ),
            )
            self._source_committed(cohort_id, file_id)
            reservation = self._reserve_source_dependency(
                cohort_id=cohort_id,
                file_id=file_id,
                source_ref=committed.source_ref,
                representation_id=committed.representation_id,
                context=context,
            )
            if reservation.status == "reserved":
                self.sources.precommit_recheck_usage(reservation.usage_id)
            elif reservation.status != "acknowledged":
                raise JournalImportCohortDrift(
                    "the import Source dependency is no longer usable"
                )
            now = _now()
            source_uri = committed.source_ref.uri
            payload = {
                "schema": IMPORT_SCHEMA,
                "cohortId": cohort_id,
                "fileId": file_id,
                "rawSha256": file_row["raw_sha256"],
                "sourceRef": source_uri,
                "representationId": committed.representation_id,
                "submissionId": committed.submission_id,
                "sourceUsageId": reservation.usage_id,
                "sourceUsageConsumerId": _source_consumer_id(cohort_id, file_id),
                "parseSha256": parsed.parse_sha256,
                "spanCount": len(parsed.spans),
            }
            with self.journal.transaction() as conn:
                cohort_row = self._cohort_row(conn, cohort_id)
                if str(cohort_row["state"]) != "staging":
                    raise JournalImportCohortStateError("the cohort changed during file staging")
                current = conn.execute(
                    "SELECT * FROM journal_import_files WHERE cohort_id=? AND file_id=?",
                    (cohort_id, file_id),
                ).fetchone()
                if current is None:
                    raise JournalImportCohortDrift("a prepared file disappeared")
                if str(current["source_usage_state"]) in {
                    "redaction_committed",
                    "released",
                }:
                    raise JournalImportCohortDrift(
                        "the import Source dependency was redacted during staging"
                    )
                if str(current["state"]) == "staged":
                    if (
                        str(current["source_ref"]) != source_uri
                        or str(current["representation_id"]) != committed.representation_id
                        or str(current["source_usage_id"]) != reservation.usage_id
                    ):
                        raise JournalImportCohortConflict("a staged file Source changed")
                else:
                    conn.execute(
                        "UPDATE journal_import_files SET state='staged',source_ref=?,"
                        "representation_id=?,submission_id=?,source_usage_id=?,"
                        "source_usage_state='reserved',staged_at=? "
                        "WHERE cohort_id=? AND file_id=? AND state='prepared'",
                        (
                            source_uri,
                            committed.representation_id,
                            committed.submission_id,
                            reservation.usage_id,
                            now,
                            cohort_id,
                            file_id,
                        ),
                    )
                    receipt_id = self._insert_receipt(
                        conn, cohort_id, "file_staged", file_id, request_sha, payload, now
                    )
                    self._upsert_progress(
                        conn,
                        cohort_id,
                        "stage_file",
                        file_id,
                        request_sha,
                        "succeeded",
                        receipt_id,
                        None,
                        now,
                    )
            self.sources.acknowledge_usage(reservation.usage_id)
            with self.journal.transaction() as conn:
                changed = conn.execute(
                    "UPDATE journal_import_files SET source_usage_state='acknowledged' "
                    "WHERE cohort_id=? AND file_id=? AND source_usage_id=? "
                    "AND source_usage_state='reserved'",
                    (cohort_id, file_id, reservation.usage_id),
                ).rowcount
                current = conn.execute(
                    "SELECT source_usage_state FROM journal_import_files "
                    "WHERE cohort_id=? AND file_id=?",
                    (cohort_id, file_id),
                ).fetchone()
                if current is None or (
                    changed == 0 and str(current["source_usage_state"]) != "acknowledged"
                ):
                    raise JournalImportCohortDrift(
                        "the import Source dependency changed during acknowledgement"
                    )
        except JournalImportCohortDrift:
            self.abort(cohort_id, abort_code="stage_parser_or_source_drift")
            raise
        except Exception as exc:
            now = _now()
            with self.journal.transaction() as conn:
                row = self._cohort_row(conn, cohort_id)
                if str(row["state"]) == "staging":
                    self._upsert_progress(
                        conn,
                        cohort_id,
                        "stage_file",
                        file_id,
                        request_sha,
                        "failed",
                        None,
                        type(exc).__name__[:128],
                        now,
                    )
            raise

    def _reserve_source_dependency(
        self,
        *,
        cohort_id: str,
        file_id: str,
        source_ref: SourceRef,
        representation_id: str,
        context: TrustedIngressContext,
    ):
        return self.sources.reserve_usage(
            source_ref=source_ref,
            representation_id=representation_id,
            principal=context.service_principal,
            purpose=IMPORT_SOURCE_PURPOSE,
            consumer_domain="journal",
            consumer_id=_source_consumer_id(cohort_id, file_id),
            use_kind=IMPORT_SOURCE_USE_KIND,
            disclosure_kind="exact_readable_copy",
            redaction_policy="scrub",
            selector={"kind": "whole"},
        )

    def _reconcile_source_dependency(
        self,
        file_row: sqlite3.Row,
        context: TrustedIngressContext,
    ) -> None:
        cohort_id = str(file_row["cohort_id"])
        file_id = str(file_row["file_id"])
        source_ref = SourceRef.parse(str(file_row["source_ref"]))
        reservation = self._reserve_source_dependency(
            cohort_id=cohort_id,
            file_id=file_id,
            source_ref=source_ref,
            representation_id=str(file_row["representation_id"]),
            context=context,
        )
        expected_consumer = _source_consumer_id(cohort_id, file_id)
        if (
            file_row["source_usage_id"] is not None
            and str(file_row["source_usage_id"]) != reservation.usage_id
        ) or str(file_row["source_usage_consumer_id"]) != expected_consumer:
            raise JournalImportCohortConflict("a staged file Source dependency changed")
        state = str(file_row["source_usage_state"])
        if state in {"redaction_committed", "released"} or reservation.status == "released":
            raise JournalImportCohortDrift(
                "the import Source dependency was redacted or released"
            )
        if reservation.status == "reserved":
            self.sources.precommit_recheck_usage(reservation.usage_id)
            self.sources.acknowledge_usage(reservation.usage_id)
        elif reservation.status != "acknowledged":
            raise JournalImportCohortDrift("the import Source dependency is unavailable")
        with self.journal.transaction() as conn:
            conn.execute(
                "UPDATE journal_import_files SET source_usage_id=?,"
                "source_usage_state='acknowledged' WHERE cohort_id=? AND file_id=? "
                "AND source_usage_state IN ('unreserved','reserved','acknowledged')",
                (reservation.usage_id, cohort_id, file_id),
            )

    def _verify_inventory_or_abort(self, cohort_id: str, root: Path, code: str) -> None:
        with self.journal._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM journal_import_files WHERE cohort_id=? ORDER BY relative_path",
                (cohort_id,),
            ).fetchall()
        expected = tuple(
            LegacyJournalInventoryEntry(
                relative_path=str(row["relative_path"]),
                local_date=row["local_date"],
                byte_length=int(row["byte_length"]),
                mtime_ns=int(row["mtime_ns"]),
                raw_sha256=str(row["raw_sha256"]),
                encoding=str(row["encoding"]),
                newline=str(row["newline"]),
            )
            for row in rows
        )
        try:
            verify_frozen_inventory(root, expected)
        except LegacyJournalImportError as exc:
            self.abort(cohort_id, abort_code=code)
            raise JournalImportCohortDrift("the frozen Journal source changed") from exc

    def _verify_retained_source(self, file_row: sqlite3.Row) -> bytes:
        source = file_row["source_ref"]
        representation = file_row["representation_id"]
        if source is None or representation is None:
            raise JournalImportCohortDrift("the staged file has no retained Source")
        try:
            source_ref = SourceRef.parse(str(source))
            with self.sources.connect() as conn:
                row = self.sources._representation_row(conn, source_ref, str(representation))
                raw = self.sources._read_representation_row(row)
        except Exception as exc:
            raise JournalImportCohortDrift("the retained Source is unavailable") from exc
        if (
            len(raw) != int(file_row["byte_length"])
            or _sha_bytes(raw) != str(file_row["raw_sha256"])
        ):
            raise JournalImportCohortDrift("the retained Source differs from inventory")
        return raw

    @staticmethod
    def _insert_prepared_span(
        conn: sqlite3.Connection,
        cohort_id: str,
        file_id: str,
        span: LegacyJournalSpan,
        mapping: LegacyJournalImportMapping,
    ) -> None:
        target = mapping.targets.get(span.disposition) if span.reason_code is None else None
        materialize = int(target is not None)
        item_id = _item_id(cohort_id, span.logical_id) if target is not None else None
        conn.execute(
            """
            INSERT INTO journal_import_spans(
                cohort_id,logical_id,file_id,disposition,section_key,start_byte,end_byte,
                raw_sha256,normalized_sha256,structural_sha256,managed_projections_json,
                reason_code,materialize,item_id,item_kind,classification_id,
                module_instance_id,module_instance_version,privacy_class,search_mode,
                interaction_behavior_id,interaction_behavior_version,receipt_sha256
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                cohort_id,
                span.logical_id,
                file_id,
                span.disposition,
                span.section_key,
                span.start_byte,
                span.end_byte,
                span.raw_sha256,
                span.normalized_sha256,
                span.structural_sha256,
                _canonical([item.to_dict() for item in span.managed_projections]),
                span.reason_code,
                materialize,
                item_id,
                target.item_kind if target else None,
                (target.classification_id or span.section_key) if target else None,
                target.module_instance_id if target else None,
                target.module_instance_version if target else None,
                target.privacy_class if target else None,
                target.search_mode if target else None,
                target.interaction_behavior_id if target else None,
                target.interaction_behavior_version if target else None,
                _sha_json(span.to_receipt()),
            ),
        )

    @staticmethod
    def _cohort_row(conn: sqlite3.Connection, cohort_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM journal_import_cohorts WHERE cohort_id=?", (cohort_id,)
        ).fetchone()
        if row is None:
            raise JournalImportCohortError("the import cohort is unavailable")
        return row

    @staticmethod
    def _cohort(row: sqlite3.Row) -> JournalImportCohort:
        return JournalImportCohort(
            cohort_id=str(row["cohort_id"]),
            state=str(row["state"]),
            state_revision=int(row["state_revision"]),
            request_sha256=str(row["request_sha256"]),
            inventory_sha256=str(row["inventory_sha256"]),
            mapping_version=str(row["mapping_version"]),
            mapping_sha256=str(row["mapping_sha256"]),
            expected_file_count=int(row["expected_file_count"]),
            expected_byte_count=int(row["expected_byte_count"]),
            expected_span_count=int(row["expected_span_count"]),
            expected_item_count=int(row["expected_item_count"]),
            verified_at=row["verified_at"],
            sealed_at=row["sealed_at"],
            aborted_at=row["aborted_at"],
            abort_code=row["abort_code"],
            seal_sha256=row["seal_sha256"],
            typed_mapping_sha256=(
                None
                if row["typed_mapping_sha256"] is None
                else str(row["typed_mapping_sha256"])
            ),
            expected_observation_count=int(row["expected_observation_count"]),
        )

    @staticmethod
    def _insert_receipt(
        conn: sqlite3.Connection,
        cohort_id: str,
        kind: str,
        subject: str,
        request_sha: str,
        payload: Mapping[str, Any],
        created_at: str,
    ) -> str:
        payload_json = _canonical(dict(payload))
        result_sha = _sha_text(payload_json)
        receipt_id = _receipt_id(cohort_id, kind, subject, request_sha)
        conn.execute(
            "INSERT OR IGNORE INTO journal_import_receipts("
            "receipt_id,cohort_id,receipt_kind,subject_id,request_sha256,result_sha256,"
            "payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                cohort_id,
                kind,
                subject,
                request_sha,
                result_sha,
                payload_json,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT request_sha256,result_sha256,payload_json FROM journal_import_receipts "
            "WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        if row is None or (
            str(row["request_sha256"]) != request_sha
            or str(row["result_sha256"]) != result_sha
            or str(row["payload_json"]) != payload_json
        ):
            raise JournalImportCohortConflict("an import receipt changed during replay")
        return receipt_id

    @staticmethod
    def _upsert_progress(
        conn: sqlite3.Connection,
        cohort_id: str,
        phase: str,
        subject: str,
        request_sha: str,
        state: str,
        receipt_id: str | None,
        error_code: str | None,
        updated_at: str,
    ) -> None:
        existing = conn.execute(
            "SELECT request_sha256 FROM journal_import_progress "
            "WHERE cohort_id=? AND phase=? AND subject_id=?",
            (cohort_id, phase, subject),
        ).fetchone()
        if existing is not None and str(existing["request_sha256"]) != request_sha:
            raise JournalImportCohortConflict("import progress request changed")
        conn.execute(
            """
            INSERT INTO journal_import_progress(
                cohort_id,phase,subject_id,request_sha256,state,attempts,
                receipt_id,error_code,updated_at
            ) VALUES(?,?,?,?,?,1,?,?,?)
            ON CONFLICT(cohort_id,phase,subject_id) DO UPDATE SET
                state=excluded.state,
                attempts=journal_import_progress.attempts+1,
                receipt_id=excluded.receipt_id,
                error_code=excluded.error_code,
                updated_at=excluded.updated_at
            """,
            (
                cohort_id,
                phase,
                subject,
                request_sha,
                state,
                receipt_id,
                error_code,
                updated_at,
            ),
        )

    @staticmethod
    def _transition(
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        to_state: str,
        request_sha: str,
        now: str,
        *,
        abort_code: str | None = None,
        seal_sha: str | None = None,
    ) -> None:
        allowed = {
            "prepared": {"staging", "aborted"},
            "staging": {"verified", "aborted"},
            "verified": {"sealed", "aborted"},
            "sealed": set(),
            "aborted": set(),
        }
        from_state = str(row["state"])
        if to_state not in allowed.get(from_state, set()):
            raise JournalImportCohortStateError(
                f"cannot move an import cohort from {from_state} to {to_state}"
            )
        revision = int(row["state_revision"]) + 1
        cursor = conn.execute(
            """
            UPDATE journal_import_cohorts SET
                state=?,state_revision=?,updated_at=?,
                verified_at=CASE WHEN ?='verified' THEN ? ELSE verified_at END,
                sealed_at=CASE WHEN ?='sealed' THEN ? ELSE sealed_at END,
                aborted_at=CASE WHEN ?='aborted' THEN ? ELSE aborted_at END,
                abort_code=CASE WHEN ?='aborted' THEN ? ELSE abort_code END,
                seal_sha256=CASE WHEN ?='sealed' THEN ? ELSE seal_sha256 END
            WHERE cohort_id=? AND state=? AND state_revision=?
            """,
            (
                to_state,
                revision,
                now,
                to_state,
                now,
                to_state,
                now,
                to_state,
                now,
                to_state,
                abort_code,
                to_state,
                seal_sha,
                row["cohort_id"],
                from_state,
                row["state_revision"],
            ),
        )
        if cursor.rowcount != 1:
            raise JournalImportCohortConflict("the import cohort changed concurrently")
        conn.execute(
            "INSERT INTO journal_import_state_transitions("
            "cohort_id,state_revision,from_state,to_state,request_sha256,actor_json,created_at"
            ") VALUES(?,?,?,?,?,?,?)",
            (
                row["cohort_id"],
                revision,
                from_state,
                to_state,
                request_sha,
                row["actor_json"],
                now,
            ),
        )


__all__ = [
    "IMPORT_SCHEMA",
    "JournalImportCohort",
    "JournalImportCohortConflict",
    "JournalImportCohortDrift",
    "JournalImportCohortError",
    "JournalImportCohortStateError",
    "JournalImportTarget",
    "LegacyJournalImportMapping",
    "LegacyJournalImportService",
]

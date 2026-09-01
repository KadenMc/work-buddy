"""Deterministic, inert Markdown-to-Contracts import cohorts.

The importer only operates on an explicitly supplied source directory. It
freezes every input byte string before parsing, stages normalized records in
SQLite where normal queries cannot see them, and publishes accepted members
only when one cohort seal commits.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from work_buddy.contracts_domain.service import (
    ContractConflict,
    ContractService,
    ContractValidationError,
    VALID_STATUSES,
    _canonical_json,
    _date_value,
    _normalize_payload,
    _sha,
    _stable_id,
    normalize_alias,
)
from work_buddy.contracts_domain.store import ContractStore
from work_buddy.installed_authority import (
    confirm_domain_seal,
    mark_domain_released,
    prepare_domain_seal,
)
from work_buddy.cutover_maintenance import (
    CutoverMaintenanceError,
    IsolatedRehearsalAuthorization,
    mark_postseal_pending,
    pause_cutover_maintenance,
    prior_postseal_release_evidence,
    release_postseal_maintenance,
    resume_preseal_maintenance,
    require_isolated_rehearsal_path,
)
from work_buddy.sources import SourceStore, TrustedIngressContext
from work_buddy.sources.import_dependency import (
    ExactImportSourceError,
    ExactImportSourceService,
)


PARSER_VERSION = 1
IMPORT_SOURCE_PURPOSE = "contracts.history_import"
IMPORT_SOURCE_USE_KIND = "contract_history_import"
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_CHECKBOX_RE = re.compile(r"^- \[([ xX])\] (.+)$", re.MULTILINE)
_KNOWN_SECTIONS = {
    "claim",
    "why it matters",
    "current constraint",
    "must-have evidence",
    "optional / nice-to-have",
    "kill rule",
    "rescope rule",
    "draft threshold",
}


class ContractImportError(RuntimeError):
    pass


class ImportIdempotencyConflict(ContractImportError):
    pass


@dataclass(frozen=True, slots=True)
class FrozenInput:
    source_key: str
    legacy_alias: str
    data: bytes
    sha256: str


def _freeze(root: Path) -> list[FrozenInput]:
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise ContractImportError("contract import source directory does not exist")
    items: list[FrozenInput] = []
    for path in sorted(resolved.glob("*.md"), key=lambda value: value.name.casefold()):
        if not path.is_file():
            continue
        data = path.read_bytes()
        key = path.relative_to(resolved).as_posix()
        items.append(
            FrozenInput(
                source_key=key,
                legacy_alias=key,
                data=data,
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    return items


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    text = text.removeprefix("\ufeff")
    if not text.startswith("---"):
        raise ContractValidationError("frontmatter is required")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ContractValidationError("frontmatter opening delimiter is invalid")
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise ContractValidationError("frontmatter closing delimiter is missing")
    try:
        parsed = yaml.safe_load("".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise ContractValidationError("frontmatter YAML is invalid") from exc
    if not isinstance(parsed, dict):
        raise ContractValidationError("frontmatter must be an object")
    return dict(parsed), "".join(lines[end + 1 :]).lstrip("\r\n")


def _sections(body: str) -> dict[str, str]:
    matches = list(_HEADING_RE.finditer(body))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        key = match.group(2).strip().casefold()
        if key not in _KNOWN_SECTIONS:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        result[key] = body[match.end() : end].strip()
    return result


def _normalized_date(value: Any, label: str) -> str | None:
    if value is None or value == "":
        return None
    return _date_value(value, label)


def _progress(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, str):
        raw = value.strip().removesuffix("%")
        if not raw.isdigit():
            raise ContractValidationError("estimated_progress is invalid")
        value = int(raw)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ContractValidationError("estimated_progress is invalid")
    return value


def _parse_item(item: FrozenInput) -> dict[str, Any]:
    try:
        text = item.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractValidationError("source is not valid UTF-8") from exc
    frontmatter, body = _frontmatter(text)
    title = frontmatter.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ContractValidationError("title is required")
    raw_status = frontmatter.get("status")
    if not isinstance(raw_status, str):
        raise ContractValidationError("status is missing or ambiguous")
    status = raw_status.strip().lower()
    if status not in VALID_STATUSES:
        raise ContractValidationError(f"status {raw_status!r} is invalid or ambiguous")
    contract_type = frontmatter.get("type")
    if not isinstance(contract_type, str) or not contract_type.strip():
        raise ContractValidationError("type is required")
    dates: dict[str, str] = {}
    for key in ("deadline", "last_reviewed", "start_date", "completed_at"):
        value = _normalized_date(frontmatter.get(key), key)
        if value is not None:
            dates[key] = value
    parsed_sections = _sections(body)
    commitments: list[dict[str, Any]] = []
    claim = parsed_sections.get("claim", "").strip()
    if claim:
        commitments.append({"kind": "claim", "text": claim, "state": "open"})
    why = parsed_sections.get("why it matters", "").strip()
    if why:
        commitments.append(
            {"kind": "why_it_matters", "text": why, "state": "open"}
        )
    constraints: list[dict[str, Any]] = []
    current_constraint = frontmatter.get("current_constraint")
    if current_constraint is not None and not isinstance(current_constraint, str):
        raise ContractValidationError("current_constraint is ambiguous")
    if isinstance(current_constraint, str) and current_constraint.strip():
        constraints.append(
            {"kind": "current", "text": current_constraint.strip(), "state": "current"}
        )
    elif parsed_sections.get("current constraint", "").strip():
        constraints.append(
            {
                "kind": "current",
                "text": parsed_sections["current constraint"].strip(),
                "state": "current",
            }
        )
    for section, kind in (
        ("kill rule", "kill_rule"),
        ("rescope rule", "rescope_rule"),
        ("draft threshold", "draft_threshold"),
    ):
        value = parsed_sections.get(section, "").strip()
        if value:
            constraints.append({"kind": kind, "text": value, "state": "current"})
    evidence_links: list[dict[str, Any]] = []
    for section, requirement in (
        ("must-have evidence", "must_have"),
        ("optional / nice-to-have", "optional"),
    ):
        for checkbox in _CHECKBOX_RE.finditer(parsed_sections.get(section, "")):
            task = checkbox.group(2).strip()
            evidence_links.append(
                {
                    "evidence_ref": "legacy-requirement://"
                    + hashlib.sha256(task.encode("utf-8")).hexdigest()[:24],
                    "label": task,
                    "requirement": requirement,
                    "state": "satisfied" if checkbox.group(1).lower() == "x" else "open",
                }
            )
    aliases = [
        {"alias": item.legacy_alias, "kind": "legacy_path"},
        {"alias": Path(item.legacy_alias).stem, "kind": "logical_name"},
    ]
    health: dict[str, Any] = {}
    if frontmatter.get("deadline_type") is not None:
        health["deadline_type"] = str(frontmatter["deadline_type"])
    participants = frontmatter.get("participants", [])
    if isinstance(participants, str):
        participants = [{"display_name": participants, "role": "participant"}]
    elif isinstance(participants, list):
        participants = [
            {"display_name": value, "role": "participant"}
            if isinstance(value, str)
            else value
            for value in participants
        ]
    else:
        raise ContractValidationError("participants is ambiguous")
    return {
        "title": title.strip(),
        "status": status,
        "type": contract_type.strip(),
        "privacy_class": str(frontmatter.get("privacy_class", "private")),
        "estimated_progress": _progress(frontmatter.get("estimated_progress")),
        "aliases": aliases,
        "dates": dates,
        "commitments": commitments,
        "constraints": constraints,
        "health_inputs": health,
        "participants": participants,
        "evidence_links": evidence_links,
        "body_roles": [
            {
                "role": "brief",
                "mode": "plain",
                "plain_body": body,
                "interaction_contract_id": "human_value",
                "interaction_contract_version": 1,
                "privacy_class": str(frontmatter.get("privacy_class", "private")),
            }
        ],
    }


class ContractImporter:
    def __init__(
        self,
        store: ContractStore,
        source_store: SourceStore | None = None,
        source_committed: Callable[[str, str], None] | None = None,
    ) -> None:
        self.store = store
        self.service = ContractService(store)
        self.sources = source_store
        self.source_dependencies = (
            ExactImportSourceService(
                source_store,
                purpose=IMPORT_SOURCE_PURPOSE,
                consumer_domain="contracts",
                use_kind=IMPORT_SOURCE_USE_KIND,
            )
            if source_store is not None
            else None
        )
        self._source_committed = source_committed or (
            lambda _cohort_id, _source_key: None
        )

    @staticmethod
    def _actor_sha256(actor: str) -> str:
        if not isinstance(actor, str) or not actor.strip():
            raise ContractImportError("import actor is required")
        return _sha({"actor": actor})

    def pause_mutations(
        self,
        *,
        cohort_id: str,
        inventory_sha256: str,
        mutation_id: str,
        actor: str,
    ) -> dict[str, Any]:
        """Fence ordinary Contract writes before final cutover staging."""

        actor_sha256 = self._actor_sha256(actor)
        with self.store.write_transaction() as connection:
            authority = connection.execute(
                "SELECT state FROM contract_authority WHERE singleton=1"
            ).fetchone()
            if authority is None or authority["state"] != "legacy":
                raise ContractImportError(
                    "Contracts cannot enter preseal maintenance after authority cutover"
                )
            return pause_cutover_maintenance(
                connection,
                domain="contracts",
                cohort_id=cohort_id,
                inventory_sha256=inventory_sha256,
                mutation_id=mutation_id,
                actor_sha256=actor_sha256,
            )

    def resume_preseal_mutations(
        self,
        *,
        cohort_id: str,
        mutation_id: str,
        actor: str,
    ) -> dict[str, Any]:
        """Release a preseal fence; sealed authority can only roll forward."""

        actor_sha256 = self._actor_sha256(actor)
        with self.store.write_transaction() as connection:
            authority = connection.execute(
                "SELECT state FROM contract_authority WHERE singleton=1"
            ).fetchone()
            if authority is None or authority["state"] != "legacy":
                raise ContractImportError(
                    "sealed Contracts maintenance cannot resume"
                )
            return resume_preseal_maintenance(
                connection,
                domain="contracts",
                cohort_id=cohort_id,
                mutation_id=mutation_id,
                actor_sha256=actor_sha256,
            )

    def release_postseal_mutations(
        self,
        *,
        cohort_id: str,
        mutation_id: str,
        actor: str,
        checkpoint_evidence_path: str | Path | None = None,
        search_evidence_path: str | Path | None = None,
        detachment_evidence_path: str | Path | None = None,
        rehearsal_evidence_sha256s: Mapping[str, str] | None = None,
        allow_unvalidated_rehearsal: bool = False,
        rehearsal_authorization: IsolatedRehearsalAuthorization | None = None,
    ) -> dict[str, Any]:
        """Release native writes after the exact search evidence set is durable."""

        actor_sha256 = self._actor_sha256(actor)
        with self.store.write_transaction() as connection:
            authority = connection.execute(
                "SELECT state,sealed_cohort_id FROM contract_authority WHERE singleton=1"
            ).fetchone()
            if (
                authority is None
                or authority["state"] != "native"
                or authority["sealed_cohort_id"] != cohort_id
            ):
                raise ContractImportError(
                    "Contracts postseal maintenance is unavailable"
                )
            prior_evidence = prior_postseal_release_evidence(
                connection, mutation_id=mutation_id
            )
            if allow_unvalidated_rehearsal:
                require_isolated_rehearsal_path(
                    self.store.path,
                    domain="contracts",
                    authorization=rehearsal_authorization,
                )
                if rehearsal_evidence_sha256s is None:
                    raise ContractImportError("Contracts rehearsal evidence is missing")
                evidence = dict(rehearsal_evidence_sha256s)
                if set(evidence) != {"databaseCheckpoint", "search", "detachment"}:
                    raise CutoverMaintenanceError("postseal evidence is incomplete")
                evidence["authorityHead"] = (
                    prior_evidence["authorityHead"]
                    if prior_evidence is not None
                    else hashlib.sha256(self.store.path.read_bytes()).hexdigest()
                )
            else:
                if (
                    checkpoint_evidence_path is None
                    or search_evidence_path is None
                    or detachment_evidence_path is None
                    or rehearsal_evidence_sha256s is not None
                    or rehearsal_authorization is not None
                ):
                    raise ContractImportError(
                        "Contracts configured postseal evidence is required"
                    )
                from work_buddy.cutover_release import (
                    hash_supplied_postseal_evidence,
                    validate_configured_postseal_evidence,
                )

                if prior_evidence is not None:
                    evidence = hash_supplied_postseal_evidence(
                        checkpoint_evidence_path=checkpoint_evidence_path,
                        search_evidence_path=search_evidence_path,
                        detachment_evidence_path=detachment_evidence_path,
                    )
                    evidence["authorityHead"] = prior_evidence["authorityHead"]
                else:
                    evidence = validate_configured_postseal_evidence(
                        domain="contracts",
                        authority_db_path=self.store.path,
                        checkpoint_evidence_path=checkpoint_evidence_path,
                        search_evidence_path=search_evidence_path,
                        detachment_evidence_path=detachment_evidence_path,
                    )
            result = release_postseal_maintenance(
                connection,
                domain="contracts",
                cohort_id=cohort_id,
                mutation_id=mutation_id,
                actor_sha256=actor_sha256,
                evidence_sha256s=evidence,
            )
        mark_domain_released(
            "contracts", self.store.path, cohort_id=cohort_id
        )
        return result

    @staticmethod
    def _receipt_for_intent(
        connection, intent_id: str, *, actor_ref: str | None = None
    ) -> tuple[str, dict[str, Any]] | None:
        row = connection.execute(
            "SELECT * FROM contract_import_receipts WHERE intent_id=?", (intent_id,)
        ).fetchone()
        if row is None:
            return None
        if actor_ref is not None and row["actor_ref"] != actor_ref:
            raise ImportIdempotencyConflict(
                "import intent_id belongs to another actor"
            )
        result = json.loads(str(row["result_json"]))
        if _sha(result) != row["result_sha256"]:
            raise ContractImportError("import receipt digest mismatch")
        return str(row["operation"]), result

    @staticmethod
    def _receipt_replay(
        connection,
        intent_id: str,
        request_sha256: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM contract_import_receipts WHERE intent_id=?", (intent_id,)
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_sha256:
            raise ImportIdempotencyConflict(
                "import intent_id was already used for another request"
            )
        result = json.loads(str(row["result_json"]))
        if _sha(result) != row["result_sha256"]:
            raise ContractImportError("import receipt digest mismatch")
        return result

    @staticmethod
    def _write_receipt(
        connection,
        *,
        intent_id: str,
        cohort_id: str,
        operation: str,
        request_sha256: str,
        result: Mapping[str, Any],
        actor_ref: str,
        created_at: str,
    ) -> None:
        result_json = _canonical_json(result)
        connection.execute(
            "INSERT INTO contract_import_receipts "
            "(receipt_id,intent_id,cohort_id,operation,request_sha256,result_json,"
            "result_sha256,actor_ref,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                _stable_id("contract-import-receipt", [intent_id, request_sha256]),
                intent_id,
                cohort_id,
                operation,
                request_sha256,
                result_json,
                hashlib.sha256(result_json.encode("utf-8")).hexdigest(),
                actor_ref,
                created_at,
            ),
        )

    def stage(
        self,
        source_root: str | Path,
        *,
        actor: str,
        intent_id: str,
        source_label: str | None = None,
        ingress_context: TrustedIngressContext | None = None,
    ) -> dict[str, Any]:
        if not isinstance(actor, str) or not actor.strip():
            raise ContractImportError("import actor is required")
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise ContractImportError("import intent_id is required")
        if self.store.is_native_authority():
            with self.store.read_transaction() as connection:
                prior = self._receipt_for_intent(
                    connection, intent_id, actor_ref=actor
                )
            if prior is not None and prior[0] == "stage":
                return prior[1]
            raise ContractImportError(
                "Contracts authority is already sealed; Markdown import is disabled"
            )
        pending_receipt = None
        with self.store.read_transaction() as connection:
            pending = connection.execute(
                "SELECT * FROM contract_import_cohorts WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            if pending is not None:
                pending_receipt = self._receipt_for_intent(
                    connection, intent_id, actor_ref=actor
                )
        if pending is not None:
            if pending["actor_ref"] != actor:
                raise ImportIdempotencyConflict(
                    "import intent_id belongs to another actor"
                )
            if pending_receipt is not None and pending_receipt[0] == "stage":
                return pending_receipt[1]
            if ingress_context is None:
                raise ContractImportError(
                    "contract import requires trusted ingress context"
                )
            source_dependencies = self._require_source_dependencies(ingress_context)
            self._stage_sources(str(pending["cohort_id"]), ingress_context, source_dependencies)
            result = self._stage_result(pending)
            with self.store.write_transaction() as connection:
                replay = self._receipt_replay(
                    connection, intent_id, str(pending["request_sha256"])
                )
                if replay is not None:
                    return replay
                self._write_receipt(
                    connection,
                    intent_id=intent_id,
                    cohort_id=str(pending["cohort_id"]),
                    operation="stage",
                    request_sha256=str(pending["request_sha256"]),
                    result=result,
                    actor_ref=actor,
                    created_at=str(pending["created_at"]),
                )
            return result
        if ingress_context is None:
            raise ContractImportError(
                "contract import requires trusted ingress context"
            )
        source_dependencies = self._require_source_dependencies(ingress_context)
        root = Path(source_root)
        frozen = _freeze(root)
        label = source_label or root.name or "legacy-contracts"
        if not isinstance(label, str) or not label.strip():
            raise ContractImportError("import source_label is required")
        inventory_payload = [
            {
                "source_key": item.source_key,
                "source_sha256": item.sha256,
                "byte_length": len(item.data),
            }
            for item in frozen
        ]
        inventory_sha256 = _sha(inventory_payload)
        cohort_descriptor = {
            "operation": "stage",
            "parser_version": PARSER_VERSION,
            "source_label": label,
            "inventory_sha256": inventory_sha256,
        }
        request = {**cohort_descriptor, "actor_ref": actor}
        request_sha256 = _sha(request)
        cohort_id = _stable_id("contract-import-cohort", cohort_descriptor)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

        parsed: dict[str, dict[str, Any]] = {}
        dispositions: dict[str, tuple[str, str | None, str | None]] = {}
        entity_ids: dict[str, str | None] = {}
        alias_members: dict[str, list[str]] = defaultdict(list)
        for item in frozen:
            if Path(item.source_key).name.startswith("_"):
                dispositions[item.source_key] = ("ignored", None, None)
                entity_ids[item.source_key] = None
                continue
            try:
                record = _parse_item(item)
                identifier = _stable_id("legacy-contract-entity", item.source_key)
                _normalize_payload(
                    record,
                    contract_id=identifier,
                    revision=1,
                    created_at="1970-01-01T00:00:00.000+00:00",
                    updated_at="1970-01-01T00:00:00.000+00:00",
                )
                parsed[item.source_key] = record
                entity_ids[item.source_key] = identifier
                dispositions[item.source_key] = ("accepted", None, None)
                for alias in record["aliases"]:
                    alias_members[normalize_alias(alias["alias"])].append(item.source_key)
            except (ContractValidationError, OSError) as exc:
                dispositions[item.source_key] = (
                    "quarantined",
                    "invalid_legacy_contract",
                    str(exc),
                )
                entity_ids[item.source_key] = None
        for alias, members in alias_members.items():
            if len(members) <= 1:
                continue
            for source_key in members:
                dispositions[source_key] = (
                    "quarantined",
                    "ambiguous_legacy_alias",
                    f"legacy alias {alias!r} belongs to multiple inputs",
                )
                entity_ids[source_key] = None
                parsed.pop(source_key, None)

        accepted = sum(value[0] == "accepted" for value in dispositions.values())
        quarantined = sum(value[0] == "quarantined" for value in dispositions.values())
        ignored = sum(value[0] == "ignored" for value in dispositions.values())
        result = {
            "schema": "wb.contract-import-stage-result/v1",
            "cohort_id": cohort_id,
            "state": "staged",
            "inventory_sha256": inventory_sha256,
            "item_count": len(frozen),
            "accepted_count": accepted,
            "quarantined_count": quarantined,
            "ignored_count": ignored,
        }
        with self.store.write_transaction() as connection:
            replay = self._receipt_replay(connection, intent_id, request_sha256)
            if replay is not None:
                return replay
            existing = connection.execute(
                "SELECT request_sha256,state FROM contract_import_cohorts WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha256:
                    raise ImportIdempotencyConflict("import cohort digest conflict")
                if existing["state"] == "sealed":
                    result = {**result, "state": "sealed"}
            else:
                connection.execute(
                    "INSERT INTO contract_import_cohorts "
                    "(cohort_id,intent_id,state,parser_version,inventory_sha256,"
                    "request_sha256,source_label,item_count,accepted_count,"
                    "quarantined_count,ignored_count,actor_ref,created_at) "
                    "VALUES (?,?,'staged',?,?,?,?,?,?,?,?,?,?)",
                    (
                        cohort_id,
                        intent_id,
                        PARSER_VERSION,
                        inventory_sha256,
                        request_sha256,
                        label,
                        len(frozen),
                        accepted,
                        quarantined,
                        ignored,
                        actor,
                        timestamp,
                    ),
                )
                for item in frozen:
                    disposition, code, detail = dispositions[item.source_key]
                    connection.execute(
                        "INSERT INTO contract_import_inventory "
                        "(cohort_id,source_key,legacy_alias,source_sha256,byte_length,"
                        "frozen_bytes,disposition,quarantine_code,quarantine_detail,entity_id) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            cohort_id,
                            item.source_key,
                            item.legacy_alias,
                            item.sha256,
                            len(item.data),
                            item.data,
                            disposition,
                            code,
                            detail,
                            entity_ids[item.source_key],
                        ),
                    )
                    if disposition == "accepted":
                        record_json = _canonical_json(parsed[item.source_key])
                        connection.execute(
                            "INSERT INTO contract_import_stage VALUES (?,?,?,?,?)",
                            (
                                cohort_id,
                                item.source_key,
                                entity_ids[item.source_key],
                                record_json,
                                hashlib.sha256(record_json.encode("utf-8")).hexdigest(),
                            ),
                        )
                    connection.execute(
                        "INSERT INTO contract_import_source_dependencies "
                        "(cohort_id,source_key,ingress_client_mutation_id,"
                        "source_usage_consumer_id) VALUES (?,?,?,?)",
                        (
                            cohort_id,
                            item.source_key,
                            self._ingress_mutation_id(cohort_id, item.source_key),
                            self._source_consumer_id(cohort_id, item.source_key),
                        ),
                    )
        self._stage_sources(cohort_id, ingress_context, source_dependencies)
        with self.store.write_transaction() as connection:
            replay = self._receipt_replay(connection, intent_id, request_sha256)
            if replay is not None:
                return replay
            self._write_receipt(
                connection,
                intent_id=intent_id,
                cohort_id=cohort_id,
                operation="stage",
                request_sha256=request_sha256,
                result=result,
                actor_ref=actor,
                created_at=timestamp,
            )
        return result

    def verify(self, cohort_id: str) -> dict[str, Any]:
        with self.store.read_transaction() as connection:
            cohort = connection.execute(
                "SELECT * FROM contract_import_cohorts WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if cohort is None:
                raise ContractImportError("import cohort does not exist")
        self._verify_sources(cohort_id)
        return {
            "schema": "wb.contract-import-source-verification/v1",
            "cohort_id": cohort_id,
            "inventory_sha256": str(cohort["inventory_sha256"]),
            "source_count": int(cohort["item_count"]),
            "source_acknowledged_count": int(cohort["item_count"]),
        }

    def seal(
        self,
        cohort_id: str,
        *,
        expected_inventory_sha256: str,
        actor: str,
        intent_id: str,
        coordinator_decision_id: str | None = None,
        coordinator_decision_sha256: str | None = None,
        retain_maintenance_fence: bool = False,
        allow_unfenced_rehearsal: bool = False,
        rehearsal_authorization: IsolatedRehearsalAuthorization | None = None,
    ) -> dict[str, Any]:
        if not isinstance(actor, str) or not actor.strip():
            raise ContractImportError("import actor is required")
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise ContractImportError("import intent_id is required")
        if not isinstance(retain_maintenance_fence, bool):
            raise ContractImportError("retain_maintenance_fence must be boolean")
        if not isinstance(allow_unfenced_rehearsal, bool):
            raise ContractImportError("allow_unfenced_rehearsal must be boolean")
        if retain_maintenance_fence and allow_unfenced_rehearsal:
            raise ContractImportError("Contracts seal modes are mutually exclusive")
        if allow_unfenced_rehearsal:
            require_isolated_rehearsal_path(
                self.store.path,
                domain="contracts",
                authorization=rehearsal_authorization,
            )
        elif rehearsal_authorization is not None:
            raise ContractImportError(
                "Contracts rehearsal authorization requires rehearsal mode"
            )
        decision_id = coordinator_decision_id or _stable_id(
            "contract-import-decision", [cohort_id, expected_inventory_sha256]
        )
        if not isinstance(decision_id, str) or not decision_id.strip():
            raise ContractImportError("coordinator decision identity is required")
        decision_sha = coordinator_decision_sha256 or _sha(
            {
                "cohort_id": cohort_id,
                "inventory_sha256": expected_inventory_sha256,
                "decision_id": decision_id,
                "decision": "publish_native_contract_authority",
            }
        )
        if (
            not isinstance(decision_sha, str)
            or re.fullmatch(r"[0-9a-f]{64}", decision_sha) is None
        ):
            raise ContractImportError("coordinator decision digest must be lowercase SHA-256")
        request = {
            "operation": "seal",
            "cohort_id": cohort_id,
            "expected_inventory_sha256": expected_inventory_sha256,
            "coordinator_decision_id": decision_id,
            "coordinator_decision_sha256": decision_sha,
            "actor_ref": actor,
        }
        if retain_maintenance_fence:
            request["retain_maintenance_fence"] = True
        if allow_unfenced_rehearsal:
            request["allow_unfenced_rehearsal"] = True
        request_sha256 = _sha(request)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self.store.write_transaction() as connection:
            replay = self._receipt_replay(connection, intent_id, request_sha256)
            if replay is not None:
                prepare_domain_seal(
                    "contracts", self.store.path, cohort_id=cohort_id
                )
                confirm_domain_seal(
                    "contracts", self.store.path, cohort_id=cohort_id
                )
                return replay
            cohort = connection.execute(
                "SELECT * FROM contract_import_cohorts WHERE cohort_id=?", (cohort_id,)
            ).fetchone()
            if cohort is None:
                raise ContractImportError("import cohort does not exist")
            if cohort["inventory_sha256"] != expected_inventory_sha256:
                raise ContractImportError("import inventory changed before seal")
            authority = connection.execute(
                "SELECT * FROM contract_authority WHERE singleton=1"
            ).fetchone()
            if authority["state"] == "native" and authority["sealed_cohort_id"] != cohort_id:
                raise ContractImportError("another cohort already owns Contracts authority")
            maintenance = connection.execute(
                "SELECT state,cohort_id,inventory_sha256 FROM cutover_maintenance "
                "WHERE singleton=1"
            ).fetchone()
            if maintenance is None:
                raise ContractImportError("Contracts cutover maintenance is unavailable")
            if retain_maintenance_fence:
                if (
                    maintenance["state"] != "preseal_fenced"
                    or maintenance["cohort_id"] != cohort_id
                    or maintenance["inventory_sha256"] != expected_inventory_sha256
                ):
                    raise ContractImportError(
                        "Contracts cutover maintenance does not match the sealed cohort"
                    )
            elif maintenance["state"] != "open":
                raise ContractImportError(
                    "an active Contracts cutover fence must remain held through seal"
                )
            elif not allow_unfenced_rehearsal:
                raise ContractImportError(
                    "Contracts authority sealing requires preseal maintenance"
                )
            if cohort["state"] == "sealed":
                seal = connection.execute(
                    "SELECT * FROM contract_import_seals WHERE cohort_id=?", (cohort_id,)
                ).fetchone()
                if (
                    seal is None
                    or seal["coordinator_decision_id"] != decision_id
                    or seal["coordinator_decision_sha256"] != decision_sha
                ):
                    raise ContractImportError(
                        "sealed cohort coordinator decision cannot be changed"
                    )

            dependencies = connection.execute(
                "SELECT d.*,i.source_sha256,i.byte_length "
                "FROM contract_import_source_dependencies d "
                "JOIN contract_import_inventory i USING(cohort_id,source_key) "
                "WHERE d.cohort_id=? ORDER BY d.source_key",
                (cohort_id,),
            ).fetchall()
            if cohort["state"] == "staged":
                if len(dependencies) != int(cohort["item_count"]) or any(
                    row["source_usage_state"] != "acknowledged" for row in dependencies
                ):
                    raise ContractImportError(
                        "contract cohort has incomplete Source dependencies"
                    )
                self._verify_source_rows(dependencies)

            inventory = connection.execute(
                "SELECT * FROM contract_import_inventory WHERE cohort_id=? "
                "ORDER BY source_key",
                (cohort_id,),
            ).fetchall()
            descriptor = []
            for item in inventory:
                frozen = bytes(item["frozen_bytes"])
                if hashlib.sha256(frozen).hexdigest() != item["source_sha256"]:
                    raise ContractImportError("frozen import input digest mismatch")
                descriptor.append(
                    {
                        "source_key": item["source_key"],
                        "source_sha256": item["source_sha256"],
                        "byte_length": item["byte_length"],
                    }
                )
            if _sha(descriptor) != expected_inventory_sha256:
                raise ContractImportError("import inventory manifest digest mismatch")

            if cohort["state"] == "staged":
                stage_rows = connection.execute(
                    "SELECT s.*, i.source_sha256,d.source_ref "
                    "FROM contract_import_stage s "
                    "JOIN contract_import_inventory i USING(cohort_id,source_key) "
                    "JOIN contract_import_source_dependencies d "
                    "USING(cohort_id,source_key) "
                    "WHERE s.cohort_id=? ORDER BY s.source_key",
                    (cohort_id,),
                ).fetchall()
                for row in stage_rows:
                    record_json = str(row["record_json"])
                    if hashlib.sha256(record_json.encode("utf-8")).hexdigest() != row[
                        "record_sha256"
                    ]:
                        raise ContractImportError("staged contract digest mismatch")
                    record = json.loads(record_json)
                    try:
                        self.service.create(
                            record,
                            actor=actor,
                            intent_id=f"contract-import:{cohort_id}:{row['source_key']}",
                            source_ref=str(row["source_ref"]),
                            contract_id=str(row["contract_id"]),
                            operation="legacy_import",
                            enforce_wip=False,
                            connection=connection,
                        )
                    except ContractConflict as exc:
                        raise ContractImportError(
                            "staged contract conflicts with current authority"
                        ) from exc
                seal_id = _stable_id(
                    "contract-import-seal", [cohort_id, expected_inventory_sha256, decision_sha]
                )
                connection.execute(
                    "INSERT INTO contract_import_seals "
                    "(seal_id,cohort_id,inventory_sha256,coordinator_decision_id,"
                    "coordinator_decision_sha256,actor_ref,sealed_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        seal_id,
                        cohort_id,
                        expected_inventory_sha256,
                        decision_id,
                        decision_sha,
                        actor,
                        timestamp,
                    ),
                )
                connection.execute(
                    "UPDATE contract_import_cohorts SET state='sealed',sealed_at=? "
                    "WHERE cohort_id=? AND state='staged'",
                    (timestamp, cohort_id),
                )
                prepare_domain_seal(
                    "contracts", self.store.path, cohort_id=cohort_id
                )
                connection.execute(
                    "UPDATE contract_authority SET state='native', "
                    "authority_epoch=authority_epoch+1,"
                    "sealed_cohort_id=?,coordinator_decision_id=?,"
                    "coordinator_decision_sha256=?,sealed_at=? WHERE singleton=1",
                    (cohort_id, decision_id, decision_sha, timestamp),
                )
                if retain_maintenance_fence:
                    mark_postseal_pending(
                        connection,
                        domain="contracts",
                        cohort_id=cohort_id,
                        inventory_sha256=expected_inventory_sha256,
                        at=timestamp,
                    )
            result = {
                "schema": "wb.contract-import-seal-result/v1",
                "cohort_id": cohort_id,
                "state": "sealed",
                "inventory_sha256": expected_inventory_sha256,
                "accepted_count": int(cohort["accepted_count"]),
                "quarantined_count": int(cohort["quarantined_count"]),
                "ignored_count": int(cohort["ignored_count"]),
                "authority": "native",
            }
            self._write_receipt(
                connection,
                intent_id=intent_id,
                cohort_id=cohort_id,
                operation="seal",
                request_sha256=request_sha256,
                result=result,
                actor_ref=actor,
                created_at=timestamp,
            )
        confirm_domain_seal(
            "contracts", self.store.path, cohort_id=cohort_id
        )
        return result

    def _stage_sources(
        self,
        cohort_id: str,
        context: TrustedIngressContext,
        service: ExactImportSourceService,
    ) -> None:
        with self.store.read_transaction() as connection:
            rows = connection.execute(
                "SELECT d.*,i.source_sha256,i.byte_length,i.frozen_bytes "
                "FROM contract_import_source_dependencies d "
                "JOIN contract_import_inventory i USING(cohort_id,source_key) "
                "WHERE d.cohort_id=? ORDER BY d.source_key",
                (cohort_id,),
            ).fetchall()
        for row in rows:
            if row["source_usage_state"] == "released":
                raise ContractImportError(
                    "a contract import Source dependency was released"
                )
            try:
                if row["source_ref"] is None:
                    raw = bytes(row["frozen_bytes"])
                    if (
                        len(raw) != int(row["byte_length"])
                        or hashlib.sha256(raw).hexdigest() != str(row["source_sha256"])
                    ):
                        raise ContractImportError(
                            "frozen contract input changed before Source retention"
                        )
                    binding = service.retain(
                        exact_content=raw,
                        client_mutation_id=str(row["ingress_client_mutation_id"]),
                        consumer_id=str(row["source_usage_consumer_id"]),
                        context=context,
                        source_committed=lambda _commit, key=str(row["source_key"]): (
                            self._source_committed(cohort_id, key)
                        ),
                    )
                else:
                    binding = service.reconcile(
                        source_ref=str(row["source_ref"]),
                        representation_id=str(row["representation_id"]),
                        consumer_id=str(row["source_usage_consumer_id"]),
                        context=context,
                    )
                if row["source_usage_id"] is not None and str(
                    row["source_usage_id"]
                ) != binding.usage_id:
                    raise ContractImportError(
                        "a contract import Source dependency changed"
                    )
                now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                with self.store.write_transaction() as connection:
                    connection.execute(
                        "UPDATE contract_import_source_dependencies SET "
                        "source_ref=?,representation_id=?,"
                        "submission_id=COALESCE(?,submission_id),"
                        "source_usage_id=?,source_usage_state='reserved',"
                        "retained_at=COALESCE(retained_at,?) "
                        "WHERE cohort_id=? AND source_key=? "
                        "AND source_usage_state IN ('unreserved','reserved')",
                        (
                            binding.source_ref,
                            binding.representation_id,
                            binding.submission_id,
                            binding.usage_id,
                            now,
                            cohort_id,
                            row["source_key"],
                        ),
                    )
                service.acknowledge(binding.usage_id)
                with self.store.write_transaction() as connection:
                    connection.execute(
                        "UPDATE contract_import_source_dependencies SET "
                        "source_usage_state='acknowledged',"
                        "acknowledged_at=COALESCE(acknowledged_at,?) "
                        "WHERE cohort_id=? AND source_key=? AND source_usage_id=? "
                        "AND source_usage_state IN ('reserved','acknowledged')",
                        (now, cohort_id, row["source_key"], binding.usage_id),
                    )
            except ExactImportSourceError as exc:
                raise ContractImportError(str(exc)) from exc

    def _verify_sources(self, cohort_id: str) -> None:
        with self.store.read_transaction() as connection:
            cohort = connection.execute(
                "SELECT item_count FROM contract_import_cohorts WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT d.*,i.source_sha256,i.byte_length "
                "FROM contract_import_source_dependencies d "
                "JOIN contract_import_inventory i USING(cohort_id,source_key) "
                "WHERE d.cohort_id=? ORDER BY d.source_key",
                (cohort_id,),
            ).fetchall()
        if cohort is None or len(rows) != int(cohort["item_count"]) or any(
            row["source_usage_state"] != "acknowledged" for row in rows
        ):
            raise ContractImportError(
                "contract cohort has incomplete Source dependencies"
            )
        self._verify_source_rows(rows)

    def _verify_source_rows(self, rows) -> None:
        service = self._require_source_dependencies(None)
        for row in rows:
            try:
                service.verify_exact(
                    source_ref=str(row["source_ref"]),
                    representation_id=str(row["representation_id"]),
                    expected_sha256=str(row["source_sha256"]),
                    expected_byte_length=int(row["byte_length"]),
                )
            except ExactImportSourceError as exc:
                raise ContractImportError(str(exc)) from exc

    def _require_source_dependencies(
        self, context: TrustedIngressContext | None
    ) -> ExactImportSourceService:
        if self.source_dependencies is None:
            raise ContractImportError(
                "contract import requires an isolated Sources authority"
            )
        if context is None:
            return self.source_dependencies
        if IMPORT_SOURCE_PURPOSE not in context.permitted_purposes:
            raise ContractImportError(
                "trusted ingress does not permit contract history import"
            )
        return self.source_dependencies

    @staticmethod
    def _ingress_mutation_id(cohort_id: str, source_key: str) -> str:
        digest = _sha({"cohort_id": cohort_id, "source_key": source_key})
        return f"contract-history-import:{cohort_id}:{digest[:24]}"

    @staticmethod
    def _source_consumer_id(cohort_id: str, source_key: str) -> str:
        digest = _sha({"cohort_id": cohort_id, "source_key": source_key})
        return f"contract-import:{cohort_id}:{digest[:24]}"

    @staticmethod
    def _stage_result(cohort) -> dict[str, Any]:
        return {
            "schema": "wb.contract-import-stage-result/v1",
            "cohort_id": str(cohort["cohort_id"]),
            "state": "sealed" if cohort["state"] == "sealed" else "staged",
            "inventory_sha256": str(cohort["inventory_sha256"]),
            "item_count": int(cohort["item_count"]),
            "accepted_count": int(cohort["accepted_count"]),
            "quarantined_count": int(cohort["quarantined_count"]),
            "ignored_count": int(cohort["ignored_count"]),
        }

    def quarantine(self, cohort_id: str) -> list[dict[str, Any]]:
        with self.store.read_transaction() as connection:
            return [
                {
                    "source_key": row["source_key"],
                    "source_sha256": row["source_sha256"],
                    "code": row["quarantine_code"],
                    "detail": row["quarantine_detail"],
                }
                for row in connection.execute(
                    "SELECT * FROM contract_import_inventory "
                    "WHERE cohort_id=? AND disposition='quarantined' ORDER BY source_key",
                    (cohort_id,),
                )
            ]


__all__ = [
    "ContractImportError",
    "ContractImporter",
    "ImportIdempotencyConflict",
    "PARSER_VERSION",
]

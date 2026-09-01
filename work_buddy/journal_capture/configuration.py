"""Journal-owned immutable profile configuration and preview service."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from typing import Any, Mapping, Sequence

from work_buddy.journal_capture.domain import _canonical, _now, _schedule_membership, _sha
from work_buddy.journal_capture.models import (
    JournalCaptureConflict,
    JournalCaptureValidationError,
)
from work_buddy.journal_capture.interaction_policy import (
    ai_contribution_allowed,
    module_requires_ai_contribution,
)
from work_buddy.journal_capture.store import JournalCaptureStore


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_USER_ID_PREFIX = "user."
_VALUE_KINDS = {
    "short_text", "long_text", "number", "scale", "boolean", "single_select",
    "multi_select", "local_time", "instant", "date", "duration", "reference",
}
_SCHEDULE_KINDS = {"always", "weekdays", "date_range", "manual_only"}
_REQUIREDNESS = {"optional", "required"}
_PRIVACY_CLASSES = {"private", "sensitive", "internal"}
_SEARCH_MODES = {
    "structured_only", "lexical", "dense", "lexical_dense", "excluded",
}


def _text(value: Any, label: str, *, maximum: int = 160, required: bool = True) -> str:
    if not isinstance(value, str):
        raise JournalCaptureValidationError(f"{label} must be text.")
    result = value.strip()
    if required and not result:
        raise JournalCaptureValidationError(f"{label} is required.")
    if len(result) > maximum:
        raise JournalCaptureValidationError(f"{label} is too long.")
    return result


def _identifier(value: Any, label: str) -> str:
    result = _text(value, label, maximum=128)
    if _ID.fullmatch(result) is None:
        raise JournalCaptureValidationError(f"{label} is invalid.")
    return result


def _user_owned_identifier(value: Any, label: str) -> str:
    result = _identifier(value, label)
    if not result.startswith(_USER_ID_PREFIX):
        raise JournalCaptureValidationError(
            f"{label} is reserved. Fork it to a user-owned identity before editing."
        )
    return result


def _is_user_owned_identifier(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_USER_ID_PREFIX)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise JournalCaptureValidationError(f"{label} is invalid.")
    return dict(value)


def _sequence(value: Any, label: str, *, maximum: int) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise JournalCaptureValidationError(f"{label} is invalid.")
    if len(value) > maximum:
        raise JournalCaptureValidationError(f"{label} has too many entries.")
    if any(not isinstance(item, Mapping) for item in value):
        raise JournalCaptureValidationError(f"{label} is invalid.")
    return list(value)


def _version(value: Any, label: str, *, allow_zero: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise JournalCaptureValidationError(f"{label} is invalid.")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise JournalCaptureValidationError(f"{label} is invalid.")
    return value


def _schedule(kind_value: Any, value: Any) -> tuple[str, dict[str, Any]]:
    kind = _text(kind_value or "always", "Schedule", maximum=32)
    if kind not in _SCHEDULE_KINDS:
        raise JournalCaptureValidationError("That Journal schedule is unsupported.")
    settings = _mapping(value, "Schedule settings")
    if kind == "weekdays":
        weekdays = settings.get("weekdays")
        if not isinstance(weekdays, list) or not weekdays or any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0 or item > 6
            for item in weekdays
        ):
            raise JournalCaptureValidationError("Choose at least one valid weekday.")
        settings = {"weekdays": sorted(set(weekdays))}
    elif kind == "date_range":
        normalized: dict[str, str] = {}
        for key in ("start", "end"):
            candidate = settings.get(key)
            if candidate in (None, ""):
                continue
            try:
                normalized[key] = date.fromisoformat(str(candidate)).isoformat()
            except ValueError as exc:
                raise JournalCaptureValidationError("The Journal date range is invalid.") from exc
        if normalized.get("start", "0001-01-01") > normalized.get("end", "9999-12-31"):
            raise JournalCaptureValidationError("The Journal date range is reversed.")
        settings = normalized
    else:
        settings = {}
    return kind, settings


class JournalProfileConfigurationService:
    """Build immutable profile revisions from bounded, typed user drafts."""

    def __init__(self, store: JournalCaptureStore) -> None:
        self.store = store

    def catalog(self) -> dict[str, Any]:
        with self.store._connect() as conn:
            profiles = conn.execute(
                "SELECT * FROM journal_profile_revisions AS profile WHERE "
                "profile.import_cohort_id IS NULL OR ("
                "EXISTS(SELECT 1 FROM journal_import_cohorts AS cohort "
                "WHERE cohort.cohort_id=profile.import_cohort_id "
                "AND cohort.state='sealed') AND EXISTS(SELECT 1 "
                "FROM journal_authority_control AS authority "
                "WHERE authority.singleton=1 AND authority.mode='database_only')) "
                "ORDER BY profile_id,profile_revision DESC"
            ).fetchall()
            authority = conn.execute(
                "SELECT value FROM journal_domain_state WHERE key='content_authority'"
            ).fetchone()
            activation = int(conn.execute(
                "SELECT COALESCE(MAX(activation_revision),0) "
                "FROM journal_profile_activation_epochs "
                "WHERE import_cohort_id IS NULL"
            ).fetchone()[0])
            module_types = [
                {
                    "moduleTypeId": row["module_type_id"],
                    "moduleTypeVersion": int(row["module_type_version"]),
                    "definition": json.loads(row["definition_json"]),
                }
                for row in conn.execute(
                    "SELECT t.* FROM journal_module_type_revisions t "
                    "JOIN (SELECT module_type_id,MAX(module_type_version) AS version "
                    "FROM journal_module_type_revisions GROUP BY module_type_id) latest "
                    "ON latest.module_type_id=t.module_type_id "
                    "AND latest.version=t.module_type_version ORDER BY t.module_type_id"
                ).fetchall()
            ]
            behaviors = [
                {
                    "behaviorId": row["behavior_id"],
                    "behaviorVersion": int(row["behavior_version"]),
                    "definition": json.loads(row["definition_json"]),
                }
                for row in conn.execute(
                    "SELECT b.* FROM journal_interaction_behavior_revisions b "
                    "JOIN (SELECT behavior_id,MAX(behavior_version) AS version "
                    "FROM journal_interaction_behavior_revisions GROUP BY behavior_id) latest "
                    "ON latest.behavior_id=b.behavior_id "
                    "AND latest.version=b.behavior_version ORDER BY b.behavior_id"
                ).fetchall()
            ]
            functions = [
                {
                    "functionId": row["function_id"],
                    "functionVersion": int(row["function_version"]),
                    "valueKind": row["value_kind"],
                    "unit": row["unit"],
                    "cardinality": row["cardinality"],
                    "definition": json.loads(row["definition_json"]),
                }
                for row in conn.execute(
                    "SELECT f.* FROM journal_function_contract_revisions f "
                    "JOIN (SELECT function_id,MAX(function_version) AS version "
                    "FROM journal_function_contract_revisions GROUP BY function_id) latest "
                    "ON latest.function_id=f.function_id "
                    "AND latest.version=f.function_version ORDER BY f.function_id"
                ).fetchall()
            ]
            detailed = [self._profile(conn, row) for row in profiles]
        return {
            "schemaVersion": 1,
            "authorityState": str(authority[0]) if authority else "legacy_compatibility",
            "activationRevision": activation,
            "profiles": detailed,
            "moduleTypes": module_types,
            "behaviors": behaviors,
            "functions": functions,
            "valueKinds": sorted(_VALUE_KINDS),
            "scheduleKinds": sorted(_SCHEDULE_KINDS),
        }

    def preview(self, draft: Mapping[str, Any], *, local_date: str) -> dict[str, Any]:
        try:
            target = date.fromisoformat(local_date)
        except (TypeError, ValueError) as exc:
            raise JournalCaptureValidationError("Choose a valid preview date.") from exc
        with self.store._connect() as conn:
            normalized = self._normalize(conn, draft)
        modules = []
        for item in normalized["modules"]:
            membership, evidence = _schedule_membership(
                item["schedule_kind"], item["schedule"], target
            )
            fields = []
            for field in item["fields"]:
                prompt = field["prompt"]
                prompt_included = prompt is not None
                if prompt_included:
                    prompt_membership = _schedule_membership(
                        prompt["schedule_kind"], prompt["schedule"], target
                    )[0]
                    prompt_included = prompt_membership == "included"
                fields.append(
                    {
                        "slotId": field["slot_id"],
                        "fieldId": field["field_id"],
                        "label": field["label"],
                        "description": field["description"],
                        "valueKind": field["value_kind"],
                        "unit": field["unit"],
                        "functionId": field["function_id"],
                        "functionVersion": field["function_version"],
                        "promptWording": (
                            prompt["wording"] if prompt_included else None
                        ),
                        "promptHelp": (
                            prompt["help_text"] if prompt_included else None
                        ),
                        "requiredness": (
                            prompt["requiredness"] if prompt_included else "optional"
                        ),
                    }
                )
            modules.append(
                {
                    "slotId": item["slot_id"],
                    "ordinal": item["ordinal"],
                    "moduleInstanceId": item["module_instance_id"],
                    "moduleTypeId": item["module_type_id"],
                    "label": item["label"],
                    "semanticMembership": membership,
                    "scheduleEvidence": evidence,
                    "fields": fields,
                }
            )
        return {
            "schemaVersion": 1,
            "localDate": target.isoformat(),
            "profile": {
                "profileId": normalized["profile_id"],
                "name": normalized["name"],
                "description": normalized["description"],
            },
            "modules": modules,
        }

    def save(
        self,
        draft: Mapping[str, Any],
        *,
        client_mutation_id: str,
        actor: Mapping[str, Any],
    ) -> dict[str, Any]:
        mutation_id = _identifier(client_mutation_id, "Journal mutation key")
        request = {"operation": "journal.profile.save", "draft": dict(draft)}
        request_sha = _sha(request)
        now = _now()
        with self.store.transaction() as conn:
            prior = conn.execute(
                "SELECT request_sha256,result_json FROM journal_mutations "
                "WHERE client_mutation_id=?", (mutation_id,),
            ).fetchone()
            if prior is not None:
                if prior["request_sha256"] != request_sha:
                    raise JournalCaptureConflict(
                        "That Journal mutation key was used for another change."
                    )
                return json.loads(prior["result_json"])
            normalized = self._normalize(conn, draft)
            for module in normalized["modules"]:
                field_refs: list[tuple[str, int, str | None, int | None]] = []
                for field in module["fields"]:
                    field_version = self._insert_field(conn, field, now)
                    prompt_id: str | None = None
                    prompt_version: int | None = None
                    if field["prompt"] is not None:
                        prompt_id = field["prompt"]["prompt_id"]
                        prompt_version = self._insert_prompt(
                            conn, field["prompt"], field["field_id"], field_version, now
                        )
                    field_refs.append(
                        (field["field_id"], field_version, prompt_id, prompt_version)
                    )
                module["new_version"] = self._insert_module(
                    conn, module, field_refs, now
                )
            profile_id = normalized["profile_id"]
            current = int(conn.execute(
                "SELECT COALESCE(MAX(profile_revision),0) FROM journal_profile_revisions "
                "WHERE profile_id=?", (profile_id,),
            ).fetchone()[0])
            if current != normalized["expected_revision"]:
                raise JournalCaptureConflict("The Journal profile changed before this edit.")
            revision = current + 1
            module_refs = [
                {
                    "slotId": item["slot_id"],
                    "moduleInstanceId": item["module_instance_id"],
                    "moduleInstanceVersion": item["new_version"],
                }
                for item in normalized["modules"]
            ]
            digest = _sha({"formatVersion": 1, "modules": module_refs})
            order = [item["slot_id"] for item in normalized["modules"]]
            conn.execute(
                "INSERT INTO journal_profile_revisions "
                "(profile_id,profile_revision,format_version,name,description,"
                "canonical_order_json,profile_digest,created_by,created_at,"
                "supersedes_revision) VALUES(?,?,1,?,?,?,?,?,?,?)",
                (
                    profile_id, revision, normalized["name"], normalized["description"],
                    _canonical(order), digest, str(actor.get("subject") or "local-user"),
                    now, current or None,
                ),
            )
            for item in normalized["modules"]:
                conn.execute(
                    "INSERT INTO journal_profile_module_slots "
                    "(profile_id,profile_revision,slot_id,ordinal,module_instance_id,"
                    "module_instance_version,required) VALUES(?,?,?,?,?,?,?)",
                    (
                        profile_id, revision, item["slot_id"], item["ordinal"],
                        item["module_instance_id"], item["new_version"],
                        int(item["required"]),
                    ),
                )
            result = {
                "schemaVersion": 1,
                "profileId": profile_id,
                "profileRevision": revision,
                "profileDigest": digest,
                "activationRevision": int(conn.execute(
                    "SELECT COALESCE(MAX(activation_revision),0) "
                    "FROM journal_profile_activation_epochs"
                ).fetchone()[0]),
            }
            conn.execute(
                "INSERT INTO journal_mutations "
                "(client_mutation_id,request_sha256,result_json,created_at) VALUES(?,?,?,?)",
                (mutation_id, request_sha, _canonical(result), now),
            )
        return result

    def _normalize(self, conn, draft: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(draft, Mapping):
            raise JournalCaptureValidationError("The Journal profile draft is invalid.")
        profile_id = _user_owned_identifier(
            draft.get("profileId"), "Profile identity"
        )
        expected_revision = _version(
            draft.get("expectedRevision", 0), "Profile revision", allow_zero=True
        )
        name = _text(draft.get("name"), "Profile name", maximum=100)
        description = _text(
            draft.get("description", ""), "Profile description", maximum=500,
            required=False,
        )
        raw_modules = _sequence(draft.get("modules", []), "Journal sections", maximum=24)
        modules: list[dict[str, Any]] = []
        slots: set[str] = set()
        instances: set[str] = set()
        for ordinal, raw in enumerate(raw_modules):
            slot_id = _identifier(raw.get("slotId"), "Section slot identity")
            instance_id = _user_owned_identifier(
                raw.get("moduleInstanceId"), "Section identity"
            )
            if slot_id in slots or instance_id in instances:
                raise JournalCaptureValidationError("Journal section identities must be unique.")
            slots.add(slot_id)
            instances.add(instance_id)
            type_id = _identifier(raw.get("moduleTypeId"), "Section type")
            type_version = _version(raw.get("moduleTypeVersion", 1), "Section type version")
            if conn.execute(
                "SELECT 1 FROM journal_module_type_revisions "
                "WHERE module_type_id=? AND module_type_version=?",
                (type_id, type_version),
            ).fetchone() is None:
                raise JournalCaptureValidationError("That Journal section type is unavailable.")
            behavior_id = _identifier(raw.get("behaviorId", "human_value"), "Behavior")
            behavior_version = _version(raw.get("behaviorVersion", 1), "Behavior version")
            behavior_row = conn.execute(
                "SELECT definition_json FROM journal_interaction_behavior_revisions "
                "WHERE behavior_id=? AND behavior_version=?",
                (behavior_id, behavior_version),
            ).fetchone()
            if behavior_row is None:
                raise JournalCaptureValidationError("That Journal behavior is unavailable.")
            behavior_definition = json.loads(str(behavior_row["definition_json"]))
            if module_requires_ai_contribution(type_id) and not ai_contribution_allowed(
                behavior_definition
            ):
                raise JournalCaptureValidationError(
                    "That Journal section requires a behavior that permits AI contribution."
                )
            schedule_kind, schedule = _schedule(
                raw.get("scheduleKind", "always"), raw.get("schedule")
            )
            raw_fields = _sequence(raw.get("fields", []), "Journal fields", maximum=32)
            fields = [self._normalize_field(conn, item) for item in raw_fields]
            settings = _mapping(raw.get("settings"), "Section settings")
            if type_id == "document":
                if behavior_id != "provenance_only":
                    raise JournalCaptureValidationError(
                        "A Journal document section must retain Co-work provenance."
                    )
                if fields:
                    raise JournalCaptureValidationError(
                        "A Journal document section cannot contain scalar fields."
                    )
                if settings.get("truthEligibility", "allowed") != "allowed":
                    raise JournalCaptureValidationError(
                        "A Journal working document must allow explicit Truth activation."
                    )
                if settings.get("initialTruthActivation", "disabled") != "disabled":
                    raise JournalCaptureValidationError(
                        "Journal documents must start with Truth disabled."
                    )
                settings = {
                    **settings,
                    "documentRole": _identifier(
                        settings.get("documentRole", "journal_document"),
                        "Document role",
                    ),
                    "truthEligibility": "allowed",
                    "initialTruthActivation": "disabled",
                }
            modules.append(
                {
                    "slot_id": slot_id,
                    "ordinal": ordinal,
                    "module_instance_id": instance_id,
                    "expected_version": _version(
                        raw.get("expectedVersion", 0), "Section version", allow_zero=True
                    ),
                    "module_type_id": type_id,
                    "module_type_version": type_version,
                    "label": _text(raw.get("label"), "Section label", maximum=100),
                    "settings": settings,
                    "behavior_id": behavior_id,
                    "behavior_version": behavior_version,
                    "schedule_kind": schedule_kind,
                    "schedule": schedule,
                    "required": bool(raw.get("required", False)),
                    "fields": fields,
                }
            )
        return {
            "profile_id": profile_id,
            "expected_revision": expected_revision,
            "name": name,
            "description": description,
            "modules": modules,
        }

    @staticmethod
    def _normalize_field(
        conn: sqlite3.Connection,
        raw: Mapping[str, Any],
    ) -> dict[str, Any]:
        field_id = _user_owned_identifier(raw.get("fieldId"), "Field identity")
        expected_version = _version(
            raw.get("expectedVersion", 0), "Field version", allow_zero=True
        )
        owner = _text(raw.get("owner", "user"), "Field owner", maximum=80)
        if owner != "user":
            raise JournalCaptureValidationError(
                "Journal fields configured here must remain owned by the user."
            )
        stable_key = _identifier(raw.get("stableKey"), "Field key")
        current_field = conn.execute(
            "SELECT owner,stable_key FROM journal_field_definition_versions "
            "WHERE field_id=? ORDER BY definition_version DESC LIMIT 1",
            (field_id,),
        ).fetchone()
        if current_field is not None:
            if str(current_field["owner"]) != "user":
                raise JournalCaptureValidationError(
                    "That Journal field is not user-owned and cannot be revised here."
                )
            if str(current_field["stable_key"]) != stable_key:
                raise JournalCaptureValidationError(
                    "A Journal field key cannot change across revisions."
                )
        kind = _text(raw.get("valueKind"), "Field type", maximum=32)
        if kind not in _VALUE_KINDS:
            raise JournalCaptureValidationError("That Journal field type is unsupported.")
        unit = (
            _text(raw["unit"], "Field unit", maximum=40, required=False)
            if raw.get("unit") is not None else None
        )
        prompt_raw = raw.get("prompt")
        prompt = None
        if prompt_raw is not None:
            if not isinstance(prompt_raw, Mapping):
                raise JournalCaptureValidationError("The Journal prompt is invalid.")
            requiredness = _text(
                prompt_raw.get("requiredness", "optional"), "Prompt requirement", maximum=16
            )
            if requiredness not in _REQUIREDNESS:
                raise JournalCaptureValidationError("The prompt requirement is invalid.")
            prompt_schedule, prompt_schedule_value = _schedule(
                prompt_raw.get("scheduleKind", "always"), prompt_raw.get("schedule")
            )
            prompt_id = _user_owned_identifier(
                prompt_raw.get("promptId"), "Prompt identity"
            )
            prompt_expected_version = _version(
                prompt_raw.get("expectedVersion", 0), "Prompt version", allow_zero=True
            )
            current_prompt = conn.execute(
                "SELECT field_id FROM journal_prompt_definition_versions "
                "WHERE prompt_id=? ORDER BY prompt_version DESC LIMIT 1",
                (prompt_id,),
            ).fetchone()
            if (
                current_prompt is not None
                and str(current_prompt["field_id"] or "") != field_id
            ):
                raise JournalCaptureValidationError(
                    "A Journal prompt identity cannot move to another field."
                )
            prompt = {
                "prompt_id": prompt_id,
                "expected_version": prompt_expected_version,
                "wording": _text(prompt_raw.get("wording"), "Prompt", maximum=300),
                "help_text": _text(
                    prompt_raw.get("helpText", ""), "Prompt help", maximum=500,
                    required=False,
                ),
                "requiredness": requiredness,
                "schedule_kind": prompt_schedule,
                "schedule": prompt_schedule_value,
            }
        behavior_id = _identifier(raw.get("behaviorId", "human_value"), "Field behavior")
        raw_function_id = raw.get("functionId")
        raw_function_version = raw.get("functionVersion")
        function_id = (
            None
            if raw_function_id is None or raw_function_id == ""
            else _identifier(raw_function_id, "Field function")
        )
        function_version = (
            None
            if raw_function_version is None
            else _version(raw_function_version, "Field function version")
        )
        if (function_id is None) != (function_version is None):
            raise JournalCaptureValidationError(
                "A Journal function identity and version must be selected together."
            )
        if function_id is not None:
            contract = conn.execute(
                "SELECT value_kind,unit FROM journal_function_contract_revisions "
                "WHERE function_id=? AND function_version=?",
                (function_id, function_version),
            ).fetchone()
            if (
                contract is None
                or str(contract["value_kind"]) != kind
                or (
                    None if contract["unit"] is None else str(contract["unit"])
                ) != unit
            ):
                raise JournalCaptureValidationError(
                    "The Journal field is incompatible with that function contract."
                )
        privacy_class = _text(
            raw.get("privacyClass", "private"), "Field privacy", maximum=32
        )
        if privacy_class not in _PRIVACY_CLASSES:
            raise JournalCaptureValidationError(
                "That Journal field privacy class is unsupported."
            )
        search_mode = _text(
            raw.get("searchMode", "structured_only"), "Field search", maximum=32
        )
        if search_mode not in _SEARCH_MODES:
            raise JournalCaptureValidationError(
                "That Journal field search mode is unsupported."
            )
        return {
            "slot_id": _identifier(raw.get("slotId"), "Field slot identity"),
            "field_id": field_id,
            "expected_version": expected_version,
            "owner": owner,
            "stable_key": stable_key,
            "label": _text(raw.get("label"), "Field label", maximum=100),
            "description": _text(
                raw.get("description", ""), "Field description", maximum=500,
                required=False,
            ),
            "value_kind": kind,
            "unit": unit,
            "constraints": _mapping(raw.get("constraints"), "Field constraints"),
            "function_id": function_id,
            "function_version": function_version,
            "behavior_id": behavior_id,
            "behavior_version": _version(raw.get("behaviorVersion", 1), "Field behavior version"),
            "privacy_class": privacy_class,
            "search_mode": search_mode,
            "disclosure_policy_id": _identifier(
                raw.get("disclosurePolicyId", "private_default/v1"), "Disclosure policy"
            ),
            "prompt": prompt,
        }

    @staticmethod
    def _insert_field(conn, field: dict[str, Any], now: str) -> int:
        current = int(conn.execute(
            "SELECT COALESCE(MAX(definition_version),0) "
            "FROM journal_field_definition_versions WHERE field_id=?",
            (field["field_id"],),
        ).fetchone()[0])
        if current != field["expected_version"]:
            raise JournalCaptureConflict("A Journal field changed before this edit.")
        if conn.execute(
            "SELECT 1 FROM journal_interaction_behavior_revisions "
            "WHERE behavior_id=? AND behavior_version=?",
            (field["behavior_id"], field["behavior_version"]),
        ).fetchone() is None:
            raise JournalCaptureValidationError("That field behavior is unavailable.")
        version = current + 1
        payload = {
            "fieldId": field["field_id"], "label": field["label"],
            "valueKind": field["value_kind"], "unit": field["unit"],
            "constraints": field["constraints"], "behavior": [field["behavior_id"], field["behavior_version"]],
            "function": [field["function_id"], field["function_version"]],
        }
        conn.execute(
            "INSERT INTO journal_field_definition_versions "
            "(field_id,definition_version,owner,stable_key,label,description,value_kind,"
            "unit,constraints_json,value_codec_version,function_id,function_version,"
            "behavior_id,behavior_version,privacy_class,search_mode,disclosure_policy_id,"
            "definition_sha256,created_at,supersedes_version) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)",
            (
                field["field_id"], version, field["owner"], field["stable_key"],
                field["label"], field["description"], field["value_kind"], field["unit"],
                _canonical(field["constraints"]), field["function_id"],
                field["function_version"], field["behavior_id"],
                field["behavior_version"], field["privacy_class"], field["search_mode"],
                field["disclosure_policy_id"], _sha(payload), now, current or None,
            ),
        )
        return version

    @staticmethod
    def _insert_prompt(conn, prompt: dict[str, Any], field_id: str, field_version: int, now: str) -> int:
        current = int(conn.execute(
            "SELECT COALESCE(MAX(prompt_version),0) "
            "FROM journal_prompt_definition_versions WHERE prompt_id=?",
            (prompt["prompt_id"],),
        ).fetchone()[0])
        if current != prompt["expected_version"]:
            raise JournalCaptureConflict("A Journal prompt changed before this edit.")
        version = current + 1
        payload = {**prompt, "field": [field_id, field_version]}
        conn.execute(
            "INSERT INTO journal_prompt_definition_versions "
            "(prompt_id,prompt_version,field_id,field_definition_version,wording,help_text,"
            "requiredness,schedule_kind,schedule_json,disposition_policy_json,"
            "definition_sha256,created_at,supersedes_version) VALUES(?,?,?,?,?,?,?,?,?,'{}',?,?,?)",
            (
                prompt["prompt_id"], version, field_id, field_version, prompt["wording"],
                prompt["help_text"], prompt["requiredness"], prompt["schedule_kind"],
                _canonical(prompt["schedule"]), _sha(payload), now, current or None,
            ),
        )
        return version

    @staticmethod
    def _insert_module(conn, module: dict[str, Any], fields, now: str) -> int:
        current = int(conn.execute(
            "SELECT COALESCE(MAX(instance_version),0) "
            "FROM journal_module_instance_versions WHERE module_instance_id=?",
            (module["module_instance_id"],),
        ).fetchone()[0])
        if current != module["expected_version"]:
            raise JournalCaptureConflict("A Journal section changed before this edit.")
        version = current + 1
        conn.execute(
            "INSERT INTO journal_module_instance_versions "
            "(module_instance_id,instance_version,module_type_id,module_type_version,label,"
            "settings_schema_version,settings_json,settings_sha256,behavior_id,behavior_version,"
            "schedule_kind,schedule_json,reveal_policy_json,created_at,supersedes_version) "
            "VALUES(?,?,?,?,?,1,?,?,?,?,?,?,'{}',?,?)",
            (
                module["module_instance_id"], version, module["module_type_id"],
                module["module_type_version"], module["label"],
                _canonical(module["settings"]), _sha(module["settings"]),
                module["behavior_id"], module["behavior_version"],
                module["schedule_kind"], _canonical(module["schedule"]), now,
                current or None,
            ),
        )
        for ordinal, (field_id, field_version, prompt_id, prompt_version) in enumerate(fields):
            conn.execute(
                "INSERT INTO journal_module_field_slots "
                "(module_instance_id,module_instance_version,slot_id,ordinal,field_id,"
                "field_definition_version,prompt_id,prompt_version) VALUES(?,?,?,?,?,?,?,?)",
                (
                    module["module_instance_id"], version,
                    module["fields"][ordinal]["slot_id"], ordinal, field_id,
                    field_version, prompt_id, prompt_version,
                ),
            )
        return version

    def _profile(self, conn, row) -> dict[str, Any]:
        modules = []
        for module in conn.execute(
            "SELECT s.slot_id,s.ordinal,s.required,m.* FROM journal_profile_module_slots s "
            "JOIN journal_module_instance_versions m ON m.module_instance_id=s.module_instance_id "
            "AND m.instance_version=s.module_instance_version "
            "WHERE s.profile_id=? AND s.profile_revision=? ORDER BY s.ordinal",
            (row["profile_id"], row["profile_revision"]),
        ).fetchall():
            fields = []
            for field in conn.execute(
                "SELECT s.*,f.*,p.wording,p.help_text,p.requiredness,p.schedule_kind AS prompt_schedule_kind,"
                "p.schedule_json AS prompt_schedule_json FROM journal_module_field_slots s "
                "JOIN journal_field_definition_versions f ON f.field_id=s.field_id "
                "AND f.definition_version=s.field_definition_version "
                "LEFT JOIN journal_prompt_definition_versions p ON p.prompt_id=s.prompt_id "
                "AND p.prompt_version=s.prompt_version WHERE s.module_instance_id=? "
                "AND s.module_instance_version=? ORDER BY s.ordinal",
                (module["module_instance_id"], module["instance_version"]),
            ).fetchall():
                fields.append(
                    {
                        "slotId": field["slot_id"], "fieldId": field["field_id"],
                        "fieldDefinitionVersion": int(field["definition_version"]),
                        "owner": field["owner"], "stableKey": field["stable_key"],
                        "label": field["label"], "description": field["description"],
                        "valueKind": field["value_kind"], "unit": field["unit"],
                        "constraints": json.loads(field["constraints_json"]),
                        "functionId": field["function_id"],
                        "functionVersion": (
                            None
                            if field["function_version"] is None
                            else int(field["function_version"])
                        ),
                        "behaviorId": field["behavior_id"],
                        "behaviorVersion": int(field["behavior_version"]),
                        "privacyClass": field["privacy_class"],
                        "searchMode": field["search_mode"],
                        "disclosurePolicyId": field["disclosure_policy_id"],
                        "prompt": None if field["prompt_id"] is None else {
                            "promptId": field["prompt_id"],
                            "promptVersion": int(field["prompt_version"]),
                            "wording": field["wording"], "helpText": field["help_text"],
                            "requiredness": field["requiredness"],
                            "scheduleKind": field["prompt_schedule_kind"],
                            "schedule": json.loads(field["prompt_schedule_json"]),
                        },
                    }
                )
            modules.append(
                {
                    "slotId": module["slot_id"], "ordinal": int(module["ordinal"]),
                    "required": bool(module["required"]),
                    "moduleInstanceId": module["module_instance_id"],
                    "moduleInstanceVersion": int(module["instance_version"]),
                    "moduleTypeId": module["module_type_id"],
                    "moduleTypeVersion": int(module["module_type_version"]),
                    "label": module["label"], "settings": json.loads(module["settings_json"]),
                    "behaviorId": module["behavior_id"],
                    "behaviorVersion": module["behavior_version"],
                    "scheduleKind": module["schedule_kind"],
                    "schedule": json.loads(module["schedule_json"]), "fields": fields,
                }
            )
        editable = (
            _is_user_owned_identifier(row["profile_id"])
            and not (
                "import_cohort_id" in row.keys()
                and row["import_cohort_id"] is not None
            )
            and all(
                _is_user_owned_identifier(module["moduleInstanceId"])
                and all(
                    _is_user_owned_identifier(field["fieldId"])
                    and field["owner"] == "user"
                    and (
                        field["prompt"] is None
                        or _is_user_owned_identifier(field["prompt"]["promptId"])
                    )
                    for field in module["fields"]
                )
                for module in modules
            )
        )
        return {
            "profileId": row["profile_id"],
            "profileRevision": int(row["profile_revision"]),
            "formatVersion": int(row["format_version"]),
            "name": row["name"], "description": row["description"],
            "canonicalOrder": json.loads(row["canonical_order_json"]),
            "profileDigest": row["profile_digest"], "createdBy": row["created_by"],
            "createdAt": row["created_at"], "supersedesRevision": row["supersedes_revision"],
            "editable": editable,
            "modules": modules,
        }

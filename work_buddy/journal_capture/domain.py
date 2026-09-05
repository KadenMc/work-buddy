"""Native Journal configuration, day composition, and typed-content service.

The service deliberately separates pure resolution from persistence.  A read of
an empty or historical day computes the effective profile in memory; only an
explicit lifecycle/mutation call freezes a ``journal_day_composition_snapshot``.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from work_buddy.journal_capture.models import (
    JournalCaptureConflict,
    JournalCaptureValidationError,
    JournalDayComposition,
    JournalDayField,
    JournalDayModule,
    JournalFieldValue,
    JournalModuleInstanceVersion,
    JournalNativeItem,
    JournalProfileRevision,
    JournalRelation,
    JournalSearchEvent,
    JournalValueDisposition,
    JournalValueKind,
)
from work_buddy.journal_capture.interaction_policy import ai_contribution_allowed
from work_buddy.journal_capture.store import JournalCaptureStore


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _mapping(raw: str | None) -> Mapping[str, Any]:
    if not raw:
        return {}
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


def _sequence(raw: str | None) -> tuple[Any, ...]:
    if not raw:
        return ()
    value = json.loads(raw)
    return tuple(value) if isinstance(value, list) else ()


def _day_id(
    local_date: str,
    timezone: str,
    boundary: str,
) -> str:
    return f"journal-day:{local_date}:{timezone}:{boundary}"


def _validate_local_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise JournalCaptureValidationError("The Journal day is invalid.") from exc
    if parsed.isoformat() != value:
        raise JournalCaptureValidationError("The Journal day is invalid.")
    return parsed


def _validate_stated_instant(value: str) -> datetime:
    """Require a stated occurrence time to name one absolute instant.

    Day membership is a policy question the calling surface answers against the
    target day's window.  The domain only refuses a time that no zone can place.
    """

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise JournalCaptureValidationError("The stated time is invalid.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise JournalCaptureValidationError("The stated time needs a time zone offset.")
    return parsed


def _schedule_membership(
    schedule_kind: str,
    schedule: Mapping[str, Any],
    target: date,
) -> tuple[str, Mapping[str, Any]]:
    evidence: dict[str, Any] = {
        "evaluatedLocalDate": target.isoformat(),
        "scheduleKind": schedule_kind,
    }
    if schedule_kind in {"always", "manual_only"}:
        evidence["matched"] = True
        if schedule_kind == "manual_only":
            evidence["automaticTriggerAllowed"] = False
        return "included", evidence
    if schedule_kind == "weekdays":
        raw = schedule.get("weekdays")
        allowed = {
            int(item)
            for item in raw
            if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 6
        } if isinstance(raw, list) else set()
        evidence.update({"weekday": target.weekday(), "weekdays": sorted(allowed)})
        matched = target.weekday() in allowed
        evidence["matched"] = matched
        return ("included" if matched else "excluded_by_schedule"), evidence
    if schedule_kind == "date_range":
        start_raw = schedule.get("start")
        end_raw = schedule.get("end")
        try:
            start = date.fromisoformat(start_raw) if isinstance(start_raw, str) else None
            end = date.fromisoformat(end_raw) if isinstance(end_raw, str) else None
        except ValueError:
            start = end = None
        matched = (start is None or target >= start) and (end is None or target <= end)
        evidence.update(
            {
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "matched": matched,
            }
        )
        return ("included" if matched else "excluded_by_schedule"), evidence
    return "unavailable", {**evidence, "matched": False, "reason": "unknown_schedule"}


class JournalDomainService:
    """Native domain operations over the existing Journal SQLite authority."""

    def __init__(self, store: JournalCaptureStore) -> None:
        self.store = store

    # ------------------------------------------------------------------
    # Immutable configuration and pure day resolution

    def list_profiles(self) -> tuple[JournalProfileRevision, ...]:
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM journal_profile_revisions AS profile WHERE "
                "profile.import_cohort_id IS NULL OR ("
                "EXISTS(SELECT 1 FROM journal_import_cohorts AS cohort "
                "WHERE cohort.cohort_id=profile.import_cohort_id AND cohort.state='sealed') "
                "AND EXISTS(SELECT 1 FROM journal_authority_control AS authority "
                "WHERE authority.singleton=1 AND authority.mode='database_only')) "
                "ORDER BY profile_id, profile_revision DESC"
            ).fetchall()
        return tuple(self._profile(row) for row in rows)

    def authority_state(self) -> str:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT value FROM journal_domain_state WHERE key='content_authority'"
            ).fetchone()
        return str(row[0]) if row is not None else "legacy_compatibility"

    def profile_for_date(
        self,
        local_date: str,
    ) -> tuple[JournalProfileRevision, int]:
        _validate_local_date(local_date)
        with self.store._connect() as conn:
            row = conn.execute(
                """
                SELECT p.*, a.activation_revision
                FROM journal_profile_activation_epochs AS a
                JOIN journal_profile_revisions AS p
                  ON p.profile_id=a.profile_id
                 AND p.profile_revision=a.profile_revision
                WHERE a.import_cohort_id IS NULL AND a.effective_local_date <= ?
                ORDER BY a.effective_local_date DESC, a.activation_revision DESC
                LIMIT 1
                """,
                (local_date,),
            ).fetchone()
        if row is None:
            raise JournalCaptureValidationError("No Journal profile applies to that day.")
        return self._profile(row), int(row["activation_revision"])

    def create_interaction_behavior_version(
        self,
        *,
        behavior_id: str,
        definition: Mapping[str, Any],
        expected_version: int = 0,
    ) -> int:
        if not behavior_id or not isinstance(definition, Mapping):
            raise JournalCaptureValidationError(
                "The Journal interaction behavior is invalid."
            )
        payload = dict(definition)
        with self.store.transaction() as conn:
            current = int(
                conn.execute(
                    "SELECT COALESCE(MAX(behavior_version),0) "
                    "FROM journal_interaction_behavior_revisions WHERE behavior_id=?",
                    (behavior_id,),
                ).fetchone()[0]
            )
            if current != expected_version:
                raise JournalCaptureConflict(
                    "The Journal interaction behavior changed."
                )
            version = current + 1
            conn.execute(
                """
                INSERT INTO journal_interaction_behavior_revisions(
                    behavior_id,behavior_version,definition_json,
                    definition_sha256,created_at,supersedes_version
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    behavior_id,
                    version,
                    _canonical(payload),
                    _sha(payload),
                    _now(),
                    current or None,
                ),
            )
        return version

    def create_function_contract_version(
        self,
        *,
        function_id: str,
        value_kind: str,
        cardinality: str,
        definition: Mapping[str, Any],
        unit: str | None = None,
        expected_version: int = 0,
    ) -> int:
        try:
            JournalValueKind(value_kind)
        except ValueError as exc:
            raise JournalCaptureValidationError(
                "That Journal function value type is invalid."
            ) from exc
        if not function_id or cardinality not in {"single", "multiple"}:
            raise JournalCaptureValidationError(
                "The Journal function contract is invalid."
            )
        payload = {
            "functionId": function_id,
            "valueKind": value_kind,
            "unit": unit,
            "cardinality": cardinality,
            "definition": dict(definition),
        }
        with self.store.transaction() as conn:
            current = int(
                conn.execute(
                    "SELECT COALESCE(MAX(function_version),0) "
                    "FROM journal_function_contract_revisions WHERE function_id=?",
                    (function_id,),
                ).fetchone()[0]
            )
            if current != expected_version:
                raise JournalCaptureConflict("The Journal function contract changed.")
            version = current + 1
            conn.execute(
                """
                INSERT INTO journal_function_contract_revisions(
                    function_id,function_version,value_kind,unit,cardinality,
                    definition_json,definition_sha256,created_at,supersedes_version
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    function_id,
                    version,
                    value_kind,
                    unit,
                    cardinality,
                    _canonical(dict(definition)),
                    _sha(payload),
                    _now(),
                    current or None,
                ),
            )
        return version

    def create_module_type_version(
        self,
        *,
        module_type_id: str,
        definition: Mapping[str, Any],
        expected_version: int = 0,
    ) -> int:
        if not module_type_id or not isinstance(definition, Mapping):
            raise JournalCaptureValidationError("The Journal module type is invalid.")
        payload = dict(definition)
        with self.store.transaction() as conn:
            current = int(
                conn.execute(
                    "SELECT COALESCE(MAX(module_type_version),0) "
                    "FROM journal_module_type_revisions WHERE module_type_id=?",
                    (module_type_id,),
                ).fetchone()[0]
            )
            if current != expected_version:
                raise JournalCaptureConflict("The Journal module type changed.")
            version = current + 1
            conn.execute(
                """
                INSERT INTO journal_module_type_revisions(
                    module_type_id,module_type_version,definition_json,
                    definition_sha256,created_at,supersedes_version
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    module_type_id,
                    version,
                    _canonical(payload),
                    _sha(payload),
                    _now(),
                    current or None,
                ),
            )
        return version

    def create_field_definition_version(
        self,
        *,
        field_id: str,
        owner: str,
        stable_key: str,
        label: str,
        value_kind: str,
        function_id: str | None = None,
        function_version: int | None = None,
        constraints: Mapping[str, Any] | None = None,
        description: str = "",
        unit: str | None = None,
        value_codec_version: int = 1,
        behavior_id: str = "human_value",
        behavior_version: int = 1,
        privacy_class: str = "private",
        search_mode: str = "structured_only",
        disclosure_policy_id: str = "private_default/v1",
        expected_version: int = 0,
    ) -> int:
        try:
            JournalValueKind(value_kind)
        except ValueError as exc:
            raise JournalCaptureValidationError("That Journal field type is invalid.") from exc
        if (function_id is None) != (function_version is None):
            raise JournalCaptureValidationError(
                "A Journal function identity and version must be provided together."
            )
        payload = {
            "fieldId": field_id,
            "owner": owner,
            "stableKey": stable_key,
            "label": label,
            "description": description,
            "valueKind": value_kind,
            "unit": unit,
            "constraints": dict(constraints or {}),
            "valueCodecVersion": value_codec_version,
            "function": [function_id, function_version],
            "behavior": [behavior_id, behavior_version],
            "privacyClass": privacy_class,
            "searchMode": search_mode,
            "disclosurePolicyId": disclosure_policy_id,
        }
        with self.store.transaction() as conn:
            current = int(
                conn.execute(
                    "SELECT COALESCE(MAX(definition_version),0) "
                    "FROM journal_field_definition_versions WHERE field_id=?",
                    (field_id,),
                ).fetchone()[0]
            )
            if current != expected_version:
                raise JournalCaptureConflict("The Journal field definition changed.")
            if function_id is not None:
                contract = conn.execute(
                    "SELECT value_kind,unit FROM journal_function_contract_revisions "
                    "WHERE function_id=? AND function_version=?",
                    (function_id, function_version),
                ).fetchone()
                if (
                    contract is None
                    or contract["value_kind"] != value_kind
                    or (
                        contract["unit"] is not None
                        and contract["unit"] != unit
                    )
                ):
                    raise JournalCaptureValidationError(
                        "The Journal field is incompatible with that function contract."
                    )
            version = current + 1
            conn.execute(
                """
                INSERT INTO journal_field_definition_versions(
                    field_id,definition_version,owner,stable_key,label,description,
                    value_kind,unit,constraints_json,value_codec_version,
                    function_id,function_version,behavior_id,behavior_version,
                    privacy_class,search_mode,
                    disclosure_policy_id,definition_sha256,created_at,supersedes_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    field_id,
                    version,
                    owner,
                    stable_key,
                    label,
                    description,
                    value_kind,
                    unit,
                    _canonical(dict(constraints or {})),
                    value_codec_version,
                    function_id,
                    function_version,
                    behavior_id,
                    behavior_version,
                    privacy_class,
                    search_mode,
                    disclosure_policy_id,
                    _sha(payload),
                    _now(),
                    current or None,
                ),
            )
        return version

    def create_prompt_definition_version(
        self,
        *,
        prompt_id: str,
        wording: str,
        field_id: str | None = None,
        field_definition_version: int | None = None,
        help_text: str = "",
        requiredness: str = "optional",
        schedule_kind: str = "always",
        schedule: Mapping[str, Any] | None = None,
        disposition_policy: Mapping[str, Any] | None = None,
        expected_version: int = 0,
    ) -> int:
        if not prompt_id or not wording or ((field_id is None) != (field_definition_version is None)):
            raise JournalCaptureValidationError("The Journal prompt definition is invalid.")
        payload = {
            "promptId": prompt_id,
            "wording": wording,
            "helpText": help_text,
            "field": [field_id, field_definition_version],
            "requiredness": requiredness,
            "scheduleKind": schedule_kind,
            "schedule": dict(schedule or {}),
            "dispositionPolicy": dict(disposition_policy or {}),
        }
        with self.store.transaction() as conn:
            current = int(
                conn.execute(
                    "SELECT COALESCE(MAX(prompt_version),0) "
                    "FROM journal_prompt_definition_versions WHERE prompt_id=?",
                    (prompt_id,),
                ).fetchone()[0]
            )
            if current != expected_version:
                raise JournalCaptureConflict("The Journal prompt definition changed.")
            version = current + 1
            conn.execute(
                """
                INSERT INTO journal_prompt_definition_versions(
                    prompt_id,prompt_version,field_id,field_definition_version,
                    wording,help_text,requiredness,schedule_kind,schedule_json,
                    disposition_policy_json,definition_sha256,created_at,
                    supersedes_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    prompt_id,
                    version,
                    field_id,
                    field_definition_version,
                    wording,
                    help_text,
                    requiredness,
                    schedule_kind,
                    _canonical(dict(schedule or {})),
                    _canonical(dict(disposition_policy or {})),
                    _sha(payload),
                    _now(),
                    current or None,
                ),
            )
        return version

    def create_module_instance_version(
        self,
        *,
        module_instance_id: str,
        module_type_id: str,
        module_type_version: int,
        label: str,
        settings: Mapping[str, Any] | None = None,
        behavior_id: str | None = None,
        behavior_version: int | None = None,
        schedule_kind: str = "always",
        schedule: Mapping[str, Any] | None = None,
        reveal_policy: Mapping[str, Any] | None = None,
        fields: Sequence[Mapping[str, Any]] = (),
        expected_version: int = 0,
    ) -> JournalModuleInstanceVersion:
        if not module_instance_id or not label:
            raise JournalCaptureValidationError("A module identity and label are required.")
        settings_value = dict(settings or {})
        schedule_value = dict(schedule or {})
        reveal_value = dict(reveal_policy or {})
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT MAX(instance_version) FROM journal_module_instance_versions "
                "WHERE module_instance_id=?",
                (module_instance_id,),
            ).fetchone()
            current = int(row[0] or 0)
            if current != expected_version:
                raise JournalCaptureConflict("The Journal module changed before this edit.")
            version = current + 1
            conn.execute(
                """
                INSERT INTO journal_module_instance_versions(
                    module_instance_id,instance_version,module_type_id,module_type_version,
                    label,settings_schema_version,settings_json,settings_sha256,
                    behavior_id,behavior_version,schedule_kind,schedule_json,
                    reveal_policy_json,created_at,supersedes_version
                ) VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)
                """,
                (
                    module_instance_id,
                    version,
                    module_type_id,
                    module_type_version,
                    label,
                    _canonical(settings_value),
                    _sha(settings_value),
                    behavior_id,
                    behavior_version,
                    schedule_kind,
                    _canonical(schedule_value),
                    _canonical(reveal_value),
                    _now(),
                    current or None,
                ),
            )
            seen_field_slots: set[str] = set()
            for ordinal, field in enumerate(fields):
                slot_id = str(field.get("slot_id") or "")
                field_id = str(field.get("field_id") or "")
                field_version = int(field.get("field_definition_version") or 0)
                prompt_id = field.get("prompt_id")
                prompt_version = field.get("prompt_version")
                if (
                    not slot_id
                    or slot_id in seen_field_slots
                    or not field_id
                    or field_version < 1
                    or ((prompt_id is None) != (prompt_version is None))
                ):
                    raise JournalCaptureValidationError("The module field list is invalid.")
                seen_field_slots.add(slot_id)
                conn.execute(
                    """
                    INSERT INTO journal_module_field_slots(
                        module_instance_id,module_instance_version,slot_id,ordinal,
                        field_id,field_definition_version,prompt_id,prompt_version
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        module_instance_id,
                        version,
                        slot_id,
                        ordinal,
                        field_id,
                        field_version,
                        prompt_id,
                        prompt_version,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM journal_module_instance_versions "
                "WHERE module_instance_id=? AND instance_version=?",
                (module_instance_id, version),
            ).fetchone()
        return self._module(row)

    def create_profile_revision(
        self,
        *,
        profile_id: str,
        name: str,
        modules: Sequence[Mapping[str, Any]],
        created_by: str,
        description: str = "",
        expected_revision: int = 0,
    ) -> JournalProfileRevision:
        if not profile_id or not name:
            raise JournalCaptureValidationError("A profile identity and name are required.")
        normalized: list[dict[str, Any]] = []
        seen_slots: set[str] = set()
        for ordinal, item in enumerate(modules):
            slot_id = str(item.get("slot_id") or "")
            instance_id = str(item.get("module_instance_id") or "")
            version = int(item.get("module_instance_version") or 0)
            if not slot_id or slot_id in seen_slots or not instance_id or version < 1:
                raise JournalCaptureValidationError("The profile module list is invalid.")
            seen_slots.add(slot_id)
            normalized.append(
                {
                    "slot_id": slot_id,
                    "ordinal": ordinal,
                    "module_instance_id": instance_id,
                    "module_instance_version": version,
                    "required": bool(item.get("required", False)),
                }
            )
        payload = {"formatVersion": 1, "modules": normalized}
        digest = _sha(payload)
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT MAX(profile_revision) FROM journal_profile_revisions "
                "WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
            current = int(row[0] or 0)
            if current != expected_revision:
                raise JournalCaptureConflict("The Journal profile changed before this edit.")
            revision = current + 1
            for item in normalized:
                exists = conn.execute(
                    "SELECT 1 FROM journal_module_instance_versions "
                    "WHERE module_instance_id=? AND instance_version=?",
                    (item["module_instance_id"], item["module_instance_version"]),
                ).fetchone()
                if exists is None:
                    raise JournalCaptureValidationError("The profile references an unknown module.")
            conn.execute(
                """
                INSERT INTO journal_profile_revisions(
                    profile_id,profile_revision,format_version,name,description,
                    canonical_order_json,profile_digest,created_by,created_at,
                    supersedes_revision
                ) VALUES(?,?,1,?,?,?,?,?,?,?)
                """,
                (
                    profile_id,
                    revision,
                    name,
                    description,
                    _canonical([item["slot_id"] for item in normalized]),
                    digest,
                    created_by,
                    _now(),
                    current or None,
                ),
            )
            for item in normalized:
                conn.execute(
                    """
                    INSERT INTO journal_profile_module_slots(
                        profile_id,profile_revision,slot_id,ordinal,
                        module_instance_id,module_instance_version,required
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        profile_id,
                        revision,
                        item["slot_id"],
                        item["ordinal"],
                        item["module_instance_id"],
                        item["module_instance_version"],
                        int(item["required"]),
                    ),
                )
            row = conn.execute(
                "SELECT * FROM journal_profile_revisions "
                "WHERE profile_id=? AND profile_revision=?",
                (profile_id, revision),
            ).fetchone()
        return self._profile(row)

    def activate_profile(
        self,
        *,
        profile_id: str,
        profile_revision: int,
        effective_local_date: str,
        expected_activation_revision: int,
        client_mutation_id: str,
        actor: Mapping[str, Any],
    ) -> int:
        _validate_local_date(effective_local_date)
        request = {
            "profile_id": profile_id,
            "profile_revision": profile_revision,
            "effective_local_date": effective_local_date,
            "expected_activation_revision": expected_activation_revision,
        }
        request_sha = _sha(request)
        with self.store.transaction() as conn:
            prior = conn.execute(
                "SELECT request_sha256,result_json FROM journal_mutations "
                "WHERE client_mutation_id=?",
                (client_mutation_id,),
            ).fetchone()
            if prior is not None:
                if prior["request_sha256"] != request_sha:
                    raise JournalCaptureConflict(
                        "That Journal mutation key was used for another request."
                    )
                return int(json.loads(prior["result_json"])["activation_revision"])
            current = int(
                conn.execute(
                    "SELECT COALESCE(MAX(activation_revision),0) "
                    "FROM journal_profile_activation_epochs "
                    "WHERE import_cohort_id IS NULL"
                ).fetchone()[0]
            )
            if current != expected_activation_revision:
                raise JournalCaptureConflict("The selected Journal profile changed.")
            profile = conn.execute(
                "SELECT profile_digest FROM journal_profile_revisions "
                "WHERE profile_id=? AND profile_revision=? AND ("
                "import_cohort_id IS NULL OR ("
                "EXISTS(SELECT 1 FROM journal_import_cohorts AS cohort "
                "WHERE cohort.cohort_id=journal_profile_revisions.import_cohort_id "
                "AND cohort.state='sealed') AND EXISTS(SELECT 1 "
                "FROM journal_authority_control AS authority "
                "WHERE authority.singleton=1 AND authority.mode='database_only')))",
                (profile_id, profile_revision),
            ).fetchone()
            if profile is None:
                raise JournalCaptureValidationError("That Journal profile is unavailable.")
            revision = int(
                conn.execute(
                    "SELECT COALESCE(MAX(activation_revision),0)+1 "
                    "FROM journal_profile_activation_epochs"
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO journal_profile_activation_epochs(
                    activation_revision,profile_id,profile_revision,profile_digest,
                    effective_local_date,actor_json,client_mutation_id,request_sha256,
                    activated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    revision,
                    profile_id,
                    profile_revision,
                    profile["profile_digest"],
                    effective_local_date,
                    _canonical(actor),
                    client_mutation_id,
                    request_sha,
                    _now(),
                ),
            )
            result = {"activation_revision": revision}
            conn.execute(
                "INSERT INTO journal_mutations(client_mutation_id,request_sha256,result_json,created_at) "
                "VALUES(?,?,?,?)",
                (client_mutation_id, request_sha, _canonical(result), _now()),
            )
        return revision

    def resolve_day(
        self,
        *,
        local_date: str,
        timezone: str,
        boundary: str,
        window_start: str,
        window_end: str,
        boundary_policy_revision: str | None = None,
    ) -> JournalDayComposition:
        del boundary_policy_revision  # pinned when the day is explicitly persisted
        target = _validate_local_date(local_date)
        with self.store._connect() as conn:
            snapshot = conn.execute(
                """
                SELECT s.*, d.local_date, d.timezone, d.boundary, d.window_start, d.window_end
                FROM journal_day_composition_snapshots AS s
                JOIN journal_days AS d ON d.day_id=s.day_id
                WHERE d.local_date=? AND (
                    s.import_cohort_id IS NULL OR (
                        EXISTS(SELECT 1 FROM journal_import_cohorts AS cohort
                            WHERE cohort.cohort_id=s.import_cohort_id
                              AND cohort.state='sealed')
                        AND EXISTS(SELECT 1 FROM journal_authority_control AS authority
                            WHERE authority.singleton=1
                              AND authority.mode='database_only')
                    )
                )
                """,
                (local_date,),
            ).fetchone()
            if snapshot is not None:
                return self._persisted_composition(conn, snapshot)
            profile_row = conn.execute(
                """
                SELECT p.*, a.activation_revision
                FROM journal_profile_activation_epochs AS a
                JOIN journal_profile_revisions AS p
                  ON p.profile_id=a.profile_id
                 AND p.profile_revision=a.profile_revision
                WHERE a.import_cohort_id IS NULL AND a.effective_local_date <= ?
                ORDER BY a.effective_local_date DESC, a.activation_revision DESC
                LIMIT 1
                """,
                (local_date,),
            ).fetchone()
            if profile_row is None:
                raise JournalCaptureValidationError("No Journal profile applies to that day.")
            profile = self._profile(profile_row)
            activation_revision = int(profile_row["activation_revision"])
            module_rows = conn.execute(
                """
                SELECT s.slot_id,s.ordinal,m.*
                FROM journal_profile_module_slots AS s
                JOIN journal_module_instance_versions AS m
                  ON m.module_instance_id=s.module_instance_id
                 AND m.instance_version=s.module_instance_version
                WHERE s.profile_id=? AND s.profile_revision=?
                ORDER BY s.ordinal
                """,
                (profile.profile_id, profile.profile_revision),
            ).fetchall()
            modules: list[JournalDayModule] = []
            fields: list[JournalDayField] = []
            for module_row in module_rows:
                module = self._module(module_row)
                membership, evidence = _schedule_membership(
                    module.schedule_kind, module.schedule, target
                )
                modules.append(
                    JournalDayModule(
                        slot_id=str(module_row["slot_id"]),
                        ordinal=int(module_row["ordinal"]),
                        module=module,
                        semantic_membership=membership,
                        schedule_evidence=evidence,
                    )
                )
                if membership != "included":
                    continue
                field_rows = conn.execute(
                    """
                    SELECT s.*,
                           f.label,f.description,f.value_kind,f.unit,
                           f.constraints_json,f.value_codec_version,
                           f.function_id,f.function_version,
                           f.behavior_id,f.behavior_version,
                           f.privacy_class,f.search_mode,f.disclosure_policy_id,
                           p.wording AS prompt_wording,p.help_text AS prompt_help,
                           p.requiredness AS prompt_requiredness,
                           p.schedule_kind AS prompt_schedule_kind,
                           p.schedule_json AS prompt_schedule_json
                    FROM journal_module_field_slots AS s
                    JOIN journal_field_definition_versions AS f
                      ON f.field_id=s.field_id
                     AND f.definition_version=s.field_definition_version
                    LEFT JOIN journal_prompt_definition_versions AS p
                      ON p.prompt_id=s.prompt_id AND p.prompt_version=s.prompt_version
                    WHERE s.module_instance_id=? AND s.module_instance_version=?
                    ORDER BY s.ordinal, s.slot_id
                    """,
                    (module.module_instance_id, module.instance_version),
                ).fetchall()
                for field_row in field_rows:
                    composition_slot = f"{module_row['slot_id']}:{field_row['slot_id']}"
                    prompt_included = field_row["prompt_id"] is not None
                    if prompt_included:
                        prompt_membership = _schedule_membership(
                            str(field_row["prompt_schedule_kind"]),
                            _mapping(field_row["prompt_schedule_json"]),
                            target,
                        )[0]
                        prompt_included = prompt_membership == "included"
                    fields.append(
                        JournalDayField(
                            composition_slot_id=composition_slot,
                            module_slot_id=str(module_row["slot_id"]),
                            ordinal=int(field_row["ordinal"]),
                            field_id=str(field_row["field_id"]),
                            field_definition_version=int(
                                field_row["field_definition_version"]
                            ),
                            label=str(field_row["label"]),
                            description=str(field_row["description"]),
                            value_kind=str(field_row["value_kind"]),
                            unit=field_row["unit"],
                            constraints=_mapping(field_row["constraints_json"]),
                            value_codec_version=int(field_row["value_codec_version"]),
                            function_id=field_row["function_id"],
                            function_version=(
                                int(field_row["function_version"])
                                if field_row["function_version"] is not None
                                else None
                            ),
                            behavior_id=str(field_row["behavior_id"]),
                            behavior_version=int(field_row["behavior_version"]),
                            privacy_class=str(field_row["privacy_class"]),
                            search_mode=str(field_row["search_mode"]),
                            disclosure_policy_id=str(
                                field_row["disclosure_policy_id"]
                            ),
                            prompt_id=(
                                field_row["prompt_id"] if prompt_included else None
                            ),
                            prompt_version=(
                                int(field_row["prompt_version"])
                                if prompt_included
                                and field_row["prompt_version"] is not None
                                else None
                            ),
                            prompt_wording=(
                                field_row["prompt_wording"] if prompt_included else None
                            ),
                            prompt_help=(
                                field_row["prompt_help"] if prompt_included else None
                            ),
                            prompt_requiredness=(
                                field_row["prompt_requiredness"]
                                if prompt_included
                                else None
                            ),
                        )
                    )
        composition_payload = {
            "activationRevision": activation_revision,
            "fields": [asdict(item) for item in fields],
            "modules": [
                {
                    "slotId": item.slot_id,
                    "ordinal": item.ordinal,
                    "moduleInstanceId": item.module.module_instance_id,
                    "moduleInstanceVersion": item.module.instance_version,
                    "moduleTypeId": item.module.module_type_id,
                    "moduleTypeVersion": item.module.module_type_version,
                    "membership": item.semantic_membership,
                    "scheduleEvidence": dict(item.schedule_evidence),
                }
                for item in modules
            ],
            "profileDigest": profile.profile_digest,
            "searchRecipeVersion": 1,
        }
        return JournalDayComposition(
            local_date=local_date,
            day_id=_day_id(local_date, timezone, boundary),
            timezone=timezone,
            boundary=boundary,
            window_start=window_start,
            window_end=window_end,
            profile=profile,
            activation_revision=activation_revision,
            modules=tuple(modules),
            fields=tuple(fields),
            composition_digest=_sha(composition_payload),
            persisted=False,
        )

    def ensure_day(
        self,
        *,
        local_date: str,
        timezone: str,
        boundary: str,
        window_start: str,
        window_end: str,
        boundary_policy_revision: str | None,
        created_by: str,
    ) -> JournalDayComposition:
        resolved = self.resolve_day(
            local_date=local_date,
            timezone=timezone,
            boundary=boundary,
            window_start=window_start,
            window_end=window_end,
            boundary_policy_revision=boundary_policy_revision,
        )
        if resolved.persisted:
            return resolved
        snapshot_id = "jds_" + hashlib.sha256(
            f"{resolved.day_id}\x00{resolved.composition_digest}".encode("utf-8")
        ).hexdigest()[:32]
        now = _now()
        with self.store.transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM journal_days WHERE local_date=?", (local_date,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO journal_days(
                        day_id,local_date,timezone,boundary,window_start,window_end,
                        boundary_policy_revision,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        resolved.day_id,
                        local_date,
                        timezone,
                        boundary,
                        window_start,
                        window_end,
                        boundary_policy_revision,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO journal_day_composition_snapshots(
                        snapshot_id,day_id,profile_id,profile_revision,profile_digest,
                        activation_revision,composition_digest,search_recipe_version,
                        schedule_timezone,schedule_window_start,schedule_window_end,
                        created_by,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        snapshot_id,
                        resolved.day_id,
                        resolved.profile.profile_id,
                        resolved.profile.profile_revision,
                        resolved.profile.profile_digest,
                        resolved.activation_revision,
                        resolved.composition_digest,
                        resolved.search_recipe_version,
                        timezone,
                        window_start,
                        window_end,
                        created_by,
                        now,
                    ),
                )
                for item in resolved.modules:
                    conn.execute(
                        """
                        INSERT INTO journal_day_composition_modules(
                            snapshot_id,slot_id,ordinal,module_instance_id,
                            module_instance_version,module_type_id,module_type_version,
                            semantic_membership,schedule_kind,schedule_evidence_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            snapshot_id,
                            item.slot_id,
                            item.ordinal,
                            item.module.module_instance_id,
                            item.module.instance_version,
                            item.module.module_type_id,
                            item.module.module_type_version,
                            item.semantic_membership,
                            item.module.schedule_kind,
                            _canonical(item.schedule_evidence),
                        ),
                    )
                for field in resolved.fields:
                    conn.execute(
                        """
                        INSERT INTO journal_day_composition_fields(
                            snapshot_id,composition_slot_id,module_slot_id,ordinal,
                            field_id,field_definition_version,prompt_id,prompt_version
                        ) VALUES(?,?,?,?,?,?,?,?)
                        """,
                        (
                            snapshot_id,
                            field.composition_slot_id,
                            field.module_slot_id,
                            field.ordinal,
                            field.field_id,
                            field.field_definition_version,
                            field.prompt_id,
                            field.prompt_version,
                        ),
                    )
                self._enqueue_search(
                    conn,
                    aggregate_type="day",
                    aggregate_id=resolved.day_id,
                    aggregate_revision="1",
                    event_kind="composition_changed",
                    content_sha256=resolved.composition_digest,
                    composition_digest=resolved.composition_digest,
                    privacy_class="private",
                    committed_at=now,
                )
        return self.resolve_day(
            local_date=local_date,
            timezone=timezone,
            boundary=boundary,
            window_start=window_start,
            window_end=window_end,
            boundary_policy_revision=boundary_policy_revision,
        )

    # ------------------------------------------------------------------
    # Native plain items and typed observations

    def create_native_item(
        self,
        *,
        local_date: str,
        item_kind: str,
        plain_value: str,
        source_ref: str,
        interaction_behavior_id: str,
        interaction_behavior_version: int,
        client_mutation_id: str,
        actor: Mapping[str, Any],
        module_instance_id: str | None = None,
        module_instance_version: int | None = None,
        privacy_class: str = "private",
        search_mode: str = "lexical_dense",
        authorship: str = "human",
        review_state: str = "not_applicable",
        source_dependency_id: str | None = None,
    ) -> JournalNativeItem:
        _validate_local_date(local_date)
        if not plain_value or not source_ref:
            raise JournalCaptureValidationError(
                "A retained Source and non-empty Journal value are required."
            )
        request = {
            "local_date": local_date,
            "item_kind": item_kind,
            "plain_value_sha256": _sha(plain_value),
            "source_ref": source_ref,
            "behavior": [interaction_behavior_id, interaction_behavior_version],
            "module": [module_instance_id, module_instance_version],
            "privacy_class": privacy_class,
            "search_mode": search_mode,
        }
        if source_dependency_id is not None:
            request["source_dependency_id"] = source_dependency_id
        request_sha = _sha(request)
        content_sha = _sha(plain_value)
        now = _now()
        item_id = "ji_" + uuid.uuid4().hex
        with self.store.transaction() as conn:
            replay = self._mutation_replay(conn, client_mutation_id, request_sha)
            if replay is not None:
                return self.get_native_item(str(replay["item_id"]))
            dependency = None
            if source_dependency_id is not None:
                dependency = conn.execute(
                    "SELECT * FROM journal_native_source_dependencies "
                    "WHERE dependency_id=?",
                    (source_dependency_id,),
                ).fetchone()
                if (
                    dependency is None
                    or str(dependency["client_mutation_id"]) != client_mutation_id
                    or str(dependency["source_ref"]) != source_ref
                    or str(dependency["content_sha256"]) != content_sha
                    or str(dependency["state"]) != "reserved"
                    or dependency["item_id"] is not None
                ):
                    raise JournalCaptureConflict(
                        "The native Journal Source dependency is unavailable."
                    )
            conn.execute(
                """
                INSERT INTO journal_items(
                    item_id,local_date,module_instance_id,module_instance_version,
                    item_kind,authority_kind,current_plain_value,current_content_sha256,
                    interaction_behavior_id,interaction_behavior_version,privacy_class,
                    search_mode,source_ref,created_at,updated_at
                ) VALUES(?,?,?,?,?,'native_plain',?,?,?,?,?,?,?,?,?)
                """,
                (
                    item_id,
                    local_date,
                    module_instance_id,
                    module_instance_version,
                    item_kind,
                    plain_value,
                    content_sha,
                    interaction_behavior_id,
                    interaction_behavior_version,
                    privacy_class,
                    search_mode,
                    source_ref,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO journal_item_revisions(
                    item_id,revision,authority_kind,plain_value,content_sha256,lifecycle,
                    actor_json,source_ref,authorship,review_state,intent_id,created_at
                ) VALUES(?,1,'native_plain',?,?,'current',?,?,?,?,?,?)
                """,
                (
                    item_id,
                    plain_value,
                    content_sha,
                    _canonical(actor),
                    source_ref,
                    authorship,
                    review_state,
                    client_mutation_id,
                    now,
                ),
            )
            self._enqueue_search(
                conn,
                aggregate_type="item",
                aggregate_id=item_id,
                aggregate_revision="1",
                event_kind="upsert",
                content_sha256=content_sha,
                composition_digest=self._composition_digest(conn, local_date),
                privacy_class=privacy_class,
                committed_at=now,
            )
            if source_dependency_id is not None:
                bound = conn.execute(
                    "UPDATE journal_native_source_dependencies SET item_id=?,"
                    "state='bound',updated_at=? WHERE dependency_id=? "
                    "AND state='reserved' AND item_id IS NULL",
                    (item_id, now, source_dependency_id),
                ).rowcount
                if bound != 1:
                    raise JournalCaptureConflict(
                        "The native Journal Source dependency changed concurrently."
                    )
            self._record_mutation(
                conn, client_mutation_id, request_sha, {"item_id": item_id}, now
            )
        return self.get_native_item(item_id)

    def update_native_item(
        self,
        *,
        item_id: str,
        expected_revision: int,
        plain_value: str,
        client_mutation_id: str,
        actor: Mapping[str, Any],
        source_ref: str | None = None,
        authorship: str = "human",
        review_state: str = "not_applicable",
        operation: str = "edit",
        source_dependency_id: str | None = None,
        stated_at: str | None = None,
    ) -> JournalNativeItem:
        if operation not in {"edit", "correct"}:
            raise JournalCaptureValidationError(
                "That Journal content operation is invalid."
            )
        if not plain_value:
            raise JournalCaptureValidationError("A Journal value cannot be empty.")
        if stated_at is not None:
            _validate_stated_instant(stated_at)
        request = {
            "operation": operation,
            "item_id": item_id,
            "expected_revision": expected_revision,
            "plain_value_sha256": _sha(plain_value),
            "source_ref": source_ref,
            "authorship": authorship,
            "review_state": review_state,
        }
        if stated_at is not None:
            request["stated_at"] = stated_at
        if source_dependency_id is not None:
            request["source_dependency_id"] = source_dependency_id
        request_sha = _sha(request)
        now = _now()
        with self.store.transaction() as conn:
            replay = self._mutation_replay(conn, client_mutation_id, request_sha)
            if replay is not None:
                return self.get_native_item(str(replay["item_id"]))
            row = conn.execute(
                """
                SELECT * FROM journal_items AS item WHERE item.item_id=?
                  AND (
                    item.import_cohort_id IS NULL
                    OR (
                      EXISTS(
                        SELECT 1 FROM journal_import_cohorts AS cohort
                        WHERE cohort.cohort_id=item.import_cohort_id
                          AND cohort.state='sealed'
                      )
                      AND EXISTS(
                        SELECT 1 FROM journal_authority_control AS authority
                        WHERE authority.singleton=1 AND authority.mode='database_only'
                      )
                    )
                  )
                """,
                (item_id,),
            ).fetchone()
            if row is None or row["authority_kind"] != "native_plain":
                raise JournalCaptureValidationError("That Journal item is not editable here.")
            if int(row["current_revision"]) != expected_revision:
                raise JournalCaptureConflict("The Journal item changed before this edit.")
            if row["lifecycle"] in {"tombstoned", "superseded"}:
                raise JournalCaptureConflict(
                    "That Journal item must be restored before its content can change."
                )
            revision = expected_revision + 1
            next_source = source_ref or row["source_ref"]
            dependency = None
            if source_dependency_id is not None:
                dependency = conn.execute(
                    "SELECT * FROM journal_item_revision_source_dependencies "
                    "WHERE dependency_id=?",
                    (source_dependency_id,),
                ).fetchone()
                if (
                    dependency is None
                    or str(dependency["client_mutation_id"]) != client_mutation_id
                    or str(dependency["item_id"]) != item_id
                    or int(dependency["expected_revision"]) != expected_revision
                    or str(dependency["operation_kind"]) != operation
                    or str(dependency["source_ref"]) != next_source
                    or str(dependency["content_sha256"]) != _sha(plain_value)
                    or str(dependency["state"]) != "reserved"
                    or dependency["item_revision"] is not None
                ):
                    raise JournalCaptureConflict(
                        "The Journal edit Source dependency is unavailable."
                    )
            action_actor = {
                "schema": "wb.journal-item-action-actor/v1",
                "operation": operation,
                "actor": dict(actor),
            }
            # ``created_at`` carries the occurrence time a person states, which
            # is what the day's chronological order and displayed time read.
            # A correction that names a new time moves the entry to where it
            # belongs. One that names none leaves the stated time untouched.
            conn.execute(
                "UPDATE journal_items SET current_plain_value=?,current_content_sha256=?,"
                "source_ref=?,lifecycle='current',current_revision=?,"
                "created_at=COALESCE(?,created_at),updated_at=? "
                "WHERE item_id=?",
                (
                    plain_value,
                    _sha(plain_value),
                    next_source,
                    revision,
                    stated_at,
                    now,
                    item_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO journal_item_revisions(
                    item_id,revision,authority_kind,plain_value,content_sha256,lifecycle,
                    actor_json,source_ref,authorship,review_state,intent_id,created_at
                ) VALUES(?,?,'native_plain',?,?,'current',?,?,?,?,?,?)
                """,
                (
                    item_id,
                    revision,
                    plain_value,
                    _sha(plain_value),
                    _canonical(action_actor),
                    next_source,
                    authorship,
                    review_state,
                    client_mutation_id,
                    now,
                ),
            )
            self._enqueue_search(
                conn,
                aggregate_type="item",
                aggregate_id=item_id,
                aggregate_revision=str(revision),
                event_kind="upsert",
                content_sha256=_sha(plain_value),
                composition_digest=self._composition_digest(conn, row["local_date"]),
                privacy_class=row["privacy_class"],
                committed_at=now,
            )
            if source_dependency_id is not None:
                bound = conn.execute(
                    "UPDATE journal_item_revision_source_dependencies SET "
                    "item_revision=?,state='bound',updated_at=? "
                    "WHERE dependency_id=? AND state='reserved' "
                    "AND item_revision IS NULL",
                    (revision, now, source_dependency_id),
                ).rowcount
                if bound != 1:
                    raise JournalCaptureConflict(
                        "The Journal edit Source dependency changed concurrently."
                    )
            self._record_mutation(
                conn, client_mutation_id, request_sha, {"item_id": item_id}, now
            )
        return self.get_native_item(item_id)

    def transition_native_item(
        self,
        *,
        item_id: str,
        expected_revision: int,
        operation: str,
        client_mutation_id: str,
        actor: Mapping[str, Any],
    ) -> JournalNativeItem:
        """Resolve, tombstone, or restore a native item with CAS history.

        Lifecycle actions never invent a new content Source.  Their immutable
        revision records retain the actor and action while carrying forward the
        exact content/source identity of the prior revision.
        """

        transitions = {
            "resolve": ({"current"}, "resolved"),
            "tombstone": ({"current", "resolved", "archived"}, "tombstoned"),
            "restore": ({"resolved", "archived", "tombstoned"}, "current"),
        }
        selected = transitions.get(operation)
        if selected is None:
            raise JournalCaptureValidationError(
                "That Journal lifecycle operation is invalid."
            )
        request = {
            "operation": operation,
            "item_id": item_id,
            "expected_revision": expected_revision,
        }
        request_sha = _sha(request)
        now = _now()
        with self.store.transaction() as conn:
            replay = self._mutation_replay(conn, client_mutation_id, request_sha)
            if replay is not None:
                return self.get_native_item(str(replay["item_id"]))
            row = self._visible_item_row(conn, item_id)
            if row is None or row["authority_kind"] not in {"native_plain", "generated"}:
                raise JournalCaptureValidationError(
                    "That Journal item is unavailable for this action."
                )
            if int(row["current_revision"]) != expected_revision:
                raise JournalCaptureConflict(
                    "The Journal item changed before this action."
                )
            allowed, next_lifecycle = selected
            if str(row["lifecycle"]) not in allowed:
                raise JournalCaptureConflict(
                    "That Journal item is no longer in a state that permits this action."
                )
            revision = expected_revision + 1
            action_actor = {
                "schema": "wb.journal-item-action-actor/v1",
                "operation": operation,
                "actor": dict(actor),
            }
            prior_revision = conn.execute(
                "SELECT authorship,review_state FROM journal_item_revisions "
                "WHERE item_id=? AND revision=?",
                (item_id, expected_revision),
            ).fetchone()
            authorship = (
                str(prior_revision["authorship"])
                if prior_revision is not None
                else "unknown"
            )
            review_state = (
                str(prior_revision["review_state"])
                if prior_revision is not None
                else "unknown"
            )
            conn.execute(
                "UPDATE journal_items SET lifecycle=?,current_revision=?,updated_at=? "
                "WHERE item_id=?",
                (next_lifecycle, revision, now, item_id),
            )
            conn.execute(
                "INSERT INTO journal_item_revisions("
                "item_id,revision,authority_kind,plain_value,content_sha256,lifecycle,"
                "actor_json,source_ref,authorship,review_state,intent_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    revision,
                    row["authority_kind"],
                    row["current_plain_value"],
                    row["current_content_sha256"],
                    next_lifecycle,
                    _canonical(action_actor),
                    row["source_ref"],
                    authorship,
                    review_state,
                    client_mutation_id,
                    now,
                ),
            )
            if operation == "restore":
                self._archive_active_routes(
                    conn,
                    item_id=item_id,
                    actor=action_actor,
                    intent_id=client_mutation_id,
                    now=now,
                )
            self._enqueue_search(
                conn,
                aggregate_type="item",
                aggregate_id=item_id,
                aggregate_revision=str(revision),
                event_kind="delete" if next_lifecycle == "tombstoned" else "upsert",
                content_sha256=str(row["current_content_sha256"] or _sha("")),
                composition_digest=self._composition_digest(
                    conn, str(row["local_date"])
                ),
                privacy_class=str(row["privacy_class"]),
                committed_at=now,
            )
            self._record_mutation(
                conn, client_mutation_id, request_sha, {"item_id": item_id}, now
            )
        return self.get_native_item(item_id)

    def route_native_item(
        self,
        *,
        item_id: str,
        expected_revision: int,
        target_domain: str,
        target_id: str,
        client_mutation_id: str,
        actor: Mapping[str, Any],
        target_revision: str | None = None,
    ) -> tuple[JournalNativeItem, JournalRelation]:
        """Route an item to a typed domain reference and resolve it atomically."""

        allowed_domains = {
            "task",
            "project",
            "contract",
            "entity",
            "session",
            "calendar_event",
            "cowork_document",
            "consideration",
        }
        if target_domain not in allowed_domains or not target_id.strip():
            raise JournalCaptureValidationError(
                "Choose a supported Journal route destination."
            )
        request = {
            "operation": "route",
            "item_id": item_id,
            "expected_revision": expected_revision,
            "target_domain": target_domain,
            "target_id": target_id,
            "target_revision": target_revision,
        }
        request_sha = _sha(request)
        now = _now()
        with self.store.transaction() as conn:
            replay = self._mutation_replay(conn, client_mutation_id, request_sha)
            if replay is not None:
                return (
                    self.get_native_item(str(replay["item_id"])),
                    self.get_relation(str(replay["relation_id"])),
                )
            row = self._visible_item_row(conn, item_id)
            if row is None or row["authority_kind"] not in {"native_plain", "generated"}:
                raise JournalCaptureValidationError(
                    "That Journal item is unavailable for routing."
                )
            if int(row["current_revision"]) != expected_revision:
                raise JournalCaptureConflict(
                    "The Journal item changed before it could be routed."
                )
            if str(row["lifecycle"]) not in {"current", "resolved"}:
                raise JournalCaptureConflict(
                    "That Journal item is no longer available for routing."
                )
            action_actor = {
                "schema": "wb.journal-item-action-actor/v1",
                "operation": "route",
                "actor": dict(actor),
                "target": {
                    "domain": target_domain,
                    "id": target_id,
                    "revision": target_revision,
                },
            }
            self._archive_active_routes(
                conn,
                item_id=item_id,
                actor=action_actor,
                intent_id=client_mutation_id,
                now=now,
            )
            relation_id = "jr_" + uuid.uuid4().hex
            conn.execute(
                "INSERT INTO journal_relations("
                "relation_id,source_item_id,relation_kind,target_domain,target_id,"
                "target_revision,actor_json,created_at,updated_at) "
                "VALUES(?,?,'routed_to',?,?,?,?,?,?)",
                (
                    relation_id,
                    item_id,
                    target_domain,
                    target_id,
                    target_revision,
                    _canonical(action_actor),
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO journal_relation_revisions("
                "relation_id,revision,relation_kind,target_domain,target_id,"
                "target_revision,lifecycle,actor_json,source_ref,intent_id,created_at) "
                "VALUES(?,1,'routed_to',?,?,?,'current',?,NULL,?,?)",
                (
                    relation_id,
                    target_domain,
                    target_id,
                    target_revision,
                    _canonical(action_actor),
                    client_mutation_id,
                    now,
                ),
            )
            revision = expected_revision + 1
            prior_revision = conn.execute(
                "SELECT authorship,review_state FROM journal_item_revisions "
                "WHERE item_id=? AND revision=?",
                (item_id, expected_revision),
            ).fetchone()
            conn.execute(
                "UPDATE journal_items SET lifecycle='resolved',current_revision=?,"
                "updated_at=? WHERE item_id=?",
                (revision, now, item_id),
            )
            conn.execute(
                "INSERT INTO journal_item_revisions("
                "item_id,revision,authority_kind,plain_value,content_sha256,lifecycle,"
                "actor_json,source_ref,authorship,review_state,intent_id,created_at) "
                "VALUES(?,?,?,?,?,'resolved',?,?,?,?,?,?)",
                (
                    item_id,
                    revision,
                    row["authority_kind"],
                    row["current_plain_value"],
                    row["current_content_sha256"],
                    _canonical(action_actor),
                    row["source_ref"],
                    str(prior_revision["authorship"]) if prior_revision else "unknown",
                    str(prior_revision["review_state"]) if prior_revision else "unknown",
                    client_mutation_id,
                    now,
                ),
            )
            for aggregate_type, aggregate_id, aggregate_revision, content in (
                ("item", item_id, str(revision), row["current_content_sha256"]),
                ("relation", relation_id, "1", _sha(request)),
            ):
                self._enqueue_search(
                    conn,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    aggregate_revision=aggregate_revision,
                    event_kind="upsert",
                    content_sha256=str(content or _sha("")),
                    composition_digest=self._composition_digest(
                        conn, str(row["local_date"])
                    ),
                    privacy_class=str(row["privacy_class"]),
                    committed_at=now,
                )
            self._record_mutation(
                conn,
                client_mutation_id,
                request_sha,
                {"item_id": item_id, "relation_id": relation_id},
                now,
            )
        return self.get_native_item(item_id), self.get_relation(relation_id)

    def get_native_item(self, item_id: str) -> JournalNativeItem:
        with self.store._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM journal_items AS item WHERE item.item_id=?
                  AND (
                    item.import_cohort_id IS NULL
                    OR (
                      EXISTS(
                        SELECT 1 FROM journal_import_cohorts AS cohort
                        WHERE cohort.cohort_id=item.import_cohort_id
                          AND cohort.state='sealed'
                      )
                      AND EXISTS(
                        SELECT 1 FROM journal_authority_control AS authority
                        WHERE authority.singleton=1 AND authority.mode='database_only'
                      )
                    )
                  )
                """,
                (item_id,),
            ).fetchone()
        if row is None:
            raise JournalCaptureValidationError("That Journal item is unavailable.")
        return self._item(row)

    def list_native_items(
        self,
        local_date: str,
        *,
        include_inactive: bool = False,
    ) -> tuple[JournalNativeItem, ...]:
        _validate_local_date(local_date)
        lifecycle_filter = (
            ""
            if include_inactive
            else "AND item.lifecycle NOT IN ('tombstoned','superseded')"
        )
        with self.store._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM journal_items AS item
                WHERE item.local_date=?
                  {lifecycle_filter}
                  AND (
                    item.import_cohort_id IS NULL
                    OR (
                      EXISTS(
                        SELECT 1 FROM journal_import_cohorts AS cohort
                        WHERE cohort.cohort_id=item.import_cohort_id
                          AND cohort.state='sealed'
                      )
                      AND EXISTS(
                        SELECT 1 FROM journal_authority_control AS authority
                        WHERE authority.singleton=1 AND authority.mode='database_only'
                      )
                    )
                  )
                ORDER BY item.created_at,item.item_id
                """,
                (local_date,),
            ).fetchall()
        return tuple(self._item(row) for row in rows)

    def create_relation(
        self,
        *,
        source_item_id: str,
        relation_kind: str,
        target_domain: str,
        target_id: str,
        client_mutation_id: str,
        actor: Mapping[str, Any],
        target_revision: str | None = None,
        source_ref: str | None = None,
        relation_id: str | None = None,
    ) -> JournalRelation:
        if not source_item_id or not relation_kind or not target_domain or not target_id:
            raise JournalCaptureValidationError("The Journal relation is invalid.")
        request = {
            "operation": "create_relation",
            "relation_id": relation_id,
            "source_item_id": source_item_id,
            "relation_kind": relation_kind,
            "target_domain": target_domain,
            "target_id": target_id,
            "target_revision": target_revision,
            "source_ref": source_ref,
        }
        request_sha = _sha(request)
        now = _now()
        next_id = relation_id or "jr_" + uuid.uuid4().hex
        with self.store.transaction() as conn:
            replay = self._mutation_replay(conn, client_mutation_id, request_sha)
            if replay is not None:
                return self.get_relation(str(replay["relation_id"]))
            item = conn.execute(
                """
                SELECT item.local_date,item.privacy_class,item.lifecycle
                FROM journal_items AS item
                WHERE item.item_id=?
                  AND (
                    item.import_cohort_id IS NULL
                    OR (
                      EXISTS(
                        SELECT 1 FROM journal_import_cohorts AS cohort
                        WHERE cohort.cohort_id=item.import_cohort_id
                          AND cohort.state='sealed'
                      )
                      AND EXISTS(
                        SELECT 1 FROM journal_authority_control AS authority
                        WHERE authority.singleton=1 AND authority.mode='database_only'
                      )
                    )
                  )
                """,
                (source_item_id,),
            ).fetchone()
            if item is None or item["lifecycle"] in {"tombstoned", "superseded"}:
                raise JournalCaptureValidationError(
                    "That Journal relation source is unavailable."
                )
            duplicate = conn.execute(
                "SELECT relation_id FROM journal_relations "
                "WHERE source_item_id=? AND relation_kind=? AND target_domain=? "
                "AND target_id=?",
                (source_item_id, relation_kind, target_domain, target_id),
            ).fetchone()
            if duplicate is not None:
                raise JournalCaptureConflict("That Journal relation already exists.")
            conn.execute(
                """
                INSERT INTO journal_relations(
                    relation_id,source_item_id,relation_kind,target_domain,target_id,
                    target_revision,actor_json,source_ref,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    next_id,
                    source_item_id,
                    relation_kind,
                    target_domain,
                    target_id,
                    target_revision,
                    _canonical(actor),
                    source_ref,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO journal_relation_revisions(
                    relation_id,revision,relation_kind,target_domain,target_id,
                    target_revision,lifecycle,actor_json,source_ref,intent_id,created_at
                ) VALUES(?,1,?,?,?,?, 'current',?,?,?,?)
                """,
                (
                    next_id,
                    relation_kind,
                    target_domain,
                    target_id,
                    target_revision,
                    _canonical(actor),
                    source_ref,
                    client_mutation_id,
                    now,
                ),
            )
            self._enqueue_search(
                conn,
                aggregate_type="relation",
                aggregate_id=next_id,
                aggregate_revision="1",
                event_kind="upsert",
                content_sha256=_sha(request),
                composition_digest=self._composition_digest(
                    conn, str(item["local_date"])
                ),
                privacy_class=str(item["privacy_class"]),
                committed_at=now,
            )
            self._record_mutation(
                conn,
                client_mutation_id,
                request_sha,
                {"relation_id": next_id},
                now,
            )
        return self.get_relation(next_id)

    def update_relation(
        self,
        *,
        relation_id: str,
        expected_revision: int,
        target_revision: str | None,
        lifecycle: str,
        client_mutation_id: str,
        actor: Mapping[str, Any],
        source_ref: str | None = None,
    ) -> JournalRelation:
        if lifecycle not in {"current", "archived", "tombstoned"}:
            raise JournalCaptureValidationError(
                "That Journal relation lifecycle is invalid."
            )
        request = {
            "operation": "update_relation",
            "relation_id": relation_id,
            "expected_revision": expected_revision,
            "target_revision": target_revision,
            "lifecycle": lifecycle,
            "source_ref": source_ref,
        }
        request_sha = _sha(request)
        now = _now()
        with self.store.transaction() as conn:
            replay = self._mutation_replay(conn, client_mutation_id, request_sha)
            if replay is not None:
                return self.get_relation(str(replay["relation_id"]))
            row = conn.execute(
                "SELECT r.*,i.local_date,i.privacy_class "
                "FROM journal_relations AS r "
                "JOIN journal_items AS i ON i.item_id=r.source_item_id "
                "WHERE r.relation_id=?",
                (relation_id,),
            ).fetchone()
            if row is None:
                raise JournalCaptureValidationError(
                    "That Journal relation is unavailable."
                )
            if int(row["revision"]) != expected_revision:
                raise JournalCaptureConflict(
                    "The Journal relation changed before this edit."
                )
            revision = expected_revision + 1
            next_source = source_ref or row["source_ref"]
            conn.execute(
                "UPDATE journal_relations SET target_revision=?,lifecycle=?,revision=?,"
                "actor_json=?,source_ref=?,updated_at=? WHERE relation_id=?",
                (
                    target_revision,
                    lifecycle,
                    revision,
                    _canonical(actor),
                    next_source,
                    now,
                    relation_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO journal_relation_revisions(
                    relation_id,revision,relation_kind,target_domain,target_id,
                    target_revision,lifecycle,actor_json,source_ref,intent_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    relation_id,
                    revision,
                    row["relation_kind"],
                    row["target_domain"],
                    row["target_id"],
                    target_revision,
                    lifecycle,
                    _canonical(actor),
                    next_source,
                    client_mutation_id,
                    now,
                ),
            )
            event_kind = "delete" if lifecycle == "tombstoned" else "upsert"
            self._enqueue_search(
                conn,
                aggregate_type="relation",
                aggregate_id=relation_id,
                aggregate_revision=str(revision),
                event_kind=event_kind,
                content_sha256=_sha(request),
                composition_digest=self._composition_digest(
                    conn, str(row["local_date"])
                ),
                privacy_class=str(row["privacy_class"]),
                committed_at=now,
            )
            self._record_mutation(
                conn,
                client_mutation_id,
                request_sha,
                {"relation_id": relation_id},
                now,
            )
        return self.get_relation(relation_id)

    def get_relation(self, relation_id: str) -> JournalRelation:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_relations WHERE relation_id=?",
                (relation_id,),
            ).fetchone()
        if row is None:
            raise JournalCaptureValidationError(
                "That Journal relation is unavailable."
            )
        return self._relation(row)

    def list_relations(self, source_item_id: str) -> tuple[JournalRelation, ...]:
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM journal_relations WHERE source_item_id=? "
                "ORDER BY created_at,relation_id",
                (source_item_id,),
            ).fetchall()
        return tuple(self._relation(row) for row in rows)

    def put_field_value(
        self,
        *,
        value_id: str,
        local_date: str,
        module_instance_id: str,
        module_instance_version: int,
        field_id: str,
        field_definition_version: int,
        client_mutation_id: str,
        expected_revision: int,
        actor: Mapping[str, Any],
        value: Any = None,
        disposition: str | None = None,
        composition_slot_id: str | None = None,
        prompt_id: str | None = None,
        prompt_version: int | None = None,
        source_ref: str | None = None,
        authorship: str = "human",
        review_state: str = "not_applicable",
        observed_at: str | None = None,
        stated_at: str | None = None,
        source_dependency_id: str | None = None,
    ) -> JournalFieldValue:
        _validate_local_date(local_date)
        slot_id = composition_slot_id or f"field:{field_id}"
        now = _now()
        with self.store.transaction() as conn:
            definition = conn.execute(
                "SELECT * FROM journal_field_definition_versions "
                "WHERE field_id=? AND definition_version=?",
                (field_id, field_definition_version),
            ).fetchone()
            if definition is None:
                raise JournalCaptureValidationError("That Journal field is unavailable.")
            encoded, options, references, frozen = self._encode_field_value(
                definition, value=value, disposition=disposition
            )
            request = {
                "value_id": value_id,
                "local_date": local_date,
                "module": [module_instance_id, module_instance_version],
                "field": [field_id, field_definition_version],
                "prompt": [prompt_id, prompt_version],
                "slot": slot_id,
                "expected_revision": expected_revision,
                "value": frozen,
            }
            if source_dependency_id is not None:
                request["source_dependency_id"] = source_dependency_id
            request_sha = _sha(request)
            replay = self._mutation_replay(conn, client_mutation_id, request_sha)
            if replay is not None:
                return self.get_field_value(str(replay["value_id"]))
            dependency = None
            if source_dependency_id is not None:
                dependency = conn.execute(
                    "SELECT * FROM journal_field_source_dependencies "
                    "WHERE dependency_id=?",
                    (source_dependency_id,),
                ).fetchone()
                if (
                    dependency is None
                    or str(dependency["client_mutation_id"]) != client_mutation_id
                    or str(dependency["source_ref"]) != str(source_ref or "")
                    or str(dependency["value_id"]) != value_id
                    or str(dependency["state"]) != "reserved"
                    or dependency["value_revision"] is not None
                ):
                    raise JournalCaptureConflict(
                        "The Journal field Source dependency is unavailable."
                    )
            prior = conn.execute(
                "SELECT * FROM journal_field_values WHERE value_id=?", (value_id,)
            ).fetchone()
            current = int(prior["current_revision"]) if prior is not None else 0
            if current != expected_revision:
                raise JournalCaptureConflict("The Journal field changed before this edit.")
            revision = current + 1
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
            if prior is None:
                conn.execute(
                    """
                    INSERT INTO journal_field_values(
                        value_id,local_date,composition_slot_id,module_instance_id,
                        module_instance_version,field_id,field_definition_version,
                        prompt_id,prompt_version,value_codec_version,value_kind,
                        disposition,text_value,number_value,boolean_value,temporal_value,
                        duration_seconds,option_value,collection_present,source_ref,
                        authorship,review_state,observed_at,stated_at,ingested_at,
                        current_revision,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        value_id,
                        local_date,
                        slot_id,
                        module_instance_id,
                        module_instance_version,
                        field_id,
                        field_definition_version,
                        prompt_id,
                        prompt_version,
                        int(definition["value_codec_version"]),
                        definition["value_kind"],
                        *columns,
                        source_ref,
                        authorship,
                        review_state,
                        observed_at,
                        stated_at,
                        now,
                        revision,
                        now,
                    ),
                )
            else:
                immutable_identity = (
                    prior["local_date"],
                    prior["module_instance_id"],
                    int(prior["module_instance_version"]),
                    prior["field_id"],
                    int(prior["field_definition_version"]),
                )
                requested_identity = (
                    local_date,
                    module_instance_id,
                    module_instance_version,
                    field_id,
                    field_definition_version,
                )
                if immutable_identity != requested_identity:
                    raise JournalCaptureConflict("A field value cannot change its identity.")
                conn.execute(
                    """
                    UPDATE journal_field_values SET
                        disposition=?,text_value=?,number_value=?,boolean_value=?,
                        temporal_value=?,duration_seconds=?,option_value=?,
                        collection_present=?,source_ref=?,authorship=?,review_state=?,
                        observed_at=?,stated_at=?,current_revision=?,updated_at=?
                    WHERE value_id=?
                    """,
                    (*columns, source_ref, authorship, review_state, observed_at,
                     stated_at, revision, now, value_id),
                )
                conn.execute("DELETE FROM journal_field_value_options WHERE value_id=?", (value_id,))
                conn.execute("DELETE FROM journal_field_value_references WHERE value_id=?", (value_id,))
            for ordinal, option in enumerate(options):
                conn.execute(
                    "INSERT INTO journal_field_value_options(value_id,ordinal,option_id) "
                    "VALUES(?,?,?)",
                    (value_id, ordinal, option),
                )
            for ordinal, reference in enumerate(references):
                conn.execute(
                    """
                    INSERT INTO journal_field_value_references(
                        value_id,ordinal,reference_kind,reference_id,reference_revision
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        value_id,
                        ordinal,
                        reference["kind"],
                        reference["id"],
                        reference.get("revision"),
                    ),
                )
            conn.execute(
                """
                INSERT INTO journal_field_value_revisions(
                    value_id,revision,value_json,value_sha256,actor_json,source_ref,
                    intent_id,created_at,authorship,review_state
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    value_id,
                    revision,
                    _canonical(frozen),
                    _sha(frozen),
                    _canonical(actor),
                    source_ref,
                    client_mutation_id,
                    now,
                    authorship,
                    review_state,
                ),
            )
            self._enqueue_search(
                conn,
                aggregate_type="field_value",
                aggregate_id=value_id,
                aggregate_revision=str(revision),
                event_kind="upsert",
                content_sha256=_sha(frozen),
                composition_digest=self._composition_digest(conn, local_date),
                privacy_class=definition["privacy_class"],
                committed_at=now,
            )
            if source_dependency_id is not None:
                bound = conn.execute(
                    "UPDATE journal_field_source_dependencies SET value_revision=?,"
                    "value_sha256=?,state='bound',updated_at=? WHERE dependency_id=? "
                    "AND state='reserved' AND value_revision IS NULL",
                    (revision, _sha(frozen), now, source_dependency_id),
                ).rowcount
                if bound != 1:
                    raise JournalCaptureConflict(
                        "The Journal field Source dependency changed concurrently."
                    )
            self._record_mutation(
                conn, client_mutation_id, request_sha, {"value_id": value_id}, now
            )
        return self.get_field_value(value_id)

    def get_field_value(self, value_id: str) -> JournalFieldValue:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_field_values AS value WHERE value.value_id=? "
                "AND (value.import_cohort_id IS NULL OR ("
                "EXISTS(SELECT 1 FROM journal_import_cohorts AS cohort "
                "WHERE cohort.cohort_id=value.import_cohort_id AND cohort.state='sealed') "
                "AND EXISTS(SELECT 1 FROM journal_authority_control AS authority "
                "WHERE authority.singleton=1 AND authority.mode='database_only'))) ",
                (value_id,),
            ).fetchone()
            if row is None:
                raise JournalCaptureValidationError("That Journal value is unavailable.")
            options = [
                str(item[0])
                for item in conn.execute(
                    "SELECT option_id FROM journal_field_value_options "
                    "WHERE value_id=? ORDER BY ordinal",
                    (value_id,),
                )
            ]
            references = [
                {
                    "kind": item[0],
                    "id": item[1],
                    "revision": item[2],
                }
                for item in conn.execute(
                    "SELECT reference_kind,reference_id,reference_revision "
                    "FROM journal_field_value_references WHERE value_id=? ORDER BY ordinal",
                    (value_id,),
                )
            ]
        return self._field_value(row, options=options, references=references)

    def list_field_values(self, local_date: str) -> tuple[JournalFieldValue, ...]:
        _validate_local_date(local_date)
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT value_id FROM journal_field_values AS value WHERE local_date=? "
                "AND lifecycle NOT IN ('tombstoned','superseded') "
                "AND (value.import_cohort_id IS NULL OR ("
                "EXISTS(SELECT 1 FROM journal_import_cohorts AS cohort "
                "WHERE cohort.cohort_id=value.import_cohort_id AND cohort.state='sealed') "
                "AND EXISTS(SELECT 1 FROM journal_authority_control AS authority "
                "WHERE authority.singleton=1 AND authority.mode='database_only'))) "
                "ORDER BY module_instance_id,composition_slot_id,value_id",
                (local_date,),
            ).fetchall()
        return tuple(self.get_field_value(str(row[0])) for row in rows)

    # ------------------------------------------------------------------
    # Prompt/result lineage.  These methods only persist supplied receipts;
    # they never invoke a provider on activation, read, or creation.

    def create_prompt_interaction(
        self,
        *,
        interaction_id: str,
        local_date: str,
        module_instance_id: str,
        module_instance_version: int,
        prompt_id: str,
        prompt_version: int,
        input_text: str,
        source_ref: str,
        result_retention: str,
        result_search_mode: str,
        day_id: str | None = None,
        composition_snapshot_id: str | None = None,
        client_mutation_id: str | None = None,
        source_dependency: Mapping[str, Any] | None = None,
    ) -> None:
        _validate_local_date(local_date)
        if not input_text or not source_ref:
            raise JournalCaptureValidationError(
                "A prompt interaction requires immutable input and its Source."
            )
        request = {
            "operation": "create_prompt_interaction",
            "interaction_id": interaction_id,
            "local_date": local_date,
            "module": [module_instance_id, module_instance_version],
            "prompt": [prompt_id, prompt_version],
            "input_sha256": _sha(input_text),
            "source_ref": source_ref,
            "result_retention": result_retention,
            "result_search_mode": result_search_mode,
            "day_id": day_id,
            "composition_snapshot_id": composition_snapshot_id,
        }
        request_sha = _sha(request)
        now = _now()
        with self.store.transaction() as conn:
            if client_mutation_id is not None:
                replay = self._mutation_replay(
                    conn, client_mutation_id, request_sha
                )
                if replay is not None:
                    if str(replay.get("interaction_id")) != interaction_id:
                        raise JournalCaptureConflict(
                            "That prompt mutation key is already bound differently."
                        )
                    return
            existing = conn.execute(
                "SELECT * FROM journal_prompt_interactions WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["input_sha256"] != _sha(input_text)
                    or str(existing["source_ref"]) != source_ref
                    or str(existing["local_date"]) != local_date
                    or str(existing["module_instance_id"]) != module_instance_id
                    or int(existing["module_instance_version"])
                    != module_instance_version
                    or str(existing["prompt_id"]) != prompt_id
                    or int(existing["prompt_version"]) != prompt_version
                    or str(existing["result_retention"]) != result_retention
                    or str(existing["result_search_mode"]) != result_search_mode
                    or existing["day_id"] != day_id
                    or existing["composition_snapshot_id"]
                    != composition_snapshot_id
                ):
                    raise JournalCaptureConflict("That prompt interaction already has other input.")
                return
            occupied_slot = conn.execute(
                "SELECT interaction_id FROM journal_prompt_interactions "
                "WHERE local_date=? AND module_instance_id=? "
                "AND module_instance_version=? AND prompt_id=? AND prompt_version=?",
                (
                    local_date,
                    module_instance_id,
                    module_instance_version,
                    prompt_id,
                    prompt_version,
                ),
            ).fetchone()
            if occupied_slot is not None:
                raise JournalCaptureConflict(
                    "That Journal prompt already has an interaction for this day."
                )
            conn.execute(
                """
                INSERT INTO journal_prompt_interactions(
                    interaction_id,local_date,day_id,composition_snapshot_id,
                    module_instance_id,module_instance_version,
                    prompt_id,prompt_version,input_text,input_sha256,source_ref,
                    result_retention,result_search_mode,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    interaction_id,
                    local_date,
                    day_id,
                    composition_snapshot_id,
                    module_instance_id,
                    module_instance_version,
                    prompt_id,
                    prompt_version,
                    input_text,
                    _sha(input_text),
                    source_ref,
                    result_retention,
                    result_search_mode,
                    now,
                    now,
                ),
            )
            if source_dependency is not None:
                required = {
                    "dependency_id",
                    "client_mutation_id",
                    "request_sha256",
                    "source_usage_consumer_id",
                    "representation_id",
                    "source_usage_id",
                    "purpose",
                }
                if required - set(source_dependency):
                    raise JournalCaptureConflict(
                        "The prompt input Source dependency is incomplete."
                    )
                conn.execute(
                    "INSERT INTO journal_prompt_input_source_dependencies("
                    "dependency_id,client_mutation_id,request_sha256,"
                    "source_usage_consumer_id,source_ref,representation_id,"
                    "source_usage_id,purpose,interaction_id,input_sha256,state,"
                    "created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,'bound',?,?)",
                    (
                        source_dependency["dependency_id"],
                        source_dependency["client_mutation_id"],
                        source_dependency["request_sha256"],
                        source_dependency["source_usage_consumer_id"],
                        source_ref,
                        source_dependency["representation_id"],
                        source_dependency["source_usage_id"],
                        source_dependency["purpose"],
                        interaction_id,
                        _sha(input_text),
                        now,
                        now,
                    ),
                )
            if client_mutation_id is not None:
                self._record_mutation(
                    conn,
                    client_mutation_id,
                    request_sha,
                    {"interaction_id": interaction_id},
                    now,
                )

    def request_prompt_generation(
        self,
        *,
        interaction_id: str,
        expected_revision: int,
        client_mutation_id: str,
        actor: Mapping[str, Any],
        context_manifest: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Persist a manual generation request without running a model inline."""

        context_sha = _sha(context_manifest)
        request = {
            "operation": "request_prompt_generation",
            "interaction_id": interaction_id,
            "expected_revision": expected_revision,
            "context_manifest_sha256": context_sha,
        }
        request_sha = _sha(request)
        now = _now()
        request_id = "jpgr_" + uuid.uuid4().hex
        with self.store.transaction() as conn:
            replay = self._mutation_replay(conn, client_mutation_id, request_sha)
            if replay is not None:
                return {
                    **self.get_prompt_generation_request(
                        str(replay["generation_request_id"])
                    ),
                    "deduplicated": True,
                }
            interaction = conn.execute(
                "SELECT * FROM journal_prompt_interactions WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if interaction is None or str(interaction["lifecycle"]) in {
                "archived",
                "tombstoned",
            }:
                raise JournalCaptureValidationError(
                    "That prompt interaction is unavailable."
                )
            if int(interaction["current_revision"]) != expected_revision:
                raise JournalCaptureConflict(
                    "The prompt result set changed before generation was requested."
                )
            prompt = conn.execute(
                "SELECT definition_sha256 FROM journal_prompt_definition_versions "
                "WHERE prompt_id=? AND prompt_version=?",
                (interaction["prompt_id"], interaction["prompt_version"]),
            ).fetchone()
            if prompt is None:
                raise JournalCaptureValidationError(
                    "That prompt definition is unavailable."
                )
            module_behavior = conn.execute(
                "SELECT module.module_type_id,behavior.definition_json "
                "FROM journal_module_instance_versions AS module "
                "LEFT JOIN journal_interaction_behavior_revisions AS behavior "
                "ON behavior.behavior_id=module.behavior_id "
                "AND behavior.behavior_version=module.behavior_version "
                "WHERE module.module_instance_id=? AND module.instance_version=?",
                (
                    interaction["module_instance_id"],
                    interaction["module_instance_version"],
                ),
            ).fetchone()
            if (
                module_behavior is None
                or str(module_behavior["module_type_id"]) != "prompt_result"
                or module_behavior["definition_json"] is None
                or not ai_contribution_allowed(
                    json.loads(str(module_behavior["definition_json"]))
                )
            ):
                raise JournalCaptureValidationError(
                    "AI generation is not permitted by this Journal section."
                )
            existing = conn.execute(
                "SELECT * FROM journal_prompt_generation_requests "
                "WHERE interaction_id=? AND interaction_revision=?",
                (interaction_id, expected_revision),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO journal_prompt_generation_requests("
                    "request_id,interaction_id,interaction_revision,client_mutation_id,"
                    "request_sha256,input_sha256,prompt_definition_sha256,"
                    "context_manifest_json,context_manifest_sha256,"
                    "requested_by_actor_json,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                    (
                        request_id,
                        interaction_id,
                        expected_revision,
                        client_mutation_id,
                        request_sha,
                        interaction["input_sha256"],
                        prompt["definition_sha256"],
                        _canonical(context_manifest),
                        context_sha,
                        _canonical(actor),
                        now,
                        now,
                    ),
                )
            else:
                status = str(existing["status"])
                lease_expired = (
                    status == "leased"
                    and existing["lease_expires_at"] is not None
                    and str(existing["lease_expires_at"]) <= now
                )
                if status != "failed" and not lease_expired:
                    raise JournalCaptureConflict(
                        "Generation was already requested for this prompt revision."
                    )
                # A new, explicit Generate gesture is the retry boundary. Keep
                # the durable request identity and attempt count, but revoke
                # the old lease before a replacement worker is launched. A
                # late worker therefore cannot commit under its stale token.
                request_id = str(existing["request_id"])
                changed = conn.execute(
                    "UPDATE journal_prompt_generation_requests SET "
                    "client_mutation_id=?,request_sha256=?,requested_by_actor_json=?,"
                    "status='pending',lease_owner=NULL,lease_token_sha256=NULL,"
                    "lease_expires_at=NULL,variant_id=NULL,producer_id=NULL,"
                    "provider_id=NULL,model_id=NULL,error_code=NULL,completed_at=NULL,"
                    "updated_at=? WHERE request_id=? AND "
                    "(status='failed' OR (status='leased' AND lease_expires_at<=?))",
                    (
                        client_mutation_id,
                        request_sha,
                        _canonical(actor),
                        now,
                        request_id,
                        now,
                    ),
                ).rowcount
                if changed != 1:
                    raise JournalCaptureConflict(
                        "The prompt generation request changed concurrently."
                    )
            self._record_mutation(
                conn,
                client_mutation_id,
                request_sha,
                {"generation_request_id": request_id},
                now,
            )
        return {
            **self.get_prompt_generation_request(request_id),
            "deduplicated": False,
        }

    def claim_prompt_generation(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 300,
    ) -> Mapping[str, Any] | None:
        """Lease one queued generation to a trusted background worker."""

        if not worker_id or lease_seconds < 30 or lease_seconds > 3600:
            raise JournalCaptureValidationError(
                "The prompt generation lease is invalid."
            )
        with self.store.transaction() as conn:
            now = _now()
            conn.execute(
                "UPDATE journal_prompt_generation_requests SET status='failed',"
                "error_code='generation_lease_expired',completed_at=?,updated_at=? "
                "WHERE status='leased' AND lease_expires_at<=? AND attempts>=3",
                (now, now, now),
            )
            conn.execute(
                "UPDATE journal_prompt_generation_requests SET status='pending',"
                "lease_owner=NULL,lease_token_sha256=NULL,lease_expires_at=NULL,"
                "updated_at=? WHERE status='leased' AND lease_expires_at<=? "
                "AND attempts<3",
                (now, now),
            )
            row = conn.execute(
                "SELECT request_id FROM journal_prompt_generation_requests "
                "WHERE status='pending' ORDER BY created_at,request_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            request_id = str(row["request_id"])
        return self.claim_prompt_generation_request(
            request_id=request_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )

    def claim_prompt_generation_request(
        self,
        *,
        request_id: str,
        worker_id: str,
        lease_seconds: int = 300,
        provider_id: str | None = None,
        model_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Lease one exact queued request to a preflighted worker session."""

        if not worker_id or lease_seconds < 30 or lease_seconds > 3600:
            raise JournalCaptureValidationError(
                "The prompt generation lease is invalid."
            )
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        token = secrets.token_urlsafe(32)
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE journal_prompt_generation_requests SET status='failed',"
                "error_code='generation_lease_expired',completed_at=?,updated_at=? "
                "WHERE request_id=? AND status='leased' AND lease_expires_at<=? "
                "AND attempts>=3",
                (now, now, request_id, now),
            )
            conn.execute(
                "UPDATE journal_prompt_generation_requests SET status='pending',"
                "lease_owner=NULL,lease_token_sha256=NULL,lease_expires_at=NULL,"
                "updated_at=? WHERE request_id=? AND status='leased' "
                "AND lease_expires_at<=? AND attempts<3",
                (now, request_id, now),
            )
            changed = conn.execute(
                "UPDATE journal_prompt_generation_requests SET status='leased',"
                "attempts=attempts+1,lease_owner=?,lease_token_sha256=?,"
                "lease_expires_at=?,provider_id=?,model_id=?,updated_at=? "
                "WHERE request_id=? AND status='pending'",
                (
                    worker_id,
                    _sha(token),
                    expires,
                    provider_id,
                    model_id,
                    now,
                    request_id,
                ),
            ).rowcount
            if changed != 1:
                return None
            leased = conn.execute(
                "SELECT r.*,i.input_text,i.source_ref,p.wording,p.help_text "
                "FROM journal_prompt_generation_requests AS r "
                "JOIN journal_prompt_interactions AS i "
                "ON i.interaction_id=r.interaction_id "
                "JOIN journal_prompt_definition_versions AS p "
                "ON p.prompt_id=i.prompt_id AND p.prompt_version=i.prompt_version "
                "WHERE r.request_id=?",
                (request_id,),
            ).fetchone()
        assert leased is not None
        return {
            **self._prompt_generation_request(leased),
            "leaseToken": token,
            "inputText": str(leased["input_text"]),
            "inputSourceRef": str(leased["source_ref"]),
            "promptWording": str(leased["wording"]),
            "promptHelp": leased["help_text"],
        }

    def get_prompt_generation_request(self, request_id: str) -> Mapping[str, Any]:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_prompt_generation_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if row is None:
            raise JournalCaptureValidationError(
                "That prompt generation request is unavailable."
            )
        return self._prompt_generation_request(row)

    def validate_prompt_generation_lease(
        self,
        *,
        request_id: str,
        lease_token: str,
    ) -> Mapping[str, Any]:
        """Validate a worker capability before any result Source is resolved."""

        now = _now()
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_prompt_generation_requests "
                "WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if (
            row is None
            or str(row["status"]) != "leased"
            or str(row["lease_token_sha256"]) != _sha(lease_token)
            or str(row["lease_expires_at"]) <= now
        ):
            raise JournalCaptureConflict(
                "That prompt generation lease is unavailable."
            )
        return self._prompt_generation_request(row)

    def validate_prompt_generation_worker_lease(
        self,
        *,
        request_id: str,
        lease_token: str,
        worker_id: str,
    ) -> Mapping[str, Any]:
        """Validate both the capability secret and its bound execution session."""

        result = self.validate_prompt_generation_lease(
            request_id=request_id,
            lease_token=lease_token,
        )
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT lease_owner FROM journal_prompt_generation_requests "
                "WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if row is None or str(row["lease_owner"]) != worker_id:
            raise JournalCaptureConflict(
                "That prompt generation lease belongs to another worker."
            )
        return result

    def fail_prompt_generation(
        self,
        *,
        request_id: str,
        lease_token: str,
        error_code: str,
    ) -> Mapping[str, Any]:
        now = _now()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_prompt_generation_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None or str(row["status"]) != "leased":
                raise JournalCaptureConflict(
                    "That prompt generation lease is unavailable."
                )
            if str(row["lease_token_sha256"]) != _sha(lease_token):
                raise JournalCaptureConflict(
                    "That prompt generation lease is unavailable."
                )
            if str(row["lease_expires_at"]) <= now:
                raise JournalCaptureConflict("That prompt generation lease expired.")
            conn.execute(
                "UPDATE journal_prompt_generation_requests SET status='failed',"
                "error_code=?,completed_at=?,updated_at=? WHERE request_id=?",
                (error_code[:128], now, now, request_id),
            )
        return self.get_prompt_generation_request(request_id)

    def record_prompt_result(
        self,
        *,
        interaction_id: str,
        expected_revision: int,
        client_mutation_id: str,
        producer_id: str,
        context_manifest_sha256: str,
        generation_receipt: Mapping[str, Any],
        result_text: str,
        provider_id: str | None = None,
        model_id: str | None = None,
        generation_request_id: str | None = None,
        lease_token: str | None = None,
        source_ref: str | None = None,
        source_dependency_id: str | None = None,
    ) -> str:
        if not result_text:
            raise JournalCaptureValidationError(
                "A generated prompt result cannot be empty."
            )
        if (generation_request_id is None) != (lease_token is None):
            raise JournalCaptureValidationError(
                "The prompt generation lease is incomplete."
            )
        request = {
            "interaction_id": interaction_id,
            "expected_revision": expected_revision,
            "producer_id": producer_id,
            "context_manifest_sha256": context_manifest_sha256,
            "result_sha256": _sha(result_text),
            "generation_request_id": generation_request_id,
            "source_ref": source_ref,
        }
        if source_dependency_id is not None:
            request["source_dependency_id"] = source_dependency_id
        request_sha = _sha(request)
        now = _now()
        with self.store.transaction() as conn:
            replay = self._mutation_replay(conn, client_mutation_id, request_sha)
            if replay is not None:
                return str(replay["variant_id"])
            interaction = conn.execute(
                "SELECT * FROM journal_prompt_interactions WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if interaction is None:
                raise JournalCaptureValidationError("That prompt interaction is unavailable.")
            if int(interaction["current_revision"]) != expected_revision:
                raise JournalCaptureConflict("The prompt result set changed before generation.")
            generation = None
            if generation_request_id is not None:
                generation = conn.execute(
                    "SELECT * FROM journal_prompt_generation_requests "
                    "WHERE request_id=?",
                    (generation_request_id,),
                ).fetchone()
                if (
                    generation is None
                    or str(generation["interaction_id"]) != interaction_id
                    or int(generation["interaction_revision"]) != expected_revision
                    or str(generation["status"]) != "leased"
                    or str(generation["lease_token_sha256"]) != _sha(lease_token or "")
                    or str(generation["lease_expires_at"]) <= now
                    or str(generation["input_sha256"]) != str(interaction["input_sha256"])
                    or str(generation["context_manifest_sha256"])
                    != context_manifest_sha256
                ):
                    raise JournalCaptureConflict(
                        "That prompt generation lease is unavailable."
                    )
            dependency = None
            if source_dependency_id is not None:
                dependency = conn.execute(
                    "SELECT * FROM journal_prompt_result_source_dependencies "
                    "WHERE dependency_id=?",
                    (source_dependency_id,),
                ).fetchone()
                if (
                    dependency is None
                    or str(dependency["client_mutation_id"]) != client_mutation_id
                    or str(dependency["interaction_id"]) != interaction_id
                    or str(dependency["generation_request_id"])
                    != str(generation_request_id)
                    or str(dependency["source_ref"]) != str(source_ref)
                    or str(dependency["result_sha256"]) != _sha(result_text)
                    or str(dependency["state"]) != "reserved"
                    or dependency["variant_id"] is not None
                ):
                    raise JournalCaptureConflict(
                        "The prompt result Source dependency is unavailable."
                    )
            ordinal = int(
                conn.execute(
                    "SELECT COALESCE(MAX(run_ordinal),0)+1 FROM journal_prompt_runs "
                    "WHERE interaction_id=?",
                    (interaction_id,),
                ).fetchone()[0]
            )
            run_id = "jprun_" + uuid.uuid4().hex
            variant_id = "jpv_" + uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO journal_prompt_runs(
                    run_id,interaction_id,run_ordinal,producer_id,provider_id,model_id,
                    input_sha256,context_manifest_sha256,generation_receipt_json,state,
                    created_at,completed_at,generation_request_id
                ) VALUES(?,?,?,?,?,?,?,?,?,'succeeded',?,?,?)
                """,
                (
                    run_id,
                    interaction_id,
                    ordinal,
                    producer_id,
                    provider_id,
                    model_id,
                    interaction["input_sha256"],
                    context_manifest_sha256,
                    _canonical(generation_receipt),
                    now,
                    now,
                    generation_request_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO journal_prompt_result_variants(
                    variant_id,interaction_id,run_id,variant_ordinal,result_authority,
                    result_text,result_content_sha256,source_ref,authorship,review_state,
                    created_at,updated_at
                ) VALUES(?,?,?,?,'derived_value',?,?,?,'generated','unreviewed',?,?)
                """,
                (
                    variant_id,
                    interaction_id,
                    run_id,
                    ordinal,
                    result_text,
                    _sha(result_text),
                    source_ref,
                    now,
                    now,
                ),
            )
            if str(interaction["result_retention"]) == "latest_only":
                conn.execute(
                    "UPDATE journal_prompt_result_variants SET lifecycle='archived',"
                    "updated_at=? WHERE interaction_id=? AND variant_id<>? "
                    "AND lifecycle='current'",
                    (now, interaction_id, variant_id),
                )
            revision = expected_revision + 1
            conn.execute(
                "UPDATE journal_prompt_interactions SET current_revision=?,updated_at=? "
                "WHERE interaction_id=?",
                (revision, now, interaction_id),
            )
            self._enqueue_search(
                conn,
                aggregate_type="prompt_result",
                aggregate_id=variant_id,
                aggregate_revision="1",
                event_kind="upsert",
                content_sha256=_sha(result_text),
                composition_digest=self._composition_digest(conn, interaction["local_date"]),
                privacy_class="private",
                committed_at=now,
            )
            if source_dependency_id is not None:
                bound = conn.execute(
                    "UPDATE journal_prompt_result_source_dependencies SET "
                    "variant_id=?,state='bound',updated_at=? WHERE dependency_id=? "
                    "AND state='reserved' AND variant_id IS NULL",
                    (variant_id, now, source_dependency_id),
                ).rowcount
                if bound != 1:
                    raise JournalCaptureConflict(
                        "The prompt result Source dependency changed concurrently."
                    )
            if generation is not None:
                changed = conn.execute(
                    "UPDATE journal_prompt_generation_requests SET "
                    "status='succeeded',variant_id=?,producer_id=?,provider_id=?,"
                    "model_id=?,completed_at=?,updated_at=? WHERE request_id=? "
                    "AND status='leased'",
                    (
                        variant_id,
                        producer_id,
                        provider_id,
                        model_id,
                        now,
                        now,
                        generation_request_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise JournalCaptureConflict(
                        "The prompt generation request changed concurrently."
                    )
            self._record_mutation(
                conn,
                client_mutation_id,
                request_sha,
                {"variant_id": variant_id},
                now,
            )
        return variant_id

    def decide_prompt_result(
        self,
        *,
        interaction_id: str,
        variant_id: str,
        decision_kind: str,
        expected_revision: int,
        client_mutation_id: str,
        actor: Mapping[str, Any],
    ) -> int:
        if decision_kind not in {"accept", "archive", "reject"}:
            raise JournalCaptureValidationError(
                "That prompt result decision is invalid."
            )
        request = {
            "interaction_id": interaction_id,
            "variant_id": variant_id,
            "decision_kind": decision_kind,
            "expected_revision": expected_revision,
        }
        request_sha = _sha(request)
        now = _now()
        with self.store.transaction() as conn:
            replay = self._mutation_replay(conn, client_mutation_id, request_sha)
            if replay is not None:
                return int(replay["interaction_revision"])
            interaction = conn.execute(
                "SELECT * FROM journal_prompt_interactions WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if interaction is None or int(interaction["current_revision"]) != expected_revision:
                raise JournalCaptureConflict("The prompt result set changed before this decision.")
            variant = conn.execute(
                "SELECT lifecycle FROM journal_prompt_result_variants "
                "WHERE interaction_id=? AND variant_id=?",
                (interaction_id, variant_id),
            ).fetchone()
            if variant is None:
                raise JournalCaptureValidationError("That prompt result is unavailable.")
            revision = expected_revision + 1
            decision_id = "jpd_" + uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO journal_prompt_decisions(
                    decision_id,interaction_id,variant_id,decision_kind,
                    interaction_revision,actor_json,client_mutation_id,request_sha256,
                    created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    decision_id,
                    interaction_id,
                    variant_id,
                    decision_kind,
                    revision,
                    _canonical(actor),
                    client_mutation_id,
                    request_sha,
                    now,
                ),
            )
            variant_lifecycle = {
                "accept": "accepted",
                "archive": "archived",
                "reject": "rejected",
            }[decision_kind]
            if decision_kind == "accept":
                conn.execute(
                    "UPDATE journal_prompt_result_variants SET lifecycle='archived',"
                    "updated_at=? WHERE interaction_id=? AND variant_id<>? "
                    "AND lifecycle='accepted'",
                    (now, interaction_id, variant_id),
                )
            conn.execute(
                "UPDATE journal_prompt_result_variants SET lifecycle=?,updated_at=? "
                "WHERE variant_id=?",
                (variant_lifecycle, now, variant_id),
            )
            accepted_remains = decision_kind == "accept" or conn.execute(
                "SELECT 1 FROM journal_prompt_result_variants "
                "WHERE interaction_id=? AND lifecycle='accepted' LIMIT 1",
                (interaction_id,),
            ).fetchone() is not None
            interaction_lifecycle = "accepted" if accepted_remains else "current"
            conn.execute(
                "UPDATE journal_prompt_interactions SET lifecycle=?,current_revision=?,updated_at=? "
                "WHERE interaction_id=?",
                (interaction_lifecycle, revision, now, interaction_id),
            )
            self._record_mutation(
                conn,
                client_mutation_id,
                request_sha,
                {"interaction_revision": revision},
                now,
            )
        return revision

    def get_prompt_interaction(self, interaction_id: str) -> Mapping[str, Any]:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT i.*,p.wording,p.help_text,p.requiredness "
                "FROM journal_prompt_interactions AS i "
                "JOIN journal_prompt_definition_versions AS p "
                "ON p.prompt_id=i.prompt_id AND p.prompt_version=i.prompt_version "
                "WHERE i.interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if row is None:
                raise JournalCaptureValidationError(
                    "That prompt interaction is unavailable."
                )
            variants = conn.execute(
                "SELECT v.*,r.producer_id,r.provider_id,r.model_id,"
                "r.context_manifest_sha256,r.generation_receipt_json,"
                "r.generation_request_id,r.created_at AS run_created_at "
                "FROM journal_prompt_result_variants AS v "
                "JOIN journal_prompt_runs AS r ON r.run_id=v.run_id "
                "WHERE v.interaction_id=? ORDER BY v.variant_ordinal",
                (interaction_id,),
            ).fetchall()
            decisions = conn.execute(
                "SELECT decision_id,variant_id,decision_kind,interaction_revision,"
                "actor_json,created_at FROM journal_prompt_decisions "
                "WHERE interaction_id=? ORDER BY interaction_revision,decision_id",
                (interaction_id,),
            ).fetchall()
            generations = conn.execute(
                "SELECT * FROM journal_prompt_generation_requests "
                "WHERE interaction_id=? ORDER BY created_at,request_id",
                (interaction_id,),
            ).fetchall()
        return {
            "interactionId": str(row["interaction_id"]),
            "localDate": str(row["local_date"]),
            "moduleInstanceId": str(row["module_instance_id"]),
            "moduleInstanceVersion": int(row["module_instance_version"]),
            "promptId": str(row["prompt_id"]),
            "promptVersion": int(row["prompt_version"]),
            "promptWording": str(row["wording"]),
            "promptHelp": row["help_text"],
            "promptRequiredness": str(row["requiredness"]),
            "inputText": str(row["input_text"]),
            "inputSha256": str(row["input_sha256"]),
            "inputSourceRef": row["source_ref"],
            "resultRetention": str(row["result_retention"]),
            "resultSearchMode": str(row["result_search_mode"]),
            "lifecycle": str(row["lifecycle"]),
            "currentRevision": int(row["current_revision"]),
            "createdAt": str(row["created_at"]),
            "updatedAt": str(row["updated_at"]),
            "variants": [
                {
                    "variantId": str(item["variant_id"]),
                    "runId": str(item["run_id"]),
                    "variantOrdinal": int(item["variant_ordinal"]),
                    "resultAuthority": str(item["result_authority"]),
                    "resultItemId": item["result_item_id"],
                    "resultText": item["result_text"],
                    "resultContentSha256": str(item["result_content_sha256"]),
                    "sourceRef": item["source_ref"],
                    "authorship": str(item["authorship"]),
                    "reviewState": str(item["review_state"]),
                    "lifecycle": str(item["lifecycle"]),
                    "currentRevision": int(item["current_revision"]),
                    "producerId": str(item["producer_id"]),
                    "providerId": item["provider_id"],
                    "modelId": item["model_id"],
                    "contextManifestSha256": str(item["context_manifest_sha256"]),
                    "generationReceipt": _mapping(item["generation_receipt_json"]),
                    "generationRequestId": item["generation_request_id"],
                    "createdAt": str(item["created_at"]),
                    "updatedAt": str(item["updated_at"]),
                }
                for item in variants
            ],
            "decisions": [
                {
                    "decisionId": str(item["decision_id"]),
                    "variantId": str(item["variant_id"]),
                    "decisionKind": str(item["decision_kind"]),
                    "interactionRevision": int(item["interaction_revision"]),
                    "actor": _mapping(item["actor_json"]),
                    "createdAt": str(item["created_at"]),
                }
                for item in decisions
            ],
            "generationRequests": [
                self._prompt_generation_request(item) for item in generations
            ],
        }

    def list_prompt_interactions(
        self,
        local_date: str,
        *,
        include_tombstoned: bool = False,
    ) -> tuple[Mapping[str, Any], ...]:
        _validate_local_date(local_date)
        lifecycle = "" if include_tombstoned else "AND lifecycle<>'tombstoned'"
        with self.store._connect() as conn:
            rows = conn.execute(
                f"SELECT interaction_id FROM journal_prompt_interactions "
                f"WHERE local_date=? {lifecycle} ORDER BY created_at,interaction_id",
                (local_date,),
            ).fetchall()
        return tuple(
            self.get_prompt_interaction(str(row["interaction_id"])) for row in rows
        )

    # ------------------------------------------------------------------
    # Search outbox

    def pending_search_events(self, *, limit: int = 100) -> tuple[JournalSearchEvent, ...]:
        with self.store._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM journal_search_outbox AS event
                WHERE event.state IN ('pending','failed')
                  AND (
                    event.visibility_cohort_id IS NULL
                    OR (
                      EXISTS(
                        SELECT 1 FROM journal_import_cohorts AS cohort
                        WHERE cohort.cohort_id=event.visibility_cohort_id
                          AND cohort.state='sealed'
                      )
                      AND EXISTS(
                        SELECT 1 FROM journal_authority_control AS authority
                        WHERE authority.singleton=1 AND authority.mode='database_only'
                      )
                    )
                  )
                ORDER BY event.committed_at,event.event_id LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return tuple(self._search_event(row) for row in rows)

    def mark_search_events_delivered(self, event_ids: Iterable[str]) -> None:
        ids = tuple(dict.fromkeys(str(item) for item in event_ids if item))
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self.store.transaction() as conn:
            conn.execute(
                f"UPDATE journal_search_outbox SET state='delivered',delivered_at=?,"
                f"lease_owner=NULL,lease_expires_at=NULL,error_code=NULL "
                f"WHERE event_id IN ({placeholders})",
                (_now(), *ids),
            )

    # ------------------------------------------------------------------
    # Internal row/validation helpers

    @staticmethod
    def _profile(row: sqlite3.Row) -> JournalProfileRevision:
        return JournalProfileRevision(
            profile_id=str(row["profile_id"]),
            profile_revision=int(row["profile_revision"]),
            format_version=int(row["format_version"]),
            name=str(row["name"]),
            description=str(row["description"]),
            canonical_order=tuple(str(item) for item in _sequence(row["canonical_order_json"])),
            profile_digest=str(row["profile_digest"]),
            created_by=str(row["created_by"]),
            created_at=str(row["created_at"]),
            supersedes_revision=(
                int(row["supersedes_revision"])
                if row["supersedes_revision"] is not None
                else None
            ),
        )

    @staticmethod
    def _module(row: sqlite3.Row) -> JournalModuleInstanceVersion:
        return JournalModuleInstanceVersion(
            module_instance_id=str(row["module_instance_id"]),
            instance_version=int(row["instance_version"]),
            module_type_id=str(row["module_type_id"]),
            module_type_version=int(row["module_type_version"]),
            label=str(row["label"]),
            settings_schema_version=int(row["settings_schema_version"]),
            settings=_mapping(row["settings_json"]),
            settings_sha256=str(row["settings_sha256"]),
            behavior_id=row["behavior_id"],
            behavior_version=(
                int(row["behavior_version"])
                if row["behavior_version"] is not None
                else None
            ),
            schedule_kind=str(row["schedule_kind"]),
            schedule=_mapping(row["schedule_json"]),
            reveal_policy=_mapping(row["reveal_policy_json"]),
        )

    def _persisted_composition(
        self,
        conn: sqlite3.Connection,
        snapshot: sqlite3.Row,
    ) -> JournalDayComposition:
        profile_row = conn.execute(
            "SELECT * FROM journal_profile_revisions WHERE profile_id=? AND profile_revision=?",
            (snapshot["profile_id"], snapshot["profile_revision"]),
        ).fetchone()
        module_rows = conn.execute(
            """
            SELECT c.*,m.* FROM journal_day_composition_modules AS c
            JOIN journal_module_instance_versions AS m
              ON m.module_instance_id=c.module_instance_id
             AND m.instance_version=c.module_instance_version
            WHERE c.snapshot_id=? ORDER BY c.ordinal
            """,
            (snapshot["snapshot_id"],),
        ).fetchall()
        modules = tuple(
            JournalDayModule(
                slot_id=str(row["slot_id"]),
                ordinal=int(row["ordinal"]),
                module=self._module(row),
                semantic_membership=str(row["semantic_membership"]),
                schedule_evidence=_mapping(row["schedule_evidence_json"]),
            )
            for row in module_rows
        )
        field_rows = conn.execute(
            """
            SELECT c.*,
                   f.label,f.description,f.value_kind,f.unit,
                   f.constraints_json,f.value_codec_version,
                   f.function_id,f.function_version,
                   f.behavior_id,f.behavior_version,
                   f.privacy_class,f.search_mode,f.disclosure_policy_id,
                   p.wording AS prompt_wording,p.help_text AS prompt_help,
                   p.requiredness AS prompt_requiredness
            FROM journal_day_composition_fields AS c
            JOIN journal_field_definition_versions AS f
              ON f.field_id=c.field_id
             AND f.definition_version=c.field_definition_version
            LEFT JOIN journal_prompt_definition_versions AS p
              ON p.prompt_id=c.prompt_id AND p.prompt_version=c.prompt_version
            WHERE c.snapshot_id=?
            ORDER BY c.module_slot_id,c.ordinal,c.composition_slot_id
            """,
            (snapshot["snapshot_id"],),
        ).fetchall()
        fields = tuple(
            JournalDayField(
                composition_slot_id=str(row["composition_slot_id"]),
                module_slot_id=str(row["module_slot_id"]),
                ordinal=int(row["ordinal"]),
                field_id=str(row["field_id"]),
                field_definition_version=int(row["field_definition_version"]),
                label=str(row["label"]),
                description=str(row["description"]),
                value_kind=str(row["value_kind"]),
                unit=row["unit"],
                constraints=_mapping(row["constraints_json"]),
                value_codec_version=int(row["value_codec_version"]),
                function_id=row["function_id"],
                function_version=(
                    int(row["function_version"])
                    if row["function_version"] is not None
                    else None
                ),
                behavior_id=str(row["behavior_id"]),
                behavior_version=int(row["behavior_version"]),
                privacy_class=str(row["privacy_class"]),
                search_mode=str(row["search_mode"]),
                disclosure_policy_id=str(row["disclosure_policy_id"]),
                prompt_id=row["prompt_id"],
                prompt_version=(
                    int(row["prompt_version"])
                    if row["prompt_version"] is not None
                    else None
                ),
                prompt_wording=row["prompt_wording"],
                prompt_help=row["prompt_help"],
                prompt_requiredness=row["prompt_requiredness"],
            )
            for row in field_rows
        )
        return JournalDayComposition(
            local_date=str(snapshot["local_date"]),
            day_id=str(snapshot["day_id"]),
            timezone=str(snapshot["timezone"]),
            boundary=str(snapshot["boundary"]),
            window_start=str(snapshot["window_start"]),
            window_end=str(snapshot["window_end"]),
            profile=self._profile(profile_row),
            activation_revision=int(snapshot["activation_revision"]),
            modules=modules,
            fields=fields,
            composition_digest=str(snapshot["composition_digest"]),
            persisted=True,
            snapshot_id=str(snapshot["snapshot_id"]),
            snapshot_version=int(snapshot["snapshot_version"]),
            override_id=snapshot["override_id"],
            search_recipe_version=int(snapshot["search_recipe_version"]),
        )

    @staticmethod
    def _visible_item_row(
        conn: sqlite3.Connection,
        item_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT * FROM journal_items AS item WHERE item.item_id=?
              AND (
                item.import_cohort_id IS NULL
                OR (
                  EXISTS(
                    SELECT 1 FROM journal_import_cohorts AS cohort
                    WHERE cohort.cohort_id=item.import_cohort_id
                      AND cohort.state='sealed'
                  )
                  AND EXISTS(
                    SELECT 1 FROM journal_authority_control AS authority
                    WHERE authority.singleton=1 AND authority.mode='database_only'
                  )
                )
              )
            """,
            (item_id,),
        ).fetchone()

    def _archive_active_routes(
        self,
        conn: sqlite3.Connection,
        *,
        item_id: str,
        actor: Mapping[str, Any],
        intent_id: str,
        now: str,
    ) -> None:
        routes = conn.execute(
            "SELECT * FROM journal_relations WHERE source_item_id=? "
            "AND relation_kind='routed_to' AND lifecycle='current' "
            "ORDER BY relation_id",
            (item_id,),
        ).fetchall()
        for route in routes:
            revision = int(route["revision"]) + 1
            conn.execute(
                "UPDATE journal_relations SET lifecycle='archived',revision=?,"
                "actor_json=?,updated_at=? WHERE relation_id=?",
                (revision, _canonical(actor), now, route["relation_id"]),
            )
            conn.execute(
                "INSERT INTO journal_relation_revisions("
                "relation_id,revision,relation_kind,target_domain,target_id,"
                "target_revision,lifecycle,actor_json,source_ref,intent_id,created_at) "
                "VALUES(?,?,?,?,?,?,'archived',?,?,?,?)",
                (
                    route["relation_id"],
                    revision,
                    route["relation_kind"],
                    route["target_domain"],
                    route["target_id"],
                    route["target_revision"],
                    _canonical(actor),
                    route["source_ref"],
                    f"{intent_id}:archive-route:{route['relation_id']}",
                    now,
                ),
            )

    @staticmethod
    def _prompt_generation_request(row: sqlite3.Row) -> Mapping[str, Any]:
        lease_expires_at = row["lease_expires_at"]
        stored_status = str(row["status"])
        expired = (
            stored_status == "leased"
            and lease_expires_at is not None
            and str(lease_expires_at) <= _now()
        )
        return {
            "requestId": str(row["request_id"]),
            "interactionId": str(row["interaction_id"]),
            "interactionRevision": int(row["interaction_revision"]),
            "inputSha256": str(row["input_sha256"]),
            "promptDefinitionSha256": str(row["prompt_definition_sha256"]),
            "contextManifest": _mapping(row["context_manifest_json"]),
            "contextManifestSha256": str(row["context_manifest_sha256"]),
            "status": "expired" if expired else stored_status,
            "storedStatus": stored_status,
            "retryable": stored_status == "failed" or expired,
            "attempts": int(row["attempts"]),
            "leaseExpiresAt": lease_expires_at,
            "variantId": row["variant_id"],
            "producerId": row["producer_id"],
            "providerId": row["provider_id"],
            "modelId": row["model_id"],
            "errorCode": row["error_code"],
            "createdAt": str(row["created_at"]),
            "updatedAt": str(row["updated_at"]),
            "completedAt": row["completed_at"],
        }

    @staticmethod
    def _item(row: sqlite3.Row) -> JournalNativeItem:
        return JournalNativeItem(
            item_id=str(row["item_id"]),
            local_date=str(row["local_date"]),
            day_id=row["day_id"],
            module_instance_id=row["module_instance_id"],
            module_instance_version=(
                int(row["module_instance_version"])
                if row["module_instance_version"] is not None
                else None
            ),
            item_kind=str(row["item_kind"]),
            authority_kind=str(row["authority_kind"]),
            legacy_entry_id=row["legacy_entry_id"],
            plain_value=row["current_plain_value"],
            content_sha256=row["current_content_sha256"],
            interaction_behavior_id=str(row["interaction_behavior_id"]),
            interaction_behavior_version=int(row["interaction_behavior_version"]),
            privacy_class=str(row["privacy_class"]),
            search_mode=str(row["search_mode"]),
            source_ref=row["source_ref"],
            lifecycle=str(row["lifecycle"]),
            current_revision=int(row["current_revision"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _relation(row: sqlite3.Row) -> JournalRelation:
        return JournalRelation(
            relation_id=str(row["relation_id"]),
            source_item_id=str(row["source_item_id"]),
            relation_kind=str(row["relation_kind"]),
            target_domain=str(row["target_domain"]),
            target_id=str(row["target_id"]),
            target_revision=row["target_revision"],
            lifecycle=str(row["lifecycle"]),
            revision=int(row["revision"]),
            source_ref=row["source_ref"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _field_value(
        row: sqlite3.Row,
        *,
        options: Sequence[str],
        references: Sequence[Mapping[str, Any]],
    ) -> JournalFieldValue:
        kind = JournalValueKind(str(row["value_kind"]))
        disposition = (
            JournalValueDisposition(str(row["disposition"]))
            if row["disposition"] is not None
            else None
        )
        if disposition is not None:
            value: Any = None
        elif kind in {JournalValueKind.SHORT_TEXT, JournalValueKind.LONG_TEXT}:
            value = row["text_value"]
        elif kind in {JournalValueKind.NUMBER, JournalValueKind.SCALE}:
            value = float(row["number_value"])
        elif kind is JournalValueKind.BOOLEAN:
            value = bool(row["boolean_value"])
        elif kind in {JournalValueKind.LOCAL_TIME, JournalValueKind.INSTANT, JournalValueKind.DATE}:
            value = row["temporal_value"]
        elif kind is JournalValueKind.DURATION:
            value = int(row["duration_seconds"])
        elif kind is JournalValueKind.SINGLE_SELECT:
            value = row["option_value"]
        elif kind is JournalValueKind.MULTI_SELECT:
            value = list(options)
        else:
            value = list(references)
        return JournalFieldValue(
            value_id=str(row["value_id"]),
            local_date=str(row["local_date"]),
            day_id=row["day_id"],
            composition_snapshot_id=row["composition_snapshot_id"],
            composition_slot_id=row["composition_slot_id"],
            module_instance_id=str(row["module_instance_id"]),
            module_instance_version=int(row["module_instance_version"]),
            field_id=str(row["field_id"]),
            field_definition_version=int(row["field_definition_version"]),
            value_kind=kind,
            disposition=disposition,
            value=value,
            current_revision=int(row["current_revision"]),
            authorship=str(row["authorship"]),
            review_state=str(row["review_state"]),
            source_ref=row["source_ref"],
            observed_at=row["observed_at"],
            stated_at=row["stated_at"],
            ingested_at=str(row["ingested_at"]),
            lifecycle=str(row["lifecycle"]),
        )

    @staticmethod
    def _encode_field_value(
        definition: sqlite3.Row,
        *,
        value: Any,
        disposition: str | None,
    ) -> tuple[dict[str, Any], list[str], list[Mapping[str, Any]], Mapping[str, Any]]:
        kind = JournalValueKind(str(definition["value_kind"]))
        encoded: dict[str, Any] = {
            "disposition": None,
            "text_value": None,
            "number_value": None,
            "boolean_value": None,
            "temporal_value": None,
            "duration_seconds": None,
            "option_value": None,
            "collection_present": 0,
        }
        options: list[str] = []
        references: list[Mapping[str, Any]] = []
        if disposition is not None:
            try:
                selected = JournalValueDisposition(disposition)
            except ValueError as exc:
                raise JournalCaptureValidationError("That missing-value disposition is invalid.") from exc
            if value is not None:
                raise JournalCaptureValidationError("A missing disposition cannot also have a value.")
            encoded["disposition"] = selected.value
            return encoded, options, references, {"disposition": selected.value}
        if value is None:
            raise JournalCaptureValidationError(
                "Choose a value or an explicit missing, skipped, or declined disposition."
            )
        constraints = _mapping(definition["constraints_json"])
        if kind in {JournalValueKind.SHORT_TEXT, JournalValueKind.LONG_TEXT}:
            if not isinstance(value, str):
                raise JournalCaptureValidationError("That Journal field requires text.")
            max_length = int(constraints.get("maxLength") or (500 if kind is JournalValueKind.SHORT_TEXT else 100_000))
            if len(value) > max_length:
                raise JournalCaptureValidationError("That Journal text is too long.")
            encoded["text_value"] = value
        elif kind in {JournalValueKind.NUMBER, JournalValueKind.SCALE}:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise JournalCaptureValidationError("That Journal field requires a number.")
            numeric = float(value)
            minimum = constraints.get("minimum")
            maximum = constraints.get("maximum")
            if minimum is not None and numeric < float(minimum):
                raise JournalCaptureValidationError("That Journal value is below its minimum.")
            if maximum is not None and numeric > float(maximum):
                raise JournalCaptureValidationError("That Journal value is above its maximum.")
            encoded["number_value"] = numeric
        elif kind is JournalValueKind.BOOLEAN:
            if not isinstance(value, bool):
                raise JournalCaptureValidationError("That Journal field requires true or false.")
            encoded["boolean_value"] = int(value)
        elif kind in {JournalValueKind.LOCAL_TIME, JournalValueKind.INSTANT, JournalValueKind.DATE}:
            if not isinstance(value, str):
                raise JournalCaptureValidationError("That Journal field requires a time or date.")
            try:
                if kind is JournalValueKind.DATE:
                    date.fromisoformat(value)
                elif kind is JournalValueKind.INSTANT:
                    parsed = datetime.fromisoformat(value)
                    if parsed.tzinfo is None or parsed.utcoffset() is None:
                        raise ValueError
                else:
                    datetime.strptime(value, "%H:%M")
            except ValueError as exc:
                raise JournalCaptureValidationError("That Journal time or date is invalid.") from exc
            encoded["temporal_value"] = value
        elif kind is JournalValueKind.DURATION:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise JournalCaptureValidationError("That Journal duration is invalid.")
            encoded["duration_seconds"] = value
        elif kind is JournalValueKind.SINGLE_SELECT:
            if not isinstance(value, str):
                raise JournalCaptureValidationError("Choose one Journal option.")
            allowed = constraints.get("options")
            if isinstance(allowed, list) and value not in allowed:
                raise JournalCaptureValidationError("That Journal option is unavailable.")
            encoded["option_value"] = value
        elif kind is JournalValueKind.MULTI_SELECT:
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise JournalCaptureValidationError("Choose valid Journal options.")
            options = list(dict.fromkeys(value))
            allowed = constraints.get("options")
            if isinstance(allowed, list) and any(item not in allowed for item in options):
                raise JournalCaptureValidationError("A selected Journal option is unavailable.")
            encoded["collection_present"] = 1
        else:
            if not isinstance(value, list):
                raise JournalCaptureValidationError("That Journal field requires references.")
            for item in value:
                if (
                    not isinstance(item, Mapping)
                    or not isinstance(item.get("kind"), str)
                    or not isinstance(item.get("id"), str)
                    or not item["kind"]
                    or not item["id"]
                ):
                    raise JournalCaptureValidationError("A Journal reference is invalid.")
                references.append(
                    {
                        "kind": item["kind"],
                        "id": item["id"],
                        "revision": item.get("revision"),
                    }
                )
            encoded["collection_present"] = 1
        frozen_value: Mapping[str, Any] = {"kind": kind.value, "value": value}
        return encoded, options, references, frozen_value

    @staticmethod
    def _mutation_replay(
        conn: sqlite3.Connection,
        client_mutation_id: str,
        request_sha256: str,
    ) -> Mapping[str, Any] | None:
        row = conn.execute(
            "SELECT request_sha256,result_json FROM journal_mutations "
            "WHERE client_mutation_id=?",
            (client_mutation_id,),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_sha256:
            raise JournalCaptureConflict(
                "That Journal mutation key was used for another request."
            )
        result = json.loads(row["result_json"])
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _record_mutation(
        conn: sqlite3.Connection,
        client_mutation_id: str,
        request_sha256: str,
        result: Mapping[str, Any],
        created_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO journal_mutations(client_mutation_id,request_sha256,result_json,created_at) "
            "VALUES(?,?,?,?)",
            (client_mutation_id, request_sha256, _canonical(result), created_at),
        )

    @staticmethod
    def _composition_digest(conn: sqlite3.Connection, local_date: str) -> str | None:
        row = conn.execute(
            """
            SELECT s.composition_digest
            FROM journal_day_composition_snapshots AS s
            JOIN journal_days AS d ON d.day_id=s.day_id
            WHERE d.local_date=? AND (
                s.import_cohort_id IS NULL OR (
                    EXISTS(SELECT 1 FROM journal_import_cohorts AS cohort
                        WHERE cohort.cohort_id=s.import_cohort_id
                          AND cohort.state='sealed')
                    AND EXISTS(SELECT 1 FROM journal_authority_control AS authority
                        WHERE authority.singleton=1
                          AND authority.mode='database_only')
                )
            )
            """,
            (local_date,),
        ).fetchone()
        return str(row[0]) if row is not None else None

    @staticmethod
    def _enqueue_search(
        conn: sqlite3.Connection,
        *,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_revision: str,
        event_kind: str,
        content_sha256: str,
        composition_digest: str | None,
        privacy_class: str,
        committed_at: str,
    ) -> str:
        event_key = {
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "aggregate_revision": aggregate_revision,
            "event_kind": event_kind,
        }
        event_id = "jso_" + _sha(event_key)[:32]
        conn.execute(
            """
            INSERT OR IGNORE INTO journal_search_outbox(
                event_id,aggregate_type,aggregate_id,aggregate_revision,event_kind,
                content_sha256,composition_digest,search_recipe_version,privacy_class,
                committed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                aggregate_type,
                aggregate_id,
                aggregate_revision,
                event_kind,
                content_sha256,
                composition_digest,
                1,
                privacy_class,
                committed_at,
            ),
        )
        return event_id

    @staticmethod
    def _search_event(row: sqlite3.Row) -> JournalSearchEvent:
        return JournalSearchEvent(
            event_id=str(row["event_id"]),
            aggregate_type=str(row["aggregate_type"]),
            aggregate_id=str(row["aggregate_id"]),
            aggregate_revision=str(row["aggregate_revision"]),
            event_kind=str(row["event_kind"]),
            content_sha256=str(row["content_sha256"]),
            composition_digest=row["composition_digest"],
            search_recipe_version=int(row["search_recipe_version"]),
            privacy_class=str(row["privacy_class"]),
            state=str(row["state"]),
            attempts=int(row["attempts"]),
            committed_at=str(row["committed_at"]),
        )

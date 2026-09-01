"""Neutral contracts for staging typed observations with a Journal import cohort.

This module deliberately knows nothing about a user's legacy paths, marker syntax,
or field names.  Private one-off operators translate frozen bytes into these
contracts; the Journal importer validates and publishes them atomically.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .domain import JournalDomainService
from work_buddy.journal_day import parse_local_time
from .models import (
    JournalCaptureValidationError,
    JournalValueDisposition,
    JournalValueKind,
)


TYPED_IMPORT_MAPPING_SCHEMA = "wb.journal-import-profile-mapping/v1"
TYPED_IMPORT_OBSERVATION_SCHEMA = "wb.journal-import-typed-observation/v1"
_AUTHORSHIP = {"human", "ai", "mixed", "unknown", "generated"}
_REVIEW_STATES = {"not_applicable", "unknown", "unreviewed", "reviewed", "rejected"}
_PRIVACY = {"private", "sensitive", "internal"}
_SEARCH = {"structured_only", "lexical", "dense", "lexical_dense", "excluded"}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _required_text(value: Any, label: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise JournalCaptureValidationError(f"A bounded {label} is required.")
    return value


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise JournalCaptureValidationError(f"A positive {label} is required.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise JournalCaptureValidationError(f"A positive {label} is required.") from exc
    if parsed < 1:
        raise JournalCaptureValidationError(f"A positive {label} is required.")
    return parsed


@dataclass(frozen=True, slots=True)
class JournalImportFieldMapping:
    field_id: str
    definition_version: int
    owner: str
    stable_key: str
    label: str
    value_kind: str
    unit: str | None = None
    description: str = ""
    constraints: Mapping[str, Any] = field(default_factory=dict)
    value_codec_version: int = 1
    function_id: str | None = None
    function_version: int | None = None
    behavior_id: str = "provenance_only"
    behavior_version: int = 1
    privacy_class: str = "private"
    search_mode: str = "structured_only"
    disclosure_policy_id: str = "private_default/v1"
    slot_id: str | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.field_id, "field identity"),
            (self.owner, "field owner"),
            (self.stable_key, "stable field key"),
            (self.label, "field label"),
            (self.behavior_id, "interaction behavior"),
            (self.disclosure_policy_id, "disclosure policy"),
        ):
            _required_text(value, label)
        _positive(self.definition_version, "field definition version")
        _positive(self.value_codec_version, "value codec version")
        _positive(self.behavior_version, "interaction behavior version")
        try:
            JournalValueKind(self.value_kind)
        except ValueError as exc:
            raise JournalCaptureValidationError("The import field type is invalid.") from exc
        if (self.function_id is None) != (self.function_version is None):
            raise JournalCaptureValidationError(
                "An import field function identity and version must be supplied together."
            )
        if self.function_version is not None:
            _positive(self.function_version, "function version")
        if self.privacy_class not in _PRIVACY or self.search_mode not in _SEARCH:
            raise JournalCaptureValidationError("The import field policy is invalid.")
        object.__setattr__(self, "constraints", dict(self.constraints))
        if self.slot_id is None:
            object.__setattr__(self, "slot_id", f"field:{self.field_id}")
        else:
            _required_text(self.slot_id, "field slot identity")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JournalImportFieldMapping":
        return cls(
            field_id=_required_text(value.get("fieldId"), "field identity"),
            definition_version=_positive(
                value.get("definitionVersion", 1), "field definition version"
            ),
            owner=_required_text(value.get("owner"), "field owner"),
            stable_key=_required_text(value.get("stableKey"), "stable field key"),
            label=_required_text(value.get("label"), "field label"),
            description=str(value.get("description") or ""),
            value_kind=_required_text(value.get("valueKind"), "field type"),
            unit=value.get("unit"),
            constraints=dict(value.get("constraints") or {}),
            value_codec_version=_positive(
                value.get("valueCodecVersion", 1), "value codec version"
            ),
            function_id=value.get("functionId"),
            function_version=value.get("functionVersion"),
            behavior_id=str(value.get("behaviorId") or "provenance_only"),
            behavior_version=_positive(
                value.get("behaviorVersion", 1), "interaction behavior version"
            ),
            privacy_class=str(value.get("privacyClass") or "private"),
            search_mode=str(value.get("searchMode") or "structured_only"),
            disclosure_policy_id=str(
                value.get("disclosurePolicyId") or "private_default/v1"
            ),
            slot_id=value.get("slotId"),
        )

    def definition_payload(self) -> dict[str, Any]:
        return {
            "fieldId": self.field_id,
            "owner": self.owner,
            "stableKey": self.stable_key,
            "label": self.label,
            "description": self.description,
            "valueKind": self.value_kind,
            "unit": self.unit,
            "constraints": dict(self.constraints),
            "valueCodecVersion": self.value_codec_version,
            "function": [self.function_id, self.function_version],
            "behavior": [self.behavior_id, self.behavior_version],
            "privacyClass": self.privacy_class,
            "searchMode": self.search_mode,
            "disclosurePolicyId": self.disclosure_policy_id,
        }

    @property
    def definition_sha256(self) -> str:
        return sha256_json(self.definition_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.definition_payload(),
            "definitionVersion": self.definition_version,
            "slotId": self.slot_id,
            "definitionSha256": self.definition_sha256,
        }


@dataclass(frozen=True, slots=True)
class JournalImportProfileModuleRef:
    """One exact module revision in an imported profile's frozen order."""

    slot_id: str
    ordinal: int
    module_instance_id: str
    module_instance_version: int
    required: bool = False

    def __post_init__(self) -> None:
        _required_text(self.slot_id, "profile module slot identity")
        _required_text(self.module_instance_id, "profile module identity")
        if isinstance(self.ordinal, bool) or self.ordinal < 0:
            raise JournalCaptureValidationError(
                "A non-negative profile module ordinal is required."
            )
        _positive(self.module_instance_version, "profile module version")
        if not isinstance(self.required, bool):
            raise JournalCaptureValidationError(
                "The profile module required flag must be boolean."
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JournalImportProfileModuleRef":
        ordinal = value.get("ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise JournalCaptureValidationError(
                "A non-negative profile module ordinal is required."
            )
        required = value.get("required", False)
        if not isinstance(required, bool):
            raise JournalCaptureValidationError(
                "The profile module required flag must be boolean."
            )
        return cls(
            slot_id=_required_text(value.get("slotId"), "profile module slot identity"),
            ordinal=ordinal,
            module_instance_id=_required_text(
                value.get("moduleInstanceId"), "profile module identity"
            ),
            module_instance_version=_positive(
                value.get("moduleInstanceVersion"), "profile module version"
            ),
            required=required,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "slotId": self.slot_id,
            "ordinal": self.ordinal,
            "moduleInstanceId": self.module_instance_id,
            "moduleInstanceVersion": self.module_instance_version,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class JournalImportProfileMapping:
    mapping_version: str
    profile_id: str
    profile_revision: int
    profile_name: str
    module_instance_id: str
    module_instance_version: int
    module_label: str
    fields: Sequence[JournalImportFieldMapping]
    day_timezone: str
    day_boundary: str
    boundary_policy_revision: str
    profile_description: str = ""
    module_type_id: str = "field_group"
    module_type_version: int = 1
    module_slot_id: str = "historical-fields"
    module_settings: Mapping[str, Any] = field(default_factory=dict)
    behavior_id: str = "provenance_only"
    behavior_version: int = 1
    authorship: str = "unknown"
    review_state: str = "unknown"
    profile_modules: Sequence[JournalImportProfileModuleRef] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for value, label in (
            (self.mapping_version, "typed import mapping version"),
            (self.profile_id, "profile identity"),
            (self.profile_name, "profile name"),
            (self.module_instance_id, "module identity"),
            (self.module_label, "module label"),
            (self.module_type_id, "module type"),
            (self.module_slot_id, "module slot identity"),
            (self.behavior_id, "module interaction behavior"),
            (self.day_timezone, "historical day timezone"),
            (self.boundary_policy_revision, "historical boundary policy revision"),
        ):
            _required_text(value, label)
        try:
            ZoneInfo(self.day_timezone)
            parse_local_time(self.day_boundary)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise JournalCaptureValidationError(
                "The typed import historical day policy is invalid."
            ) from exc
        for value, label in (
            (self.profile_revision, "profile revision"),
            (self.module_instance_version, "module version"),
            (self.module_type_version, "module type version"),
            (self.behavior_version, "module interaction behavior version"),
        ):
            _positive(value, label)
        normalized = tuple(self.fields)
        if not normalized:
            raise JournalCaptureValidationError("A typed import mapping needs fields.")
        if len({item.field_id for item in normalized}) != len(normalized):
            raise JournalCaptureValidationError("Typed import field identities must be unique.")
        if len({item.slot_id for item in normalized}) != len(normalized):
            raise JournalCaptureValidationError("Typed import field slots must be unique.")
        if len({(item.owner, item.stable_key) for item in normalized}) != len(normalized):
            raise JournalCaptureValidationError("Typed import stable field keys must be unique.")
        if self.authorship not in _AUTHORSHIP or self.review_state not in _REVIEW_STATES:
            raise JournalCaptureValidationError("The typed import provenance state is invalid.")
        modules = tuple(self.profile_modules) or (
            JournalImportProfileModuleRef(
                slot_id=self.module_slot_id,
                ordinal=0,
                module_instance_id=self.module_instance_id,
                module_instance_version=self.module_instance_version,
            ),
        )
        if len({item.slot_id for item in modules}) != len(modules):
            raise JournalCaptureValidationError("Imported profile module slots must be unique.")
        if len({item.ordinal for item in modules}) != len(modules):
            raise JournalCaptureValidationError("Imported profile module ordinals must be unique.")
        if sorted(item.ordinal for item in modules) != list(range(len(modules))):
            raise JournalCaptureValidationError(
                "Imported profile module ordinals must be contiguous."
            )
        mapped = [
            item
            for item in modules
            if item.slot_id == self.module_slot_id
            and item.module_instance_id == self.module_instance_id
            and item.module_instance_version == self.module_instance_version
        ]
        if len(mapped) != 1:
            raise JournalCaptureValidationError(
                "The imported field module must appear exactly once in its profile."
            )
        object.__setattr__(self, "fields", normalized)
        object.__setattr__(self, "module_settings", dict(self.module_settings))
        object.__setattr__(self, "profile_modules", modules)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JournalImportProfileMapping":
        if value.get("schema") != TYPED_IMPORT_MAPPING_SCHEMA:
            raise JournalCaptureValidationError("The typed import mapping schema is unsupported.")
        fields = value.get("fields")
        if not isinstance(fields, list) or any(not isinstance(item, Mapping) for item in fields):
            raise JournalCaptureValidationError("The typed import field mapping is invalid.")
        day_policy = value.get("dayPolicy")
        if not isinstance(day_policy, Mapping):
            raise JournalCaptureValidationError(
                "The typed import needs a frozen historical day policy."
            )
        profile_modules = value.get("profileModules", ())
        if not isinstance(profile_modules, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in profile_modules
        ):
            raise JournalCaptureValidationError(
                "The typed import profile module mapping is invalid."
            )
        return cls(
            mapping_version=_required_text(
                value.get("mappingVersion"), "typed import mapping version"
            ),
            profile_id=_required_text(value.get("profileId"), "profile identity"),
            profile_revision=_positive(value.get("profileRevision", 1), "profile revision"),
            profile_name=_required_text(value.get("profileName"), "profile name"),
            profile_description=str(value.get("profileDescription") or ""),
            module_instance_id=_required_text(
                value.get("moduleInstanceId"), "module identity"
            ),
            module_instance_version=_positive(
                value.get("moduleInstanceVersion", 1), "module version"
            ),
            module_label=_required_text(value.get("moduleLabel"), "module label"),
            day_timezone=_required_text(
                day_policy.get("timezone"), "historical day timezone"
            ),
            day_boundary=_required_text(
                day_policy.get("boundary"), "historical day boundary"
            ),
            boundary_policy_revision=_required_text(
                day_policy.get("policyRevision"),
                "historical boundary policy revision",
            ),
            module_type_id=str(value.get("moduleTypeId") or "field_group"),
            module_type_version=_positive(
                value.get("moduleTypeVersion", 1), "module type version"
            ),
            module_slot_id=str(value.get("moduleSlotId") or "historical-fields"),
            module_settings=dict(value.get("moduleSettings") or {}),
            behavior_id=str(value.get("behaviorId") or "provenance_only"),
            behavior_version=_positive(
                value.get("behaviorVersion", 1), "module interaction behavior version"
            ),
            authorship=str(value.get("authorship") or "unknown"),
            review_state=str(value.get("reviewState") or "unknown"),
            fields=tuple(JournalImportFieldMapping.from_dict(item) for item in fields),
            profile_modules=tuple(
                JournalImportProfileModuleRef.from_dict(item)
                for item in profile_modules
            ),
        )

    @property
    def module_settings_sha256(self) -> str:
        return sha256_json(dict(self.module_settings))

    @property
    def profile_digest(self) -> str:
        return sha256_json(
            {
                "formatVersion": 1,
                "modules": [item.to_dict() for item in self.profile_modules],
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TYPED_IMPORT_MAPPING_SCHEMA,
            "mappingVersion": self.mapping_version,
            "profileId": self.profile_id,
            "profileRevision": self.profile_revision,
            "profileName": self.profile_name,
            "profileDescription": self.profile_description,
            "moduleInstanceId": self.module_instance_id,
            "moduleInstanceVersion": self.module_instance_version,
            "moduleTypeId": self.module_type_id,
            "moduleTypeVersion": self.module_type_version,
            "moduleLabel": self.module_label,
            "moduleSlotId": self.module_slot_id,
            "moduleSettings": dict(self.module_settings),
            "profileModules": [item.to_dict() for item in self.profile_modules],
            "dayPolicy": {
                "timezone": self.day_timezone,
                "boundary": self.day_boundary,
                "policyRevision": self.boundary_policy_revision,
            },
            "behaviorId": self.behavior_id,
            "behaviorVersion": self.behavior_version,
            "authorship": self.authorship,
            "reviewState": self.review_state,
            "fields": [item.to_dict() for item in self.fields],
            "profileDigest": self.profile_digest,
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.to_dict())

    def field(self, field_id: str) -> JournalImportFieldMapping:
        for candidate in self.fields:
            if candidate.field_id == field_id:
                return candidate
        raise JournalCaptureValidationError("The typed observation field is unmapped.")


@dataclass(frozen=True, slots=True)
class JournalImportTypedObservation:
    relative_path: str
    local_date: str
    field_id: str
    evidence_start_byte: int
    evidence_end_byte: int
    evidence_sha256: str
    extractor_receipt_sha256: str
    value: Any = field(default=None, repr=False)
    disposition: str | None = None
    observed_at: str | None = None
    stated_at: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.relative_path, "relative source path", maximum=1024)
        _required_text(self.field_id, "field identity")
        try:
            date.fromisoformat(self.local_date)
        except (TypeError, ValueError) as exc:
            raise JournalCaptureValidationError("The typed observation date is invalid.") from exc
        if (
            isinstance(self.evidence_start_byte, bool)
            or isinstance(self.evidence_end_byte, bool)
            or self.evidence_start_byte < 0
            or self.evidence_end_byte <= self.evidence_start_byte
        ):
            raise JournalCaptureValidationError("The typed observation evidence range is invalid.")
        for digest in (self.evidence_sha256, self.extractor_receipt_sha256):
            if not isinstance(digest, str) or len(digest) != 64:
                raise JournalCaptureValidationError("A typed observation digest is invalid.")
        if self.disposition is None:
            if self.value is None:
                raise JournalCaptureValidationError("A typed observation needs a value.")
        else:
            try:
                JournalValueDisposition(self.disposition)
            except ValueError as exc:
                raise JournalCaptureValidationError(
                    "The typed observation disposition is invalid."
                ) from exc
            if self.value is not None:
                raise JournalCaptureValidationError(
                    "A typed observation cannot have a value and disposition."
                )

    def normalized_value(
        self, field_mapping: JournalImportFieldMapping | Mapping[str, Any]
    ) -> tuple[dict[str, Any], list[str], list[Mapping[str, Any]], Mapping[str, Any]]:
        if isinstance(field_mapping, JournalImportFieldMapping):
            value_kind = field_mapping.value_kind
            constraints = field_mapping.constraints
        else:
            value_kind = str(field_mapping["value_kind"])
            constraints_raw = field_mapping["constraints_json"]
            constraints = (
                json.loads(str(constraints_raw))
                if isinstance(constraints_raw, str)
                else dict(constraints_raw)
            )
        definition = {
            "value_kind": value_kind,
            "constraints_json": canonical_json(dict(constraints)),
        }
        return JournalDomainService._encode_field_value(
            definition, value=self.value, disposition=self.disposition
        )

    def identity_payload(self, cohort_id: str, file_id: str) -> dict[str, Any]:
        return {
            "schema": TYPED_IMPORT_OBSERVATION_SCHEMA,
            "cohortId": cohort_id,
            "fileId": file_id,
            "fieldId": self.field_id,
            "localDate": self.local_date,
            "evidenceStartByte": self.evidence_start_byte,
            "evidenceEndByte": self.evidence_end_byte,
            "evidenceSha256": self.evidence_sha256,
        }

    def observation_id(self, cohort_id: str, file_id: str) -> str:
        return "jhio_" + sha256_json(self.identity_payload(cohort_id, file_id))[:32]

    def value_id(self, cohort_id: str, file_id: str) -> str:
        return "jfv_" + sha256_json(
            {**self.identity_payload(cohort_id, file_id), "kind": "field_value"}
        )[:32]


__all__ = [
    "JournalImportFieldMapping",
    "JournalImportProfileMapping",
    "JournalImportProfileModuleRef",
    "JournalImportTypedObservation",
    "TYPED_IMPORT_MAPPING_SCHEMA",
    "TYPED_IMPORT_OBSERVATION_SCHEMA",
    "canonical_json",
    "sha256_json",
]

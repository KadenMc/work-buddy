"""Declarative ``store.yaml`` profiles for targeted truth stores.

Profiles constrain new writes and presentation policy. They never reinterpret
or invalidate rows already present in a ledger. Claim kinds, confirmation
surfaces, validator names, and extension keys are intentionally open sets.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from work_buddy.truth.contracts import ProfileError, StorePaths


_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DOTTED_FIELD_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$"
)
_DURATION_RE = re.compile(r"^([1-9][0-9]*)([smhdw])$")
_DURATION_MULTIPLIERS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}
_PROJECTION_MODES = frozenset({"resident", "on_demand", "none"})
_REJECTED_CONTENT_POLICIES = frozenset({"redact", "retain"})

_TOP_LEVEL_KEYS = frozenset(
    {
        "store_id",
        "profile",
        "title",
        "name",
        "allowed_claim_kinds",
        "required_fields",
        "gate",
        "projection",
        "export_committed",
        "proposal_max_age",
        "validators",
        "extensions",
    }
)
_GATE_KEYS = frozenset(
    {
        "rejected_content",
        "confirmation_surfaces",
        "block_materialize_on_flags",
    }
)


def _plain_copy(value: Any) -> Any:
    """Return an independent copy of YAML-compatible profile data."""
    return copy.deepcopy(value)


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProfileError(f"{label} must be a mapping")
    return value


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"{label} must be a nonempty string")
    return value.strip()


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ProfileError(f"{label} must be true or false")
    return value


def _unique_strings(value: Any, label: str, *, nonempty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ProfileError(f"{label} must be a list")
    items = tuple(_require_nonempty_string(item, label) for item in value)
    if nonempty and not items:
        raise ProfileError(f"{label} must contain at least one value")
    if len(set(items)) != len(items):
        raise ProfileError(f"{label} must not contain duplicates")
    return items


def normalize_store_id(value: Any) -> str:
    """Validate a UUID-shaped store identity and return lowercase hex."""
    text = _require_nonempty_string(value, "store_id")
    if not (
        re.fullmatch(r"[0-9A-Fa-f]{32}", text)
        or re.fullmatch(
            r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
            r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
            text,
        )
    ):
        raise ProfileError("store_id must be 32 hex characters or a canonical UUID")
    try:
        return uuid.UUID(text).hex
    except ValueError as exc:
        raise ProfileError("store_id must be UUID-compatible") from exc


def _normalize_proposal_max_age(value: Any) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ProfileError("proposal_max_age must be null, positive seconds, or a duration")
    if isinstance(value, int):
        if value <= 0:
            raise ProfileError("proposal_max_age seconds must be positive")
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if _DURATION_RE.fullmatch(text):
            return text
    raise ProfileError(
        "proposal_max_age must be null, positive seconds, or a duration such as 30d"
    )


def _proposal_max_age_seconds(value: int | str | None) -> int | None:
    if value is None or isinstance(value, int):
        return value
    match = _DURATION_RE.fullmatch(value)
    if match is None:  # pragma: no cover, construction always validates this
        raise ProfileError(f"invalid proposal_max_age: {value!r}")
    amount, unit = match.groups()
    return int(amount) * _DURATION_MULTIPLIERS[unit]


@dataclass(frozen=True, slots=True)
class GatePolicy:
    """Content and confirmation policy declared by one store profile."""

    rejected_content: str
    confirmation_surfaces: tuple[str, ...]
    block_materialize_on_flags: bool
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "rejected_content": self.rejected_content,
            "confirmation_surfaces": list(self.confirmation_surfaces),
            "block_materialize_on_flags": self.block_materialize_on_flags,
        }
        data.update(_plain_copy(dict(self.extensions)))
        return data


@dataclass(frozen=True, slots=True)
class StoreProfile:
    """Validated declarative policy for one targeted truth store."""

    store_id: str
    profile: str
    title: str
    allowed_claim_kinds: tuple[str, ...]
    required_fields: Mapping[str, tuple[str, ...]]
    gate: GatePolicy
    projection: str
    export_committed: bool
    proposal_max_age: int | str | None = None
    validators: Mapping[str, Any] = field(default_factory=dict)
    extensions: Mapping[str, Any] = field(default_factory=dict)
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def proposal_max_age_seconds(self) -> int | None:
        """Return the configured proposal lifetime in seconds."""
        return _proposal_max_age_seconds(self.proposal_max_age)

    def to_dict(self) -> dict[str, Any]:
        """Return a YAML-safe mapping without discarding extension data."""
        data: dict[str, Any] = {
            "store_id": self.store_id,
            "profile": self.profile,
            "title": self.title,
            "allowed_claim_kinds": list(self.allowed_claim_kinds),
            "required_fields": {
                kind: list(paths) for kind, paths in self.required_fields.items()
            },
            "gate": self.gate.to_dict(),
            "projection": self.projection,
            "export_committed": self.export_committed,
            "proposal_max_age": self.proposal_max_age,
        }
        if self.validators:
            data["validators"] = _plain_copy(dict(self.validators))
        if self.extensions:
            data["extensions"] = _plain_copy(dict(self.extensions))
        data.update(_plain_copy(dict(self.extra)))
        return data


def _parse_gate(value: Any) -> GatePolicy:
    raw = _require_mapping(value, "gate")
    rejected_content = _require_nonempty_string(
        raw.get("rejected_content"), "gate.rejected_content"
    )
    if rejected_content not in _REJECTED_CONTENT_POLICIES:
        raise ProfileError("gate.rejected_content must be exactly redact or retain")
    confirmation_surfaces = _unique_strings(
        raw.get("confirmation_surfaces"), "gate.confirmation_surfaces"
    )
    block_materialize = _require_bool(
        raw.get("block_materialize_on_flags"),
        "gate.block_materialize_on_flags",
    )
    extras = {key: _plain_copy(item) for key, item in raw.items() if key not in _GATE_KEYS}
    return GatePolicy(
        rejected_content=rejected_content,
        confirmation_surfaces=confirmation_surfaces,
        block_materialize_on_flags=block_materialize,
        extensions=extras,
    )


def _parse_required_fields(
    value: Any,
    allowed_claim_kinds: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    raw = _require_mapping(value, "required_fields")
    allowed = set(allowed_claim_kinds)
    parsed: dict[str, tuple[str, ...]] = {}
    for raw_kind, raw_paths in raw.items():
        kind = _require_nonempty_string(raw_kind, "required_fields claim kind")
        if kind not in allowed:
            raise ProfileError(
                f"required_fields declares disallowed claim kind {kind!r}"
            )
        paths = _unique_strings(
            raw_paths,
            f"required_fields.{kind}",
            nonempty=False,
        )
        invalid = [path for path in paths if _DOTTED_FIELD_RE.fullmatch(path) is None]
        if invalid:
            raise ProfileError(
                f"required_fields.{kind} contains invalid dotted paths: {invalid}"
            )
        parsed[kind] = paths
    return parsed


def validate_profile(value: StoreProfile | Mapping[str, Any]) -> StoreProfile:
    """Validate profile data and return its normalized model.

    This validates policy configuration only. It does not inspect existing
    claims, so tightening a profile can never invalidate ledger history.
    """
    if isinstance(value, StoreProfile):
        value = value.to_dict()
    raw = _require_mapping(value, "profile")

    profile_name = _require_nonempty_string(raw.get("profile"), "profile")
    if _PROFILE_NAME_RE.fullmatch(profile_name) is None:
        raise ProfileError(
            "profile must start with a lowercase letter or digit and contain "
            "only lowercase letters, digits, underscores, or hyphens"
        )

    title_value = raw.get("title")
    name_value = raw.get("name")
    if title_value is not None and name_value is not None:
        title = _require_nonempty_string(title_value, "title")
        name = _require_nonempty_string(name_value, "name")
        if title != name:
            raise ProfileError("title and name must match when both are declared")
    else:
        title = _require_nonempty_string(
            title_value if title_value is not None else name_value,
            "title or name",
        )

    allowed_claim_kinds = _unique_strings(
        raw.get("allowed_claim_kinds"), "allowed_claim_kinds"
    )
    required_fields = _parse_required_fields(
        raw.get("required_fields", {}), allowed_claim_kinds
    )
    gate = _parse_gate(raw.get("gate"))

    projection = _require_nonempty_string(raw.get("projection"), "projection")
    if projection not in _PROJECTION_MODES:
        raise ProfileError("projection must be exactly resident, on_demand, or none")

    validators_raw = raw.get("validators", {})
    validators = _require_mapping(validators_raw, "validators")
    extensions_raw = raw.get("extensions", {})
    extensions = _require_mapping(extensions_raw, "extensions")
    extra = {
        key: _plain_copy(item)
        for key, item in raw.items()
        if key not in _TOP_LEVEL_KEYS
    }

    return StoreProfile(
        store_id=normalize_store_id(raw.get("store_id")),
        profile=profile_name,
        title=title,
        allowed_claim_kinds=allowed_claim_kinds,
        required_fields=required_fields,
        gate=gate,
        projection=projection,
        export_committed=_require_bool(
            raw.get("export_committed"), "export_committed"
        ),
        proposal_max_age=_normalize_proposal_max_age(
            raw.get("proposal_max_age")
        ),
        validators=_plain_copy(dict(validators)),
        extensions=_plain_copy(dict(extensions)),
        extra=extra,
    )


def _profile_path(path_or_root: str | Path) -> Path:
    path = Path(path_or_root).expanduser()
    if path.name == "store.yaml":
        return path.resolve()
    return StorePaths.from_root(path).config


def load_profile(path_or_root: str | Path) -> StoreProfile:
    """Load and validate ``store.yaml`` from a file, sidecar, or scope root."""
    path = _profile_path(path_or_root)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"could not load profile {path}: {exc}") from exc
    try:
        return validate_profile(raw)
    except ProfileError as exc:
        raise ProfileError(f"invalid profile {path}: {exc}") from exc


def dump_profile(
    profile: StoreProfile | Mapping[str, Any],
    path_or_root: str | Path,
) -> Path:
    """Validate and write ``store.yaml`` to a file, sidecar, or scope root."""
    validated = validate_profile(profile)
    path = _profile_path(path_or_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(
        validated.to_dict(),
        sort_keys=False,
        allow_unicode=True,
    )
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ProfileError(f"could not write profile {path}: {exc}") from exc
    return path


def _structured_mapping(value: Mapping[str, Any] | str | None) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProfileError("structured claim data must be valid JSON") from exc
        if not isinstance(parsed, Mapping):
            raise ProfileError("structured claim data must contain an object")
        return parsed
    if not isinstance(value, Mapping):
        raise ProfileError("structured claim data must be a mapping or JSON object")
    return value


def _has_required_value(structured: Mapping[str, Any], dotted_path: str) -> bool:
    current: Any = structured
    for segment in dotted_path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return False
        current = current[segment]
    return current is not None and not (
        isinstance(current, str) and not current.strip()
    )


def validate_new_claim(
    profile: StoreProfile | Mapping[str, Any],
    *,
    claim_kind: str,
    structured: Mapping[str, Any] | str | None = None,
    confirmation_surface: str | None = None,
) -> None:
    """Validate one proposed or confirmed NEW claim against current policy.

    Callers must not use this function to revalidate stored claims after a
    profile edit. Existing history remains valid regardless of later policy.
    """
    profile = validate_profile(profile)
    kind = _require_nonempty_string(claim_kind, "claim_kind")
    if kind not in profile.allowed_claim_kinds:
        raise ProfileError(
            f"claim kind {kind!r} is not allowed by profile {profile.profile!r}"
        )

    structured_map = _structured_mapping(structured)
    missing = [
        path
        for path in profile.required_fields.get(kind, ())
        if not _has_required_value(structured_map, path)
    ]
    if missing:
        raise ProfileError(
            f"new {kind!r} claim is missing required structured fields: {missing}"
        )

    if confirmation_surface is not None:
        surface = _require_nonempty_string(
            confirmation_surface, "confirmation_surface"
        )
        if surface not in profile.gate.confirmation_surfaces:
            raise ProfileError(
                f"confirmation surface {surface!r} is not allowed by profile "
                f"{profile.profile!r}"
            )


__all__ = [
    "GatePolicy",
    "StoreProfile",
    "dump_profile",
    "load_profile",
    "normalize_store_id",
    "validate_new_claim",
    "validate_profile",
]

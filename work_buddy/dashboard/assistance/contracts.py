"""Both hosts consume form_schemas.json; no second hand-maintained field list.

Snapshots are immutable per-turn evidence, not a server-side draft. Patches are
advisory, contain no selectors/callbacks/submit operation, and are all-or-nothing
validated before the mounted host decides which fields are still uncontested.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

IDENTITY_KEYS = (
    "profileId", "workspaceId", "appId", "viewId", "instanceId",
    "widgetTypeId", "draftName", "scopeKey",
)
FORBIDDEN = frozenset({"__proto__", "constructor", "prototype"})
PURPOSE = "dashboard.assisted_draft"


class AssistanceError(ValueError):
    """Public, content-free errors: never echo model output or draft values."""

    def __init__(self, code: str, message: str | None = None, status: int = 400):
        self.code = code
        self.status = status
        super().__init__(message or code.replace("_", " "))


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def manifest() -> dict[str, Any]:
    return json.loads(Path(__file__).with_name("form_schemas.json").read_text(encoding="utf-8"))


def form_schema(draft_name: str, schema: Any = None) -> dict[str, Any]:
    form = manifest()["forms"].get(draft_name)
    if form is None or (schema is not None and schema != form["schema"]):
        raise AssistanceError("unknown_assisted_schema")
    return form


def text_id(value: Any, label: str = "identity") -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(ch) < 32 for ch in value):
        raise AssistanceError(f"invalid_{label}")
    return value


def validate_identity(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(IDENTITY_KEYS):
        raise AssistanceError("invalid_draft_identity")
    return {key: text_id(value[key], "draft_identity") for key in IDENTITY_KEYS}


def field_for(form: Mapping[str, Any], path: Any) -> Mapping[str, Any]:
    if not isinstance(path, list) or not path or any(not isinstance(part, str) or part in FORBIDDEN or not part or part == "*" for part in path):
        raise AssistanceError("invalid_field_path")
    field = next((field for field in form["fields"] if field["path"] == path), None)
    if field is None or field["sensitivity"] == "secret" or field["disclosure"] != "explicit_start":
        raise AssistanceError("field_not_assistable")
    return field


def validate_value(field: Mapping[str, Any], value: Any) -> None:
    kind = field["type"]
    valid = (
        (kind == "string" and isinstance(value, str))
        or (kind == "boolean" and isinstance(value, bool))
        or (kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool))
    )
    if not valid:
        raise AssistanceError("invalid_field_type")
    if "enum" in field and value not in field["enum"]:
        raise AssistanceError("invalid_field_value")
    if kind == "number" and (value < field.get("minimum", float("-inf")) or value > field.get("maximum", float("inf"))):
        raise AssistanceError("invalid_field_value")
    if isinstance(value, str):
        if len(value) > field.get("maxLength", 8192):
            raise AssistanceError("field_too_large")
        if "pattern" in field and re.fullmatch(field["pattern"], value) is None:
            raise AssistanceError("invalid_field_value")
        if field.get("contentPolicy") == "non_secret_json" and value.strip():
            try:
                parameters = json.loads(value)
            except (ValueError, TypeError) as exc:
                raise AssistanceError("parameters_must_be_valid_json") from exc
            def inspect(item: Any) -> None:
                if isinstance(item, dict):
                    for key, child in item.items():
                        if key in FORBIDDEN or re.search(r"password|passwd|secret|token|api[_-]?key|credential|authorization|private[_-]?key", key, re.IGNORECASE):
                            raise AssistanceError("secret_parameters_not_disclosed")
                        inspect(child)
                elif isinstance(item, list):
                    for child in item:
                        inspect(child)
            inspect(parameters)
    canonical(value)  # reject NaN/Infinity as non-JSON


def validate_snapshot(form: Mapping[str, Any], value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AssistanceError("invalid_snapshot")
    if len(canonical(value).encode("utf-8")) > form["maxSnapshotBytes"]:
        raise AssistanceError("snapshot_too_large")
    # Paths are arrays, and this recursive walk refuses unknown/intermediate
    # objects. Extending the canonical manifest can opt in nested fields.
    def walk(node: Mapping[str, Any], prefix: list[str]) -> None:
        for key, item in node.items():
            if key in FORBIDDEN:
                raise AssistanceError("invalid_field_path")
            path = [*prefix, key]
            if isinstance(item, Mapping):
                if not any(field["path"][:len(path)] == path for field in form["fields"]):
                    raise AssistanceError("field_not_assistable")
                walk(item, path)
            else:
                validate_value(field_for(form, path), item)
    walk(value, [])
    return json.loads(canonical(value))


def validate_operations(form: Mapping[str, Any], operations: Any) -> list[dict[str, Any]]:
    if not isinstance(operations, list) or len(operations) > form["maxOperations"]:
        raise AssistanceError("invalid_patch_operations")
    if len(canonical(operations).encode("utf-8")) > form["maxPatchBytes"]:
        raise AssistanceError("patch_too_large")
    seen: set[tuple[str, ...]] = set()
    result = []
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("op") not in form["allowedOperations"]:
            raise AssistanceError("invalid_patch_operation")
        expected = {"op", "path", "value"} if operation["op"] == "set" else {"op", "path"}
        if set(operation) != expected:
            raise AssistanceError("invalid_patch_operation")
        field = field_for(form, operation["path"])
        path = tuple(operation["path"])
        if path in seen:
            raise AssistanceError("duplicate_patch_field")
        seen.add(path)
        if operation["op"] == "remove":
            if field["required"]:
                raise AssistanceError("required_field_remove")
        else:
            validate_value(field, operation["value"])
        result.append(dict(operation))
    return result


def structured_reply_schema(form: Mapping[str, Any]) -> dict[str, Any]:
    variants = []
    for field in form["fields"]:
        if field["sensitivity"] == "secret" or field["disclosure"] != "explicit_start":
            continue
        value_schema = {key: field[key] for key in ("type", "maxLength", "enum", "pattern", "minimum", "maximum") if key in field}
        variants.append({
            "type": "object", "additionalProperties": False,
            "properties": {"op": {"const": "set"}, "path": {"const": field["path"]}, "value": value_schema},
            "required": ["op", "path", "value"],
        })
        if not field["required"]:
            variants.append({
                "type": "object", "additionalProperties": False,
                "properties": {"op": {"const": "remove"}, "path": {"const": field["path"]}},
                "required": ["op", "path"],
            })
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "reply": {"type": "string", "minLength": 1, "maxLength": 4000},
            "operations": {"type": "array", "maxItems": form["maxOperations"], "items": {"anyOf": variants}},
        },
        "required": ["reply", "operations"],
    }

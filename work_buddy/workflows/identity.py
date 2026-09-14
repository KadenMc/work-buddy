"""Stable identity and revision helpers for authored Workflows."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any


WORKFLOW_ID_PATTERN = re.compile(r"^wfd_[0-9a-f]{32}$")
WORKFLOW_REVISION_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_DERIVED_ID_NAMESPACE = uuid.UUID("2585b83a-d50f-4af4-b5e8-80b36c41cc9f")


def new_workflow_id() -> str:
    """Return a new opaque Workflow-definition ID.

    Definition IDs deliberately use ``wfd_`` rather than the conductor's
    ``wf_`` run prefix, so definition and execution identities cannot be
    confused at API or persistence boundaries.
    """

    return f"wfd_{uuid.uuid4().hex}"


def derived_workflow_id(workflow_name: str) -> str:
    """Return a deterministic read-compat ID for an unmigrated definition.

    This is never written implicitly. It keeps a user-owned local Workflow
    readable until an authoring/edit path persists an opaque ID.
    """

    return f"wfd_{uuid.uuid5(_DERIVED_ID_NAMESPACE, workflow_name).hex}"


def is_valid_workflow_id(value: Any) -> bool:
    """Return whether *value* is a canonical Workflow-definition ID."""

    return isinstance(value, str) and WORKFLOW_ID_PATTERN.fullmatch(value) is not None


def require_workflow_id(value: Any) -> str:
    """Return a valid definition ID or raise a stable validation error."""

    if not is_valid_workflow_id(value):
        raise ValueError(
            "workflow_id must match 'wfd_' followed by 32 lowercase hex characters"
        )
    return value


def _jsonable(value: Any) -> Any:
    """Normalize authored definition data for deterministic hashing."""

    if hasattr(value, "to_dict"):
        value = value.to_dict()
    elif is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(item) for item in value)
    return value


def compute_workflow_revision(
    definition: Any,
    *,
    bound_instructions: Any | None = None,
    resolved_workflow_refs: Any | None = None,
) -> str:
    """Hash a canonical authored snapshot used to pin a Workflow definition.

    Source paths and generic knowledge-hierarchy fields are excluded so moving
    a unit does not manufacture a new definition. The directly bound Directions
    unit is included when present. Resolved child-Workflow identities are also
    included when supplied, so reassigning an executable alias cannot silently
    change the compiled target without changing the parent revision. Recursive
    rendering of referenced Directions remains outside this initial revision
    seam.
    """

    if hasattr(definition, "to_dict"):
        authored = definition.to_dict()
    elif is_dataclass(definition) and not isinstance(definition, type):
        authored = asdict(definition)
    else:
        authored = definition
    if isinstance(authored, Mapping):
        # Only loader-derived top-level fields are excluded. A nested field
        # named ``path`` may be an execution input and therefore belongs in
        # the revision.
        authored = dict(authored)
        for key in (
            "path",
            "scope",
            "parents",
            "children",
            "workflow_file",
            "bound_directions_path",
            "workflow_revision",
        ):
            authored.pop(key, None)
    snapshot: dict[str, Any] = {"workflow": _jsonable(authored)}
    if bound_instructions is not None:
        if hasattr(bound_instructions, "to_dict"):
            instructions = bound_instructions.to_dict()
        elif is_dataclass(bound_instructions) and not isinstance(
            bound_instructions, type
        ):
            instructions = asdict(bound_instructions)
        else:
            instructions = bound_instructions
        if isinstance(instructions, Mapping):
            instructions = dict(instructions)
            for key in ("path", "scope", "parents", "children", "workflow"):
                instructions.pop(key, None)
        snapshot["bound_instructions"] = _jsonable(instructions)
    if resolved_workflow_refs is not None:
        snapshot["resolved_workflow_refs"] = _jsonable(resolved_workflow_refs)
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


__all__ = [
    "WORKFLOW_ID_PATTERN",
    "WORKFLOW_REVISION_PATTERN",
    "compute_workflow_revision",
    "is_valid_workflow_id",
    "derived_workflow_id",
    "new_workflow_id",
    "require_workflow_id",
]

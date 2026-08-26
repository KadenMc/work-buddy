"""Canonical, non-dispatching registry projection for Jobs authoring.

The Jobs picker and hosted form assistance must agree about the names and
parameter shapes a human may put into a scheduled-job draft.  This module is
deliberately metadata-only: it never dispatches a capability or starts a
workflow.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

MAX_REFERENCE_RESULTS = 8


def project_param_schema(raw: Any) -> list[dict[str, Any]]:
    """Flatten a registry parameter mapping into the Jobs UI contract."""

    out: list[dict[str, Any]] = []
    for name, details in (raw or {}).items():
        if not isinstance(details, Mapping):
            continue
        out.append(
            {
                "name": name,
                "type": details.get("type", ""),
                "description": details.get("description", ""),
                "required": bool(details.get("required", False)),
            }
        )
    return out


def job_registry_projection() -> dict[str, list[dict[str, Any]]]:
    """Return the exact capability/workflow catalog shown by Jobs.

    This is a read-only authoring projection, not an execution allowlist.  The
    existing create-job validator remains authoritative at human submission.
    """

    from work_buddy.mcp_server.registry import (
        Capability,
        WorkflowDefinition,
        get_registry,
    )

    result: dict[str, list[dict[str, Any]]] = {
        "capabilities": [],
        "workflows": [],
    }
    for name, entry in sorted(get_registry().items()):
        if isinstance(entry, WorkflowDefinition):
            bucket = "workflows"
            raw_parameters = entry.params_schema
        elif isinstance(entry, Capability):
            bucket = "capabilities"
            raw_parameters = entry.parameters
        else:
            continue
        description = (getattr(entry, "description", "") or "").split("\n", 1)[0]
        item = {
            "name": name,
            "description": description,
            "parameters": project_param_schema(raw_parameters),
            "slash_command": getattr(entry, "slash_command", None) or "",
        }
        result[bucket].append(item)
    return result


def _search_text(value: str) -> str:
    return " ".join(re.split(r"[^a-z0-9]+", value.casefold())).strip()


def _match_rank(item: Mapping[str, Any], query: str) -> tuple[int, str] | None:
    name = _search_text(str(item.get("name", "")))
    slash = _search_text(str(item.get("slash_command", "")))
    description = _search_text(str(item.get("description", "")))
    needle = _search_text(query)
    if not needle:
        return (4, name)
    if needle in {name, slash}:
        return (0, name)
    if name.startswith(needle) or slash.startswith(needle):
        return (1, name)
    if needle in name or needle in slash:
        return (2, name)
    words = needle.split()
    haystack = f"{name} {slash} {description}"
    if all(word in haystack for word in words):
        return (3, name)
    return None


def search_job_registry(
    *, reference_kind: str, query: str, limit: int = MAX_REFERENCE_RESULTS
) -> list[dict[str, Any]]:
    """Search the host-visible Jobs catalog without invoking any entry."""

    key = {
        "job_capability": "capabilities",
        "job_workflow": "workflows",
    }.get(reference_kind)
    if key is None:
        raise ValueError("unsupported job registry reference kind")
    bounded_limit = max(1, min(int(limit), MAX_REFERENCE_RESULTS))
    ranked = []
    for item in job_registry_projection()[key]:
        rank = _match_rank(item, query)
        if rank is not None:
            ranked.append((rank, item))
    ranked.sort(key=lambda pair: pair[0])
    return [dict(item) for _, item in ranked[:bounded_limit]]

"""Declarative native Settings contributions for Work Buddy applications.

Definitions, pages, and placements remain separate on purpose. The Journal day
boundary is defined once and rendered on its owning App page. Views link to that
canonical page instead of creating duplicate navigation or editing surfaces.
"""

from __future__ import annotations

import copy
from typing import Any

from work_buddy.journal_day import DEFAULT_DAY_BOUNDARY, parse_local_time


SCHEMA_VERSION = 1
REGISTRY_REVISION = "settings-registry:1"
JOURNAL_DAY_BOUNDARY_ID = "wb.journal.day-boundary"
PROFILE_SCOPE_ID = "default"


_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "setting_id": JOURNAL_DAY_BOUNDARY_ID,
        "definition_version": 1,
        "value_version": 1,
        "owner": {"kind": "app", "id": "wb.journal", "label": "Journal"},
        "provenance": {
            "complement_id": "wb.journal",
            "label": "Journal",
            "trust_tier": "native",
        },
        "title": "Day starts",
        "short_description": (
            "Choose when a new Journal day begins instead of assuming midnight."
        ),
        "long_description": (
            "Work completed after midnight but before this time belongs to the "
            "previous Journal day. Changes begin at the next safe day boundary; "
            "existing Journal days retain the window under which they were created."
        ),
        "keywords": ["cutoff", "boundary", "midnight", "late night", "next day"],
        "tags": ["time", "journal-day-lifecycle"],
        "value_schema": {
            "type": "string",
            "format": "local-time",
            "pattern": r"^(?:[01]\d|2[0-3]):[0-5]\d$",
        },
        "default_value": DEFAULT_DAY_BOUNDARY,
        "allowed_scopes": ["profile"],
        "default_scope": "profile",
        "applies_to": [
            {"kind": "app", "id": "wb.journal", "label": "Journal"},
            {
                "kind": "subsystem",
                "id": "wb.journal/day-lifecycle",
                "label": "Journal day lifecycle",
            },
            {"kind": "view", "id": "wb.journal.main", "label": "Journal view"},
        ],
        "affects": [
            {
                "ref": {
                    "kind": "view",
                    "id": "wb.journal.main",
                    "label": "Journal view",
                },
                "note": "Changes the Journal header, Timeline, and List day window.",
            },
            {
                "ref": {
                    "kind": "capability",
                    "id": "journal_state",
                    "label": "Journal state",
                },
                "note": "Changes which newly resolved Journal day owns an instant.",
            },
        ],
        "presentation": {
            "control": "time",
            "minute_step": 15,
            "apply_behavior": "next-boundary",
        },
        "visibility": "frontend",
        "sensitivity": "ordinary",
    },
)


_PAGES: tuple[dict[str, Any], ...] = (
    {
        "page_id": "wb.settings.app.journal",
        "context_id": "wb.settings.app.journal",
        "context": {"kind": "app", "id": "wb.journal", "label": "Journal"},
        "owner": {"kind": "app", "id": "wb.journal", "label": "Journal"},
        "route": "/app/settings/apps/journal",
        "label": "Journal",
        "description": "Behavior shared by Journal and every Journal view.",
        "navigation_group": "apps",
        "navigation_category": "built-in",
        "order": 10,
        "sections": [
            {
                "section_id": "day-behavior",
                "label": "Day behavior",
                "order": 10,
            }
        ],
    },
)


_PLACEMENTS: tuple[dict[str, Any], ...] = (
    {
        "placement_id": "wb.settings.placement.app.journal.day-boundary",
        "setting_id": JOURNAL_DAY_BOUNDARY_ID,
        "page_id": "wb.settings.app.journal",
        "context_id": "wb.settings.app.journal",
        "section_id": "day-behavior",
        "order": 10,
    },
)


def _validate_native_registry() -> None:
    definitions = {item["setting_id"]: item for item in _DEFINITIONS}
    if len(definitions) != len(_DEFINITIONS):
        raise RuntimeError("duplicate native setting definition ID")
    pages = {item["page_id"]: item for item in _PAGES}
    if len(pages) != len(_PAGES):
        raise RuntimeError("duplicate native settings page ID")
    placements = {item["placement_id"]: item for item in _PLACEMENTS}
    if len(placements) != len(_PLACEMENTS):
        raise RuntimeError("duplicate native setting placement ID")

    for definition in _DEFINITIONS:
        if definition["setting_id"] == JOURNAL_DAY_BOUNDARY_ID:
            parse_local_time(definition["default_value"])
        if definition["default_scope"] not in definition["allowed_scopes"]:
            raise RuntimeError(
                f"invalid default scope for {definition['setting_id']}"
            )

    for placement in _PLACEMENTS:
        if placement["setting_id"] not in definitions:
            raise RuntimeError(f"unknown setting in placement {placement['placement_id']}")
        page = pages.get(placement["page_id"])
        if page is None:
            raise RuntimeError(f"unknown page in placement {placement['placement_id']}")
        section_ids = {section["section_id"] for section in page["sections"]}
        if placement["section_id"] not in section_ids:
            raise RuntimeError(
                f"unknown section in placement {placement['placement_id']}"
            )


_validate_native_registry()


def registry_payload() -> dict[str, Any]:
    """Return a detached JSON-compatible registry snapshot."""
    return {
        "schema_version": SCHEMA_VERSION,
        "registry_revision": REGISTRY_REVISION,
        "definitions": copy.deepcopy(list(_DEFINITIONS)),
        "pages": copy.deepcopy(list(_PAGES)),
        "placements": copy.deepcopy(list(_PLACEMENTS)),
    }


def definition_for(setting_id: str) -> dict[str, Any] | None:
    for definition in _DEFINITIONS:
        if definition["setting_id"] == setting_id:
            return copy.deepcopy(definition)
    return None


def page_for_context(context_id: str) -> dict[str, Any] | None:
    for page in _PAGES:
        if page["context_id"] == context_id:
            return copy.deepcopy(page)
    return None


def setting_ids_for_context(context_id: str | None) -> list[str]:
    if context_id is None:
        return [definition["setting_id"] for definition in _DEFINITIONS]
    if page_for_context(context_id) is None:
        raise KeyError(context_id)
    ordered = sorted(
        (item for item in _PLACEMENTS if item["context_id"] == context_id),
        key=lambda item: (item.get("order", 0), item["placement_id"]),
    )
    return list(dict.fromkeys(item["setting_id"] for item in ordered))

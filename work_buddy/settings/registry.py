"""Declarative native Settings contributions for Work Buddy applications.

Definitions, pages, and placements remain separate on purpose. Each setting is
defined once and rendered on its owning System or App page. Views link to that
canonical page instead of creating duplicate navigation or editing surfaces.
"""

from __future__ import annotations

import copy
from typing import Any

from work_buddy.journal_day import DEFAULT_DAY_BOUNDARY, parse_local_time


SCHEMA_VERSION = 1
REGISTRY_REVISION = "settings-registry:7"
JOURNAL_DAY_BOUNDARY_ID = "wb.journal.day-boundary"
JOURNAL_SMART_PROCESSING_ID = "wb.journal.smart-processing"
JOURNAL_SMART_EXECUTION_ID = "wb.journal.smart-execution"
JOURNAL_SMART_EXECUTION_SUBSCRIPTION_AGENT = "subscription_agent"
JOURNAL_SMART_EXECUTION_API_MODEL = "api_model"
DASHBOARD_ASSISTANCE_ID = "wb.dashboard.assistance"
DASHBOARD_ASSISTANCE_TIER_ID = "wb.dashboard.assistance-tier"
DASHBOARD_CHAT_EXECUTION_DEFAULT_ID = "wb.dashboard.chat-execution-default"
DASHBOARD_AI_CONTEXT_ID = "wb.settings.system.dashboard-ai"
COWORK_REVIEW_NAV_BINDING_ID = "wb.cowork.review.nav-binding"
PROFILE_SCOPE_ID = "default"

COWORK_REVIEW_SHORTCUT_DEFAULTS = {
    "previous": "j",
    "next": "k",
    "accept": "a",
    "amend": "e",
    "reject": "x",
    "defer": ".",
}

COWORK_REVIEW_SHORTCUT_COMMANDS = (
    ("previous", "Previous review item", "Move up the Queue."),
    ("next", "Next review item", "Move down the Queue."),
    (
        "accept",
        "Accept, endorse, or confirm",
        "Choose the positive decision for the current item.",
    ),
    ("amend", "Amend", "Open the replacement editor for the current suggestion."),
    (
        "reject",
        "Reject or dismiss",
        "Choose the direct negative decision for the current item.",
    ),
    ("defer", "Defer", "Leave the current suggestion for later."),
)


_ADDITIONAL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    *tuple({
        "setting_id": setting_id, "definition_version": 1, "value_version": 1,
        "owner": {"kind": "system", "id": "wb.dashboard", "label": "Dashboard AI"},
        "provenance": {"complement_id": "wb.dashboard", "label": "Dashboard", "trust_tier": "native"},
        "title": title, "short_description": description,
        "long_description": (
            "Only Start authorizes sharing the disclosed form and chat with your selected model; "
            "submission stays yours."
        ) if setting_id == DASHBOARD_ASSISTANCE_ID else (
            "Retained for compatibility; this legacy tier no longer selects interactive chat models."
        ),
        "keywords": ["assistant", "draft", "model", "privacy", "form"], "tags": ["assistance", "privacy"],
        "value_schema": {"type": "string", "enum": [item[0] for item in options]},
        "default_value": default, "allowed_scopes": ["profile"], "default_scope": "profile",
        "applies_to": [{"kind": "system", "id": "wb.dashboard", "label": "Dashboard AI"}],
        "affects": [{"ref": {"kind": "system", "id": "wb.dashboard", "label": "Dashboard AI"},
                     "note": "Controls opt-in conversational assistance on supported widget drafts."}],
        "presentation": {"control": "select", "apply_behavior": "immediate", "options": [
            {"value": value, "label": label} for value, label in options
        ]}, "visibility": "frontend", "sensitivity": "ordinary",
    } for setting_id, title, description, default, options in (
        (DASHBOARD_ASSISTANCE_ID, "Form assistance", "Allow an assistant to help shape supported forms.", "disabled",
         (("disabled", "Off — no model assistance"), ("enabled", "Allow form assistance"))),
        (DASHBOARD_ASSISTANCE_TIER_ID, "Legacy assistant model tier", "Preserved legacy inference preference; not used by interactive chats.", "frontier_fast",
         (("frontier_fast", "Frontier fast"), ("frontier_balanced", "Frontier balanced"), ("frontier_best", "Frontier best"))),
    )),
    {
        "setting_id": DASHBOARD_CHAT_EXECUTION_DEFAULT_ID,
        "definition_version": 1,
        "value_version": 1,
        "owner": {"kind": "system", "id": "wb.dashboard", "label": "Dashboard AI"},
        "provenance": {"complement_id": "wb.dashboard", "label": "Dashboard", "trust_tier": "native"},
        "title": "Default chat model",
        "short_description": "New chats start with this model; each chat keeps its own choice.",
        "long_description": "Changing the default does not start a model or change existing chats.",
        "keywords": ["assistant", "chat", "model", "provider", "default", "Claude", "Codex", "local"],
        "tags": ["assistance", "execution"],
        "value_schema": {
            "type": "object",
            "properties": {
                "provider_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "model_id": {"type": "string", "minLength": 1, "maxLength": 256},
            },
            "required": ["provider_id", "model_id"],
            "additionalProperties": False,
        },
        "default_value": {"provider_id": "claude-code", "model_id": "sonnet"},
        "allowed_scopes": ["profile"],
        "default_scope": "profile",
        "applies_to": [{"kind": "system", "id": "wb.dashboard", "label": "Dashboard AI"}],
        "affects": [{
            "ref": {"kind": "system", "id": "wb.dashboard", "label": "Dashboard AI"},
            "note": "Sets the initial provider/model for new, unbound dashboard chats only.",
        }],
        "presentation": {"control": "execution-profile", "apply_behavior": "immediate"},
        "visibility": "frontend",
        "sensitivity": "ordinary",
    },
    {
        "setting_id": JOURNAL_SMART_PROCESSING_ID,
        "definition_version": 1, "value_version": 1,
        "owner": {"kind": "app", "id": "wb.journal", "label": "Journal"},
        "provenance": {"complement_id": "wb.journal", "label": "Journal", "trust_tier": "native"},
        "title": "Smart capture",
        "short_description": "Allow optional model processing after your exact capture is saved.",
        "long_description": (
            "Off by default. Enabling Smart allows up to 32 KiB of the exact saved capture "
            "to reach the configured model when you choose Smart. Quick Capture displays "
            "the concrete provider and model before submission. No tools or web access; "
            "task proposals still require review and acceptance. Direct capture and Save "
            "and propose task do not use a model. Provider selection follows journal.smart_processing.tier."
        ),
        "keywords": ["smart", "capture", "model", "privacy", "automatic routing"],
        "tags": ["journal", "privacy"],
        "value_schema": {"type": "string", "enum": ["disabled", "enabled"]},
        "default_value": "disabled", "allowed_scopes": ["profile"], "default_scope": "profile",
        "applies_to": [{"kind": "app", "id": "wb.journal", "label": "Journal"}],
        "affects": [{"ref": {"kind": "view", "id": "wb.journal.main", "label": "Journal view"},
                     "note": "Controls whether Smart processing is available; does not submit any capture."}],
        "presentation": {"control": "select", "apply_behavior": "immediate", "options": [
            {"value": "disabled", "label": "Off — no model processing"},
            {"value": "enabled", "label": "Allow Smart capture"},
        ]},
        "visibility": "frontend", "sensitivity": "ordinary",
    },
    {
        "setting_id": JOURNAL_SMART_EXECUTION_ID,
        "definition_version": 1, "value_version": 1,
        "owner": {"kind": "app", "id": "wb.journal", "label": "Journal"},
        "provenance": {"complement_id": "wb.journal", "label": "Journal", "trust_tier": "native"},
        "title": "Smart capture processing",
        "short_description": (
            "Choose whether Smart capture runs on your subscription agent or an API model."
        ),
        "long_description": (
            "This choice applies only while Smart capture is on. Your subscription agent "
            "reuses the provider and model from Default chat model and signs in with the "
            "subscription you already pay for, so a capture adds no separate charge but "
            "takes longer to settle. An API model answers in a single direct call for the "
            "shortest wait and bills your API account for each capture. It keeps its own "
            "configured model, because Default chat model names an agent runtime rather "
            "than an API endpoint. Either way the same 32 KiB limit, the same provider and "
            "model disclosure, and the same task-proposal review apply."
        ),
        "keywords": ["smart", "capture", "model", "provider", "subscription", "api", "speed", "cost"],
        "tags": ["journal", "execution"],
        "value_schema": {
            "type": "string",
            "enum": [
                JOURNAL_SMART_EXECUTION_SUBSCRIPTION_AGENT,
                JOURNAL_SMART_EXECUTION_API_MODEL,
            ],
        },
        "default_value": JOURNAL_SMART_EXECUTION_SUBSCRIPTION_AGENT,
        "allowed_scopes": ["profile"], "default_scope": "profile",
        "applies_to": [{"kind": "app", "id": "wb.journal", "label": "Journal"}],
        "affects": [{"ref": {"kind": "view", "id": "wb.journal.main", "label": "Journal view"},
                     "note": "Changes which provider and model run Smart capture and how soon a Smart capture settles."}],
        "presentation": {"control": "select", "apply_behavior": "immediate", "options": [
            {
                "value": JOURNAL_SMART_EXECUTION_SUBSCRIPTION_AGENT,
                "label": "Your subscription agent (slower, no separate charge)",
            },
            {
                "value": JOURNAL_SMART_EXECUTION_API_MODEL,
                "label": "An API model (fastest, billed for each capture)",
            },
        ]},
        "visibility": "frontend", "sensitivity": "ordinary",
    },
)


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
    {
        "setting_id": COWORK_REVIEW_NAV_BINDING_ID,
        "definition_version": 2,
        "value_version": 2,
        "owner": {"kind": "app", "id": "wb.cowork", "label": "Co-work"},
        "provenance": {
            "complement_id": "wb.cowork",
            "label": "Co-work",
            "trust_tier": "native",
        },
        "title": "Review keyboard shortcuts",
        "short_description": (
            "Choose the keys used to move through and decide Queue items."
        ),
        "long_description": (
            "These shortcuts are active only while Queue is visible. They never "
            "take over while you are typing."
        ),
        "keywords": [
            "keyboard",
            "shortcut",
            "navigation",
            "accept",
            "amend",
            "reject",
            "defer",
            "j",
            "k",
            "vim",
            "review",
        ],
        "tags": ["keyboard", "review"],
        "value_schema": {
            "type": "object",
            "properties": {
                command_id: {"type": "string"}
                for command_id in COWORK_REVIEW_SHORTCUT_DEFAULTS
            },
            "required": list(COWORK_REVIEW_SHORTCUT_DEFAULTS),
            "additionalProperties": False,
        },
        "default_value": COWORK_REVIEW_SHORTCUT_DEFAULTS,
        "allowed_scopes": ["profile"],
        "default_scope": "profile",
        "applies_to": [
            {"kind": "app", "id": "wb.cowork", "label": "Co-work"},
            {
                "kind": "view",
                "id": "wb.cowork.workspace",
                "label": "Co-work view",
            },
        ],
        "affects": [
            {
                "ref": {
                    "kind": "view",
                    "id": "wb.cowork.workspace",
                    "label": "Co-work view",
                },
                "note": "Changes the shortcuts used to navigate and decide Queue items.",
            }
        ],
        "presentation": {
            "control": "keybinding-map",
            "commands": [
                {
                    "command_id": command_id,
                    "label": label,
                    "description": description,
                }
                for command_id, label, description in COWORK_REVIEW_SHORTCUT_COMMANDS
            ],
            "apply_behavior": "immediate",
        },
        "visibility": "frontend",
        "sensitivity": "ordinary",
    },
)


_DEFINITIONS += _ADDITIONAL_DEFINITIONS


_PAGES: tuple[dict[str, Any], ...] = (
    {
        "page_id": DASHBOARD_AI_CONTEXT_ID, "context_id": DASHBOARD_AI_CONTEXT_ID,
        "context": {"kind": "system", "id": "wb.dashboard", "label": "Dashboard AI"},
        "owner": {"kind": "system", "id": "wb.dashboard", "label": "Dashboard AI"},
        "route": "/app/settings/system/dashboard-ai", "label": "Dashboard AI",
        "description": "Shared chat defaults and form assistance across the dashboard.",
        "navigation_group": "system", "order": 30,
        "sections": [{"section_id": "assistance", "label": "Chat and form assistance", "order": 10}],
    },
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
            },
            {"section_id": "capture", "label": "Capture and privacy", "order": 20},
        ],
    },
    {
        "page_id": "wb.settings.app.cowork",
        "context_id": "wb.settings.app.cowork",
        "context": {"kind": "app", "id": "wb.cowork", "label": "Co-work"},
        "owner": {"kind": "app", "id": "wb.cowork", "label": "Co-work"},
        "route": "/app/settings/apps/cowork",
        "label": "Co-work",
        "description": "Configure the Co-work document review and writing surface.",
        "navigation_group": "apps",
        "navigation_category": "built-in",
        "order": 20,
        "fallback_return_path": "/app/cowork",
        "sections": [
            {
                "section_id": "review-keyboard",
                "label": "Review keyboard",
                "order": 10,
            }
        ],
    },
)


_PLACEMENTS: tuple[dict[str, Any], ...] = (
    *tuple({
        "placement_id": f"wb.settings.placement.system.dashboard-ai.{suffix}",
        "setting_id": setting_id, "page_id": DASHBOARD_AI_CONTEXT_ID,
        "context_id": DASHBOARD_AI_CONTEXT_ID, "section_id": "assistance", "order": order,
    } for suffix, setting_id, order in (
        ("assistance", DASHBOARD_ASSISTANCE_ID, 10),
        ("chat-execution-default", DASHBOARD_CHAT_EXECUTION_DEFAULT_ID, 20),
    )),
    {
        "placement_id": "wb.settings.placement.app.journal.smart-processing",
        "setting_id": JOURNAL_SMART_PROCESSING_ID,
        "page_id": "wb.settings.app.journal", "context_id": "wb.settings.app.journal",
        "section_id": "capture", "order": 20,
    },
    {
        "placement_id": "wb.settings.placement.app.journal.smart-execution",
        "setting_id": JOURNAL_SMART_EXECUTION_ID,
        "page_id": "wb.settings.app.journal", "context_id": "wb.settings.app.journal",
        "section_id": "capture", "order": 30,
    },
    {
        "placement_id": "wb.settings.placement.app.journal.day-boundary",
        "setting_id": JOURNAL_DAY_BOUNDARY_ID,
        "page_id": "wb.settings.app.journal",
        "context_id": "wb.settings.app.journal",
        "section_id": "day-behavior",
        "order": 10,
    },
    {
        "placement_id": "wb.settings.placement.app.cowork.nav-binding",
        "setting_id": COWORK_REVIEW_NAV_BINDING_ID,
        "page_id": "wb.settings.app.cowork",
        "context_id": "wb.settings.app.cowork",
        "section_id": "review-keyboard",
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
        if definition["presentation"]["apply_behavior"] not in {
            "immediate",
            "next-boundary",
        }:
            raise RuntimeError(
                f"unsupported apply behavior for {definition['setting_id']}"
            )
        if definition["setting_id"] == JOURNAL_DAY_BOUNDARY_ID:
            parse_local_time(definition["default_value"])
        value_schema = definition["value_schema"]
        enum_values = value_schema.get("enum")
        if enum_values is not None and definition["default_value"] not in enum_values:
            raise RuntimeError(
                f"default value is outside enum for {definition['setting_id']}"
            )
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

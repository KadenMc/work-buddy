"""Regenerate capability declaration files from the live registry.

Part of the capability bulk-migration: turns a generated capability unit into
a *declaration* — a ``kind: capability`` knowledge unit that names an ``op``.
For one registry category, this rebuilds each capability's
``knowledge/store/<path>.md`` as a full declaration: ``op``, ``schema_version``,
and every runtime-metadata field mirrored from the live ``Capability``.

The op itself (the callable registered under ``op.wb.<name>``) is wired
separately in ``work_buddy/mcp_server/ops/<category>_ops.py`` — this script only
emits the data half. It also reports each capability's callable (module +
qualname) so the op module can be written, flagging closures that must be
hoisted to module scope before they can be registered as an op.

Run from the work-buddy conda env:

    conda run -n work-buddy python -m scripts.migrate_capabilities_to_declarations <category>
"""

from __future__ import annotations

import sys

from work_buddy.knowledge import file_store
from work_buddy.knowledge.store import _STORE_DIR
from work_buddy.mcp_server import registry as R

SCHEMA_VERSION = "wb-capability/v1"

# Registry category → its builder-function name. Resolved lazily via getattr
# so a category whose builder has already been removed (migrated) does not
# break the script at import time.
_CATEGORY_BUILDERS = {
    "messaging": "_messaging_capabilities",
    "contracts": "_contract_capabilities",
    "status": "_status_capabilities",
    "journal": "_journal_capabilities",
    "memory": "_memory_capabilities",
    "pipelines": "_pipeline_capabilities",
    "threads": "_thread_capabilities",
    "tasks": "_task_capabilities",
    "context": "_context_capabilities",
    "projects": "_project_capabilities",
    "sidecar": "_sidecar_capabilities",
    "llm": "_llm_capabilities",
    "consent": "_consent_capabilities",
    "notifications": "_notification_capabilities",
    "conversations": "_conversation_capabilities",
    "inline": "_inline_capabilities",
    "remote_session": "_remote_session_capabilities",
    "ledger": "_ledger_capabilities",
    "knowledge": "_knowledge_capabilities",
    "artifacts": "_artifact_capabilities",
    "email": "_email_capabilities",
    "backups": "_backup_capabilities",
    "conversation_observability": "_conversation_observability_capabilities",
}

# Registry category → store path prefix (categories absent here use the
# category name itself). Mirrors the prefix map the generated units were
# originally emitted under, so a declaration overwrites its own unit file.
_CATEGORY_PATH_MAP = {
    "consent": "notifications/consent",
    "sidecar": "status",
    "llm": "status",
}


def _humanize(name: str) -> str:
    """task_create → Task Create."""
    return name.replace("_", " ").replace("-", " ").title()


def _name_to_tags(name: str, category: str) -> list[str]:
    """Extract search tags from a capability name."""
    parts = name.replace("-", "_").split("_")
    tags = [category] + [p for p in parts if p != category]
    return list(dict.fromkeys(tags))


def _declaration_dict(cap) -> dict:
    """Build the declaration unit dict for a live ``Capability``."""
    category = cap.category
    unit: dict = {
        "kind": "capability",
        "name": _humanize(cap.name),
        "description": cap.description,
        "capability_name": cap.name,
        "category": category,
        "op": f"op.wb.{cap.name}",
        "schema_version": SCHEMA_VERSION,
    }
    if cap.parameters:
        unit["parameters"] = cap.parameters
    if cap.requires:
        unit["requires"] = list(cap.requires)
    if cap.invokes:
        unit["invokes"] = list(cap.invokes)
    if cap.mutates_state:
        unit["mutates_state"] = True
        unit["retry_policy"] = cap.retry_policy
    if not cap.auto_retry:
        unit["auto_retry"] = False
    if cap.consent_operations:
        unit["consent_operations"] = list(cap.consent_operations)
    if getattr(cap.callable, "_requires_consent", False):
        unit["consent_required"] = True
    if cap.param_aliases:
        unit["param_aliases"] = dict(cap.param_aliases)
    if cap.slash_command:
        unit["slash_command"] = cap.slash_command
    if cap.is_action:
        unit["is_action"] = True
    if cap.intrinsic_amplifiers:
        unit["intrinsic_amplifiers"] = dict(cap.intrinsic_amplifiers)
    if cap.search_aliases:
        unit["aliases"] = list(cap.search_aliases)
    unit["tags"] = _name_to_tags(cap.name, category)
    prefix = _CATEGORY_PATH_MAP.get(category, category)
    unit["parents"] = [prefix]
    return unit


def migrate_category(category: str) -> dict:
    """Regenerate every declaration file for one registry category."""
    builder_name = _CATEGORY_BUILDERS.get(category)
    if builder_name is None:
        raise SystemExit(
            f"Unknown category {category!r}. "
            f"Known: {', '.join(sorted(_CATEGORY_BUILDERS))}"
        )
    builder = getattr(R, builder_name, None)
    if builder is None:
        raise SystemExit(
            f"Category {category!r} has no builder {builder_name!r} — "
            "already migrated?"
        )

    caps = builder()
    written: list[str] = []
    closures: list[str] = []
    effects_caps: list[str] = []

    print(f"=== {category}: {len(caps)} capabilities ===")
    for cap in caps:
        prefix = _CATEGORY_PATH_MAP.get(cap.category, cap.category)
        path = f"{prefix}/{cap.name}"
        file_store.write_unit(_STORE_DIR, path, _declaration_dict(cap))
        written.append(path)

        fn = cap.callable
        mod = getattr(fn, "__module__", "?")
        qn = getattr(fn, "__qualname__", "?")
        is_closure = "." in qn or qn == "<lambda>"
        marker = "  CLOSURE — hoist" if is_closure else ""
        if is_closure:
            closures.append(cap.name)
        if cap.effects:
            effects_caps.append(cap.name)
            marker += "  [effects]"
        print(f"  op.wb.{cap.name:<32} <- {mod}:{qn}{marker}")

    print(f"\nWrote {len(written)} declaration files.")
    if closures:
        print(f"Closures needing a module-level home: {closures}")
    if effects_caps:
        print(f"Capabilities with effects (register op-side): {effects_caps}")
    return {"category": category, "written": written, "closures": closures}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m scripts.migrate_capabilities_to_declarations <category>")
    migrate_category(sys.argv[1])

"""Validation command for the unified knowledge store.

Runs structural integrity checks on the store: DAG validity,
command-to-store mappings, required fields, kind-specific fields,
and thinned command format. Returns a structured report.

Usage:
    python -m work_buddy.knowledge.validate
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from work_buddy.knowledge.model import (
    CapabilityUnit,
    DirectionsUnit,
    PromptUnit,
    SystemUnit,
    WorkflowUnit,
    validate_dag,
)
from work_buddy.knowledge.store import load_store
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SLASH_CMD_DIR = _REPO_ROOT / ".claude" / "commands"
_MAX_THIN_LINES = 7


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_dag(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 1: DAG integrity — parent/child referential integrity + cycles."""
    raw_errors = validate_dag(store)
    return [{"check": "dag_integrity", "path": "", "message": e} for e in raw_errors]


def _check_command_mapping(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 2: Every wb-*.md slash command should have a DirectionsUnit with matching command field."""
    errors: list[dict[str, str]] = []

    if not _SLASH_CMD_DIR.is_dir():
        return errors

    # Build reverse map: command name → store path
    command_to_path: dict[str, str] = {}
    for path, unit in store.items():
        cmd = None
        if isinstance(unit, DirectionsUnit) and unit.command:
            cmd = unit.command
        elif isinstance(unit, WorkflowUnit) and unit.command:
            cmd = unit.command
        if cmd:
            command_to_path[cmd] = path

    for cmd_file in sorted(_SLASH_CMD_DIR.glob("wb-*.md")):
        cmd_name = cmd_file.stem  # e.g. "wb-task-triage"
        if cmd_name not in command_to_path:
            errors.append({
                "check": "command_mapping",
                "path": cmd_name,
                "message": f"Slash command '{cmd_name}' has no matching unit with command={cmd_name!r}",
            })

    return errors


def _check_thinned_commands(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 3: Every wb-*.md command file should be thinned (< N lines)."""
    errors: list[dict[str, str]] = []

    if not _SLASH_CMD_DIR.is_dir():
        return errors

    for cmd_file in sorted(_SLASH_CMD_DIR.glob("wb-*.md")):
        lines = cmd_file.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_THIN_LINES:
            errors.append({
                "check": "thinned_commands",
                "path": cmd_file.stem,
                "message": f"Command file has {len(lines)} lines (max {_MAX_THIN_LINES}). Needs thinning.",
            })

    return errors


def _check_store_path_in_commands() -> list[dict[str, str]]:
    """Check 4: Store paths referenced in thinned commands should exist in the store."""
    errors: list[dict[str, str]] = []
    store = load_store()

    if not _SLASH_CMD_DIR.is_dir():
        return errors

    # Pattern: "path": "some/path" in agent_docs calls
    path_pattern = re.compile(r'"path"\s*:\s*"([^"]+)"')

    for cmd_file in sorted(_SLASH_CMD_DIR.glob("wb-*.md")):
        text = cmd_file.read_text(encoding="utf-8")
        for match in path_pattern.finditer(text):
            ref_path = match.group(1)
            if ref_path not in store:
                errors.append({
                    "check": "store_path_validity",
                    "path": cmd_file.stem,
                    "message": f"References store path '{ref_path}' which does not exist",
                })

    return errors


def _check_required_fields(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 5: Required fields on all units."""
    errors: list[dict[str, str]] = []

    for path, unit in sorted(store.items()):
        if not unit.name:
            errors.append({
                "check": "required_fields",
                "path": path,
                "message": "Missing 'name'",
            })
        if not unit.description:
            errors.append({
                "check": "required_fields",
                "path": path,
                "message": "Missing 'description'",
            })

    return errors


def _check_directions_fields(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 5b: DirectionsUnit-specific required fields."""
    errors: list[dict[str, str]] = []

    for path, unit in sorted(store.items()):
        if not isinstance(unit, DirectionsUnit):
            continue
        if not unit.trigger:
            errors.append({
                "check": "directions_fields",
                "path": path,
                "message": "DirectionsUnit missing 'trigger'",
            })
        if not unit.content.get("full"):
            errors.append({
                "check": "directions_fields",
                "path": path,
                "message": "DirectionsUnit missing content.full",
            })

    return errors


def _check_kind_specific_fields(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 6: Kind-specific required fields."""
    errors: list[dict[str, str]] = []

    for path, unit in sorted(store.items()):
        if isinstance(unit, CapabilityUnit):
            if not unit.capability_name:
                errors.append({
                    "check": "kind_fields",
                    "path": path,
                    "message": "CapabilityUnit missing 'capability_name'",
                })
        elif isinstance(unit, WorkflowUnit):
            if not unit.workflow_name:
                errors.append({
                    "check": "kind_fields",
                    "path": path,
                    "message": "WorkflowUnit missing 'workflow_name'",
                })

    return errors


def _check_placeholder_duplicates(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 8: No placeholder target may appear more than once in a
    single unit's ``content["full"]``.

    Duplicate placeholders are a hard error, not a hint: the runtime
    per-unit-occurrence cap renders subsequent references as
    back-reference markers, so duplicates produce zero new content.
    They're never the right authorial choice. The editor rejects them
    before disk; this corpus-wide check catches any that slipped in
    via direct JSON edits, programmatic generators, or pre-rule legacy
    content.
    """
    # Imported here to avoid circular import — editor imports from this
    # module's siblings, and check_duplicate_placeholders is the
    # canonical implementation.
    from work_buddy.knowledge.editor import check_duplicate_placeholders

    errors: list[dict[str, str]] = []
    for path, unit in sorted(store.items()):
        full = unit.content.get("full", "")
        if not isinstance(full, str) or "<<wb:" not in full:
            continue
        duplicates = check_duplicate_placeholders(full)
        for dup in duplicates:
            errors.append({
                "check": "placeholder_duplicate",
                "path": path,
                "message": (
                    f"Placeholder for {dup['placeholder']!r} appears "
                    f"{dup['count']} times — duplicates render as "
                    "back-reference markers at read time; remove the extras."
                ),
            })
    return errors


def _check_capability_op_resolution(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 9: declaration-based capabilities resolve to a registered op.

    Only capability units carrying an ``op`` field are declaration-based;
    generated capability units have no ``op`` and are ignored. Emits
    *warnings* (not errors): the direct and declaration-based capability
    registration paths coexist, so an unresolved declaration is surfaced
    without failing the whole store. The loader is the single source of this
    logic; this check surfaces its findings corpus-wide.
    """
    has_declarations = any(
        isinstance(u, CapabilityUnit) and getattr(u, "op", "")
        for u in store.values()
    )
    if not has_declarations:
        return []

    from work_buddy.knowledge.capability_loader import load_declared_capabilities

    _caps, issues = load_declared_capabilities(store)
    return issues


def _check_parent_child_symmetry(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 7: If A lists B as child, B should list A as parent (and vice versa)."""
    errors: list[dict[str, str]] = []

    for path, unit in sorted(store.items()):
        for child in unit.children:
            child_unit = store.get(child)
            if child_unit and path not in child_unit.parents:
                errors.append({
                    "check": "parent_child_symmetry",
                    "path": path,
                    "message": f"Lists '{child}' as child, but child doesn't list '{path}' as parent",
                })
        for parent in unit.parents:
            parent_unit = store.get(parent)
            if parent_unit and path not in parent_unit.children:
                errors.append({
                    "check": "parent_child_symmetry",
                    "path": path,
                    "message": f"Lists '{parent}' as parent, but parent doesn't list '{path}' as child",
                })

    return errors


# ---------------------------------------------------------------------------
# Main validation runner
# ---------------------------------------------------------------------------

_CHECKS = [
    ("dag_integrity", _check_dag),
    ("command_mapping", _check_command_mapping),
    ("thinned_commands", _check_thinned_commands),
    ("store_path_validity", _check_store_path_in_commands),
    ("required_fields", _check_required_fields),
    ("directions_fields", _check_directions_fields),
    ("kind_specific_fields", _check_kind_specific_fields),
    ("placeholder_duplicate", _check_placeholder_duplicates),
    ("parent_child_symmetry", _check_parent_child_symmetry),
    ("capability_op_resolution", _check_capability_op_resolution),
]


def validate_store(
    *,
    checks: list[str] | None = None,
) -> dict[str, Any]:
    """Run all (or selected) validation checks on the knowledge store.

    Args:
        checks: List of check names to run. None = run all.

    Returns:
        Dict with ``passed``, ``failed``, ``warnings``, ``total_units``, an
        ``errors`` list (blocking, ``severity != "warning"``), and an
        ``issues`` list (every finding, errors and warnings together).

    Severity: a check may tag a finding with ``severity: "warning"`` to mark
    it non-blocking. Findings without a ``severity`` key default to ``error``,
    so existing checks are unaffected. ``passed`` and ``failed`` count blocking
    errors only.
    """
    store = load_store()
    all_errors: list[dict[str, str]] = []

    checks_run: list[str] = []
    for name, fn in _CHECKS:
        if checks and name not in checks:
            continue
        checks_run.append(name)

        # Some checks need the store, some don't
        try:
            import inspect
            sig = inspect.signature(fn)
            if sig.parameters:
                errs = fn(store)
            else:
                errs = fn()
        except Exception as e:
            all_errors.append({
                "check": name,
                "path": "",
                "message": f"Check raised exception: {e}",
            })
            continue

        all_errors.extend(errs)

    # Split blocking errors from non-blocking warnings. A finding with no
    # ``severity`` key defaults to "error" — existing checks are unchanged.
    errors = [e for e in all_errors if e.get("severity", "error") != "warning"]
    warnings = [e for e in all_errors if e.get("severity", "error") == "warning"]

    # Summarize by check (errors and warnings together)
    summary: dict[str, int] = {}
    for err in all_errors:
        summary[err["check"]] = summary.get(err["check"], 0) + 1

    return {
        "total_units": len(store),
        "checks_run": checks_run,
        "passed": len(errors) == 0,
        "failed": len(errors),
        "warnings": len(warnings),
        "summary": summary,
        "errors": errors,
        "issues": all_errors,
    }


# ---------------------------------------------------------------------------
# MCP-facing callable
# ---------------------------------------------------------------------------

def docs_validate(
    *,
    checks: str | None = None,
) -> dict[str, Any]:
    """Validate the knowledge store: DAG integrity, command mappings, required fields.

    Args:
        checks: Comma-separated check names to run. Empty = run all.
                 Available: dag_integrity, command_mapping, thinned_commands,
                 store_path_validity, required_fields, directions_fields,
                 kind_specific_fields, placeholder_duplicate,
                 parent_child_symmetry, capability_op_resolution
    """
    check_list = [c.strip() for c in checks.split(",") if c.strip()] if checks else None
    return validate_store(checks=check_list)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    result = validate_store()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["passed"] else 1)

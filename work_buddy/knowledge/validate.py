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
    ("parent_child_symmetry", _check_parent_child_symmetry),
]


def validate_store(
    *,
    checks: list[str] | None = None,
) -> dict[str, Any]:
    """Run all (or selected) validation checks on the knowledge store.

    Args:
        checks: List of check names to run. None = run all.

    Returns:
        Dict with ``passed``, ``failed``, ``total_units``, and ``errors`` list.
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

    # Summarize by check
    summary: dict[str, int] = {}
    for err in all_errors:
        summary[err["check"]] = summary.get(err["check"], 0) + 1

    return {
        "total_units": len(store),
        "checks_run": checks_run,
        "passed": len(all_errors) == 0,
        "failed": len(all_errors),
        "summary": summary,
        "errors": all_errors,
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
                 kind_specific_fields, parent_child_symmetry
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

"""Documentation site generator for the knowledge store.

Loads the unified JSON store and renders type-specific Markdown pages
for MkDocs. Each unit kind gets a tailored template:

- DirectionsUnit → behavioral guide with trigger, linked workflow/capabilities
- SystemUnit → reference page with ports, entry points
- CapabilityUnit → capability card with parameter table
- WorkflowUnit → workflow page with step table, execution policy

The nav structure mirrors the DAG hierarchy.

Usage:
    python -m work_buddy.knowledge.docs_gen [--write]

Without --write, prints the nav YAML to stdout.
With --write, generates docs/handbook/ and updates mkdocs.yml nav.
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
)
from work_buddy.knowledge.store import load_store
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DOCS_DIR = _REPO_ROOT / "docs" / "handbook"


# ---------------------------------------------------------------------------
# Page renderers
# ---------------------------------------------------------------------------

def _render_directions(unit: DirectionsUnit) -> str:
    """Render a DirectionsUnit as a Markdown page."""
    lines = [f"# {unit.name}", ""]

    if unit.description:
        lines += [f"> {unit.description}", ""]

    if unit.trigger:
        lines += ["## When to use", "", unit.trigger, ""]

    if unit.command:
        lines += [f"**Slash command:** `/{unit.command}`", ""]

    if unit.workflow:
        lines += [f"**Linked workflow:** `{unit.workflow}`", ""]

    if unit.capabilities:
        lines += ["## Related capabilities", ""]
        for cap in unit.capabilities:
            lines.append(f"- `{cap}`")
        lines.append("")

    content = unit.content.get("full") or unit.content.get("summary", "")
    if content:
        lines += ["## Directions", "", content, ""]

    if unit.requires:
        lines += ["## Requirements", ""]
        for req in unit.requires:
            lines.append(f"- {req}")
        lines.append("")

    return "\n".join(lines)


def _render_system(unit: SystemUnit) -> str:
    """Render a SystemUnit as a Markdown page."""
    lines = [f"# {unit.name}", ""]

    if unit.description:
        lines += [f"> {unit.description}", ""]

    if unit.ports:
        lines += ["## Ports", ""]
        for port in unit.ports:
            lines.append(f"- `{port}`")
        lines.append("")

    if unit.entry_points:
        lines += ["## Entry points", ""]
        for ep in unit.entry_points:
            lines.append(f"- `{ep}`")
        lines.append("")

    content = unit.content.get("full") or unit.content.get("summary", "")
    if content:
        lines += ["## Details", "", content, ""]

    if unit.requires:
        lines += ["## Requirements", ""]
        for req in unit.requires:
            lines.append(f"- {req}")
        lines.append("")

    if unit.children:
        lines += ["## Children", ""]
        for child in sorted(unit.children):
            child_name = child.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").title()
            lines.append(f"- [{child_name}]({child.replace('/', '_')}.md)")
        lines.append("")

    return "\n".join(lines)


def _render_capability(unit: CapabilityUnit) -> str:
    """Render a CapabilityUnit as a Markdown page."""
    lines = [f"# {unit.name}", ""]

    if unit.description:
        lines += [f"> {unit.description}", ""]

    lines += [f"**MCP name:** `{unit.capability_name}`", ""]
    if unit.category:
        lines += [f"**Category:** {unit.category}", ""]

    if unit.consent_required:
        lines += [":warning: **Requires consent**", ""]

    if unit.mutates_state:
        lines += [f"**Mutates state** (retry policy: `{unit.retry_policy}`)", ""]

    if unit.parameters:
        lines += ["## Parameters", "", "| Name | Type | Required | Description |", "|------|------|----------|-------------|"]
        for pname, pinfo in sorted(unit.parameters.items()):
            ptype = pinfo.get("type", "str")
            preq = "Yes" if pinfo.get("required") else "No"
            pdesc = pinfo.get("description", "")
            lines.append(f"| `{pname}` | `{ptype}` | {preq} | {pdesc} |")
        lines.append("")

    content = unit.content.get("full") or unit.content.get("summary", "")
    if content:
        lines += ["## Details", "", content, ""]

    if unit.requires:
        lines += ["## Requirements", ""]
        for req in unit.requires:
            lines.append(f"- {req}")
        lines.append("")

    return "\n".join(lines)


def _render_workflow(unit: WorkflowUnit) -> str:
    """Render a WorkflowUnit as a Markdown page."""
    lines = [f"# {unit.name}", ""]

    if unit.description:
        lines += [f"> {unit.description}", ""]

    lines += [f"**Workflow name:** `{unit.workflow_name}`", ""]
    lines += [f"**Execution:** `{unit.execution}`", ""]

    if unit.command:
        lines += [f"**Slash command:** `/{unit.command}`", ""]

    if not unit.allow_override:
        lines += ["**Override not allowed**", ""]

    if unit.steps:
        lines += ["## Steps", "", "| # | ID | Name | Type | Depends on |", "|---|-----|------|------|------------|"]
        for i, step in enumerate(unit.steps, 1):
            sid = step.get("id", "")
            sname = step.get("name", "")
            stype = step.get("step_type", "")
            deps = ", ".join(step.get("depends_on", []))
            lines.append(f"| {i} | `{sid}` | {sname} | `{stype}` | {deps} |")
        lines.append("")

    if unit.step_instructions:
        lines += ["## Step instructions", ""]
        for step_id, instruction in unit.step_instructions.items():
            lines += [f"### `{step_id}`", "", instruction, ""]

    content = unit.content.get("full") or unit.content.get("summary", "")
    if content:
        lines += ["## Context", "", content, ""]

    return "\n".join(lines)


_RENDERERS: dict[str, Any] = {
    "directions": _render_directions,
    "system": _render_system,
    "capability": _render_capability,
    "workflow": _render_workflow,
}


def _format_code_references(text: str) -> str:
    """Post-process rendered markdown to wrap code patterns in backticks.

    Catches MCP calls, capability names, and function-like references
    that appear as plain text in knowledge store content.
    """
    # Wrap mcp__work-buddy__wb_* calls in backticks (if not already wrapped)
    text = re.sub(
        r'(?<!`)mcp__work-buddy__\w+\([^)]*\)(?!`)',
        lambda m: f'`{m.group(0)}`',
        text,
    )
    # Wrap wb_run/wb_search/wb_advance/wb_status calls
    text = re.sub(
        r'(?<!`)wb_(run|search|advance|status|step_result)\([^)]*\)(?!`)',
        lambda m: f'`{m.group(0)}`',
        text,
    )
    return text


def _render_unit(unit: PromptUnit) -> str:
    """Render a unit to Markdown using its kind-specific renderer."""
    renderer = _RENDERERS.get(unit.kind, _render_system)
    return _format_code_references(renderer(unit))


# ---------------------------------------------------------------------------
# File path helpers
# ---------------------------------------------------------------------------

def _unit_to_filename(path: str) -> str:
    """Convert a unit path to a filename: 'tasks/triage' → 'tasks_triage.md'."""
    return path.replace("/", "_") + ".md"


# ---------------------------------------------------------------------------
# Navigation builder
# ---------------------------------------------------------------------------

def _build_nav(store: dict[str, PromptUnit]) -> list[dict[str, Any]]:
    """Build MkDocs nav entries grouped by top-level domain.

    Returns a list of nav items suitable for mkdocs.yml.
    """
    # Group by top-level prefix
    groups: dict[str, list[tuple[str, str]]] = {}
    for path, unit in sorted(store.items()):
        top = path.split("/")[0]
        filename = _unit_to_filename(path)
        groups.setdefault(top, []).append((unit.name, f"handbook/{filename}"))

    nav: list[dict[str, Any]] = []
    for group_name, entries in sorted(groups.items()):
        section_name = group_name.replace("-", " ").replace("_", " ").title()
        section_items = [{name: filepath} for name, filepath in entries]
        nav.append({section_name: section_items})

    return nav


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------

def _render_index(store: dict[str, PromptUnit]) -> str:
    """Render the handbook index page."""
    lines = [
        "# Work Buddy Knowledge Handbook",
        "",
        "This documentation is auto-generated from the knowledge store.",
        "",
        f"**{len(store)} units** across 4 types:",
        "",
    ]

    kinds: dict[str, int] = {}
    for unit in store.values():
        kinds[unit.kind] = kinds.get(unit.kind, 0) + 1

    for kind, count in sorted(kinds.items()):
        lines.append(f"- **{kind}**: {count}")
    lines.append("")

    # Group by top-level domain
    groups: dict[str, list[PromptUnit]] = {}
    for path, unit in sorted(store.items()):
        top = path.split("/")[0]
        groups.setdefault(top, []).append(unit)

    for group_name, units in sorted(groups.items()):
        section_name = group_name.replace("-", " ").replace("_", " ").title()
        lines += [f"## {section_name}", ""]
        for unit in units:
            filename = _unit_to_filename(unit.path)
            badge = f"`{unit.kind}`"
            lines.append(f"- [{unit.name}]({filename}) {badge} — {unit.description}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def _write_nav_to_mkdocs(nav: list[dict[str, Any]]) -> None:
    """Write the generated nav into mkdocs.yml between sentinel markers.

    Replaces everything between ``# AUTOGEN_NAV_START`` and
    ``# AUTOGEN_NAV_END`` with the generated nav structure, flattening
    handbook sections into the top-level navigation.
    """
    import yaml

    mkdocs_path = _REPO_ROOT / "mkdocs.yml"
    text = mkdocs_path.read_text(encoding="utf-8")

    start_marker = "# AUTOGEN_NAV_START"
    end_marker = "# AUTOGEN_NAV_END"

    start_idx = text.find(start_marker)
    end_idx = text.find(end_marker)
    if start_idx == -1 or end_idx == -1:
        logger.warning("Nav markers not found in mkdocs.yml — skipping nav update")
        return

    # Build top-level nav: Home + Handbook index + flattened sections
    full_nav = [{"Home": "index.md"}, {"Handbook": "handbook/index.md"}] + nav
    nav_yaml = yaml.dump({"nav": full_nav}, default_flow_style=False, sort_keys=False)

    new_text = (
        text[:start_idx]
        + start_marker + "\n"
        + nav_yaml
        + end_marker
    )

    mkdocs_path.write_text(new_text, encoding="utf-8")
    logger.info("Updated mkdocs.yml nav with %d sections", len(nav))


def generate_docs(write: bool = False) -> dict[str, Any]:
    """Generate Markdown documentation from the knowledge store.

    Args:
        write: If True, write files to docs/handbook/. Otherwise, dry run.

    Returns:
        Summary dict with counts and file paths.
    """
    store = load_store()

    pages: dict[str, str] = {}
    for path, unit in sorted(store.items()):
        filename = _unit_to_filename(path)
        pages[filename] = _render_unit(unit)

    # Index page
    pages["index.md"] = _render_index(store)

    # Nav structure
    nav = _build_nav(store)

    result = {
        "pages": len(pages),
        "units": len(store),
    }

    if write:
        _DOCS_DIR.mkdir(parents=True, exist_ok=True)

        # Clean old generated files
        for old_file in _DOCS_DIR.glob("*.md"):
            old_file.unlink()

        # Write pages
        for filename, content in pages.items():
            filepath = _DOCS_DIR / filename
            filepath.write_text(content, encoding="utf-8")

        # Update mkdocs.yml nav
        _write_nav_to_mkdocs(nav)

        result["output_dir"] = str(_DOCS_DIR)
        result["nav_sections"] = len(nav)
        logger.info("Generated %d pages in %s", len(pages), _DOCS_DIR)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    write = "--write" in sys.argv
    result = generate_docs(write=write)
    print(json.dumps(result, indent=2))

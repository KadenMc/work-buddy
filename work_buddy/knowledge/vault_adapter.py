"""Vault adapter — load personal knowledge from Obsidian vault markdown files.

Reads markdown files with YAML frontmatter from a configured vault directory
and converts them into :class:`VaultUnit` instances that participate in the
unified knowledge search alongside system :class:`PromptUnit` objects.

Expected frontmatter schema (all optional except ``name``)::

    ---
    name: Branch Explosion
    category: work_pattern
    severity: HIGH
    tags: [wb/metacognition, wb/metacognition/work-pattern]
    aliases: [branching too much, too many directions]
    context_before: [metacognition/blindspot-directions]
    context_after: []
    parents: [personal/metacognition]
    children: []
    last_observed: "2026-04-03"
    observation_count: 12
    ---

The markdown body becomes ``content["full"]``.  The first paragraph (or
``## Definition`` section if present) becomes ``content["summary"]``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from work_buddy.frontmatter import parse_frontmatter
from work_buddy.knowledge.model import VaultUnit
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_DEFINITION_RE = re.compile(
    r"^##\s+Definition\s*\n(.*?)(?=\n##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


def load_vault_units(vault_dir: Path) -> dict[str, VaultUnit]:
    """Load all markdown files from *vault_dir* as VaultUnit instances.

    Args:
        vault_dir: Absolute path to the vault subdirectory containing
                   personal knowledge files (e.g., ``<vault>/Meta/WorkBuddy``).

    Returns:
        Dict mapping unit path (``personal/<relative_stem>``) to VaultUnit.
        Returns empty dict if *vault_dir* doesn't exist or isn't a directory.
    """
    if not vault_dir.is_dir():
        return {}

    units: dict[str, VaultUnit] = {}

    for md_path in sorted(vault_dir.rglob("*.md")):
        if not md_path.is_file():
            continue

        fm, body = parse_frontmatter(md_path)

        # Require at least a name to consider this a personal knowledge file
        if not fm.get("name"):
            # Skip silently — not every .md in the dir needs to be a unit
            continue

        # Build the unit path: personal/ + relative stem
        try:
            rel = md_path.relative_to(vault_dir)
        except ValueError:
            logger.warning("Skipping %s: not relative to %s", md_path, vault_dir)
            continue

        stem = rel.with_suffix("").as_posix()  # forward slashes, no .md
        unit_path = f"personal/{stem}"

        # Extract content
        summary = _extract_summary(body)
        description = fm.get("description", _first_sentence(summary))

        # Build the VaultUnit
        try:
            unit = VaultUnit(
                path=unit_path,
                name=fm["name"],
                description=description,
                aliases=_as_list(fm.get("aliases", [])),
                tags=_as_list(fm.get("tags", [])),
                content={"summary": summary, "full": body},
                requires=_as_list(fm.get("requires", [])),
                parents=_as_list(fm.get("parents", [])),
                children=_as_list(fm.get("children", [])),
                context_before=_as_list(fm.get("context_before", [])),
                context_after=_as_list(fm.get("context_after", [])),
                category=fm.get("category", ""),
                severity=fm.get("severity", ""),
                last_observed=str(fm.get("last_observed", "")),
                observation_count=int(fm.get("observation_count", 0)),
                source_file=rel.as_posix(),
            )
            units[unit_path] = unit
        except Exception as e:
            logger.warning("Failed to create VaultUnit from %s: %s", md_path.name, e)
            continue

    if units:
        logger.info("Loaded %d personal units from %s", len(units), vault_dir)

    return units


# ---------------------------------------------------------------------------
# Content extraction helpers
# ---------------------------------------------------------------------------

def _extract_summary(body: str) -> str:
    """Extract a summary from the markdown body.

    Prefers the ``## Definition`` section content if present.
    Falls back to the first paragraph (text before the first blank line).
    """
    # Try ## Definition section first
    m = _DEFINITION_RE.search(body)
    if m:
        return m.group(1).strip()

    # Fall back to first paragraph
    paragraphs = body.split("\n\n")
    for p in paragraphs:
        text = p.strip()
        # Skip headings and empty content
        if text and not text.startswith("#"):
            return text

    return ""


def _first_sentence(text: str) -> str:
    """Extract the first sentence from text for use as description."""
    if not text:
        return ""
    # Split on sentence-ending punctuation followed by space or end
    m = re.match(r"(.+?[.!?])(?:\s|$)", text, re.DOTALL)
    return m.group(1) if m else text[:120]


def _as_list(val: Any) -> list:
    """Ensure a value is a list (handles single strings, None, etc.)."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        # Support comma-separated strings
        return [s.strip() for s in val.split(",") if s.strip()]
    return [val]

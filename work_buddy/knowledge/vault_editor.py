"""Vault editor — create and update personal knowledge units in the Obsidian vault.

The ``mint_personal_unit`` function creates a new markdown file with YAML
frontmatter in the configured vault directory, or appends evidence to an
existing file. Uses the bridge-first write pattern from vault_writer.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from work_buddy.config import load_config, USER_TZ
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Category → subdirectory mapping
_CATEGORY_DIRS: dict[str, str] = {
    "work_pattern": "work_patterns",
    "self_regulation": "self_regulation",
    "skill_gap": "skill_gaps",
    "feedback": "feedback",
    "preference": "preferences",
    "reference": "reference",
}


def _slugify(name: str) -> str:
    """Convert a name to a filesystem-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug.strip("-")


def _vault_knowledge_dir() -> Path | None:
    """Resolve the vault directory for personal knowledge."""
    cfg = load_config()
    vault_root = cfg.get("vault_root", "")
    if not vault_root:
        return None
    pk = cfg.get("personal_knowledge", {})
    subpath = pk.get("vault_path", "Meta/WorkBuddy")
    return Path(vault_root) / subpath


def mint_personal_unit(
    *,
    name: str,
    category: str,
    content_body: str = "",
    severity: str = "",
    tags: str = "",
    context_before: str = "",
    context_after: str = "",
    evidence: str = "",
    definition: str = "",
    triggers: str = "",
    signals: str = "",
    default_response: str = "",
) -> dict[str, Any]:
    """Create or update a personal knowledge unit in the Obsidian vault.

    Args:
        name: Human-readable name (e.g., "Branch Explosion").
        category: One of: work_pattern, self_regulation, skill_gap,
                  feedback, preference, reference.
        content_body: Full markdown body. If empty, builds from structured fields.
        severity: HIGH, MODERATE, or LOW (optional).
        tags: Comma-separated tags (e.g., "wb/metacognition, wb/work-pattern").
        context_before: Comma-separated unit paths to chain before.
        context_after: Comma-separated unit paths to chain after.
        evidence: Initial evidence entry (appended with timestamp).
        definition: Pattern definition text.
        triggers: What typically triggers this pattern.
        signals: Observable signals.
        default_response: Agent's default response.

    Returns:
        Dict with status, path, vault_file, and created/updated flag.
    """
    vdir = _vault_knowledge_dir()
    if vdir is None:
        return {"error": "Personal knowledge vault path not configured (check vault_root and personal_knowledge.vault_path in config)"}

    # Determine subdirectory
    subdir = _CATEGORY_DIRS.get(category, "")
    slug = _slugify(name)

    if subdir:
        target_dir = vdir / subdir
        vault_rel = f"{subdir}/{slug}.md"
    else:
        target_dir = vdir
        vault_rel = f"{slug}.md"

    abs_path = target_dir / f"{slug}.md"
    pk_cfg = load_config().get("personal_knowledge", {})
    vault_subpath = pk_cfg.get("vault_path", "Meta/WorkBuddy")
    full_vault_rel = f"{vault_subpath}/{vault_rel}"

    # Check if file already exists → append evidence
    if abs_path.exists():
        return _append_evidence(abs_path, full_vault_rel, evidence, slug, subdir)

    # Ensure directory exists
    target_dir.mkdir(parents=True, exist_ok=True)

    # Build frontmatter
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    if not tag_list:
        tag_list = [f"wb/metacognition/{category}"] if category else ["wb/metacognition"]

    before_list = [p.strip() for p in context_before.split(",") if p.strip()] if context_before else []
    after_list = [p.strip() for p in context_after.split(",") if p.strip()] if context_after else []

    now = datetime.now(USER_TZ).strftime("%Y-%m-%d")

    fm_lines = [
        "---",
        f"name: {name}",
        f"category: {category}",
    ]
    if severity:
        fm_lines.append(f"severity: {severity}")
    fm_lines.append(f"tags: [{', '.join(tag_list)}]")
    if before_list:
        fm_lines.append(f"context_before: [{', '.join(before_list)}]")
    if after_list:
        fm_lines.append(f"context_after: [{', '.join(after_list)}]")
    fm_lines.append(f"last_observed: \"{now}\"")
    fm_lines.append(f"observation_count: {1 if evidence else 0}")
    fm_lines.append("---")

    # Build body
    if content_body:
        body = content_body
    else:
        body = _build_structured_body(
            name, definition, triggers, signals, default_response, evidence, now,
        )

    full_content = "\n".join(fm_lines) + "\n\n" + body

    # Write via vault_writer pattern
    from work_buddy.obsidian.vault_writer import _write_note

    success = _write_note(full_vault_rel, abs_path, full_content)

    if not success:
        return {"error": f"Failed to write {full_vault_rel}"}

    # Invalidate vault cache
    from work_buddy.knowledge.store import invalidate_vault
    invalidate_vault()

    unit_path = f"personal/{subdir}/{slug}" if subdir else f"personal/{slug}"

    return {
        "status": "created",
        "path": unit_path,
        "vault_file": full_vault_rel,
    }


def _build_structured_body(
    name: str,
    definition: str,
    triggers: str,
    signals: str,
    default_response: str,
    evidence: str,
    date: str,
) -> str:
    """Build a structured markdown body from individual fields."""
    sections = [f"# {name}"]

    if definition:
        sections.append(f"\n## Definition\n\n{definition}")
    if triggers:
        sections.append(f"\n## Typical Triggers\n\n{triggers}")
    if signals:
        sections.append(f"\n## Observable Signals\n\n{signals}")
    if default_response:
        sections.append(f"\n## Default Response\n\n{default_response}")

    sections.append("\n## Evidence")
    if evidence:
        sections.append(f"\n* {date} - {evidence}")
    else:
        sections.append("\n*No observations yet.*")

    return "\n".join(sections) + "\n"


def _append_evidence(
    abs_path: Path,
    vault_rel: str,
    evidence: str,
    slug: str,
    subdir: str,
) -> dict[str, Any]:
    """Append evidence to an existing personal unit file."""
    if not evidence:
        unit_path = f"personal/{subdir}/{slug}" if subdir else f"personal/{slug}"
        return {"status": "exists", "path": unit_path, "message": "File exists. Provide evidence to append."}

    from work_buddy.frontmatter import parse_frontmatter

    fm, body = parse_frontmatter(abs_path)
    now = datetime.now(USER_TZ).strftime("%Y-%m-%d")

    # Update frontmatter fields
    fm["last_observed"] = now
    fm["observation_count"] = fm.get("observation_count", 0) + 1

    # Append evidence line
    evidence_line = f"* {now} - {evidence}"
    if "## Evidence" in body:
        body = body.replace("## Evidence\n", f"## Evidence\n{evidence_line}\n", 1)
    else:
        body = body.rstrip() + f"\n\n## Evidence\n{evidence_line}\n"

    # Rebuild full content
    import yaml
    fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True).strip()
    full_content = f"---\n{fm_yaml}\n---\n\n{body}"

    from work_buddy.obsidian.vault_writer import _write_note
    success = _write_note(vault_rel, abs_path, full_content)

    if not success:
        return {"error": f"Failed to update {vault_rel}"}

    from work_buddy.knowledge.store import invalidate_vault
    invalidate_vault()

    unit_path = f"personal/{subdir}/{slug}" if subdir else f"personal/{slug}"

    return {
        "status": "updated",
        "path": unit_path,
        "vault_file": vault_rel,
        "observation_count": fm["observation_count"],
    }

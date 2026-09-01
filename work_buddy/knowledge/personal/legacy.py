"""Frozen compatibility adapter for the pre-seal Markdown authority.

This module exists only for the inactive migration gate.  Once the import
cohort seals, provider and mutation dispatch stop calling it entirely.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from work_buddy.config import USER_TZ, load_config
from work_buddy.frontmatter import parse_frontmatter


def configured_legacy_root() -> Path | None:
    """Return the configured legacy root without creating anything."""

    cfg = load_config()
    personal = cfg.get("personal_knowledge", {})
    if not personal.get("enabled", True):
        return None
    vault_root = cfg.get("vault_root", "")
    if not vault_root:
        return None
    root = Path(vault_root) / personal.get("vault_path", "Meta/WorkBuddy")
    return root if root.is_dir() else None


def _configured_write_root() -> tuple[Path, str] | None:
    cfg = load_config()
    personal = cfg.get("personal_knowledge", {})
    if not personal.get("enabled", True):
        return None
    vault_root = cfg.get("vault_root", "")
    if not vault_root:
        return None
    subpath = str(personal.get("vault_path", "Meta/WorkBuddy")).strip("/\\")
    return Path(vault_root) / subpath, subpath.replace("\\", "/")


def mint_legacy_personal_unit(
    *,
    name: str,
    category: str,
    content_body: str = "",
    severity: str = "",
    tags: str = "",
    evidence: str = "",
    definition: str = "",
    triggers: str = "",
    signals: str = "",
    default_response: str = "",
) -> dict[str, Any]:
    """Preserve the shipped Markdown mutation until the cohort seal."""

    from work_buddy.knowledge.personal.service import (
        CATEGORY_PATHS,
        build_structured_body,
        slugify,
    )

    configured = _configured_write_root()
    if configured is None:
        return {
            "error": "Personal knowledge vault path not configured "
            "(check vault_root and personal_knowledge.vault_path in config)"
        }
    root, vault_subpath = configured
    subdir = CATEGORY_PATHS.get(category, "")
    slug = slugify(name)
    if not slug:
        return {"error": "name must contain at least one letter or number"}
    relative = f"{subdir}/{slug}.md" if subdir else f"{slug}.md"
    absolute = root / Path(relative)
    vault_relative = f"{vault_subpath}/{relative}"
    logical_path = f"personal/{subdir}/{slug}" if subdir else f"personal/{slug}"
    if absolute.exists():
        return _append_evidence(
            absolute=absolute,
            vault_relative=vault_relative,
            logical_path=logical_path,
            evidence=evidence,
        )

    absolute.parent.mkdir(parents=True, exist_ok=True)
    tag_list = [part.strip() for part in tags.split(",") if part.strip()]
    if not tag_list:
        tag_list = [f"wb/metacognition/{category}"] if category else ["wb/metacognition"]
    date = datetime.now(USER_TZ).date().isoformat()
    frontmatter = ["---", f"name: {name}", f"category: {category}"]
    if severity:
        frontmatter.append(f"severity: {severity}")
    frontmatter.extend(
        [
            f"tags: [{', '.join(tag_list)}]",
            f'last_observed: "{date}"',
            f"observation_count: {1 if evidence else 0}",
            "---",
        ]
    )
    body = content_body or build_structured_body(
        name, definition, triggers, signals, default_response, evidence, date
    )
    from work_buddy.obsidian.vault_writer import vault_write

    if not vault_write(
        vault_relative, absolute, "\n".join(frontmatter) + "\n\n" + body
    ):
        return {"error": f"Failed to write {vault_relative}"}
    return {"status": "created", "path": logical_path, "vault_file": vault_relative}


def _append_evidence(
    *,
    absolute: Path,
    vault_relative: str,
    logical_path: str,
    evidence: str,
) -> dict[str, Any]:
    if not evidence:
        return {
            "status": "exists",
            "path": logical_path,
            "message": "File exists. Provide evidence to append.",
        }
    frontmatter, body = parse_frontmatter(absolute)
    date = datetime.now(USER_TZ).date().isoformat()
    frontmatter["last_observed"] = date
    frontmatter["observation_count"] = int(frontmatter.get("observation_count", 0)) + 1
    evidence_line = f"* {date} - {evidence}"
    if "## Evidence" in body:
        body = body.replace("## Evidence\n", f"## Evidence\n{evidence_line}\n", 1)
    else:
        body = body.rstrip() + f"\n\n## Evidence\n{evidence_line}\n"
    rendered = (
        "---\n"
        + yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True).strip()
        + "\n---\n\n"
        + body
    )
    from work_buddy.obsidian.vault_writer import vault_write

    if not vault_write(vault_relative, absolute, rendered):
        return {"error": f"Failed to update {vault_relative}"}
    return {
        "status": "updated",
        "path": logical_path,
        "vault_file": vault_relative,
        "observation_count": frontmatter["observation_count"],
    }

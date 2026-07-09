"""Build rulesync input and run harness projection."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import json
import yaml

from work_buddy import paths
from work_buddy.harness.backends.rulesync import RulesyncBackend
from work_buddy.harness.config import load_harness_config
from work_buddy.harness.model import HarnessSyncResult
from work_buddy.harness.registry import get_harness, resolve_harnesses


def sync_harnesses(
    harness_ids: Iterable[str] | None = None,
    *,
    output_root: Path | None = None,
    dry_run: bool = False,
    check: bool = False,
    backend: RulesyncBackend | None = None,
) -> HarnessSyncResult:
    cfg = load_harness_config()
    ids = tuple(harness_ids or cfg.enabled)
    if not ids:
        raise ValueError("no harnesses selected; run `wbuddy harness enable <id>`")
    targets = resolve_harnesses(ids)
    input_root = paths.data_dir("harness/rulesync-input")
    build_rulesync_input(input_root, ids)
    out = output_root or paths.config_dir()
    runner = backend or RulesyncBackend()
    return runner.generate(
        input_root=input_root,
        output_root=out,
        targets=targets,
        dry_run=dry_run,
        check=check,
    )


def build_rulesync_input(input_root: Path, harness_ids: Iterable[str]) -> Path:
    """Materialize a generated `.rulesync/` tree from work-buddy sources."""

    rulesync_root = input_root / ".rulesync"
    _reset_generated_tree(rulesync_root)
    selected = tuple(harness_ids)
    for harness_id in selected:
        get_harness(harness_id)

    _write_mcp(rulesync_root)
    _write_rule(rulesync_root, selected)
    _write_commands_and_skills(rulesync_root)
    return rulesync_root


def _reset_generated_tree(root: Path) -> None:
    if root.exists():
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    root.mkdir(parents=True, exist_ok=True)


def _write_mcp(root: Path) -> None:
    from work_buddy.cli.commands import _mcp_config

    _write_text(root / "mcp.json", json.dumps(_mcp_config(), indent=2))


def _write_rule(root: Path, harness_ids: tuple[str, ...]) -> None:
    targets = [get_harness(h).rulesync_target for h in harness_ids]
    body = (
        "Use the work-buddy MCP gateway for workflow and capability operations. "
        "Prefer `agent_docs` for system directions and `wb_search` for callable "
        "capability discovery."
    )
    _write_frontmatter_file(
        root / "rules" / "work-buddy.md",
        {"description": "Work Buddy project instructions", "targets": targets},
        body,
    )


def _write_commands_and_skills(root: Path) -> None:
    commands_dir = paths.asset_root() / ".claude" / "commands"
    if not commands_dir.exists():
        return
    for source in sorted(commands_dir.glob("wb-*.md")):
        metadata, body = _read_frontmatter(source)
        description = str(metadata.get("description") or metadata.get("short") or source.stem)
        command_meta = {"description": description, "targets": ["claudecode"]}
        skill_meta = {
            "name": source.stem,
            "description": description,
            "targets": ["codexcli"],
        }
        _write_frontmatter_file(root / "commands" / source.name, command_meta, body)
        _write_frontmatter_file(root / "skills" / source.stem / "SKILL.md", skill_meta, body)


def _read_frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text.strip()
    lines = text.splitlines()
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return {}, text.strip()
    meta_text = "\n".join(lines[1:end])
    metadata = _parse_frontmatter(meta_text)
    if not isinstance(metadata, dict):
        metadata = {}
    body = "\n".join(lines[end + 1 :]).strip()
    return metadata, body


def _parse_frontmatter(meta_text: str) -> dict:
    try:
        parsed = yaml.safe_load(meta_text) or {}
    except yaml.YAMLError:
        parsed = {}
        for line in meta_text.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            parsed[key.strip()] = value.strip()
    return parsed if isinstance(parsed, dict) else {}


def _write_frontmatter_file(path: Path, metadata: dict, body: str) -> None:
    frontmatter = yaml.safe_dump(metadata, sort_keys=False).strip()
    _write_text(path, f"---\n{frontmatter}\n---\n{body.strip()}\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

"""Build rulesync input and run harness projection."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import json
import shutil
from datetime import datetime, timezone
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
    install_toolchain: bool = False,
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
    try:
        runner = backend or RulesyncBackend(install_toolchain=install_toolchain)
    except RuntimeError as exc:
        return HarnessSyncResult(
            ok=False,
            returncode=127,
            targets=ids,
            input_root=input_root,
            output_root=out,
            command=[],
            dry_run=dry_run,
            check=check,
            error=str(exc),
        )

    if dry_run or check:
        return runner.generate(
            input_root=input_root,
            output_root=out,
            targets=targets,
            dry_run=dry_run,
            check=check,
        )

    preview = runner.generate(
        input_root=input_root,
        output_root=out,
        targets=targets,
        dry_run=True,
    )
    if not preview.ok:
        return preview

    backup_dir, existing = _backup_existing(out, preview.generated_paths)
    result = runner.generate(
        input_root=input_root,
        output_root=out,
        targets=targets,
    )
    result.backup_dir = backup_dir
    if not result.ok:
        _restore_backup(out, result.generated_paths or preview.generated_paths, backup_dir, existing)
        return result

    _project_private_rules(out, ids, result)
    return result


def build_rulesync_input(input_root: Path, harness_ids: Iterable[str]) -> Path:
    """Materialize a generated `.rulesync/` tree from work-buddy sources."""

    rulesync_root = input_root / ".rulesync"
    _reset_generated_tree(rulesync_root)
    selected = tuple(harness_ids)
    for harness_id in selected:
        get_harness(harness_id)

    _write_mcp(rulesync_root)
    _write_rules(rulesync_root, selected)
    _write_commands_and_skills(rulesync_root)
    _write_hooks(rulesync_root, selected)
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


def _write_rules(root: Path, harness_ids: tuple[str, ...]) -> None:
    source = paths.asset_root() / "CLAUDE.md"
    body = source.read_text(encoding="utf-8") if source.exists() else (
        "Use the work-buddy MCP gateway for workflow and capability operations. "
        "Prefer `agent_docs` for system directions and `wb_search` for callable "
        "capability discovery."
    )
    for harness_id in harness_ids:
        target = get_harness(harness_id)
        projected = _instructions_for_harness(body, harness_id)
        _write_frontmatter_file(
            root / "rules" / f"work-buddy-{harness_id}.md",
            {
                "description": f"Work Buddy instructions for {target.label}",
                "targets": [target.rulesync_target],
            },
            projected,
        )


def _instructions_for_harness(body: str, harness_id: str) -> str:
    if harness_id != "codexcli":
        return body
    projected = body.replace("Claude Code", "Codex")
    projected = projected.replace("CLAUDE.local.md", "AGENTS.override.md")
    projected = projected.replace(".claude/commands/", ".agents/skills/")
    projected = projected.replace(
        "`WORK_BUDDY_SESSION_ID` is set automatically by a SessionStart hook. "
        "Read it from your conversation context (or the environment), then:",
        "`CODEX_THREAD_ID` is Codex's native session identity. The generated "
        "SessionStart hook also surfaces it in context. Read it from the "
        "environment or hook context, then:",
    )
    projected = projected.replace(
        'mcp__work-buddy__wb_init(session_id="<your WORK_BUDDY_SESSION_ID>")',
        'mcp__work-buddy__wb_init(session_id="<your CODEX_THREAD_ID>", '
        'harness_id="codexcli")',
    )
    return projected


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


def _write_hooks(root: Path, harness_ids: tuple[str, ...]) -> None:
    hooks: dict[str, dict] = {"version": 1, "hooks": {}}
    for harness_id in harness_ids:
        target = get_harness(harness_id)
        if "hooks" not in target.features:
            continue
        hooks[target.rulesync_target] = {
            "hooks": {
                "sessionStart": [
                    {
                        "type": "command",
                        "command": f"wbuddy hook session-start --harness {harness_id}",
                    }
                ],
                "beforeSubmitPrompt": [
                    {
                        "type": "command",
                        "command": (
                            f"wbuddy hook user-prompt-submit --harness {harness_id}"
                        ),
                    }
                ],
                "postToolUse": [
                    {
                        "type": "command",
                        "command": f"wbuddy hook post-tool-use --harness {harness_id}",
                    }
                ],
                "stop": [
                    {
                        "type": "command",
                        "command": f"wbuddy hook stop --harness {harness_id}",
                    }
                ],
            }
        }
    _write_text(root / "hooks.json", json.dumps(hooks, indent=2) + "\n")


def _backup_existing(
    output_root: Path,
    generated_paths: list[str],
) -> tuple[Path | None, set[str]]:
    existing: set[str] = set()
    candidates: list[tuple[str, Path]] = []
    for relative in generated_paths:
        path = _safe_output_path(output_root, relative)
        if path is not None and path.exists():
            existing.add(relative)
            candidates.append((relative, path))
    if not candidates:
        return None, existing

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = paths.data_dir("harness/backups") / stamp
    for relative, source in candidates:
        destination = backup / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination)
    return backup, existing


def _restore_backup(
    output_root: Path,
    generated_paths: list[str],
    backup_dir: Path | None,
    existing: set[str],
) -> None:
    for relative in generated_paths:
        destination = _safe_output_path(output_root, relative)
        if destination is None:
            continue
        backup = backup_dir / relative if backup_dir is not None else None
        if relative in existing and backup is not None and backup.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if backup.is_dir():
                shutil.copytree(backup, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(backup, destination)
        elif destination.is_file():
            destination.unlink()


def _safe_output_path(root: Path, relative: str) -> Path | None:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _project_private_rules(
    output_root: Path,
    harness_ids: tuple[str, ...],
    result: HarnessSyncResult,
) -> None:
    if "codexcli" not in harness_ids:
        return
    source = output_root / "CLAUDE.local.md"
    if not source.is_file():
        return
    target = output_root / "AGENTS.override.md"
    marker = "<!-- work-buddy:generated-from CLAUDE.local.md -->"
    if target.exists():
        try:
            current = target.read_text(encoding="utf-8")
        except OSError as exc:
            result.warnings.append(f"could not read AGENTS.override.md: {exc}")
            return
        if not current.startswith(marker):
            result.warnings.append(
                "kept existing AGENTS.override.md; it is not owned by work-buddy"
            )
            return
    private = source.read_text(encoding="utf-8")
    target.write_text(
        f"{marker}\n{_instructions_for_harness(private, 'codexcli').strip()}\n",
        encoding="utf-8",
    )
    features = result.data.setdefault("features", {})
    local = features.setdefault("local_rules", {"count": 0, "paths": []})
    local["count"] = 1
    local["paths"] = ["AGENTS.override.md"]


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

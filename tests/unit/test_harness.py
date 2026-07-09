"""Unit tests for harness projection."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import yaml

from work_buddy.harness.backends.rulesync import RulesyncBackend
from work_buddy.harness.model import HarnessTarget
from work_buddy.harness.sync import build_rulesync_input
from work_buddy.harness.toolchain import rulesync_command


def test_build_rulesync_input_generates_codex_skills_from_claude_commands(
    tmp_path, monkeypatch
):
    asset_root = tmp_path / "assets"
    command_dir = asset_root / ".claude" / "commands"
    command_dir.mkdir(parents=True)
    (command_dir / "wb-dev-pr.md").write_text(
        "---\nshort: Commit with verification\n---\nRun dev-pr through MCP.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("work_buddy.paths.asset_root", lambda: asset_root)
    monkeypatch.setattr("work_buddy.mcp_server.server._get_port", lambda: 5126)

    root = build_rulesync_input(tmp_path / "input", ("codexcli",))

    skill = root / "skills" / "wb-dev-pr" / "SKILL.md"
    assert skill.exists()
    meta, body = _frontmatter(skill)
    assert meta["name"] == "wb-dev-pr"
    assert meta["description"] == "Commit with verification"
    assert meta["targets"] == ["codexcli"]
    assert body == "Run dev-pr through MCP."

    command = root / "commands" / "wb-dev-pr.md"
    meta, _ = _frontmatter(command)
    assert meta["targets"] == ["claudecode"]

    rule_meta, _ = _frontmatter(root / "rules" / "work-buddy.md")
    assert rule_meta["targets"] == ["codexcli"]
    mcp = yaml.safe_load((root / "mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["work-buddy"]["url"] == "http://localhost:5126/mcp"


def test_build_rulesync_input_tolerates_unquoted_colon_in_short(tmp_path, monkeypatch):
    asset_root = tmp_path / "assets"
    command_dir = asset_root / ".claude" / "commands"
    command_dir.mkdir(parents=True)
    (command_dir / "wb-dev-release.md").write_text(
        "---\nshort: Cut a tagged release: preflight gates\n---\nRun release.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("work_buddy.paths.asset_root", lambda: asset_root)
    monkeypatch.setattr("work_buddy.mcp_server.server._get_port", lambda: 5126)

    root = build_rulesync_input(tmp_path / "input", ("codexcli",))

    meta, body = _frontmatter(root / "skills" / "wb-dev-release" / "SKILL.md")
    assert meta["description"] == "Cut a tagged release: preflight gates"
    assert body == "Run release."


def test_rulesync_backend_uses_json_generate_and_feature_union(monkeypatch, tmp_path):
    calls = {}

    def fake_run(argv, text, capture_output, check):
        calls["argv"] = argv
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "success": True,
                    "data": {
                        "features": {
                            "rules": {"count": 1, "paths": ["AGENTS.md"]},
                            "mcp": {"count": 1, "paths": [".codex/config.toml"]},
                            "skills": {
                                "count": 1,
                                "paths": [".agents/skills/wb-dev-pr/SKILL.md"],
                            },
                        },
                        "hasDiff": True,
                        "totalFiles": 3,
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = RulesyncBackend(command=["rulesync"])
    result = backend.generate(
        input_root=tmp_path / "input",
        output_root=tmp_path / "output",
        targets=[
            HarnessTarget(
                id="codexcli",
                label="Codex CLI",
                rulesync_target="codexcli",
                description="",
                features=("rules", "mcp", "skills"),
                simulate_skills=True,
            )
        ],
        dry_run=True,
    )

    argv = calls["argv"]
    assert argv[:3] == ["rulesync", "--json", "generate"]
    assert "--simulate-skills" in argv
    assert "--simulate-commands" not in argv
    assert argv[argv.index("--features") + 1] == "rules,mcp,skills"
    assert "--dry-run" in argv
    assert result.ok is True
    assert result.generated_paths == [
        ".agents/skills/wb-dev-pr/SKILL.md",
        ".codex/config.toml",
        "AGENTS.md",
    ]
    assert result.has_diff is True
    assert result.total_files == 3


def test_rulesync_command_uses_absolute_npx_when_rulesync_missing(monkeypatch):
    def fake_which(name):
        if name == "npx":
            return "C:/Program Files/nodejs/npx.CMD"
        return None

    monkeypatch.setattr(shutil, "which", fake_which)

    class Cfg:
        rulesync_command = ""
        rulesync_version = "9.6.0"

    assert rulesync_command(Cfg()) == [
        "C:/Program Files/nodejs/npx.CMD",
        "-y",
        "rulesync@9.6.0",
    ]


def test_rulesync_backend_reports_missing_executable(tmp_path):
    backend = RulesyncBackend(command=["definitely-missing-rulesync"])
    result = backend.generate(
        input_root=tmp_path / "input",
        output_root=tmp_path / "output",
        targets=[
            HarnessTarget(
                id="codexcli",
                label="Codex CLI",
                rulesync_target="codexcli",
                description="",
                features=("rules",),
            )
        ],
    )

    assert result.ok is False
    assert result.returncode == 127
    assert "not found" in result.error


def test_rulesync_backend_treats_json_stderr_error_as_failure(monkeypatch, tmp_path):
    def fake_run(argv, text, capture_output, check):
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="",
            stderr=json.dumps(
                {
                    "success": False,
                    "error": {
                        "code": "UNKNOWN_ERROR",
                        "message": "Failed to load a Rulesync MCP file",
                    },
                }
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = RulesyncBackend(command=["rulesync"])
    result = backend.generate(
        input_root=tmp_path / "input",
        output_root=tmp_path / "output",
        targets=[
            HarnessTarget(
                id="codexcli",
                label="Codex CLI",
                rulesync_target="codexcli",
                description="",
                features=("rules",),
            )
        ],
    )

    assert result.ok is False
    assert "Failed to load" in result.error


def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "---"
    end = lines[1:].index("---") + 1
    meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body

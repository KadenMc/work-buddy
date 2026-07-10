"""Unit tests for harness projection."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import yaml

from work_buddy.harness.backends.rulesync import RulesyncBackend
from work_buddy.harness.model import HarnessSyncResult, HarnessTarget
from work_buddy.harness.sync import (
    _project_private_rules,
    build_rulesync_input,
    sync_harnesses,
)
from work_buddy.harness.toolchain import install_rulesync, rulesync_command


def test_build_rulesync_input_generates_codex_skills_from_claude_commands(
    tmp_path, monkeypatch
):
    asset_root = tmp_path / "assets"
    command_dir = asset_root / ".claude" / "commands"
    command_dir.mkdir(parents=True)
    (asset_root / "CLAUDE.md").write_text(
        "You are work-buddy running in Claude Code.\n",
        encoding="utf-8",
    )
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

    rule_meta, rule_body = _frontmatter(
        root / "rules" / "work-buddy-codexcli.md"
    )
    assert rule_meta["targets"] == ["codexcli"]
    assert "Codex" in rule_body
    mcp = yaml.safe_load((root / "mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["work-buddy"]["url"] == "http://localhost:5126/mcp"
    hooks = json.loads((root / "hooks.json").read_text(encoding="utf-8"))
    assert hooks["version"] == 1
    assert hooks["codexcli"]["hooks"]["sessionStart"][0]["command"] == (
        "wbuddy hook session-start --harness codexcli"
    )


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


def test_sync_restores_existing_files_when_generation_fails(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    output = tmp_path / "project"
    output.mkdir()
    existing = output / "AGENTS.md"
    existing.write_text("user-owned instructions\n", encoding="utf-8")
    monkeypatch.setattr(
        "work_buddy.paths.data_dir",
        lambda name="": data_root / name,
    )
    monkeypatch.setattr("work_buddy.paths.asset_root", lambda: tmp_path / "assets")

    class FailingBackend:
        calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                existing.write_text("partial replacement\n", encoding="utf-8")
            return HarnessSyncResult(
                ok=self.calls == 1,
                returncode=0 if self.calls == 1 else 1,
                targets=("codexcli",),
                input_root=kwargs["input_root"],
                output_root=kwargs["output_root"],
                command=["rulesync"],
                dry_run=kwargs.get("dry_run", False),
                data={
                    "features": {
                        "rules": {"count": 1, "paths": ["AGENTS.md"]}
                    }
                },
                error="generation failed" if self.calls == 2 else "",
            )

    result = sync_harnesses(
        ("codexcli",), output_root=output, backend=FailingBackend()
    )

    assert result.ok is False
    assert existing.read_text(encoding="utf-8") == "user-owned instructions\n"
    assert list((data_root / "harness" / "backups").rglob("AGENTS.md"))


def test_install_rulesync_verifies_release_checksum(tmp_path, monkeypatch):
    binary = b"rulesync-binary"
    import hashlib

    digest = hashlib.sha256(binary).hexdigest()
    target = tmp_path / "rulesync.exe"

    class Response:
        def __init__(self, body):
            self.body = body
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size=-1):
            if size < 0:
                return self.body
            chunk = self.body[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    def fake_urlopen(url, timeout):
        if str(url).endswith("SHA256SUMS"):
            return Response(
                f"{digest}  rulesync-windows-x64.exe\n".encode("utf-8")
            )
        return Response(binary)

    monkeypatch.setattr(
        "work_buddy.harness.toolchain.managed_rulesync_path", lambda version: target
    )
    monkeypatch.setattr(
        "work_buddy.harness.toolchain._release_asset_name",
        lambda: "rulesync-windows-x64.exe",
    )
    monkeypatch.setattr("work_buddy.harness.toolchain.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "work_buddy.harness.toolchain._reports_version", lambda command, version: True
    )

    assert install_rulesync("9.6.0") == target
    assert target.read_bytes() == binary


def test_private_claude_rules_project_to_owned_codex_override(tmp_path):
    (tmp_path / "CLAUDE.local.md").write_text(
        "Use Claude Code for this private preference.\n", encoding="utf-8"
    )
    result = HarnessSyncResult(
        ok=True,
        returncode=0,
        targets=("codexcli",),
        input_root=tmp_path,
        output_root=tmp_path,
        command=["rulesync"],
    )

    _project_private_rules(tmp_path, ("codexcli",), result)

    override = (tmp_path / "AGENTS.override.md").read_text(encoding="utf-8")
    assert override.startswith("<!-- work-buddy:generated-from CLAUDE.local.md -->")
    assert "Use Codex" in override
    assert result.generated_paths == ["AGENTS.override.md"]


def test_private_projection_never_clobbers_unowned_override(tmp_path):
    (tmp_path / "CLAUDE.local.md").write_text("private\n", encoding="utf-8")
    override = tmp_path / "AGENTS.override.md"
    override.write_text("hand-written codex preference\n", encoding="utf-8")
    result = HarnessSyncResult(
        ok=True,
        returncode=0,
        targets=("codexcli",),
        input_root=tmp_path,
        output_root=tmp_path,
        command=["rulesync"],
    )

    _project_private_rules(tmp_path, ("codexcli",), result)

    assert override.read_text(encoding="utf-8") == "hand-written codex preference\n"
    assert result.warnings == [
        "kept existing AGENTS.override.md; it is not owned by work-buddy"
    ]


def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "---"
    end = lines[1:].index("---") + 1
    meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body

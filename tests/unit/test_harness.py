"""Unit tests for harness projection."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import yaml
import pytest

from work_buddy.harness.backends.rulesync import RulesyncBackend
from work_buddy.harness.model import HarnessConfig, HarnessSyncResult, HarnessTarget
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


def test_rulesync_command_uses_absolute_npx_when_rulesync_missing(monkeypatch, tmp_path):
    def fake_which(name):
        if name == "npx":
            return "C:/Program Files/nodejs/npx.CMD"
        return None

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(
        "work_buddy.harness.toolchain.managed_rulesync_path",
        lambda version: tmp_path / "missing-rulesync.exe",
    )

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


def test_sync_failure_restores_previewed_paths_missing_from_partial_result(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir()
    originals = {"AGENTS.md": "original agent rules", ".mcp.json": "original servers"}
    for relative, content in originals.items():
        (output / relative).write_text(content, encoding="utf-8")
    monkeypatch.setattr("work_buddy.paths.data_dir", lambda name="": tmp_path / "data" / name)
    monkeypatch.setattr("work_buddy.paths.asset_root", lambda: tmp_path / "assets")

    class PartiallyReportedFailure:
        def generate(self, **kwargs):
            preview = kwargs.get("dry_run", False)
            if not preview:
                for relative in originals:
                    (output / relative).write_text("partial replacement", encoding="utf-8")
                (output / "CLAUDE.md").write_text("partial new rules", encoding="utf-8")
            reported = [*originals, "CLAUDE.md"] if preview else ["AGENTS.md"]
            return HarnessSyncResult(
                ok=preview,
                returncode=0 if preview else 1,
                targets=("codexcli", "claudecode"),
                input_root=kwargs["input_root"],
                output_root=kwargs["output_root"],
                command=["rulesync"],
                dry_run=preview,
                data={"features": {"rules": {"count": len(reported), "paths": reported}}},
                error="second renderer failed" if not preview else "",
            )

    result = sync_harnesses(
        ("codexcli", "claudecode"), output_root=output, backend=PartiallyReportedFailure(),
    )
    assert not result.ok
    for relative, content in originals.items():
        assert (output / relative).read_text(encoding="utf-8") == content
    assert not (output / "CLAUDE.md").exists()


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


def test_registry_declares_browser_surface_and_unknown_target_defaults_to_none():
    from work_buddy.harness.registry import get_harness

    assert get_harness("claudecode").browser_surface == "native-pane"
    assert get_harness("codexcli").browser_surface == "mcp-playwright"
    target = HarnessTarget(id="other", label="Other", rulesync_target="other", description="", features=())
    assert target.browser_surface == "none"


@pytest.mark.parametrize("selected", [("codexcli",), ("claudecode",), ("claudecode", "codexcli")])
def test_playwright_projection_is_pinned_isolated_and_targeted(tmp_path, monkeypatch, selected):
    monkeypatch.setattr("work_buddy.paths.asset_root", lambda: tmp_path / "assets")
    cfg = HarnessConfig(playwright_mcp_version="1.2.3")
    monkeypatch.setattr("work_buddy.harness.sync.load_harness_config", lambda: cfg)
    root = build_rulesync_input(tmp_path / "input", selected)
    servers = json.loads((root / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "work-buddy" in servers
    if "codexcli" not in selected:
        assert "playwright" not in servers
    else:
        assert servers["playwright"] == {
            "type": "stdio", "command": "npx",
            "args": ["-y", "@playwright/mcp@1.2.3", "--isolated"],
            "targets": ["codexcli"],
        }


def test_playwright_projection_reads_surface_instead_of_harness_name(tmp_path, monkeypatch):
    from dataclasses import replace
    from work_buddy.harness.registry import _HARNESSES

    monkeypatch.setattr("work_buddy.paths.asset_root", lambda: tmp_path / "assets")
    monkeypatch.setitem(_HARNESSES, "codexcli", replace(_HARNESSES["codexcli"], browser_surface="none"))
    root = build_rulesync_input(tmp_path / "input", ("codexcli",))
    assert "playwright" not in json.loads((root / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]


def test_dashboard_rule_is_path_scoped_and_claude_only(tmp_path, monkeypatch):
    monkeypatch.setattr("work_buddy.paths.asset_root", lambda: tmp_path / "assets")
    root = build_rulesync_input(tmp_path / "input", ("claudecode", "codexcli"))
    meta, body = _frontmatter(root / "rules" / "dashboard-development.md")
    assert meta["targets"] == ["claudecode"]
    assert meta["globs"] == ["dashboard-react/**"]
    assert "dev/dashboard/ux-directions" in body
    assert "dev/dashboard/verification-directions" in body
    assert "unrelated work does not require" in body
    root = build_rulesync_input(tmp_path / "input", ("codexcli",))
    assert not (root / "rules" / "dashboard-development.md").exists()


def test_sync_check_surfaces_pins_and_detects_version_drift(tmp_path, monkeypatch):
    from dataclasses import replace

    cfg = HarnessConfig(playwright_mcp_version="1.2.3")
    monkeypatch.setattr("work_buddy.harness.sync.load_harness_config", lambda: cfg)
    monkeypatch.setattr("work_buddy.paths.data_dir", lambda name="": tmp_path / name)
    monkeypatch.setattr("work_buddy.paths.asset_root", lambda: tmp_path / "assets")

    class CheckingBackend:
        def generate(self, **kwargs):
            path = kwargs["input_root"] / ".rulesync" / "mcp.json"
            actual = json.loads(path.read_text(encoding="utf-8"))
            drift = "@playwright/mcp@1.2.3" not in actual["mcpServers"]["playwright"]["args"]
            return HarnessSyncResult(
                ok=True, returncode=0, targets=("codexcli",),
                input_root=kwargs["input_root"], output_root=kwargs["output_root"],
                command=["rulesync"], check=True, data={"hasDiff": drift},
            )

    result = sync_harnesses(("codexcli",), output_root=tmp_path / "out", check=True, backend=CheckingBackend())
    assert result.data["toolchain_versions"] == {"rulesync": cfg.rulesync_version, "playwright_mcp": "1.2.3"}
    assert result.has_diff is False
    cfg = replace(cfg, playwright_mcp_version="1.2.4")
    result = sync_harnesses(("codexcli",), output_root=tmp_path / "out", check=True, backend=CheckingBackend())
    assert result.data["toolchain_versions"]["playwright_mcp"] == "1.2.4"
    assert result.has_diff is True


def test_harness_selection_preserves_playwright_version_override(monkeypatch):
    from work_buddy.harness.config import load_harness_config, save_harness_selection

    local = {"harness": {"enabled": ["codexcli"], "playwright_mcp": {"version": "1.2.3"}}}
    monkeypatch.setattr("work_buddy.config.load_config", lambda: local)
    monkeypatch.setattr("work_buddy.config.read_config_local", lambda: local)
    monkeypatch.setattr("work_buddy.config.write_config_local", lambda key, value: local.__setitem__(key, value))
    assert load_harness_config().playwright_mcp_version == "1.2.3"
    assert save_harness_selection(primary="codexcli").playwright_mcp_version == "1.2.3"
    assert local["harness"]["playwright_mcp"]["version"] == "1.2.3"


def test_rulesync_multi_target_projection_filters_mcp_before_rendering(tmp_path, monkeypatch):
    from work_buddy.harness.registry import get_harness

    monkeypatch.setattr("work_buddy.paths.asset_root", lambda: tmp_path / "assets")
    input_root = tmp_path / "input"
    build_rulesync_input(input_root, ("claudecode", "codexcli"))
    observed = {}

    def fake_run(argv, **kwargs):
        target = argv[argv.index("--targets") + 1]
        root = Path(argv[argv.index("--input-root") + 1])
        observed[target] = json.loads((root / ".rulesync/mcp.json").read_text(encoding="utf-8"))["mcpServers"]
        assert "--check" in argv
        path = ".mcp.json" if target == "claudecode" else ".codex/config.toml"
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({
            "success": True,
            "data": {"features": {"mcp": {"count": 1, "paths": [path]}}, "hasDiff": target == "codexcli"},
        }), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = RulesyncBackend(command=["rulesync"]).generate(
        input_root=input_root, output_root=tmp_path / "output",
        targets=[get_harness("claudecode"), get_harness("codexcli")], check=True,
    )
    assert "playwright" not in observed["claudecode"]
    assert "playwright" in observed["codexcli"]
    assert "work-buddy" in observed["claudecode"]
    assert "work-buddy" in observed["codexcli"]
    assert result.ok
    assert result.has_diff
    assert result.generated_paths == [".codex/config.toml", ".mcp.json"]
    assert result.total_files == 2

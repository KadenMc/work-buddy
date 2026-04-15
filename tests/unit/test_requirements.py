"""Unit tests for the requirements system (health/requirements.py + requirement_checks.py)."""

import json
import os
import pytest

from work_buddy.health.requirements import (
    REQUIREMENT_REGISTRY,
    RequirementChecker,
    RequirementDef,
    RequirementResult,
)


# ---------------------------------------------------------------------------
# Registry sanity
# ---------------------------------------------------------------------------

class TestRequirementRegistry:
    def test_registry_not_empty(self):
        assert len(REQUIREMENT_REGISTRY) >= 20

    def test_all_ids_are_hierarchical(self):
        """Every ID must have at least 2 slashes (domain/subsystem/name)."""
        for req_id in REQUIREMENT_REGISTRY:
            parts = req_id.split("/")
            assert len(parts) >= 3, f"ID '{req_id}' needs at least 3 path segments"

    def test_valid_domains(self):
        valid_domains = {"core", "obsidian", "integrations", "services"}
        for req_id in REQUIREMENT_REGISTRY:
            domain = req_id.split("/")[0]
            assert domain in valid_domains, f"ID '{req_id}' has invalid domain '{domain}'"

    def test_valid_severity(self):
        for req in REQUIREMENT_REGISTRY.values():
            assert req.severity in ("required", "recommended"), (
                f"{req.id} has invalid severity '{req.severity}'"
            )

    def test_core_requirements_have_no_component(self):
        for req in REQUIREMENT_REGISTRY.values():
            if req.id.startswith("core/"):
                assert req.component is None, (
                    f"Core requirement {req.id} should have component=None"
                )

    def test_non_core_requirements_have_component(self):
        for req in REQUIREMENT_REGISTRY.values():
            if not req.id.startswith("core/"):
                assert req.component is not None, (
                    f"Non-core requirement {req.id} should have a component"
                )

    def test_check_fn_is_importable_string(self):
        for req in REQUIREMENT_REGISTRY.values():
            parts = req.check_fn.rsplit(".", 1)
            assert len(parts) == 2, f"{req.id} check_fn must be module.function"

    def test_setup_groups_are_set(self):
        for req in REQUIREMENT_REGISTRY.values():
            assert req.setup_group, f"{req.id} is missing setup_group"

    def test_bootstrap_group_for_core(self):
        for req in REQUIREMENT_REGISTRY.values():
            if req.id.startswith("core/"):
                assert req.setup_group == "bootstrap", (
                    f"Core requirement {req.id} should have setup_group='bootstrap'"
                )


# ---------------------------------------------------------------------------
# RequirementResult
# ---------------------------------------------------------------------------

class TestRequirementResult:
    def test_to_dict_pass(self):
        r = RequirementResult(
            id="core/config/vault-root",
            ok=True,
            detail="vault exists",
            fix_hint="Set vault_root",
            severity="required",
            component=None,
        )
        d = r.to_dict()
        assert d["ok"] is True
        assert d["fix_hint"] == ""  # suppressed on pass

    def test_to_dict_fail(self):
        r = RequirementResult(
            id="core/config/vault-root",
            ok=False,
            detail="vault missing",
            fix_hint="Set vault_root",
            severity="required",
            component=None,
        )
        d = r.to_dict()
        assert d["ok"] is False
        assert d["fix_hint"] == "Set vault_root"


# ---------------------------------------------------------------------------
# RequirementChecker
# ---------------------------------------------------------------------------

class TestRequirementChecker:
    def test_check_bootstrap_returns_only_core(self):
        checker = RequirementChecker()
        results = checker.check_bootstrap()
        for r in results:
            assert r.id.startswith("core/"), f"Bootstrap returned non-core: {r.id}"

    def test_check_bootstrap_count(self):
        checker = RequirementChecker()
        results = checker.check_bootstrap()
        core_count = sum(1 for r in REQUIREMENT_REGISTRY if r.startswith("core/"))
        assert len(results) == core_count

    def test_check_component_filters(self):
        checker = RequirementChecker()
        results = checker.check_component("obsidian")
        for r in results:
            assert r.component == "obsidian"
        assert len(results) > 0

    def test_check_component_unknown_returns_empty(self):
        checker = RequirementChecker()
        results = checker.check_component("nonexistent_component")
        assert results == []

    def test_check_group(self):
        checker = RequirementChecker()
        results = checker.check_group("journal")
        assert len(results) > 0
        for r in results:
            req = REQUIREMENT_REGISTRY[r.id]
            assert req.setup_group == "journal"

    def test_check_all_excludes_unwanted(self, monkeypatch):
        """If hindsight is unwanted, its requirements should be skipped."""
        import work_buddy.health.preferences as pmod
        monkeypatch.setattr(pmod, "is_wanted",
                            lambda cid: False if cid == "hindsight" else None)
        checker = RequirementChecker()
        results = checker.check_all(include_unwanted=False)
        hindsight_results = [r for r in results if r.component == "hindsight"]
        assert hindsight_results == []

    def test_check_all_includes_unwanted_when_asked(self, monkeypatch):
        import work_buddy.health.preferences as pmod
        monkeypatch.setattr(pmod, "is_wanted",
                            lambda cid: False if cid == "hindsight" else None)
        checker = RequirementChecker()
        results = checker.check_all(include_unwanted=True)
        hindsight_results = [r for r in results if r.component == "hindsight"]
        assert len(hindsight_results) > 0

    def test_check_all_always_includes_core(self, monkeypatch):
        """Core requirements run even if all components are unwanted."""
        import work_buddy.health.preferences as pmod
        monkeypatch.setattr(pmod, "is_wanted", lambda cid: False)
        checker = RequirementChecker()
        results = checker.check_all(include_unwanted=False)
        core_results = [r for r in results if r.id.startswith("core/")]
        assert len(core_results) > 0

    def test_summarize(self):
        results = [
            RequirementResult("a", True, "ok", "", "required", None),
            RequirementResult("b", False, "fail", "fix", "required", None),
            RequirementResult("c", False, "fail", "fix", "recommended", "obsidian"),
        ]
        summary = RequirementChecker.summarize(results)
        assert summary["total"] == 3
        assert summary["passed"] == 1
        assert summary["failed_required"] == 1
        assert summary["failed_recommended"] == 1
        assert summary["all_required_pass"] is False
        assert len(summary["failures"]) == 2

    def test_summarize_all_pass(self):
        results = [
            RequirementResult("a", True, "ok", "", "required", None),
            RequirementResult("b", True, "ok", "", "recommended", None),
        ]
        summary = RequirementChecker.summarize(results)
        assert summary["all_required_pass"] is True
        assert summary["failures"] == []

    def test_check_fn_error_returns_failure(self, monkeypatch):
        """If a check function raises, the result should be ok=False with error detail."""
        bad_req = RequirementDef(
            id="test/bad/check",
            component=None,
            description="Intentionally broken",
            check_fn="work_buddy.health.requirement_checks.nonexistent_function",
            severity="required",
            fix_hint="N/A",
            setup_group="bootstrap",
        )
        checker = RequirementChecker()
        result = checker._run_check(bad_req)
        assert result.ok is False
        assert "error" in result.detail.lower()


# ---------------------------------------------------------------------------
# Individual check functions (with mocked filesystem)
# ---------------------------------------------------------------------------

class TestCheckFunctions:
    """Tests that verify check functions against controlled filesystem state."""

    def test_check_config_yaml_exists_pass(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._repo_root",
            lambda: tmp_path,
        )
        (tmp_path / "config.yaml").write_text("vault_root: /test", encoding="utf-8")
        from work_buddy.health.requirement_checks import check_config_yaml_exists
        result = check_config_yaml_exists()
        assert result["ok"] is True

    def test_check_config_yaml_exists_fail(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._repo_root",
            lambda: tmp_path,
        )
        from work_buddy.health.requirement_checks import check_config_yaml_exists
        result = check_config_yaml_exists()
        assert result["ok"] is False

    def test_check_vault_root_pass(self, monkeypatch, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {"vault_root": str(vault)},
        )
        from work_buddy.health.requirement_checks import check_vault_root
        result = check_vault_root()
        assert result["ok"] is True

    def test_check_vault_root_empty(self, monkeypatch):
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {"vault_root": ""},
        )
        from work_buddy.health.requirement_checks import check_vault_root
        result = check_vault_root()
        assert result["ok"] is False

    def test_check_vault_root_nonexistent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {"vault_root": str(tmp_path / "nope")},
        )
        from work_buddy.health.requirement_checks import check_vault_root
        result = check_vault_root()
        assert result["ok"] is False

    def test_check_timezone_valid(self, monkeypatch):
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {"timezone": "America/New_York"},
        )
        from work_buddy.health.requirement_checks import check_timezone
        result = check_timezone()
        assert result["ok"] is True

    def test_check_timezone_invalid(self, monkeypatch):
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {"timezone": "Not/A/Timezone"},
        )
        from work_buddy.health.requirement_checks import check_timezone
        result = check_timezone()
        assert result["ok"] is False

    def test_check_anthropic_api_key_set(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
        from work_buddy.health.requirement_checks import check_anthropic_api_key
        result = check_anthropic_api_key()
        assert result["ok"] is True

    def test_check_anthropic_api_key_missing(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from work_buddy.health.requirement_checks import check_anthropic_api_key
        result = check_anthropic_api_key()
        assert result["ok"] is False

    def test_check_obsidian_dir_pass(self, monkeypatch, tmp_path):
        vault = tmp_path / "vault"
        (vault / ".obsidian").mkdir(parents=True)
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._vault_root",
            lambda: vault,
        )
        from work_buddy.health.requirement_checks import check_obsidian_dir
        result = check_obsidian_dir()
        assert result["ok"] is True

    def test_check_obsidian_dir_missing(self, monkeypatch, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._vault_root",
            lambda: vault,
        )
        from work_buddy.health.requirement_checks import check_obsidian_dir
        result = check_obsidian_dir()
        assert result["ok"] is False

    def test_check_journal_dir_pass(self, monkeypatch, tmp_path):
        vault = tmp_path / "vault"
        (vault / "journal").mkdir(parents=True)
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._vault_root",
            lambda: vault,
        )
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {"obsidian": {"journal_dir": "journal"}},
        )
        from work_buddy.health.requirement_checks import check_journal_dir
        result = check_journal_dir()
        assert result["ok"] is True

    def test_check_log_section_pass(self, monkeypatch, tmp_path):
        from datetime import date
        vault = tmp_path / "vault"
        journal = vault / "journal"
        journal.mkdir(parents=True)
        today = date.today().strftime("%Y-%m-%d")
        note = journal / f"{today}.md"
        note.write_text("# Sign-In\nstuff\n# Log\n* 9:00 AM - did thing\n", encoding="utf-8")
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._vault_root",
            lambda: vault,
        )
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {"obsidian": {"journal_dir": "journal"}},
        )
        from work_buddy.health.requirement_checks import check_log_section
        result = check_log_section()
        assert result["ok"] is True

    def test_check_log_section_missing(self, monkeypatch, tmp_path):
        from datetime import date
        vault = tmp_path / "vault"
        journal = vault / "journal"
        journal.mkdir(parents=True)
        today = date.today().strftime("%Y-%m-%d")
        note = journal / f"{today}.md"
        note.write_text("# Sign-In\nstuff\n# Summary\n", encoding="utf-8")
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._vault_root",
            lambda: vault,
        )
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {"obsidian": {"journal_dir": "journal"}},
        )
        from work_buddy.health.requirement_checks import check_log_section
        result = check_log_section()
        assert result["ok"] is False

    def test_check_log_section_bold_header(self, monkeypatch, tmp_path):
        """Headers with bold formatting should still match."""
        from datetime import date
        vault = tmp_path / "vault"
        journal = vault / "journal"
        journal.mkdir(parents=True)
        today = date.today().strftime("%Y-%m-%d")
        note = journal / f"{today}.md"
        note.write_text("# **Log**\n* entry\n", encoding="utf-8")
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._vault_root",
            lambda: vault,
        )
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {"obsidian": {"journal_dir": "journal"}},
        )
        from work_buddy.health.requirement_checks import check_log_section
        result = check_log_section()
        assert result["ok"] is True

    def test_check_daily_note_not_yet_created(self, monkeypatch, tmp_path):
        vault = tmp_path / "vault"
        (vault / "journal").mkdir(parents=True)
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._vault_root",
            lambda: vault,
        )
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {"obsidian": {"journal_dir": "journal"}},
        )
        from work_buddy.health.requirement_checks import check_log_section
        result = check_log_section()
        assert result["ok"] is False
        assert "does not exist" in result["detail"]

    def test_check_tasks_plugin_pass(self, monkeypatch, tmp_path):
        vault = tmp_path / "vault"
        obs = vault / ".obsidian"
        obs.mkdir(parents=True)
        cp = obs / "community-plugins.json"
        cp.write_text(json.dumps(["obsidian-tasks-plugin", "datacore"]), encoding="utf-8")
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._vault_root",
            lambda: vault,
        )
        from work_buddy.health.requirement_checks import check_tasks_plugin
        result = check_tasks_plugin()
        assert result["ok"] is True

    def test_check_tasks_plugin_missing(self, monkeypatch, tmp_path):
        vault = tmp_path / "vault"
        obs = vault / ".obsidian"
        obs.mkdir(parents=True)
        cp = obs / "community-plugins.json"
        cp.write_text(json.dumps(["datacore"]), encoding="utf-8")
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._vault_root",
            lambda: vault,
        )
        from work_buddy.health.requirement_checks import check_tasks_plugin
        result = check_tasks_plugin()
        assert result["ok"] is False

    def test_check_master_task_list_pass(self, monkeypatch, tmp_path):
        vault = tmp_path / "vault"
        (vault / "tasks").mkdir(parents=True)
        (vault / "tasks" / "master-task-list.md").write_text("# Tasks\n", encoding="utf-8")
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._vault_root",
            lambda: vault,
        )
        from work_buddy.health.requirement_checks import check_master_task_list
        result = check_master_task_list()
        assert result["ok"] is True

    def test_check_master_task_list_missing(self, monkeypatch, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._vault_root",
            lambda: vault,
        )
        from work_buddy.health.requirement_checks import check_master_task_list
        result = check_master_task_list()
        assert result["ok"] is False

    def test_check_telegram_bot_token_env(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {"telegram": {"bot_token_env": "TELEGRAM_BOT_TOKEN"}},
        )
        from work_buddy.health.requirement_checks import check_telegram_bot_token
        result = check_telegram_bot_token()
        assert result["ok"] is True

    def test_check_telegram_bot_token_dotenv(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._repo_root",
            lambda: tmp_path,
        )
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {"telegram": {"bot_token_env": "TELEGRAM_BOT_TOKEN"}},
        )
        env_file = tmp_path / ".env"
        env_file.write_text("TELEGRAM_BOT_TOKEN=123:ABC\n", encoding="utf-8")
        from work_buddy.health.requirement_checks import check_telegram_bot_token
        result = check_telegram_bot_token()
        assert result["ok"] is True

    def test_check_telegram_bot_token_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._repo_root",
            lambda: tmp_path,
        )
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {"telegram": {"bot_token_env": "TELEGRAM_BOT_TOKEN"}},
        )
        from work_buddy.health.requirement_checks import check_telegram_bot_token
        result = check_telegram_bot_token()
        assert result["ok"] is False

    def test_check_daily_notes_plugin_migration_format(self, monkeypatch, tmp_path):
        """core-plugins-migration.json uses {name: bool} format."""
        vault = tmp_path / "vault"
        obs = vault / ".obsidian"
        obs.mkdir(parents=True)
        (obs / "core-plugins-migration.json").write_text(
            json.dumps({"daily-notes": True, "file-recovery": True}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._vault_root",
            lambda: vault,
        )
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {},
        )
        from work_buddy.health.requirement_checks import check_daily_notes_plugin
        result = check_daily_notes_plugin()
        assert result["ok"] is True

    def test_check_daily_notes_plugin_disabled(self, monkeypatch, tmp_path):
        vault = tmp_path / "vault"
        obs = vault / ".obsidian"
        obs.mkdir(parents=True)
        (obs / "core-plugins-migration.json").write_text(
            json.dumps({"daily-notes": False}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._vault_root",
            lambda: vault,
        )
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {},
        )
        from work_buddy.health.requirement_checks import check_daily_notes_plugin
        result = check_daily_notes_plugin()
        assert result["ok"] is False

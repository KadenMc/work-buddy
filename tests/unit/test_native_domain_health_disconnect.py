"""Health/setup must detach from retired Markdown after native cutover."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


JOURNAL_REQUIREMENTS = {
    "obsidian/daily-note/plugin-enabled",
    "obsidian/daily-note/dir-exists",
    "obsidian/daily-note/log-section",
    "obsidian/daily-note/sign-in-section",
    "obsidian/daily-note/running-notes-section",
}
OTHER_RETIRED_REQUIREMENTS = {
    "core/config/projects-markdown-dir",
    "obsidian/contracts/dir-exists",
    "obsidian/knowledge/personal-path",
}


def _disable_retired_compatibility(monkeypatch) -> None:
    from work_buddy.health import requirement_checks as checks

    monkeypatch.setattr(
        checks, "journal_markdown_compatibility_required", lambda: False
    )
    monkeypatch.setattr(
        checks, "projects_markdown_compatibility_required", lambda: False
    )
    monkeypatch.setattr(
        checks, "contracts_markdown_compatibility_required", lambda: False
    )
    monkeypatch.setattr(
        checks,
        "personal_knowledge_markdown_compatibility_required",
        lambda: False,
    )


def test_retired_requirements_declare_domain_authority_predicates() -> None:
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY

    expected = {
        **{
            requirement_id: (
                "work_buddy.health.requirement_checks."
                "journal_markdown_compatibility_required"
            )
            for requirement_id in JOURNAL_REQUIREMENTS
        },
        "core/config/projects-markdown-dir": (
            "work_buddy.health.requirement_checks."
            "projects_markdown_compatibility_required"
        ),
        "obsidian/contracts/dir-exists": (
            "work_buddy.health.requirement_checks."
            "contracts_markdown_compatibility_required"
        ),
        "obsidian/knowledge/personal-path": (
            "work_buddy.health.requirement_checks."
            "personal_knowledge_markdown_compatibility_required"
        ),
    }
    assert {
        requirement_id: REQUIREMENT_REGISTRY[requirement_id].applies_fn
        for requirement_id in expected
    } == expected


def test_native_health_sweeps_and_checks_never_inspect_retired_roots(
    monkeypatch,
) -> None:
    from work_buddy.health import requirement_checks as checks
    from work_buddy.health.requirements import (
        REQUIREMENT_REGISTRY,
        RequirementChecker,
    )

    _disable_retired_compatibility(monkeypatch)
    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted", lambda _component: True
    )

    def touched_vault() -> Path:
        raise AssertionError("native health inspected a retired vault root")

    monkeypatch.setattr(checks, "_vault_root", touched_vault)
    assert RequirementChecker().check_group("journal") == []
    assert RequirementChecker().check_group("contracts") == []
    assert RequirementChecker().check_group("knowledge") == []
    assert not RequirementChecker().is_applicable(
        REQUIREMENT_REGISTRY["core/config/projects-markdown-dir"]
    )

    direct_results = (
        checks.check_daily_notes_plugin(),
        checks.check_journal_dir(),
        checks.check_log_section(),
        checks.check_sign_in_section(),
        checks.check_running_notes_section(),
        checks.check_contracts_dir(),
        checks.check_personal_knowledge_path(),
    )
    assert all(result["ok"] is True for result in direct_results)
    assert all("not inspected" in result["detail"] for result in direct_results)


def test_stale_fix_ids_and_direct_fixers_are_noops_after_seal(monkeypatch) -> None:
    from work_buddy import session_launcher
    from work_buddy.control.fix_runner import run_fix
    from work_buddy.health import fixers

    _disable_retired_compatibility(monkeypatch)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("retired archive fixer was invoked")

    monkeypatch.setattr(fixers, "_vault_root", forbidden)
    monkeypatch.setattr(fixers, "_journal_dir", forbidden)
    monkeypatch.setattr(session_launcher, "begin_session", forbidden)

    for requirement_id in sorted(JOURNAL_REQUIREMENTS | OTHER_RETIRED_REQUIREMENTS):
        result = run_fix(requirement_id)
        assert result["ok"] is True
        assert result["side_effects"] == []
        assert result["spawned"] is None
        assert "not applicable" in result["detail"]

    direct_results = (
        fixers.fix_journal_dir(),
        fixers.fix_log_section(),
        fixers.fix_sign_in_section(),
        fixers.fix_running_notes_section(),
        fixers.fix_contracts_dir(),
        fixers.fix_personal_knowledge_dir(),
        fixers.fix_projects_markdown_dir(path="retired/projects"),
    )
    assert all(result["ok"] is True for result in direct_results)
    assert all(result["side_effects"] == [] for result in direct_results)


def test_incomplete_installed_latch_never_reenables_missing_legacy_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from work_buddy.health import requirement_checks as checks
    from work_buddy.installed_authority import (
        InstalledAuthorityError,
        prepare_domain_seal,
    )
    from work_buddy.journal_capture import authority as journal_authority

    targets = {
        "projects": tmp_path / "db" / "projects.db",
        "contracts": tmp_path / "db" / "contracts.db",
        "personal_knowledge": tmp_path / "db" / "personal_knowledge.db",
    }
    monkeypatch.setattr(
        checks, "_authority_database_path", lambda domain: targets[domain]
    )
    monkeypatch.setattr(checks, "_cfg", lambda: {})

    assert checks.projects_markdown_compatibility_required() is True
    assert checks.contracts_markdown_compatibility_required() is True
    assert checks.personal_knowledge_markdown_compatibility_required() is True

    for domain, target in targets.items():
        prepare_domain_seal(domain, target, cohort_id=f"{domain}-health-seal")

    assert checks.projects_markdown_compatibility_required() is False
    assert checks.contracts_markdown_compatibility_required() is False
    assert checks.personal_knowledge_markdown_compatibility_required() is False

    monkeypatch.setattr(
        journal_authority,
        "existing_authority_mode",
        MagicMock(
            side_effect=InstalledAuthorityError(
                "installed Journal authority seal is incomplete"
            )
        ),
    )
    assert checks.journal_markdown_compatibility_required() is False

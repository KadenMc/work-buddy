from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "configured, expected",
    [
        ({}, False),
        ({"backups": "misconfigured"}, False),
        ({"backups": {"github": "misconfigured"}}, False),
        ({"backups": {"github": {}}}, False),
        (
            {
                "backups": {
                    "github": {"allow_unencrypted_private_content": False}
                }
            },
            False,
        ),
        (
            {
                "backups": {
                    "github": {"allow_unencrypted_private_content": "true"}
                }
            },
            False,
        ),
        (
            {
                "backups": {
                    "github": {"allow_unencrypted_private_content": 1}
                }
            },
            False,
        ),
        (
            {
                "backups": {
                    "github": {"allow_unencrypted_private_content": True}
                }
            },
            True,
        ),
    ],
)
def test_remote_private_content_opt_in_is_strict_boolean(
    monkeypatch, configured, expected
) -> None:
    from work_buddy.backups import remote

    monkeypatch.setattr(remote, "load_config", lambda: configured)
    assert remote.remote_private_content_opted_in() is expected


@pytest.mark.parametrize(
    "configured",
    [{"backups": "misconfigured"}, {"backups": {"github": "misconfigured"}}],
)
def test_malformed_remote_config_is_treated_as_unconfigured(
    monkeypatch, configured
) -> None:
    from work_buddy.backups import remote

    monkeypatch.setattr(remote, "load_config", lambda: configured)
    assert remote.get_backup_repo() is None


def test_default_backup_stays_local_when_repo_exists_without_opt_in(
    monkeypatch,
) -> None:
    from work_buddy.backups import remote
    from work_buddy.mcp_server.ops import backups_ops

    observed: dict[str, object] = {}

    def fake_run(**kwargs):
        observed.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(remote, "get_backup_repo", lambda: "owner/private-backups")
    monkeypatch.setattr(remote, "remote_private_content_opted_in", lambda: False)
    monkeypatch.setattr(backups_ops, "_run_backup_with_remote_policy", fake_run)

    assert backups_ops.data_backup() == {"status": "ok"}
    assert observed == {
        "manual": False,
        "push_remote": False,
        "repo": "owner/private-backups",
        "local_only_reason": "private_content_opt_in_required",
    }


def test_default_backup_uses_persistent_private_content_opt_in(monkeypatch) -> None:
    from work_buddy.backups import remote
    from work_buddy.mcp_server.ops import backups_ops

    observed: dict[str, object] = {}

    def fake_run(**kwargs):
        observed.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(remote, "get_backup_repo", lambda: "owner/private-backups")
    monkeypatch.setattr(remote, "remote_private_content_opted_in", lambda: True)
    monkeypatch.setattr(backups_ops, "_run_backup_with_remote_policy", fake_run)

    assert backups_ops.data_backup() == {"status": "ok"}
    assert observed == {
        "manual": False,
        "push_remote": True,
        "repo": "owner/private-backups",
        "local_only_reason": None,
    }


def test_explicit_false_overrides_persistent_opt_in(monkeypatch) -> None:
    from work_buddy.backups import remote
    from work_buddy.mcp_server.ops import backups_ops

    observed: dict[str, object] = {}

    def fake_run(**kwargs):
        observed.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(remote, "get_backup_repo", lambda: "owner/private-backups")
    monkeypatch.setattr(remote, "remote_private_content_opted_in", lambda: True)
    monkeypatch.setattr(backups_ops, "_run_backup_with_remote_policy", fake_run)

    backups_ops.data_backup(manual=True, push_remote=False)
    assert observed == {
        "manual": True,
        "push_remote": False,
        "repo": "owner/private-backups",
        "local_only_reason": "remote_push_explicitly_disabled",
    }


def test_non_boolean_remote_override_fails_closed(monkeypatch) -> None:
    from work_buddy.backups import remote
    from work_buddy.mcp_server.ops import backups_ops

    monkeypatch.setattr(remote, "get_backup_repo", lambda: "owner/private-backups")
    monkeypatch.setattr(remote, "remote_private_content_opted_in", lambda: True)
    with pytest.raises(TypeError, match="push_remote"):
        backups_ops.data_backup(push_remote="true")  # type: ignore[arg-type]


def test_explicit_remote_request_requires_exact_consent_before_snapshot(
    monkeypatch,
) -> None:
    from work_buddy.backups import remote
    from work_buddy.consent import (
        ConsentRequired,
        get_consent_metadata,
        per_invocation_authorization,
    )
    from work_buddy.mcp_server.ops import backups_ops

    calls: list[dict[str, object]] = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "remote": {"status": "ok"}}

    repo = "owner/private-backups"
    monkeypatch.setattr(remote, "get_backup_repo", lambda: repo)
    monkeypatch.setattr(remote, "remote_private_content_opted_in", lambda: False)
    monkeypatch.setattr(backups_ops, "_run_backup_with_remote_policy", fake_run)

    with pytest.raises(ConsentRequired) as blocked:
        backups_ops.data_backup(manual=True, push_remote=True)
    assert calls == []
    assert blocked.value.operation == "backup.remote_private_content_upload"
    assert blocked.value.risk == "high"
    assert blocked.value.grant_policy == "per_invocation"
    assert blocked.value.context["repo"] == repo
    assert blocked.value.context["archive_encryption"] == "none"
    assert "contracts" in blocked.value.context["vital_databases"]
    assert "personal_knowledge" in blocked.value.context["vital_databases"]
    metadata = get_consent_metadata(blocked.value.operation)
    assert metadata is not None
    assert metadata["consent_weight"] == "high"
    assert metadata["grant_policy"] == "per_invocation"

    with per_invocation_authorization(
        blocked.value.operation,
        blocked.value.fingerprint,
        request_id="backup-consent-request",
        response_surface="test",
        context=blocked.value.context,
    ):
        result = backups_ops.data_backup(manual=True, push_remote=True)

    assert result["remote"]["status"] == "ok"
    assert calls == [
        {"manual": True, "push_remote": True, "repo": repo}
    ]


def test_local_policy_never_calls_remote_and_records_reason(
    monkeypatch, tmp_path
) -> None:
    from work_buddy.backups import local, remote
    from work_buddy.mcp_server.ops import backups_ops

    snapshot_dir = tmp_path / "snap-2026-08-27T12-00-00Z"
    snapshot_dir.mkdir()
    written: list[dict[str, object]] = []

    monkeypatch.setattr(
        local,
        "run_backup",
        lambda *, manual: {
            "status": "ok",
            "snapshot_id": snapshot_dir.name,
            "tarball_path": str(snapshot_dir / "work-buddy-backup.tar.gz"),
            "manual": manual,
        },
    )
    monkeypatch.setattr(
        remote,
        "push_snapshot",
        lambda *args, **kwargs: pytest.fail("local-only backup attempted upload"),
    )
    monkeypatch.setattr(
        remote,
        "prune_remote_snapshots",
        lambda *args, **kwargs: pytest.fail("local-only backup pruned remote"),
    )
    monkeypatch.setattr(remote, "write_last_run", lambda payload: written.append(payload))

    result = backups_ops._run_backup_with_remote_policy(
        manual=False,
        push_remote=False,
        repo="owner/private-backups",
        local_only_reason="private_content_opt_in_required",
    )

    assert result["remote"] == {
        "status": "local_only",
        "reason": "private_content_opt_in_required",
    }
    assert written[0]["status"] == "ok"
    assert written[0]["remote"] == result["remote"]


def test_remote_policy_pins_authorized_repository(monkeypatch, tmp_path) -> None:
    from work_buddy.backups import local, remote
    from work_buddy.mcp_server.ops import backups_ops

    snapshot_dir = tmp_path / "snap-2026-08-27T12-00-00Z"
    snapshot_dir.mkdir()
    pushed: list[tuple[Path, str | None]] = []
    pruned: list[str | None] = []

    monkeypatch.setattr(
        local,
        "run_backup",
        lambda *, manual: {
            "status": "ok",
            "snapshot_id": snapshot_dir.name,
            "tarball_path": str(snapshot_dir / "work-buddy-backup.tar.gz"),
            "manual": manual,
        },
    )

    def push(path: Path, *, repo: str | None = None):
        pushed.append((path, repo))
        return {"status": "ok", "repo": repo}

    monkeypatch.setattr(remote, "push_snapshot", push)
    monkeypatch.setattr(
        remote,
        "prune_remote_snapshots",
        lambda repo=None: pruned.append(repo) or {"status": "ok", "pruned": []},
    )
    monkeypatch.setattr(remote, "write_last_run", lambda payload: None)

    backups_ops._run_backup_with_remote_policy(
        manual=True,
        push_remote=True,
        repo="owner/private-backups",
    )

    assert pushed == [(snapshot_dir, "owner/private-backups")]
    assert pruned == ["owner/private-backups"]


def test_low_level_remote_push_fails_closed_without_policy_authorization(
    monkeypatch, tmp_path
) -> None:
    from work_buddy.backups import remote

    snapshot_dir = tmp_path / "snap-2026-08-27T12-00-00Z"
    snapshot_dir.mkdir()
    (snapshot_dir / remote.BACKUP_FILENAME).write_bytes(b"archive")
    monkeypatch.setattr(remote, "remote_private_content_opted_in", lambda: False)
    monkeypatch.setattr(
        remote,
        "_run_gh",
        lambda *args, **kwargs: pytest.fail("privacy-blocked push reached gh"),
    )

    result = remote.push_snapshot(snapshot_dir, repo="owner/private-backups")
    assert result["status"] == "privacy_blocked"
    assert result["repo"] == "owner/private-backups"


def test_github_backup_component_requires_private_content_opt_in(monkeypatch) -> None:
    from work_buddy.backups import remote
    from work_buddy.health import components, requirement_checks

    component = components.COMPONENT_CATALOG["github_backups"]
    assert "integrations/github_backups/private-content-opt-in" in component.requirements

    monkeypatch.setattr(remote, "remote_private_content_opted_in", lambda: False)
    result = requirement_checks.check_backup_private_content_opt_in()
    assert result["ok"] is False
    assert "allow_unencrypted_private_content" in result["detail"]

    monkeypatch.setattr(remote, "remote_private_content_opted_in", lambda: True)
    assert requirement_checks.check_backup_private_content_opt_in()["ok"] is True

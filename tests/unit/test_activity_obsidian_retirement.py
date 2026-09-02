from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path

import pytest


def _window() -> tuple[datetime, datetime]:
    now = datetime.now()
    return now - timedelta(days=1), now + timedelta(days=1)


def test_deep_activity_opt_out_stops_before_config_bridge_or_vault_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy import activity

    def forbidden(*_args, **_kwargs):
        raise AssertionError("retired Obsidian activity dependency was touched")

    monkeypatch.setattr(activity, "_obsidian_activity_enabled", lambda: False)
    monkeypatch.setattr("work_buddy.config.load_config", forbidden)
    monkeypatch.setattr(activity, "_get_ledger_recent", forbidden)
    monkeypatch.setattr(activity, "_get_ktr_scores", forbidden)

    since, until = _window()
    assert activity._collect_vault_events(since, until) == []


def test_deep_activity_filters_all_sealed_domains_from_ledger_and_ktr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy import activity

    vault = tmp_path / "vault"
    sealed = (
        vault / "Daily",
        vault / "work-buddy" / "projects",
        vault / "work-buddy" / "contracts",
        vault / "Meta" / "WorkBuddy",
    )
    cfg = {
        "vault_root": str(vault),
        "obsidian": {"exclude_folders": ["ignored"]},
    }
    since, until = _window()
    observed_excludes: list[str] = []

    def ledger(_since, _until, *, exclude_folders=None):
        observed_excludes.extend(exclude_folders or [])
        return [
            {"path": "Daily/2026-08-31.md", "ts": since},
            {"path": "work-buddy/projects/one.md", "ts": since},
            {"path": "work-buddy/contracts/one.md", "ts": since},
            {"path": "Meta/WorkBuddy/preference.md", "ts": since},
            {"path": "notes/open.md", "ts": since},
        ]

    monkeypatch.setattr(activity, "_obsidian_activity_enabled", lambda: True)
    monkeypatch.setattr("work_buddy.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "work_buddy.vault_index.authority_exclusions.sealed_legacy_roots",
        lambda _cfg, allow_default_data_root=True: sealed,
    )
    monkeypatch.setattr(activity, "_get_ledger_recent", ledger)
    monkeypatch.setattr(
        "work_buddy.obsidian.ktr.get_hot_files",
        lambda **_kwargs: {
            "files": [
                {
                    "filePath": "Daily/2026-08-31.md",
                    "hot_score": 99,
                    "active_days": 10,
                    "total_buckets": 20,
                    "total_word_delta": 5000,
                },
                {
                    "filePath": "notes/open.md",
                    "hot_score": 7,
                    "active_days": 2,
                    "total_buckets": 3,
                    "total_word_delta": 42,
                },
            ]
        },
    )

    events = activity._collect_vault_events(since, until)

    assert [event.summary for event in events] == ["notes/open.md"]
    assert events[0].metadata["hot_score"] == 7
    assert set(observed_excludes) >= {
        "ignored",
        "journal",
        "Daily",
        "work-buddy/projects",
        "work-buddy/contracts",
        "Meta/WorkBuddy",
    }


def test_deep_activity_fallback_prunes_sealed_roots_before_file_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy import activity

    vault = tmp_path / "vault"
    sealed = (
        vault / "Daily",
        vault / "work-buddy" / "projects",
        vault / "work-buddy" / "contracts",
        vault / "Meta" / "WorkBuddy",
    )
    for root in sealed:
        root.mkdir(parents=True, exist_ok=True)
        (root / "must-not-be-read.md").write_text("private archive", encoding="utf-8")
    safe = vault / "notes" / "open.md"
    safe.parent.mkdir(parents=True)
    safe.write_text("open note", encoding="utf-8")

    cfg = {"vault_root": str(vault), "obsidian": {"exclude_folders": []}}
    original_stat = Path.stat
    sealed_names = tuple(os.path.normcase(os.path.abspath(str(root))) for root in sealed)

    def guarded_stat(path: Path, *args, **kwargs):
        candidate = os.path.normcase(os.path.abspath(str(path)))
        if path.suffix == ".md" and any(
            os.path.commonpath((candidate, root)) == root for root in sealed_names
        ):
            raise AssertionError(f"sealed archive file was inspected: {path}")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(activity, "_obsidian_activity_enabled", lambda: True)
    monkeypatch.setattr("work_buddy.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "work_buddy.vault_index.authority_exclusions.sealed_legacy_roots",
        lambda _cfg, allow_default_data_root=True: sealed,
    )
    monkeypatch.setattr(activity, "_get_ledger_recent", lambda *_a, **_k: None)
    monkeypatch.setattr(activity, "_get_ktr_scores", lambda *_a, **_k: {})
    monkeypatch.setattr(Path, "stat", guarded_stat)

    since, until = _window()
    events = activity._collect_vault_events(since, until)

    assert [event.summary.replace("\\", "/") for event in events] == [
        "notes/open.md"
    ]

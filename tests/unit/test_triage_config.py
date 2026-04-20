"""Unit tests for feature-local triage config loader."""

from __future__ import annotations

from typing import Any

import pytest

from work_buddy.triage import config as triage_config


def test_defaults_present() -> None:
    cfg = triage_config.TRIAGE_DEFAULTS
    assert "agent_profile" in cfg
    assert cfg["segment"]["max_tokens"] >= 1024
    assert cfg["enrich"]["top_k"] >= 1
    assert "journal_triage" in cfg["adapters"]


def test_load_triage_config_when_no_overrides(monkeypatch) -> None:
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {})
    cfg = triage_config.load_triage_config()
    assert cfg["segment"]["max_tokens"] == triage_config.TRIAGE_DEFAULTS["segment"]["max_tokens"]


def test_load_triage_config_deep_merges(monkeypatch) -> None:
    """User overrides in config.yaml should merge onto code defaults —
    unspecified keys keep their default, specified keys are replaced,
    nested dicts merge recursively."""
    override: dict[str, Any] = {
        "triage": {
            "segment": {"max_tokens": 16384},
            "enrich": {"enabled": False},
        },
    }
    monkeypatch.setattr("work_buddy.config.load_config", lambda: override)
    cfg = triage_config.load_triage_config()

    # Overridden
    assert cfg["segment"]["max_tokens"] == 16384
    assert cfg["enrich"]["enabled"] is False
    # Merged — temperature should survive at the default since only
    # max_tokens was overridden in the segment block
    assert "temperature" in cfg["segment"]
    # Untouched top-level
    assert cfg["agent_profile"] == triage_config.TRIAGE_DEFAULTS["agent_profile"]
    # Nested adapter defaults still present
    assert "journal_triage" in cfg["adapters"]


def test_resolve_profile_precedence() -> None:
    cfg = {
        "agent_profile": "default_p",
        "agent": {"profile": None},
        "segment": {"profile": "seg_p"},
    }
    # explicit override wins
    assert triage_config.resolve_profile(cfg, "agent", override="explicit") == "explicit"
    # stage-specific profile wins over top-level
    assert triage_config.resolve_profile(cfg, "segment") == "seg_p"
    # stage with None profile falls through to top-level
    assert triage_config.resolve_profile(cfg, "agent") == "default_p"


def test_resolve_profile_falls_back_when_missing() -> None:
    cfg: dict[str, Any] = {}
    assert triage_config.resolve_profile(cfg, "agent") == "local_general"


def test_adapter_config_returns_default_for_unknown_name() -> None:
    cfg = triage_config.load_triage_config()
    assert triage_config.adapter_config(cfg, "nonexistent_adapter") == {}
    assert triage_config.adapter_config(cfg, "journal_triage")["max_threads"] == 16

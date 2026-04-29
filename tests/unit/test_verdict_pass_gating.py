"""Per-source verdict-pass gating — resolution order tests.

Resolution rules:
  1. False if no config.
  2. ``triage.verdict_pass.enabled`` is the global default.
  3. ``triage.verdict_pass.sources.<source>.enabled`` overrides the
     global default for that source — explicit ``true`` AND explicit
     ``false`` win.
  4. Unknown source keys fall through to the global default (a typo
     never silently disables a real capability).

Covers all four call sites that look up the gate today:
  ``journal``, ``inline``, ``email``. The reserved ``web_browser`` key
  is checked structurally (no capability reads it yet, but the helper
  still resolves it).
"""

from __future__ import annotations

import pytest

from work_buddy.triage.config import (
    KNOWN_VERDICT_PASS_SOURCES,
    is_verdict_pass_enabled_for,
)


def test_empty_config_returns_false():
    assert is_verdict_pass_enabled_for({}, "journal") is False
    assert is_verdict_pass_enabled_for({}, "email") is False


def test_global_enabled_propagates_to_all_sources():
    cfg = {"verdict_pass": {"enabled": True}}
    for src in ("journal", "inline", "email", "web_browser"):
        assert is_verdict_pass_enabled_for(cfg, src) is True, src


def test_global_disabled_with_per_source_enabled():
    cfg = {
        "verdict_pass": {
            "enabled": False,
            "sources": {"journal": {"enabled": True}},
        },
    }
    assert is_verdict_pass_enabled_for(cfg, "journal") is True
    assert is_verdict_pass_enabled_for(cfg, "email") is False
    assert is_verdict_pass_enabled_for(cfg, "inline") is False


def test_per_source_disabled_beats_global_enabled():
    cfg = {
        "verdict_pass": {
            "enabled": True,
            "sources": {"email": {"enabled": False}},
        },
    }
    assert is_verdict_pass_enabled_for(cfg, "email") is False
    assert is_verdict_pass_enabled_for(cfg, "journal") is True


def test_unknown_source_falls_through_to_global():
    cfg_on = {"verdict_pass": {"enabled": True}}
    cfg_off = {"verdict_pass": {"enabled": False}}
    assert is_verdict_pass_enabled_for(cfg_on, "totally_made_up") is True
    assert is_verdict_pass_enabled_for(cfg_off, "totally_made_up") is False


def test_per_source_with_no_enabled_key_falls_through():
    """An empty per-source block (e.g. user wrote
    ``sources: {email: {}}`` with no ``enabled`` key) falls through to
    the global. Explicit absence is not the same as explicit false."""
    cfg = {"verdict_pass": {"enabled": True, "sources": {"email": {}}}}
    assert is_verdict_pass_enabled_for(cfg, "email") is True


def test_known_sources_table_covers_current_capabilities():
    """The KNOWN_VERDICT_PASS_SOURCES dict documents the currently-wired
    config keys. If a new triage capability ships a verdict pass without
    adding its key here, this test fails — keeps the docs honest."""
    expected = {"journal", "inline", "email", "web_browser"}
    assert set(KNOWN_VERDICT_PASS_SOURCES) == expected


def test_falsy_non_bool_per_source_value_handled():
    """If a user (or test) sets the value to None or 0, treat it as
    'no override' — fall through to global. Avoids a None-vs-False trap."""
    cfg = {
        "verdict_pass": {
            "enabled": True,
            "sources": {"journal": None},  # malformed — fall through
        },
    }
    # Falls through to global True. The ``or {}`` in the helper makes
    # None safe.
    assert is_verdict_pass_enabled_for(cfg, "journal") is True


def test_explicit_false_per_source_when_global_unset():
    """Per-source explicit false works even when there's no global key."""
    cfg = {"verdict_pass": {"sources": {"email": {"enabled": False}}}}
    assert is_verdict_pass_enabled_for(cfg, "email") is False
    # Other sources still default to False (no global, no override).
    assert is_verdict_pass_enabled_for(cfg, "journal") is False


def test_empty_verdict_pass_block():
    cfg = {"verdict_pass": {}}
    assert is_verdict_pass_enabled_for(cfg, "journal") is False

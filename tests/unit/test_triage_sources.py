"""Unit tests for the triage source descriptor registry (Slice 1)."""

from __future__ import annotations

import pytest

from work_buddy.triage import sources
from work_buddy.triage.sources import (
    KNOWN_TRIGGERS,
    SourceDescriptor,
    TRIGGER_SOURCE_REMOVED,
    TRIGGER_TAG_REMOVED,
    _build_registry,
    all_descriptors,
    get_descriptor,
    register_source,
    reset_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Drop the module's cached registry between tests."""
    reset_for_tests()
    yield
    reset_for_tests()


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults_present(monkeypatch):
    """The three core sources ship with descriptors out of the box."""
    monkeypatch.setattr(
        "work_buddy.config.load_config", lambda: {}
    )
    descriptors = {d.name: d for d in all_descriptors()}
    assert "journal_thread" in descriptors
    assert "chrome_tab" in descriptors
    assert "inline" in descriptors


def test_inline_default_has_no_ttl(monkeypatch):
    """Inline TTL is intentionally ``None`` per Slice 1 design.

    User-affirmative captures should not auto-expire — this is the
    decision behind inline ttl_days=null.
    """
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {})
    inline = get_descriptor("inline")
    assert inline is not None
    assert inline.ttl_days is None


def test_journal_default_has_edit_match(monkeypatch):
    """Journal descriptor's config carries the cosine-equivalent threshold."""
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {})
    journal = get_descriptor("journal_thread")
    assert journal is not None
    assert journal.config.get("edit_match_threshold") == 0.85
    assert "source_edited_beyond_match" in journal.quarantine_triggers


def test_chrome_default_no_edit_check(monkeypatch):
    """Chrome only quarantines on tab-gone, not on edit-match."""
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {})
    chrome = get_descriptor("chrome_tab")
    assert chrome is not None
    assert chrome.quarantine_triggers == [TRIGGER_SOURCE_REMOVED]


# ---------------------------------------------------------------------------
# Override merge
# ---------------------------------------------------------------------------


def test_user_override_extends_ttl(monkeypatch):
    """Overrides under triage.pool.sources merge on top of defaults."""
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {
            "triage": {
                "pool": {
                    "sources": {
                        "journal_thread": {"ttl_days": 14},
                    }
                }
            }
        },
    )
    journal = get_descriptor("journal_thread")
    assert journal is not None
    assert journal.ttl_days == 14
    # Other defaults still in place
    assert journal.config.get("edit_match_threshold") == 0.85


def test_user_can_register_new_source(monkeypatch):
    """Adding a source via override creates a fresh descriptor."""
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {
            "triage": {
                "pool": {
                    "sources": {
                        "telegram": {
                            "ttl_days": 3,
                            "quarantine_triggers": ["source_removed"],
                            "config": {"channel": "default"},
                        }
                    }
                }
            }
        },
    )
    descriptor = get_descriptor("telegram")
    assert descriptor is not None
    assert descriptor.ttl_days == 3
    assert descriptor.quarantine_triggers == ["source_removed"]


def test_config_block_deep_merges(monkeypatch):
    """Override of one config key preserves the other defaults."""
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {
            "triage": {
                "pool": {
                    "sources": {
                        "inline": {
                            "config": {"capture_tag": "wb/saved"},
                        }
                    }
                }
            }
        },
    )
    inline = get_descriptor("inline")
    # Override applied, but ttl_days still None and triggers unchanged
    assert inline.config.get("capture_tag") == "wb/saved"
    assert inline.ttl_days is None
    assert inline.quarantine_triggers == [TRIGGER_SOURCE_REMOVED]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_unknown_trigger_rejected_at_construction():
    """Descriptors fail loudly on unknown trigger names."""
    with pytest.raises(ValueError, match="unknown quarantine trigger"):
        SourceDescriptor(
            name="bad",
            ttl_days=1,
            quarantine_triggers=["does_not_exist"],
        )


def test_known_triggers_set_complete():
    """Sanity: every named constant lives in KNOWN_TRIGGERS."""
    assert TRIGGER_SOURCE_REMOVED in KNOWN_TRIGGERS
    assert TRIGGER_TAG_REMOVED in KNOWN_TRIGGERS


def test_get_descriptor_unknown_returns_none(monkeypatch):
    """Unknown sources return None — the pool treats this as 'no TTL'."""
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {})
    assert get_descriptor("not_a_real_source") is None


# ---------------------------------------------------------------------------
# Runtime registration
# ---------------------------------------------------------------------------


def test_register_source_idempotent(monkeypatch):
    """Calling register twice replaces, doesn't duplicate."""
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {})
    # Force initial load so the cache exists
    get_descriptor("inline")
    register_source(SourceDescriptor(
        name="custom",
        ttl_days=10,
        quarantine_triggers=[],
    ))
    register_source(SourceDescriptor(
        name="custom",
        ttl_days=20,
        quarantine_triggers=[],
    ))
    assert get_descriptor("custom").ttl_days == 20

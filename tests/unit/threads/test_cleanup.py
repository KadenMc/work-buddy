"""v5 Stage 4.0 — cleanup adapter framework.

Pins the registry pattern, can_clean_up gating, and the
CleanupResult shape. The journal-note adapter lands in 4.4 with
its own tests.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import cleanup
from work_buddy.threads.cleanup import (
    CleanupAdapter,
    CleanupResult,
)
from work_buddy.threads.models import Thread


@pytest.fixture(autouse=True)
def _clear_adapters():
    cleanup.clear_cleanup_adapters()
    yield
    cleanup.clear_cleanup_adapters()


def _adapter(source="x", *, can=True, returns=None):
    return CleanupAdapter(
        source=source,
        can_clean_up=lambda t: can,
        cleanup=lambda t: returns or CleanupResult(success=True),
        description=f"adapter for {source}",
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_then_lookup(self):
        a = _adapter("journal_note")
        cleanup.register_cleanup_adapter(a)
        assert cleanup.get_cleanup_adapter("journal_note") is a

    def test_unknown_source_returns_none(self):
        assert cleanup.get_cleanup_adapter("nonexistent") is None

    def test_re_register_replaces(self):
        cleanup.register_cleanup_adapter(_adapter("foo", can=True))
        cleanup.register_cleanup_adapter(_adapter("foo", can=False))
        # Lookup returns the second one
        a = cleanup.get_cleanup_adapter("foo")
        t = Thread(inciting_event_summary={"source": "foo"})
        assert a.can_clean_up(t) is False

    def test_clear_drops_all(self):
        cleanup.register_cleanup_adapter(_adapter("a"))
        cleanup.register_cleanup_adapter(_adapter("b"))
        cleanup.clear_cleanup_adapters()
        assert cleanup.registered_sources() == []

    def test_registered_sources_returns_sorted_list(self):
        cleanup.register_cleanup_adapter(_adapter("z"))
        cleanup.register_cleanup_adapter(_adapter("a"))
        cleanup.register_cleanup_adapter(_adapter("m"))
        assert cleanup.registered_sources() == ["a", "m", "z"]


# ---------------------------------------------------------------------------
# find_cleanup_adapter
# ---------------------------------------------------------------------------


class TestFindAdapter:
    def test_finds_by_inciting_source(self):
        cleanup.register_cleanup_adapter(_adapter("journal_note"))
        t = Thread(inciting_event_summary={"source": "journal_note"})
        assert cleanup.find_cleanup_adapter(t) is not None

    def test_returns_none_for_thread_without_summary(self):
        t = Thread()  # no inciting_event_summary
        assert cleanup.find_cleanup_adapter(t) is None

    def test_returns_none_for_unregistered_source(self):
        t = Thread(inciting_event_summary={"source": "nonexistent"})
        assert cleanup.find_cleanup_adapter(t) is None


# ---------------------------------------------------------------------------
# can_clean_up
# ---------------------------------------------------------------------------


class TestCanCleanUp:
    def test_true_when_adapter_says_yes(self):
        cleanup.register_cleanup_adapter(_adapter("foo", can=True))
        t = Thread(inciting_event_summary={"source": "foo"})
        assert cleanup.can_clean_up(t) is True

    def test_false_when_adapter_says_no(self):
        cleanup.register_cleanup_adapter(_adapter("foo", can=False))
        t = Thread(inciting_event_summary={"source": "foo"})
        assert cleanup.can_clean_up(t) is False

    def test_false_when_no_adapter_registered(self):
        t = Thread(inciting_event_summary={"source": "unknown"})
        assert cleanup.can_clean_up(t) is False

    def test_false_when_adapter_raises(self):
        def boom(t):
            raise RuntimeError("oops")
        cleanup.register_cleanup_adapter(CleanupAdapter(
            source="boom",
            can_clean_up=boom,
            cleanup=lambda t: CleanupResult(success=True),
        ))
        t = Thread(inciting_event_summary={"source": "boom"})
        assert cleanup.can_clean_up(t) is False


# ---------------------------------------------------------------------------
# perform_cleanup
# ---------------------------------------------------------------------------


class TestPerformCleanup:
    def test_returns_adapter_result(self):
        cleanup.register_cleanup_adapter(_adapter(
            "foo",
            returns=CleanupResult(success=True, detail="ok"),
        ))
        t = Thread(inciting_event_summary={"source": "foo"})
        r = cleanup.perform_cleanup(t)
        assert r.success is True
        assert r.detail == "ok"

    def test_returns_failure_for_unregistered_source(self):
        t = Thread(inciting_event_summary={"source": "unknown"})
        r = cleanup.perform_cleanup(t)
        assert r.success is False
        assert "no cleanup adapter" in r.detail.lower()

    def test_returns_failure_when_adapter_raises(self):
        def boom(t):
            raise ValueError("boom!")
        cleanup.register_cleanup_adapter(CleanupAdapter(
            source="bad",
            can_clean_up=lambda t: True,
            cleanup=boom,
        ))
        t = Thread(inciting_event_summary={"source": "bad"})
        r = cleanup.perform_cleanup(t)
        assert r.success is False
        assert "ValueError" in r.detail
        assert "boom!" in r.detail

    def test_source_already_gone_is_success(self):
        cleanup.register_cleanup_adapter(_adapter(
            "vanished",
            returns=CleanupResult(success=True, source_already_gone=True),
        ))
        t = Thread(inciting_event_summary={"source": "vanished"})
        r = cleanup.perform_cleanup(t)
        assert r.success is True
        assert r.source_already_gone is True

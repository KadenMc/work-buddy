"""Tests for ``embedding.client.wait_until_available``.

Cold-start race fix from 2026-05-04: the knowledge-dense warmup
thread previously fired embed batches before the embedding service
finished its first model load, producing
``Embedding service unavailable during knowledge alias dense build``
warnings on every cold sidecar start. The fix is a polling wait
that blocks until ``/health`` reports ``status: ok``.

These tests monkeypatch the underlying ``_request`` so we don't need
the live service to be up.
"""

from __future__ import annotations

import time

import pytest

from work_buddy.embedding import client


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    """Make ``time.sleep`` a no-op so the wait loop turns over instantly.

    The wait helper uses ``time.monotonic()`` for the deadline check, so
    we still need the deadline to advance. Patch monotonic to advance
    by the requested sleep interval each call so the loop is bounded.
    """
    state = {"now": 1000.0}

    def fake_sleep(s):
        state["now"] += s

    def fake_monotonic():
        return state["now"]

    monkeypatch.setattr(client.time, "sleep", fake_sleep)
    monkeypatch.setattr(client.time, "monotonic", fake_monotonic)
    yield state


def _make_health_responder(sequence):
    """Build a fake ``_request`` that returns the next dict from
    ``sequence`` on each call. Once the sequence is exhausted, the
    last response is returned indefinitely.

    Each entry in ``sequence`` is a dict (e.g., ``{"status": "loading"}``)
    or ``None`` (simulates a connection failure).
    """
    state = {"i": 0}

    def fake_request(method, path, data=None, timeout=30):
        i = state["i"]
        if i < len(sequence) - 1:
            state["i"] += 1
        return sequence[i]

    return fake_request


# ---------------------------------------------------------------------------
# is_available — sanity (the wait wraps it)
# ---------------------------------------------------------------------------


def test_is_available_true_on_ok(monkeypatch):
    monkeypatch.setattr(
        client, "_request",
        _make_health_responder([{"status": "ok"}]),
    )
    assert client.is_available() is True


def test_is_available_false_on_loading(monkeypatch):
    monkeypatch.setattr(
        client, "_request",
        _make_health_responder([{"status": "loading"}]),
    )
    assert client.is_available() is False


def test_is_available_false_on_unreachable(monkeypatch):
    monkeypatch.setattr(client, "_request", lambda *a, **k: None)
    assert client.is_available() is False


# ---------------------------------------------------------------------------
# wait_until_available
# ---------------------------------------------------------------------------


class TestWaitUntilAvailable:
    def test_returns_true_immediately_when_already_ok(self, monkeypatch):
        monkeypatch.setattr(
            client, "_request",
            _make_health_responder([{"status": "ok"}]),
        )
        assert client.wait_until_available(timeout_s=10.0) is True

    def test_polls_until_ready(self, monkeypatch):
        # 3 loading responses then ok — wait must catch the transition.
        monkeypatch.setattr(
            client, "_request",
            _make_health_responder([
                {"status": "loading"},
                {"status": "loading"},
                {"status": "loading"},
                {"status": "ok"},
            ]),
        )
        assert client.wait_until_available(
            timeout_s=10.0, interval_s=0.5,
        ) is True

    def test_returns_false_on_timeout(self, monkeypatch):
        # Service stays in loading forever.
        monkeypatch.setattr(
            client, "_request",
            _make_health_responder([{"status": "loading"}]),
        )
        # 1.0s budget with 0.25s interval → ~4 polls then timeout.
        assert client.wait_until_available(
            timeout_s=1.0, interval_s=0.25,
        ) is False

    def test_returns_false_on_unreachable_service(self, monkeypatch):
        monkeypatch.setattr(client, "_request", lambda *a, **k: None)
        assert client.wait_until_available(
            timeout_s=1.0, interval_s=0.25,
        ) is False

    def test_zero_timeout_does_one_check(self, monkeypatch):
        monkeypatch.setattr(
            client, "_request",
            _make_health_responder([{"status": "ok"}]),
        )
        # Even with no headroom we should at least do the eager first check.
        assert client.wait_until_available(timeout_s=0.0) is True

    def test_recovers_after_brief_unreachable_then_ready(self, monkeypatch):
        # Realistic: service was unreachable for the first poll, then
        # came up loading, then ready. Wait should ride through both
        # transient states.
        monkeypatch.setattr(
            client, "_request",
            _make_health_responder([
                None,
                {"status": "loading"},
                {"status": "ok"},
            ]),
        )
        assert client.wait_until_available(
            timeout_s=10.0, interval_s=0.5,
        ) is True

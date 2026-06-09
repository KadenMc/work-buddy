"""Tests for ``local_embed_slot`` — broker admission for in-process embedding
encode — and the ``/embed`` priority derivation.

The broker's own priority/admission ordering is covered by
``test_local_inference_broker.py``; these tests pin the *wiring*: that all local
encode shares the one ``local:embedding`` profile, that admission degrades
gracefully (never blocks the default encode path), and that the encode role
maps to the right priority.
"""
from __future__ import annotations

import pytest

from work_buddy.inference import Priority, get_broker, local_embed_slot
from work_buddy.inference import local_slot as local_slot_mod
from work_buddy.inference.broker import _reset_broker_for_tests
from work_buddy.inference.local_slot import LOCAL_EMBED_PROFILE


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Each test gets a fresh broker singleton."""
    _reset_broker_for_tests()
    yield
    _reset_broker_for_tests()


def test_acquires_slot_on_shared_profile():
    """A successful slot yields a ticket on the local:embedding profile at the
    requested priority, and records a metrics row."""
    with local_embed_slot(Priority.BACKGROUND) as ticket:
        assert ticket is not None
        assert ticket.profile == LOCAL_EMBED_PROFILE
        assert ticket.priority == Priority.BACKGROUND

    rows = get_broker().snapshot_metrics()
    assert any(
        r["profile"] == LOCAL_EMBED_PROFILE and r["status"] == "ok" for r in rows
    ), f"expected an ok metrics row on {LOCAL_EMBED_PROFILE}; got {rows}"


def test_query_and_build_share_one_profile():
    """Interactive query and background build encode use the SAME profile — the
    whole point: the broker can only serialize+prioritize them on the one GPU if
    they share a queue."""
    with local_embed_slot(Priority.INTERACTIVE) as q:
        q_profile = q.profile
    with local_embed_slot(Priority.BACKGROUND) as b:
        b_profile = b.profile
    assert q_profile == b_profile == LOCAL_EMBED_PROFILE


def test_default_priority_is_background():
    with local_embed_slot() as ticket:
        assert ticket.priority == Priority.BACKGROUND


def test_degrades_to_none_when_broker_unavailable(monkeypatch):
    """If the broker can't be obtained, the encode still runs (ticket is None)."""
    def _boom():
        raise RuntimeError("no broker in this rig")

    monkeypatch.setattr(local_slot_mod, "get_broker", _boom)

    ran = False
    with local_embed_slot(Priority.INTERACTIVE) as ticket:
        assert ticket is None
        ran = True
    assert ran, "body must still execute under graceful degradation"


def test_degrades_to_none_on_admission_backpressure(monkeypatch):
    """A queue-wait timeout / queue-full on admission must not break the encode
    path — the slot yields None and the body proceeds."""
    from work_buddy.inference.broker import QueueWaitTimeout

    class _Boom:
        def slot(self, **_kwargs):
            raise QueueWaitTimeout("saturated", kind="queue_wait_timeout")

    monkeypatch.setattr(local_slot_mod, "get_broker", lambda: _Boom())

    ran = False
    with local_embed_slot(Priority.BACKGROUND) as ticket:
        assert ticket is None
        ran = True
    assert ran


def test_body_exception_propagates():
    """A failure inside the slot body propagates (admission is best-effort, but
    we don't swallow the caller's encode error)."""
    with pytest.raises(ValueError, match="boom"):
        with local_embed_slot(Priority.BACKGROUND):
            raise ValueError("boom")


# ---------------------------------------------------------------------------
# /embed priority derivation (role → priority)
# ---------------------------------------------------------------------------

def test_embed_priority_derives_from_prompt_name():
    from work_buddy.embedding.service import _embed_priority

    # Asymmetric roles map to scheduling intent.
    assert _embed_priority({}, "query") == Priority.INTERACTIVE
    assert _embed_priority({}, "document") == Priority.BACKGROUND
    # Symmetric (no prompt) defaults to WORKFLOW — preempts background, yields
    # to explicit interactive.
    assert _embed_priority({}, None) == Priority.WORKFLOW


def test_embed_priority_explicit_wins_and_bad_value_falls_through():
    from work_buddy.embedding.service import _embed_priority

    # Explicit priority overrides the role default.
    assert _embed_priority({"priority": "background"}, "query") == Priority.BACKGROUND
    # An unparseable explicit value falls through to the role derivation.
    assert _embed_priority({"priority": "nonsense"}, "query") == Priority.INTERACTIVE

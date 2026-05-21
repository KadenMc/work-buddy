"""Characterization tests for ``work_buddy.inference.broker``.

These pin broker behaviors that the resilience-framework broker *adapter*
(design stage A2) will depend on — the contract the adapter reads when it
clamps the inference budget to a propagating deadline, maps broker errors
onto the outcome taxonomy, and forwards slot metrics to the unified
surface. They complement ``test_local_inference_broker.py`` (which covers
admission / priority / queueing) by pinning the *observable surface* an
adapter consumes, not the scheduling internals.

If one of these fails after A2/B2, the adapter has changed broker-visible
behavior — investigate before assuming the test is stale.
"""

from __future__ import annotations

import pytest

from work_buddy.inference.broker import (
    InferenceTimeout,
    LocalInferenceBroker,
    Priority,
    ProfileConfig,
    QueueWaitTimeout,
    _reset_broker_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    _reset_broker_for_tests()
    yield
    _reset_broker_for_tests()


def _make_broker(**kwargs) -> LocalInferenceBroker:
    b = LocalInferenceBroker()
    b.configure_profile(ProfileConfig(name="test", **kwargs))
    return b


# ---------------------------------------------------------------------------
# BrokerError raised inside the slot body — metrics record its kind
# ---------------------------------------------------------------------------


def test_broker_error_raised_in_body_records_its_kind():
    """A ``BrokerError`` raised *inside* the slot body is recorded with the
    exception's own ``kind`` as the metrics status — distinct from the
    generic-exception path which records ``status="error"``.

    A2 relies on this: when the adapter raises ``InferenceTimeout`` after a
    deadline-clamped call overruns, the slot metric must carry
    ``inference_timeout``, not a generic ``error``.
    """
    b = _make_broker(max_concurrent=1)

    with pytest.raises(InferenceTimeout):
        with b.slot(profile="test"):
            raise InferenceTimeout(
                "downstream call exceeded inference_s",
                kind="inference_timeout",
            )

    m = b.snapshot_metrics()[-1]
    assert m["status"] == "inference_timeout"
    assert m["error_kind"] == "inference_timeout"
    assert "inference_s" in (m["error_detail"] or "")
    assert m["finished_at"] is not None

    # Slot was released despite the error — a follow-up admits immediately.
    with b.slot(profile="test"):
        pass


def test_generic_exception_in_body_distinct_from_broker_error():
    """A non-``BrokerError`` exception records ``status="error"`` with the
    exception class name — the contrasting branch to the test above. Pins
    that the two paths stay distinguishable for the taxonomy mapping.
    """
    b = _make_broker(max_concurrent=1)

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with b.slot(profile="test"):
            raise _Boom("unexpected")

    m = b.snapshot_metrics()[-1]
    assert m["status"] == "error"
    assert m["error_kind"] == "_Boom"


# ---------------------------------------------------------------------------
# Ticket budget fields — what the deadline-clamp adapter reads
# ---------------------------------------------------------------------------


def test_ticket_carries_explicit_budget_fields():
    """The ``Ticket`` exposes ``queue_wait_s`` and ``inference_s``. A2's
    adapter reads ``ticket.inference_s`` to clamp the HTTP-layer timeout to
    the propagating deadline's remaining budget.
    """
    b = _make_broker(max_concurrent=1)
    with b.slot(
        profile="test", queue_wait_s=7.0, inference_s=42.0,
    ) as ticket:
        assert ticket.queue_wait_s == 7.0
        assert ticket.inference_s == 42.0


def test_ticket_budget_defaults_come_from_profile_config():
    """When the caller omits the budgets, the ticket carries the profile's
    configured defaults — the adapter must see resolved values, never None.
    """
    b = _make_broker(
        max_concurrent=1,
        default_queue_wait_s=11.0,
        default_inference_s=99.0,
    )
    with b.slot(profile="test") as ticket:
        assert ticket.queue_wait_s == 11.0
        assert ticket.inference_s == 99.0


# ---------------------------------------------------------------------------
# SlotMetrics.to_dict contract — what the metrics surface forwards
# ---------------------------------------------------------------------------


def test_slot_metrics_to_dict_contract():
    """Pins the serialized metrics keys A2's metrics mapping reads. A
    missing or renamed key would silently drop a signal from the unified
    surface.
    """
    b = _make_broker(max_concurrent=1)
    with b.slot(profile="test", priority=Priority.INTERACTIVE) as ticket:
        ticket.mark_started_http()
        ticket.mark_first_token()

    m = b.snapshot_metrics()[-1]
    for key in (
        "id", "profile", "priority", "queued_at", "admitted_at",
        "started_http_at", "first_token_at", "finished_at", "status",
        "error_kind", "error_detail", "queue_wait_ms", "service_time_ms",
        "total_latency_ms",
    ):
        assert key in m, f"metrics dict missing expected key {key!r}"
    # Priority is serialized as its name, not the IntEnum value.
    assert m["priority"] == "INTERACTIVE"
    assert m["status"] == "ok"


def test_snapshot_metrics_limit_returns_most_recent():
    """``snapshot_metrics(limit=N)`` returns the last N rows in order —
    the dashboard / surface reads a bounded tail, not the whole ring.
    """
    b = _make_broker(max_concurrent=4)
    for _ in range(5):
        with b.slot(profile="test"):
            pass

    tail = b.snapshot_metrics(limit=2)
    assert len(tail) == 2
    full = b.snapshot_metrics()
    assert len(full) == 5
    assert [r["id"] for r in tail] == [r["id"] for r in full[-2:]]


# ---------------------------------------------------------------------------
# A metrics row exists even when admission never succeeds
# ---------------------------------------------------------------------------


def test_metrics_row_recorded_even_on_queue_wait_timeout():
    """The metrics record is created at queue time, before admission is
    attempted — so a ticket that times out waiting for a slot still
    produces a row. The surface must be able to see calls that never ran.
    """
    import threading

    b = _make_broker(max_concurrent=1)
    barrier = threading.Event()
    release = threading.Event()

    def _holder():
        with b.slot(profile="test"):
            barrier.set()
            release.wait(timeout=5.0)

    t = threading.Thread(target=_holder)
    t.start()
    assert barrier.wait(timeout=2.0)

    with pytest.raises(QueueWaitTimeout):
        with b.slot(profile="test", queue_wait_s=0.2):
            pass

    release.set()
    t.join(timeout=3.0)

    statuses = [m["status"] for m in b.snapshot_metrics()]
    assert "queue_wait_timeout" in statuses

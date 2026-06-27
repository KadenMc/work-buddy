"""Tests for the messaging context collector's digest framing.

The avoidance / priority heuristics must count only genuine correspondence
(actionable, from a human or another agent). Machine traffic — notification
acks, retry pings, system FYIs — is collapsed to one line and never reads as a
backlog the user is avoiding.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from work_buddy.collectors.message_collector import _format_summary, _is_machine


def _old(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _msg(**kw):
    base = {
        "type": "result",
        "sender": "work-buddy",
        "subject": "s",
        "priority": "normal",
        "disposition": "actionable",
        "created_at": _old(3),  # stale by default (>48h)
    }
    base.update(kw)
    return base


def test_is_machine_classification():
    assert _is_machine("notification-system")
    assert _is_machine("sidecar")
    assert _is_machine("sidecar:retry_queue")
    assert not _is_machine("work-buddy")
    assert not _is_machine("Owner")
    assert not _is_machine("electricrag")


def test_avoidance_signal_counts_only_correspondence():
    pending = [
        _msg(sender="Owner", subject="real question"),          # correspondence, stale
        _msg(sender="notification-system", subject="consent ack",
             disposition="acknowledgement"),                    # machine ack
        _msg(sender="sidecar:retry_queue", subject="retry exhausted"),  # machine actionable
    ]
    out = _format_summary(pending, pending)

    # Avoidance signal fires for the one genuine correspondence item.
    assert "1 message(s) pending >48h" in out
    # Machine traffic is reported separately, not as avoidance.
    assert "2 system notification(s)/ping(s) pending" in out
    # Pending header counts correspondence only.
    assert "## Pending (1)" in out


def test_no_correspondence_means_no_avoidance_signal():
    """A pile of machine pings must never manufacture an avoidance signal."""
    pending = [
        _msg(sender="notification-system", disposition="acknowledgement"),
        _msg(sender="sidecar:retry_queue", subject="retry exhausted"),
        _msg(sender="sidecar", subject="fyi"),
    ]
    out = _format_summary(pending, pending)

    assert "avoidance signal" not in out
    assert "No correspondence pending" in out
    assert "3 system notification(s)/ping(s) pending" in out


def test_high_urgent_count_is_correspondence_only():
    pending = [
        _msg(sender="Owner", priority="high"),                     # counts
        _msg(sender="notification-system", priority="high",        # machine, excluded
             disposition="acknowledgement"),
    ]
    out = _format_summary(pending, pending)
    assert "1 high/urgent priority" in out

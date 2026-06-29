"""The notification->messaging callback is born resolved when it is an
acknowledgement (an echo of a consent decision the gateway already recorded),
so it never enters the pending/block path or accumulates. A genuine actionable
response stays pending until the agent ingests and resolves it.
"""

from __future__ import annotations

from unittest.mock import patch

from work_buddy.notifications.store import _dispatch_via_messaging


def _dispatch(disposition):
    captured = {}

    def _fake_send(**kwargs):
        captured.update(kwargs)
        return {"id": "m1"}

    with patch("work_buddy.messaging.client.send_message", _fake_send):
        _dispatch_via_messaging(
            {"capability": "consent_grant", "params": {}},
            title="Consent: foo",
            notification_id="n1",
            recipient_session="sess-1",
            disposition=disposition,
        )
    return captured


def test_acknowledgement_callback_is_born_resolved():
    sent = _dispatch("acknowledgement")
    assert sent["status"] == "resolved"
    assert sent["disposition"] == "acknowledgement"


def test_actionable_callback_stays_pending():
    sent = _dispatch("actionable")
    assert sent["status"] == "pending"
    assert sent["disposition"] == "actionable"

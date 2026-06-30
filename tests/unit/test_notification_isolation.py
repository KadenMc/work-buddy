"""Guards for the autouse notification-delivery isolation.

The test harness neutralizes outbound notification delivery by default
(``_isolate_notification_delivery`` in ``tests/conftest.py``) so that a
capability emitting a fire-and-forget notification (for example
``tasks.archive_completed`` -> ``_send_archive_summary_notification``) cannot
send a real message during a test run. These tests pin both halves of that
contract: stubbed by default, restorable via the opt-out marker.
"""

from __future__ import annotations

import pytest

from work_buddy.notifications.dispatcher import SurfaceDispatcher
from work_buddy.notifications.models import Notification


def test_delivery_stubbed_by_default():
    """With no marker, the autouse fixture replaces ``deliver`` with a no-op.

    A dispatch returns the empty result dict and never reaches a real surface,
    no matter how the dispatcher was constructed.
    """
    dispatcher = SurfaceDispatcher()
    assert dispatcher.deliver(Notification(title="probe", body="probe")) == {}


@pytest.mark.real_notification_delivery
def test_opt_out_restores_real_deliver():
    """The opt-out marker bypasses the stub, leaving the real method in place."""
    assert SurfaceDispatcher.deliver.__name__ == "deliver"

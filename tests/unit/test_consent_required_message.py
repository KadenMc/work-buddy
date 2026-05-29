"""Assertions on the ConsentRequired exception message.

The message is read by direct Python callers (tests, scripts, debug logs)
when consent gates fire outside the gateway's auto-consent path. It must
not suggest paths that do not exist or are forbidden.
"""

from work_buddy.consent import ConsentRequired


def test_message_does_not_suggest_grant_consent() -> None:
    """The deleted ``grant_consent`` / ``consent_grant`` capability names
    must not appear in the exception text. Agents reading this message
    previously followed those suggestions into ``Unknown capability``
    errors and forbidden self-grant attempts."""
    exc = ConsentRequired("test.op", "test reason", "low", 30)
    msg = str(exc)
    assert "grant_consent" not in msg
    assert "consent_grant" not in msg


def test_message_describes_the_gate_structurally() -> None:
    """Replacement text should explain what is happening (a consent gate
    fired) rather than instructing the reader to call something that
    does not exist."""
    exc = ConsentRequired("test.op", "test reason", "low", 30)
    msg = str(exc)
    assert "consent gate" in msg
    assert "test.op" in msg
    assert "test reason" in msg


def test_message_points_at_retry_for_gateway_callers() -> None:
    """The recovery path for an agent that saw this via the gateway is
    ``wb_run('retry', {'operation_id': ...})``."""
    exc = ConsentRequired("test.op", "test reason", "low", 30)
    msg = str(exc)
    assert "wb_run" in msg
    assert "retry" in msg

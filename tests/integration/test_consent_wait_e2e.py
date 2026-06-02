"""End-to-end: a shell-style consent `wait` resolves on approval.

Substitutes for the human-driven live test (which needs a person clicking
an approval surface): a background thread plays the role of the user,
approving — or denying, or granting out-of-band — while the CLI's blocking
`wait` polls the real consent stores (redirected to a temp dir). Proves the
full loop the gateway timeout hands off to: poll the request_id → resolve →
correct exit code.

The real Monitor-driven live test is staged for morning review.
"""

from __future__ import annotations

import threading
import time

import pytest

from work_buddy import consent
from work_buddy.notifications import store
from work_buddy.notifications.models import ResponseType, StandardResponse
from work_buddy.statusctl import cli

SESSION = "test-session-00000000"  # matches conftest's WORK_BUDDY_SESSION_ID


def _make_request(operation="task_toggle"):
    return consent.create_consent_request(
        operation=operation, reason="e2e", requester="agent:e2e",
    )["request_id"]


def _approve_after(request_id, delay, value="once"):
    def _go():
        time.sleep(delay)
        store.respond_to_notification(
            request_id,
            StandardResponse(
                response_type=ResponseType.CHOICE.value, value=value,
                surface="test",
            ),
        )
    t = threading.Thread(target=_go, daemon=True)
    t.start()
    return t


def _grant_after(request_id, delay, operation="task_toggle"):
    def _go():
        time.sleep(delay)
        consent.grant_consent(operation, mode="always", session_id=SESSION)
    t = threading.Thread(target=_go, daemon=True)
    t.start()
    return t


def test_wait_resolves_on_response_approval(tmp_agents_dir):
    rid = _make_request()
    t = _approve_after(rid, 0.3, value="once")
    rc = cli.main(["consent", "wait", rid, "--timeout", "15", "--poll-interval", "0.2"])
    t.join(timeout=2)
    assert rc == cli.EXIT_OK


def test_wait_resolves_on_out_of_band_grant(tmp_agents_dir):
    # Request stays pending (never "responded"); the grant lands directly.
    rid = _make_request(operation="task_toggle")
    t = _grant_after(rid, 0.3, operation="task_toggle")
    rc = cli.main([
        "consent", "wait", rid, "--session", SESSION,
        "--timeout", "15", "--poll-interval", "0.2",
    ])
    t.join(timeout=2)
    assert rc == cli.EXIT_OK


def test_wait_returns_denied(tmp_agents_dir):
    rid = _make_request()
    t = _approve_after(rid, 0.3, value="deny")
    rc = cli.main(["consent", "wait", rid, "--timeout", "15", "--poll-interval", "0.2"])
    t.join(timeout=2)
    assert rc == cli.EXIT_NEGATIVE


def test_wait_times_out_when_never_answered(tmp_agents_dir):
    rid = _make_request()
    start = time.monotonic()
    rc = cli.main(["consent", "wait", rid, "--timeout", "1", "--poll-interval", "0.2"])
    elapsed = time.monotonic() - start
    assert rc == cli.EXIT_TIMEOUT
    assert elapsed >= 1.0  # actually waited the full deadline

"""Read-only consent-request status composer.

Proves ``work_buddy.consent_status.consent_status`` fuses the request
record (notification store) and the grant (session consent.db) into a
single pending/granted/denied/expired/not_found verdict, and that it is
strictly read-only (it never writes a grant or mutates the request).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from work_buddy import consent, consent_status
from work_buddy.notifications import store
from work_buddy.notifications.models import ResponseType, StandardResponse


def _make_request(operation="task_toggle"):
    rec = consent.create_consent_request(
        operation=operation, reason="unit test", requester="agent:test",
    )
    return rec["request_id"]


def _respond(request_id, value):
    store.respond_to_notification(
        request_id,
        StandardResponse(
            response_type=ResponseType.CHOICE.value, value=value, surface="test",
        ),
    )


def test_not_found(tmp_agents_dir):
    s = consent_status.consent_status("req_missing", session_id="agent-x")
    assert s["state"] == "not_found"
    assert s["terminal"] is False


def test_pending(tmp_agents_dir):
    rid = _make_request()
    s = consent_status.consent_status(rid, session_id="agent-x")
    assert s["state"] == "pending"
    assert s["operation"] == "task_toggle"
    assert s["terminal"] is False


def test_granted_via_response(tmp_agents_dir):
    rid = _make_request()
    _respond(rid, "once")
    s = consent_status.consent_status(rid, session_id="agent-x")
    assert s["state"] == "granted"
    assert s["response"] == "once"
    assert s["terminal"] is True


def test_denied(tmp_agents_dir):
    rid = _make_request()
    _respond(rid, "deny")
    s = consent_status.consent_status(rid, session_id="agent-x")
    assert s["state"] == "denied"
    assert s["terminal"] is True


def test_expired_by_ttl(tmp_agents_dir):
    rid = _make_request()
    n = store.get_notification(rid)
    n.expires_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store._write_notification(n)
    s = consent_status.consent_status(rid, session_id="agent-x")
    assert s["state"] == "expired"
    assert s["terminal"] is True


def test_cancelled_is_expired(tmp_agents_dir):
    rid = _make_request()
    store.cancel_notification(rid)
    s = consent_status.consent_status(rid, session_id="agent-x")
    assert s["state"] == "expired"


def test_granted_via_grant_race(tmp_agents_dir):
    # Pending request, but a grant already landed out-of-band → granted.
    rid = _make_request(operation="task_toggle")
    consent.grant_consent("task_toggle", mode="always", session_id="agent-race")
    s = consent_status.consent_status(rid, session_id="agent-race")
    assert s["state"] == "granted"
    assert s["grant_seen"] is True


def test_no_session_skips_grant_check(tmp_agents_dir):
    # Without a session_id the grant cross-check is skipped; a pending
    # request stays pending.
    rid = _make_request()
    s = consent_status.consent_status(rid, session_id=None)
    assert s["state"] == "pending"
    assert s["grant_seen"] is False


def test_out_of_band_grant_after_expiry_is_granted(tmp_agents_dir):
    # Request expired, but the user's approval grant landed → honour it.
    rid = _make_request(operation="task_toggle")
    n = store.get_notification(rid)
    n.expires_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store._write_notification(n)
    consent.grant_consent("task_toggle", mode="always", session_id="agent-late")
    s = consent_status.consent_status(rid, session_id="agent-late")
    assert s["state"] == "granted"


def test_read_only_does_not_mutate_request(tmp_agents_dir):
    rid = _make_request()
    before = store.get_notification(rid).to_dict()
    consent_status.consent_status(rid, session_id="agent-x")
    after = store.get_notification(rid).to_dict()
    assert before == after

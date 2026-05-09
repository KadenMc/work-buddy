"""Capability-level tests for the email integration.

Exercises the public callables registered in ``_email_capabilities()`` via the
fake provider, asserting:
  - happy-path return shapes
  - typed errors get translated into ``{"ok": False, "error_kind": ...}`` dicts
    (not raised) — the gateway needs structured failures
  - dry-run does not write the pool
  - email_get / email_display return error_kind="email_message_not_found"
"""

from __future__ import annotations

import pytest

from work_buddy.email.errors import EmailMessageNotFound
from work_buddy.email.models import (
    EmailFolder,
    EmailMessageHandle,
    EmailSummary,
    stable_key_for,
)
from work_buddy.email.providers.fake import FakeEmailProvider


@pytest.fixture
def loaded_provider(monkeypatch) -> FakeEmailProvider:
    """Build a populated FakeEmailProvider and patch get_email_provider()."""
    p = FakeEmailProvider()
    p.add_account("acct1", "Personal", type="imap")
    p.add_folder(EmailFolder(
        path="imap://acct1/INBOX", name="Inbox", type="inbox",
        account_id="acct1", total_messages=1, unread_messages=1,
    ))
    rfc, sender, date, subject = "msg1@host", "Alice <alice@x>", "2026-04-28T10:00:00Z", "Test"
    p.add(
        EmailSummary(
            stable_key=stable_key_for(rfc_message_id=rfc, sender=sender, date=date, subject=subject),
            handle=EmailMessageHandle(provider_message_id=rfc, folder_path="imap://acct1/INBOX"),
            subject=subject, sender=sender, recipients="me@x", cc="",
            date=date, folder="Inbox", account_id="acct1",
            read=False, flagged=False, tags=[], preview="hello", rfc_message_id=rfc,
        ),
        body="full body of the test message",
    )

    import work_buddy.email.capabilities as cap_mod
    monkeypatch.setattr(cap_mod, "get_email_provider", lambda: p)
    return p


def test_email_health_happy_path(loaded_provider):
    from work_buddy.email.capabilities import email_health
    out = email_health()
    assert out["ok"] is True
    assert out["provider"] == "fake"


def test_email_accounts_returns_provider_payload(loaded_provider):
    from work_buddy.email.capabilities import email_accounts
    out = email_accounts()
    assert out["ok"] is True
    assert out["allowed_count"] == 1


def test_email_get_message_not_found_returns_structured_error(loaded_provider):
    from work_buddy.email.capabilities import email_get
    out = email_get(provider_message_id="missing", folder_path="imap://acct1/INBOX")
    assert out["ok"] is False
    assert out["error_kind"] == "email_message_not_found"


def test_email_get_happy_path(loaded_provider):
    from work_buddy.email.capabilities import email_get
    out = email_get(
        provider_message_id="msg1@host",
        folder_path="imap://acct1/INBOX",
        max_body_chars=12,
    )
    assert out["ok"] is True
    assert out["body"] == "full body of"
    assert out["body_truncated"] is True


def test_email_get_missing_required_args_returns_bad_request(loaded_provider):
    from work_buddy.email.capabilities import email_get
    out = email_get(provider_message_id="", folder_path="")
    assert out["ok"] is False
    assert out["error_kind"] == "bad_request"


def test_email_display_calls_provider(loaded_provider):
    from work_buddy.email.capabilities import email_display
    out = email_display(
        provider_message_id="msg1@host",
        folder_path="imap://acct1/INBOX",
        mode="window",
    )
    assert out["ok"] is True
    assert out["mode"] == "window"
    assert len(loaded_provider.display_log) == 1


# NOTE: tests for the legacy email_triage_run capability were removed
# during the clarify -> Threads migration. Email triage now flows
# through pipelines.email.EmailTriagePipeline (see
# tests/unit/pipelines/test_email_pipeline.py).

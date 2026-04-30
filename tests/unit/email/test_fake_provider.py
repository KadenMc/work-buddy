"""Unit tests for FakeEmailProvider — the substrate everything else relies on."""

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


def _fixture_provider() -> FakeEmailProvider:
    p = FakeEmailProvider()
    p.add_account("acct1", "Personal", type="imap")
    p.add_folder(EmailFolder(
        path="imap://acct1/INBOX", name="Inbox", type="inbox",
        account_id="acct1", total_messages=3, unread_messages=2,
    ))
    msgs = [
        ("alpha", "Alice <alice@x>", "2026-04-28T10:00:00Z", "Project alpha", False, "alpha body"),
        ("beta", "Bob <bob@x>", "2026-04-27T09:00:00Z", "RE: alpha review", True, "beta body"),
        ("gamma", "Spam <spam@bad>", "2026-04-26T08:00:00Z", "Win a prize", False, "spam body"),
    ]
    for rfc, sender, date, subject, read, body in msgs:
        s = EmailSummary(
            stable_key=stable_key_for(rfc_message_id=rfc, sender=sender, date=date, subject=subject),
            handle=EmailMessageHandle(provider_message_id=rfc, folder_path="imap://acct1/INBOX"),
            subject=subject, sender=sender, recipients="me@example.com", cc="",
            date=date, folder="Inbox", account_id="acct1",
            read=read, flagged=False, tags=[], preview=body[:40], rfc_message_id=rfc,
        )
        p.add(s, body=body)
    return p


def test_health_returns_ok():
    p = FakeEmailProvider()
    assert p.health()["ok"] is True


def test_list_accounts_lists_added_accounts():
    p = FakeEmailProvider()
    p.add_account("a1", "Work")
    accounts = p.list_accounts()
    assert len(accounts) == 1
    assert accounts[0]["id"] == "a1"


def test_recent_messages_orders_newest_first():
    p = _fixture_provider()
    out = p.recent_messages(unread_only=False, max_results=10)
    assert [s.subject for s in out] == [
        "Project alpha", "RE: alpha review", "Win a prize",
    ]


def test_recent_messages_unread_only_filters_correctly():
    p = _fixture_provider()
    out = p.recent_messages(unread_only=True, max_results=10)
    subjects = [s.subject for s in out]
    assert "RE: alpha review" not in subjects   # was read
    assert len(out) == 2


def test_search_messages_token_AND():
    p = _fixture_provider()
    out = p.search_messages(query="alpha review")
    assert len(out) == 1
    assert out[0].subject == "RE: alpha review"


def test_search_messages_empty_query_returns_all():
    p = _fixture_provider()
    out = p.search_messages(query="")
    assert len(out) == 3


def test_get_message_returns_body_and_truncates():
    p = _fixture_provider()
    summary = p.recent_messages(unread_only=False, max_results=10)[0]
    msg = p.get_message(summary.handle, max_body_chars=4)
    assert msg.body == "alph"
    assert msg.body_truncated is True
    assert msg.body_length == len("alpha body")


def test_get_message_unknown_handle_raises():
    p = _fixture_provider()
    with pytest.raises(EmailMessageNotFound):
        p.get_message(EmailMessageHandle(provider_message_id="missing", folder_path=""))


def test_display_message_logs_and_succeeds():
    p = _fixture_provider()
    summary = p.recent_messages(unread_only=False, max_results=10)[0]
    result = p.display_message(summary.handle, mode="tab")
    assert result["ok"] is True
    assert result["mode"] == "tab"
    assert len(p.display_log) == 1


def test_stable_key_is_idempotent_for_repeated_summaries():
    """Re-creating a summary from the same fields yields the same stable key —
    crucial for triage idempotence (don't re-submit the same message twice)."""
    p = _fixture_provider()
    summaries = p.recent_messages(unread_only=False, max_results=10)
    keys_first = sorted(s.stable_key for s in summaries)
    summaries_again = p.recent_messages(unread_only=False, max_results=10)
    keys_second = sorted(s.stable_key for s in summaries_again)
    assert keys_first == keys_second

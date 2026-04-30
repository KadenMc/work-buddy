"""Unit tests for work_buddy.email.models."""

from __future__ import annotations

from work_buddy.email.models import (
    EmailMessageHandle,
    EmailSummary,
    stable_key_for,
)


def _summary(**overrides) -> EmailSummary:
    base = dict(
        stable_key="mid:abc",
        handle=EmailMessageHandle(provider_message_id="abc", folder_path="imap://x/INBOX"),
        subject="Hello",
        sender="alice@example.com",
        recipients="me@example.com",
        cc="",
        date="2026-04-28T12:00:00Z",
        folder="Inbox",
        account_id="account1",
        read=False,
        flagged=False,
        tags=["$label1"],
        preview="Hello there",
        rfc_message_id="abc",
    )
    base.update(overrides)
    return EmailSummary(**base)


def test_stable_key_uses_rfc_message_id_when_present():
    k = stable_key_for(rfc_message_id="<id-1@host>", sender="a", date="d", subject="s")
    assert k == "mid:id-1@host"


def test_stable_key_strips_angle_brackets():
    k = stable_key_for(rfc_message_id="<x@y>", sender="", date="", subject="")
    assert "<" not in k and ">" not in k


def test_stable_key_falls_back_to_content_hash_when_no_rfc():
    k1 = stable_key_for(rfc_message_id="", sender="a@x", date="2026-04-28", subject="hi")
    k2 = stable_key_for(rfc_message_id=None, sender="a@x", date="2026-04-28", subject="hi")
    assert k1.startswith("hash:")
    assert k1 == k2  # deterministic


def test_stable_key_changes_when_subject_changes():
    k1 = stable_key_for(rfc_message_id="", sender="a", date="d", subject="hi")
    k2 = stable_key_for(rfc_message_id="", sender="a", date="d", subject="bye")
    assert k1 != k2


def test_summary_to_dict_roundtrip():
    s = _summary()
    d = s.to_dict()
    s2 = EmailSummary.from_dict(d)
    assert s2 == s


def test_summary_from_dict_handles_missing_optional_fields():
    minimal = {
        "stable_key": "x",
        "handle": {"provider_message_id": "p", "folder_path": "f"},
        "subject": "s",
        "sender": "a",
        "recipients": "",
        "cc": "",
        "date": None,
        "folder": "",
        "account_id": "",
        "read": False,
        "flagged": False,
    }
    s = EmailSummary.from_dict(minimal)
    assert s.tags == []
    assert s.preview == ""
    assert s.rfc_message_id == ""
    assert s.folder_type == ""


def test_summary_to_dict_roundtrip_with_folder_type():
    s = _summary(folder_type="inbox")
    s2 = EmailSummary.from_dict(s.to_dict())
    assert s2.folder_type == "inbox"

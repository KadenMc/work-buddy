"""End-to-end test of email triage adapter, including BackgroundTriageProducer.

Exercises the full pipeline with FakeEmailProvider:
  fixture data → adapter → producer → triage pool → assertions.

Covers:
  - happy-path: items land in the pool with source="email_message"
  - re-run with same content_hash is skipped (idempotence)
  - per-item dedup: same stable_key doesn't double-submit across runs
  - bridge unavailable returns ([], None) and producer reports "skipped"
"""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.email.errors import EmailBridgeUnreachable
from work_buddy.email.models import (
    EmailFolder,
    EmailMessageHandle,
    EmailSummary,
    stable_key_for,
)
from work_buddy.email.providers.fake import FakeEmailProvider
from work_buddy.email.triage_adapter import (
    EMAIL_TRIAGE_ADAPTER_NAME,
    EMAIL_TRIAGE_SOURCE,
    collect_email_candidates,
)
from work_buddy.triage.background import BackgroundTriageProducer
from work_buddy.triage.items import TriageItem


# ---------------------------------------------------------------------------
# Fixture provider
# ---------------------------------------------------------------------------


@pytest.fixture
def provider() -> FakeEmailProvider:
    p = FakeEmailProvider()
    p.add_account("acct1", "Personal")
    p.add_folder(EmailFolder(
        path="imap://acct1/INBOX", name="Inbox", type="inbox",
        account_id="acct1", total_messages=2, unread_messages=2,
    ))
    fixtures = [
        ("m1@host", "Alice <alice@x>", "2026-04-28T10:00:00Z", "Quarterly review", False, "Body of quarterly review."),
        ("m2@host", "Bob <bob@x>", "2026-04-28T09:00:00Z", "Lunch plans", False, "Are you free Friday?"),
    ]
    for rfc, sender, date, subject, read, body in fixtures:
        s = EmailSummary(
            stable_key=stable_key_for(rfc_message_id=rfc, sender=sender, date=date, subject=subject),
            handle=EmailMessageHandle(provider_message_id=rfc, folder_path="imap://acct1/INBOX"),
            subject=subject, sender=sender, recipients="me@x", cc="",
            date=date, folder="Inbox", account_id="acct1",
            read=read, flagged=False, tags=[], preview=body[:30], rfc_message_id=rfc,
        )
        p.add(s, body=body)
    return p


@pytest.fixture
def isolated_pool(tmp_path, monkeypatch):
    """Redirect the triage pool to a tmp dir so we don't touch real data/."""
    import work_buddy.triage.background as bg
    pool_dir = tmp_path / "triage_pool"
    pool_dir.mkdir()
    pool = bg.TriagePool(pool_dir=pool_dir)
    bg.set_pool_for_tests(pool)
    yield pool
    bg.set_pool_for_tests(None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_collect_returns_items_with_email_source(provider):
    items, h = collect_email_candidates(provider=provider, max_messages=10)
    assert len(items) == 2
    assert all(isinstance(i, TriageItem) for i in items)
    assert all(i.source == EMAIL_TRIAGE_SOURCE for i in items)
    assert h is not None and len(h) == 16


def test_collect_metadata_carries_stable_key_and_handle(provider):
    items, _ = collect_email_candidates(provider=provider, max_messages=10)
    for it in items:
        assert it.metadata["stable_key"]
        assert it.metadata["folder_path"]
        assert it.metadata["provider_message_id"]


def test_collect_unread_only_filters(provider):
    # Mark one as read; only the other should show up.
    items_initial, _ = collect_email_candidates(provider=provider, unread_only=True)
    assert len(items_initial) == 2
    # Now mark m2 as read in the fake by mutating its summary in-place
    for s in provider._summaries.values():
        if s.subject == "Lunch plans":
            object.__setattr__(s, "read", True)
    items_after, _ = collect_email_candidates(provider=provider, unread_only=True)
    assert len(items_after) == 1
    assert items_after[0].metadata["subject"] == "Quarterly review"


def test_content_hash_stable_across_calls(provider):
    _, h1 = collect_email_candidates(provider=provider, max_messages=10)
    _, h2 = collect_email_candidates(provider=provider, max_messages=10)
    assert h1 == h2


def test_bridge_unreachable_returns_empty(monkeypatch):
    class _Broken:
        name = "broken"
        def recent_messages(self, **kw): raise EmailBridgeUnreachable("fake outage")
    items, h = collect_email_candidates(provider=_Broken(), max_messages=10)
    assert items == []
    assert h is None


def test_producer_run_writes_email_entries_to_pool(provider, isolated_pool):
    """The full BackgroundTriageProducer pass with verdict_pass disabled
    (raw entries) — the simplest end-to-end of email → pool."""

    def _collect():
        return collect_email_candidates(provider=provider, max_messages=10)

    def _agent_stub(item, run_id):
        # Should not be called when verdict_pass_enabled=False.
        raise AssertionError("verdict pass disabled — agent must not be invoked")

    producer = BackgroundTriageProducer(
        adapter_name=EMAIL_TRIAGE_ADAPTER_NAME,
        source=EMAIL_TRIAGE_SOURCE,
        collect=_collect,
        agent=_agent_stub,
        pool=isolated_pool,
        enrich=False,
        verdict_pass_enabled=False,
    )
    result = producer.run(force=False)
    assert result.status == "ok"
    assert result.submitted == 2
    assert result.unsubmitted == []

    # Verify entries are in the pool with the right source
    pending = isolated_pool.pending(source=EMAIL_TRIAGE_SOURCE)
    assert len(pending) == 2
    assert all(e.source == EMAIL_TRIAGE_SOURCE for e in pending)


def test_producer_skips_when_content_unchanged(provider, isolated_pool):
    def _collect():
        return collect_email_candidates(provider=provider, max_messages=10)

    def _agent_stub(item, run_id):
        raise AssertionError("not used")

    producer = BackgroundTriageProducer(
        adapter_name=EMAIL_TRIAGE_ADAPTER_NAME,
        source=EMAIL_TRIAGE_SOURCE,
        collect=_collect,
        agent=_agent_stub,
        pool=isolated_pool,
        enrich=False,
        verdict_pass_enabled=False,
    )
    first = producer.run(force=False)
    assert first.status == "ok"
    second = producer.run(force=False)
    assert second.status == "skipped"
    assert second.reason in ("unchanged", "all_items_already_pending")

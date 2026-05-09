"""Tests for ``work_buddy.pipelines.email.EmailTriagePipeline``.

The pipeline composes the existing email collection adapter and the
algorithmic clusterer. Tests stub those externals so the pipeline
exercises without touching the Thunderbird bridge or the embedding
service.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.clarify.items import TriageItem
from work_buddy.pipelines.email import (
    EMAIL_ACTION_LIBRARY,
    EMAIL_ACTIONS,
    EmailTriagePipeline,
    _captured_from_triage_item,
    _domain_of,
    _synthesised_tags,
    count_unique_senders,
)
from work_buddy.pipelines.types import CapturedItem


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _triage_item(
    item_id: str = "email_abc",
    subject: str = "Re: dataset preprocessing",
    sender: str = "alice <alice@example.com>",
    folder_type: str = "inbox",
    flagged: bool = False,
    read: bool = False,
    tags: tuple[str, ...] = (),
) -> TriageItem:
    return TriageItem(
        id=item_id,
        text=f"Subject: {subject}\nFrom: {sender}\nPreview: …",
        label=subject,
        source="email_message",
        url=None,
        metadata={
            "subject": subject,
            "sender": sender,
            "folder_type": folder_type,
            "folder_path": f"INBOX/{folder_type}",
            "stable_key": f"key_{item_id}",
            "rfc_message_id": f"<{item_id}@example.com>",
            "provider_message_id": f"prov_{item_id}",
            "account_id": "acct_1",
            "flagged": flagged,
            "read": read,
            "tags": list(tags),
            "date": "2026-05-08T12:00:00Z",
        },
    )


# ---------------------------------------------------------------------------
# Action library shape
# ---------------------------------------------------------------------------


class TestActionLibrary:
    def test_contains_email_specific(self):
        names = {d.capability_name for d in EMAIL_ACTIONS}
        assert names == {
            "email_close",
            "email_create_tasks",
            "email_create_umbrella_task",
            "email_record_into_task",
        }

    def test_all_per_group(self):
        per_group = EMAIL_ACTION_LIBRARY.per_group_actions()
        assert len(per_group) == 4

    def test_pipeline_exposes_library(self):
        p = EmailTriagePipeline()
        assert p.action_library is EMAIL_ACTION_LIBRARY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestDomainOf:
    def test_bare_address(self):
        assert _domain_of("alice@example.com") == "example.com"

    def test_display_name_with_brackets(self):
        assert _domain_of("Alice <alice@example.com>") == "example.com"

    def test_strips_www(self):
        assert _domain_of("noreply@www.gmail.com") == "gmail.com"

    def test_empty(self):
        assert _domain_of("") == ""

    def test_no_at(self):
        assert _domain_of("not-an-email") == ""


class TestCapturedFromTriageItem:
    def test_carries_payload_fields(self):
        ti = _triage_item()
        ci = _captured_from_triage_item(ti)
        assert ci.id == "email_abc"
        assert ci.source == "email_message"
        assert ci.type == "email"
        assert ci.payload["subject"] == "Re: dataset preprocessing"
        assert ci.payload["stable_key"] == "key_email_abc"
        assert ci.payload["folder_type"] == "inbox"
        assert "Subject:" in (ci.summary or "")

    def test_truncates_long_label(self):
        ti = _triage_item(subject="x" * 200)
        ci = _captured_from_triage_item(ti)
        assert len(ci.label) <= 80
        assert ci.label.endswith("…")


class TestSynthesisedTags:
    def test_sender_domain_tag(self):
        tags = _synthesised_tags({"sender": "noreply@github.com"})
        assert "sender:github.com" in tags

    def test_folder_type_tag(self):
        tags = _synthesised_tags({
            "sender": "x@y.com", "folder_type": "inbox",
        })
        assert "folder:inbox" in tags

    def test_flagged_and_unread(self):
        tags = _synthesised_tags({
            "sender": "x@y.com", "flagged": True, "read": False,
        })
        assert "flagged" in tags
        assert "unread" in tags

    def test_no_unread_when_read(self):
        tags = _synthesised_tags({
            "sender": "x@y.com", "read": True,
        })
        assert "unread" not in tags

    def test_carries_through_message_tags(self):
        tags = _synthesised_tags({
            "sender": "x@y.com", "tags": ["Important", "Newsletter"],
        })
        assert "label:Important" in tags
        assert "label:Newsletter" in tags

    def test_empty_payload(self):
        assert _synthesised_tags({}) == ()


class TestCountUniqueSenders:
    def test_counts_distinct_domains(self):
        items = [
            CapturedItem(
                id="a", source="email_message", type="email", label="a",
                payload={"sender": "x@github.com"},
            ),
            CapturedItem(
                id="b", source="email_message", type="email", label="b",
                payload={"sender": "y@github.com"},
            ),
            CapturedItem(
                id="c", source="email_message", type="email", label="c",
                payload={"sender": "z@gmail.com"},
            ),
        ]
        assert count_unique_senders(items) == 2

    def test_skips_missing_senders(self):
        items = [
            CapturedItem(
                id="a", source="email_message", type="email", label="a",
                payload={},
            ),
        ]
        assert count_unique_senders(items) == 0


# ---------------------------------------------------------------------------
# Stage methods
# ---------------------------------------------------------------------------


class TestCollect:
    def test_collect_wraps_triage_items(self):
        triage_items = [
            _triage_item("email_a", subject="One"),
            _triage_item("email_b", subject="Two"),
        ]
        with patch(
            "work_buddy.email.triage_adapter.collect_email_candidates",
            return_value=(triage_items, "hash_xyz"),
        ):
            p = EmailTriagePipeline()
            captured = p.collect()
        assert len(captured) == 2
        assert all(isinstance(c, CapturedItem) for c in captured)
        assert captured[0].source == "email_message"

    def test_collect_returns_empty_when_bridge_unavailable(self):
        # collect_email_candidates returns ([], None) on bridge failure.
        with patch(
            "work_buddy.email.triage_adapter.collect_email_candidates",
            return_value=([], None),
        ):
            p = EmailTriagePipeline()
            assert p.collect() == []

    def test_collect_forwards_kwargs(self):
        captured_kwargs: dict = {}

        def fake(**kwargs):
            captured_kwargs.update(kwargs)
            return [], None

        with patch(
            "work_buddy.email.triage_adapter.collect_email_candidates",
            side_effect=fake,
        ):
            p = EmailTriagePipeline()
            p.collect(
                days_back=7,
                max_messages=25,
                folder_path="INBOX/Work",
                account_id="acct_2",
            )
        assert captured_kwargs["days_back"] == 7
        assert captured_kwargs["max_messages"] == 25
        assert captured_kwargs["folder_path"] == "INBOX/Work"
        assert captured_kwargs["account_id"] == "acct_2"
        # Default body budget: 800 chars per email so the LLM has
        # enough content for substantive per-email decisions.
        assert captured_kwargs["include_body_chars"] == 800

    def test_collect_explicit_zero_body_chars_respected(self):
        """Caller can force headers-only by passing include_body_chars=0."""
        captured_kwargs: dict = {}

        def fake(**kwargs):
            captured_kwargs.update(kwargs)
            return [], None

        with patch(
            "work_buddy.email.triage_adapter.collect_email_candidates",
            side_effect=fake,
        ):
            p = EmailTriagePipeline()
            p.collect(include_body_chars=0)
        assert captured_kwargs["include_body_chars"] == 0


class TestAnnotateItems:
    def test_annotate_adds_synthesised_tags(self):
        ci = _captured_from_triage_item(_triage_item())
        p = EmailTriagePipeline()
        out = p.annotate_items([ci])
        assert "sender:example.com" in out[0].tags
        assert "folder:inbox" in out[0].tags
        assert "unread" in out[0].tags

    def test_annotate_empty_short_circuits(self):
        p = EmailTriagePipeline()
        assert p.annotate_items([]) == []


class TestPrecluster:
    """Email is per-item triage by design — precluster always returns
    one singleton cluster per email. No algorithmic grouping.
    """

    def test_precluster_empty_returns_empty(self):
        p = EmailTriagePipeline()
        assert p.precluster([]) == []

    def test_precluster_returns_one_cluster_per_email(self):
        items = [
            _captured_from_triage_item(
                _triage_item(f"email_{i}", subject=f"Subject {i}"),
            )
            for i in range(3)
        ]
        p = EmailTriagePipeline()
        clusters = p.precluster(items)
        assert len(clusters) == 3
        # Each cluster has exactly one item (one email = one cluster).
        assert all(len(c.item_ids) == 1 for c in clusters)
        # item_ids cover the input set exactly.
        assert {c.item_ids[0] for c in clusters} == {
            f"email_{i}" for i in range(3)
        }

    def test_precluster_label_is_email_subject(self):
        items = [
            _captured_from_triage_item(
                _triage_item("e0", subject="Re: Q4 status"),
            ),
        ]
        p = EmailTriagePipeline()
        clusters = p.precluster(items)
        assert clusters[0].label == "Re: Q4 status"

    def test_precluster_truncates_long_subject(self):
        long_subject = "x" * 200
        items = [
            _captured_from_triage_item(
                _triage_item("e0", subject=long_subject),
            ),
        ]
        p = EmailTriagePipeline()
        clusters = p.precluster(items)
        assert len(clusters[0].label) <= 80
        assert clusters[0].label.endswith("…")

    def test_precluster_does_not_call_clusterer(self):
        """Regression guardrail: email's per-item shape must NOT
        invoke the algorithmic clusterer. Forcing emails through
        Louvain produces noise the user has to mentally undo."""
        items = [
            _captured_from_triage_item(
                _triage_item(f"email_{i}"),
            )
            for i in range(3)
        ]
        with patch(
            "work_buddy.clarify.cluster.cluster_items",
        ) as mock_cluster:
            p = EmailTriagePipeline()
            p.precluster(items)
        assert mock_cluster.call_count == 0


class TestUmbrellaSummary:
    def test_with_items_includes_sender_count(self):
        items = [
            _captured_from_triage_item(_triage_item(
                f"email_{i}",
                sender=f"x@domain{i % 3}.com",
            ))
            for i in range(5)
        ]
        p = EmailTriagePipeline()
        s = p.umbrella_summary({"item_count": 5}, items=items)
        assert s["title"] == "Email triage: 5 unread from 3 sender(s)"
        assert s["source"] == "email_triage"
        assert s["sender_count"] == 3
        assert s["item_count"] == 5

    def test_without_items_uses_count_only(self):
        p = EmailTriagePipeline()
        s = p.umbrella_summary({"item_count": 3})
        assert s["title"] == "Email triage: 3 unread"
        assert s["sender_count"] is None

    def test_empty_run(self):
        p = EmailTriagePipeline()
        s = p.umbrella_summary({"item_count": 0})
        assert s["title"] == "Email triage: nothing pending"


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_email_triage_registered(self):
        from work_buddy.pipelines.capability import PIPELINES

        assert "email_triage" in PIPELINES
        assert PIPELINES["email_triage"] is EmailTriagePipeline

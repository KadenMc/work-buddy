"""Tests for ``work_buddy.email.thread_actions`` — the per-thread
email_close / email_create_tasks / email_create_umbrella_task
capability backers.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.email.thread_actions import (
    EmailThreadActionError,
    email_close,
    email_create_tasks,
    email_create_umbrella_task,
)
from work_buddy.threads import models, store


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Per-test threads DB."""
    threads_db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: threads_db)
    yield


def _make_thread_with_emails(
    emails: list[dict],
    *,
    fsm_state=None,
    inciting_summary: dict | None = None,
):
    """Build a Thread with N email ContextItems for tests.

    Each entry in ``emails`` should be a dict with keys: id, subject,
    sender, [date], [folder_path], [stable_key].
    """
    ctx_items = tuple(
        models.ContextItem(
            id=email["id"],
            source="email_message",
            type="email",
            label=email.get("subject", email["id"]),
            payload={
                "subject": email.get("subject", "(no subject)"),
                "sender": email.get("sender", ""),
                "date": email.get("date", ""),
                "stable_key": email.get("stable_key", f"key_{email['id']}"),
                "rfc_message_id": email.get("rfc_message_id", ""),
                "provider_message_id": email.get("provider_message_id", ""),
                "folder_path": email.get("folder_path", "INBOX"),
            },
        )
        for email in emails
    )
    kwargs = {"context_items": ctx_items}
    if fsm_state is not None:
        kwargs["fsm_state"] = fsm_state
    if inciting_summary is not None:
        kwargs["inciting_event_summary"] = inciting_summary
    t = models.Thread(**kwargs)
    store.insert_thread(t)
    return t


# ---------------------------------------------------------------------------
# email_close
# ---------------------------------------------------------------------------


class TestEmailClose:
    def test_dismisses_thread(self, fresh_db):
        from work_buddy.threads.enums import FSMState

        t = _make_thread_with_emails(
            [{"id": "e0", "subject": "Newsletter"}],
            fsm_state=FSMState.AWAITING_CONFIRMATION,
        )
        result = email_close(t.thread_id, reason="newsletter")
        assert result["new_state"] == "dismissed"
        # Verify the thread was actually transitioned.
        loaded = store.get_thread(t.thread_id)
        assert loaded.fsm_state == FSMState.DISMISSED

    def test_default_reason_when_omitted(self, fresh_db):
        from work_buddy.threads.enums import FSMState

        t = _make_thread_with_emails(
            [{"id": "e0", "subject": "X"}],
            fsm_state=FSMState.AWAITING_CONFIRMATION,
        )
        # Should not raise even without a reason.
        result = email_close(t.thread_id)
        assert result["new_state"] == "dismissed"


# ---------------------------------------------------------------------------
# email_create_tasks
# ---------------------------------------------------------------------------


class TestEmailCreateTasks:
    def test_creates_one_task_per_email(self, fresh_db):
        t = _make_thread_with_emails([
            {"id": "e0", "subject": "Reply needed: dataset", "sender": "alice@example.com"},
            {"id": "e1", "subject": "Bug report", "sender": "bob@example.com"},
        ])
        with patch(
            "work_buddy.obsidian.tasks.mutations.create_task",
            return_value={"success": True, "task_line": "fake"},
        ) as mock_create:
            result = email_create_tasks(t.thread_id)
        assert mock_create.call_count == 2
        assert len(result["created"]) == 2
        assert result["failed"] == []

    def test_continues_on_per_item_failure(self, fresh_db):
        t = _make_thread_with_emails([
            {"id": "e0", "subject": "Good"},
            {"id": "e1", "subject": "Bad"},
            {"id": "e2", "subject": "Good"},
        ])
        outcomes = [
            {"success": True, "task_line": "ok"},
            RuntimeError("simulated"),
            {"success": True, "task_line": "ok"},
        ]

        def fake_create(**_kwargs):
            o = outcomes.pop(0)
            if isinstance(o, Exception):
                raise o
            return o

        with patch(
            "work_buddy.obsidian.tasks.mutations.create_task",
            side_effect=fake_create,
        ):
            result = email_create_tasks(t.thread_id)
        assert len(result["created"]) == 2
        assert len(result["failed"]) == 1
        assert result["failed"][0]["item_id"] == "e1"

    def test_failed_task_creation_is_recorded(self, fresh_db):
        t = _make_thread_with_emails([
            {"id": "e0", "subject": "X"},
        ])
        with patch(
            "work_buddy.obsidian.tasks.mutations.create_task",
            return_value={"success": False, "message": "obsidian down"},
        ):
            result = email_create_tasks(t.thread_id)
        assert result["created"] == []
        assert len(result["failed"]) == 1
        assert "obsidian down" in result["failed"][0]["error"]

    def test_empty_thread_returns_skipped_empty(self, fresh_db):
        t = _make_thread_with_emails([])
        result = email_create_tasks(t.thread_id)
        assert result.get("skipped_empty") is True
        assert result["created"] == []

    def test_missing_thread_raises(self, fresh_db):
        with pytest.raises(EmailThreadActionError, match="not found"):
            email_create_tasks("th-nope")

    def test_filters_non_email_items(self, fresh_db):
        """Items from other sources are ignored — defensive against
        cross-source ContextItems landing on the same thread."""
        ctx = (
            models.ContextItem(
                id="e0", source="email_message", type="email",
                label="Real email", payload={"subject": "Real"},
            ),
            models.ContextItem(
                id="t0", source="chrome_tab", type="tab",
                label="Random tab", payload={},
            ),
        )
        t = models.Thread(context_items=ctx)
        store.insert_thread(t)
        with patch(
            "work_buddy.obsidian.tasks.mutations.create_task",
            return_value={"success": True, "task_line": "ok"},
        ) as mock_create:
            result = email_create_tasks(t.thread_id)
        assert mock_create.call_count == 1
        assert result["created"][0]["item_id"] == "e0"


# ---------------------------------------------------------------------------
# email_create_umbrella_task
# ---------------------------------------------------------------------------


class TestEmailCreateUmbrellaTask:
    def test_creates_one_task_with_email_bullet_list(self, fresh_db):
        t = _make_thread_with_emails(
            [
                {"id": "e0", "subject": "First", "sender": "alice@x.com"},
                {"id": "e1", "subject": "Second", "sender": "bob@y.com"},
            ],
            inciting_summary={"title": "Project status emails"},
        )

        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return {"success": True, "task_line": "fake"}

        with patch(
            "work_buddy.obsidian.tasks.mutations.create_task",
            side_effect=fake_create,
        ):
            result = email_create_umbrella_task(t.thread_id)
        # Task text comes from the inciting summary's title.
        assert captured["task_text"] == "Project status emails"
        # Both emails appear in the summary body.
        body = captured["summary"] or ""
        assert "First" in body
        assert "Second" in body
        assert "alice@x.com" in body
        assert "bob@y.com" in body
        # Task creation succeeded.
        assert result["created"]["email_count"] == 2

    def test_title_override_wins(self, fresh_db):
        t = _make_thread_with_emails(
            [{"id": "e0", "subject": "X"}],
            inciting_summary={"title": "Original title"},
        )
        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return {"success": True, "task_line": "fake"}

        with patch(
            "work_buddy.obsidian.tasks.mutations.create_task",
            side_effect=fake_create,
        ):
            email_create_umbrella_task(
                t.thread_id, title_override="Custom title",
            )
        assert captured["task_text"] == "Custom title"

    def test_falls_back_to_default_title_when_no_inciting(self, fresh_db):
        t = _make_thread_with_emails([{"id": "e0", "subject": "X"}])
        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return {"success": True, "task_line": "fake"}

        with patch(
            "work_buddy.obsidian.tasks.mutations.create_task",
            side_effect=fake_create,
        ):
            email_create_umbrella_task(t.thread_id)
        assert captured["task_text"] == "Email cluster"

    def test_empty_thread_returns_skipped_empty(self, fresh_db):
        t = _make_thread_with_emails([])
        result = email_create_umbrella_task(t.thread_id)
        assert result.get("skipped_empty") is True
        assert result["created"] is None

    def test_missing_thread_raises(self, fresh_db):
        with pytest.raises(EmailThreadActionError, match="not found"):
            email_create_umbrella_task("th-nope")

    def test_create_task_failure_recorded(self, fresh_db):
        t = _make_thread_with_emails([{"id": "e0", "subject": "X"}])
        with patch(
            "work_buddy.obsidian.tasks.mutations.create_task",
            return_value={"success": False, "message": "vault unreachable"},
        ):
            result = email_create_umbrella_task(t.thread_id)
        assert result["created"] is None
        assert len(result["failed"]) == 1
        assert "vault unreachable" in result["failed"][0]["error"]

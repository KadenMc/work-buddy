"""seed_test_data — fabricate Threads in various FSM states for
UI testing.

Synthesizes Threads + events WITHOUT going through real inference
/ LLM. Useful when you want to exercise card kinds (clarification,
consent, review, redirect, cleanup-failure) the natural pipeline
doesn't easily produce.

Each seed function returns a list of new thread_ids. Idempotent
in the sense that re-running creates new threads (with fresh
ids), not duplicate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from work_buddy.threads import store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    KIND_ACTION_INFERRED,
    KIND_CONTEXT_INFERRED,
    KIND_EXECUTION_FINISHED,
    KIND_INCITING_EVENT,
    KIND_INTENT_INFERRED,
    KIND_THREAD_CREATED,
    ThreadEvent,
)
from work_buddy.threads.models import ContextItem, Thread

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_with_events(
    *,
    fsm_state: FSMState,
    title: str,
    inciting: dict[str, Any],
    intent_text: Optional[str] = None,
    action_payload: Optional[dict[str, Any]] = None,
    failure_payload: Optional[dict[str, Any]] = None,
    review_payload: Optional[dict[str, Any]] = None,
    context_items: tuple[ContextItem, ...] = (),
) -> str:
    """Synthesize a Thread + the event log it would have if it
    naturally arrived at ``fsm_state``."""
    thread = Thread(
        fsm_state=fsm_state,
        context_items=context_items,
        inciting_event_summary=inciting,
    )
    store.insert_thread(thread)

    # inciting + thread_created
    e = store.append_event(ThreadEvent(
        thread_id=thread.thread_id, kind=KIND_INCITING_EVENT,
        actor="inciting", data=inciting,
    ))
    store.append_event(ThreadEvent(
        thread_id=thread.thread_id, kind=KIND_THREAD_CREATED,
        actor="inciting", data={"seed": True}, parent_event_id=e.id,
    ))

    if intent_text:
        store.append_event(ThreadEvent(
            thread_id=thread.thread_id, kind=KIND_INTENT_INFERRED,
            actor="agent",
            data={
                "payload": {"intent": intent_text},
                "confidence": 0.85,
            },
        ))

    if action_payload:
        store.append_event(ThreadEvent(
            thread_id=thread.thread_id, kind=KIND_ACTION_INFERRED,
            actor="agent", data={"payload": action_payload},
        ))

    if review_payload:
        store.append_event(ThreadEvent(
            thread_id=thread.thread_id, kind=KIND_EXECUTION_FINISHED,
            actor="conductor", data=review_payload,
        ))

    if failure_payload:
        store.append_event(ThreadEvent(
            thread_id=thread.thread_id, kind=KIND_EXECUTION_FINISHED,
            actor="conductor",
            data={**failure_payload, "status": "failed"},
        ))

    # Refresh search blob now that we've added inferred events
    try:
        from work_buddy.threads.search import update_search_blob
        update_search_blob(thread.thread_id)
    except Exception:
        pass

    # Bump parent_event_id to the latest
    store.update_thread_state(
        thread.thread_id,
        parent_event_id=store.latest_event_id(thread.thread_id),
    )
    return thread.thread_id


def seed_all() -> dict[str, Any]:
    """Seed ~10 Threads covering every card kind + a parent/sub-thread
    hierarchy. Returns a summary dict."""
    spawned: list[str] = []

    # 1. Awaiting intent confirmation — agent has a guess, user reviews
    spawned.append(_seed_with_events(
        fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION,
        title="Sarah's birthday + gift",
        inciting={
            "source": "journal_note",
            "note_path": "journal/2026-05-12.md",
            "line_text": "- [ ] Sarah's birthday + gift",
            "description": "Sarah's birthday + gift",
        },
        intent_text="Schedule Sarah's birthday celebration and arrange a gift.",
        context_items=(
            ContextItem(id="sarah_note", source="journal_note", type="todo_line",
                        label="Sarah's note (running journal)",
                        payload={"line": "Sarah's birthday + gift"}),
        ),
    ))

    # 2. Awaiting context confirmation — multiple context items
    spawned.append(_seed_with_events(
        fsm_state=FSMState.AWAITING_CONTEXT_CONFIRMATION,
        title="ECG paper draft review",
        inciting={
            "source": "journal_note",
            "note_path": "journal/2026-05-01.md",
            "line_text": "send draft to Anna for review",
            "description": "ECG paper draft review",
        },
        intent_text="Send the ECG paper draft to Anna for collaborator review.",
        context_items=(
            ContextItem(id="paper_draft", source="vault_file", type="note",
                        label="Research/ECG/paper-draft.md",
                        payload={"path": "Research/ECG/paper-draft.md"}),
            ContextItem(id="anna_email", source="contacts", type="person",
                        label="Anna (collaborator) — anna@example.edu",
                        payload={"email": "anna@example.edu"}),
            ContextItem(id="prior_thread", source="email", type="thread",
                        label="Prior thread: 'paper feedback round 1'",
                        payload={"subject": "paper feedback round 1"}),
        ),
    ))

    # 3. Awaiting confirmation (consent gate) — high-impact action
    spawned.append(_seed_with_events(
        fsm_state=FSMState.AWAITING_CONFIRMATION,
        title="Send email to Anna re: ECG paper",
        inciting={
            "source": "journal_note",
            "note_path": "journal/2026-05-01.md",
            "line_text": "email Anna about ECG paper draft",
            "description": "Send email to Anna re: ECG paper",
        },
        intent_text="Email Anna about the ECG paper draft for her review.",
        action_payload={
            "kind": "standard",
            "name": "send_email",
            "parameters": {
                "to": "anna@example.edu",
                "subject": "ECG paper draft — your review please",
                "body": "Hi Anna,\n\nAttaching the latest draft of the "
                        "ECG paper. Specifically interested in your "
                        "thoughts on the validation section (§4.2) — "
                        "I'm not sure the cross-validation strategy "
                        "is rigorous enough.\n\nThanks!",
            },
            "confidence": 0.78,
            "intrinsic_amplifiers": {
                "reversibility": "irreversible",
                "regret_potential": "high",
            },
            "required_contexts": ["@email_send"],
            "plan_summary": "Email anna@example.edu re: ECG paper draft",
        },
    ))

    # 4. Clarification — agent has nothing
    spawned.append(_seed_with_events(
        fsm_state=FSMState.AWAITING_INTENT_CLARIFICATION,
        title="Ambiguous capture: 'follow up on dr appt'",
        inciting={
            "source": "journal_note",
            "note_path": "journal/2026-05-01.md",
            "line_text": "follow up on dr appt",
            "description": "Ambiguous capture: 'follow up on dr appt'",
        },
        # No intent — that's the point of clarification
    ))

    # 5. Awaiting review — execution complete
    spawned.append(_seed_with_events(
        fsm_state=FSMState.AWAITING_REVIEW,
        title="Schedule meeting with Bob",
        inciting={
            "description": "Schedule meeting with Bob",
            "source": "manual",
        },
        intent_text="Schedule a 30-min sync with Bob about Q3 planning.",
        action_payload={
            "kind": "standard",
            "name": "create_calendar_event",
            "parameters": {
                "title": "Sync with Bob — Q3 planning",
                "datetime": "2026-05-15T14:00:00",
                "duration_minutes": 30,
            },
        },
        review_payload={
            "status": "completed",
            "summary": "Calendar event created and invitation sent.",
            "output": {
                "calendar_event_id": "evt_abc123",
                "url": "https://calendar.example.com/event/abc123",
                "attendee_status": "invitation_sent",
            },
            "run_id": "run_42",
        },
    ))

    # 6. Awaiting redirect — execution failed
    spawned.append(_seed_with_events(
        fsm_state=FSMState.AWAITING_REDIRECT,
        title="Failed: send Slack DM to team",
        inciting={
            "description": "send Slack DM to team",
            "source": "manual",
        },
        intent_text="Send team Slack DM about the staging deploy.",
        failure_payload={
            "error": "Slack API: rate limited (HTTP 429)",
            "step": "send_message",
            "summary": "Send to #eng channel failed after 3 retries.",
        },
    ))

    # 7. Cleanup-failure — journal adapter would fail (e.g. file locked)
    spawned.append(_seed_with_events(
        fsm_state=FSMState.DONE_CLEANUP_UNSUCCESSFUL,
        title="(test) Cleanup that failed",
        inciting={
            "source": "journal_note",
            "note_path": "journal/nonexistent.md",
            "line_text": "- [ ] simulated stale todo",
            "description": "Cleanup test (file missing)",
        },
        intent_text="Delete a journal line whose file no longer exists.",
        failure_payload={
            "error": "could not read 'journal/nonexistent.md' "
                     "(bridge unreachable or file missing)",
            "summary": "Cleanup adapter could not find the source file.",
        },
    ))

    # 8. Parent + 4 sub-threads (Chrome triage style)
    parent = Thread(
        fsm_state=FSMState.MONITORING,
        inciting_event_summary={
            "source": "chrome_scrape",
            "scrape_id": "scrape-test",
            "description": "Chrome research session — ECG / Anthropic / dashboards",
        },
    )
    store.insert_thread(parent)
    e0 = store.append_event(ThreadEvent(
        thread_id=parent.thread_id, kind=KIND_INCITING_EVENT,
        actor="inciting", data=dict(parent.inciting_event_summary),
    ))
    store.append_event(ThreadEvent(
        thread_id=parent.thread_id, kind=KIND_THREAD_CREATED,
        actor="inciting", data={"seed": True}, parent_event_id=e0.id,
    ))
    spawned.append(parent.thread_id)

    sub_titles = [
        ("ECG paper draft (Google Docs)", "ECG"),
        ("Validation strategy reading (PubMed)", "ECG"),
        ("Anthropic console — agent test runs", "AI"),
        ("Dashboard mockup (Figma)", "DESIGN"),
    ]
    for i, (label, tag) in enumerate(sub_titles):
        sub = Thread(
            parent_id=parent.thread_id,
            fsm_state=FSMState.AWAITING_CONFIRMATION,
            order_index=i,
            context_items=(
                ContextItem(
                    id=f"tab-{i}",
                    source="chrome_tab", type="tab",
                    label=label,
                    payload={"url": f"https://example.com/{i}", "tag": tag},
                ),
            ),
            inciting_event_summary={
                "source": "chrome_tab",
                "url": f"https://example.com/{i}",
                "title": label,
                "description": label,
            },
        )
        store.insert_thread(sub)
        ee = store.append_event(ThreadEvent(
            thread_id=sub.thread_id, kind=KIND_INCITING_EVENT,
            actor="inciting", data=dict(sub.inciting_event_summary),
        ))
        store.append_event(ThreadEvent(
            thread_id=sub.thread_id, kind=KIND_INTENT_INFERRED,
            actor="agent",
            data={"payload": {"intent": f"{label} — keep open or archive?"},
                  "confidence": 0.6},
        ))
        store.update_thread_state(
            sub.thread_id,
            parent_event_id=store.latest_event_id(sub.thread_id),
        )
        try:
            from work_buddy.threads.search import update_search_blob
            update_search_blob(sub.thread_id)
        except Exception:
            pass
        spawned.append(sub.thread_id)

    # 9. Later'd thread (resurface_at in future) — for testing the
    # show-deferred filter. ⏳ icon should appear.
    later_id = _seed_with_events(
        fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION,
        title="(later'd) Plan vacation logistics",
        inciting={
            "description": "Plan vacation logistics",
            "source": "manual",
        },
        intent_text="Outline the trip itinerary + book accommodations.",
    )
    # Add a 'later' event so has_been_later is True
    from work_buddy.threads.events import KIND_LATER
    store.append_event(ThreadEvent(
        thread_id=later_id, kind=KIND_LATER, actor="user",
        data={"hours": 24, "resurface_at":
              (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()},
    ))
    store.update_thread_state(
        later_id,
        resurface_at=(datetime.now(timezone.utc)
                      + timedelta(hours=24)).isoformat(),
        parent_event_id=store.latest_event_id(later_id),
    )
    spawned.append(later_id)

    return {
        "status": "ok",
        "spawned_count": len(spawned),
        "spawned_thread_ids": spawned,
    }

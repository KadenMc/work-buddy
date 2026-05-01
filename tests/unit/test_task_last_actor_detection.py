"""Slice 4: last_actor detection at task mutation sites.

The ``_detect_last_actor`` helper in
``work_buddy.obsidian.tasks.mutations`` reads
``consent.get_consent_context_info()`` to decide whether a mutation
fired inside a ``user_initiated()`` block (→ ``'user'``) or from an
autonomous path (→ ``'agent'``).  The helper is the single decision
point used by ``create_task``, ``toggle_task``, and the generic
``update_task`` state-change path.

These tests don't exercise the full bridge — they pin the helper's
behaviour against the consent context manager directly.  Integration
tests (full task creation through the bridge) live in the slice-2/3
suites and reach store.create's new kwargs via the existing fakes.
"""

from __future__ import annotations

from work_buddy import consent
from work_buddy.obsidian.tasks.mutations import _detect_last_actor


def test_detect_last_actor_outside_consent_context_is_agent():
    """No user_initiated wrapper → autonomous path."""
    assert _detect_last_actor() == "agent"


def test_detect_last_actor_inside_user_initiated_is_user():
    """Inside a user_initiated() block → user-driven path."""
    with consent.user_initiated("test.dashboard_click"):
        assert _detect_last_actor() == "user"


def test_detect_last_actor_after_block_exits_resets_to_agent():
    """The context is thread-local; exit pops the marker."""
    with consent.user_initiated("test.dashboard_click"):
        assert _detect_last_actor() == "user"
    assert _detect_last_actor() == "agent"


def test_detect_last_actor_nested_user_initiated_still_user():
    """Reentrant nesting — outer or inner all read 'user'."""
    with consent.user_initiated("outer.click"):
        with consent.user_initiated("inner.click"):
            assert _detect_last_actor() == "user"
        # Outer still active.
        assert _detect_last_actor() == "user"

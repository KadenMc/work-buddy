"""v5 Stage 1.6 + 1.7 + 1.8 — LLM-call priority queue + new tiers
+ budget hook scaffolding.

Pins:
- queue.py owns the priority queue (NOT threads/).
- enqueue → dequeue priority order; multi-worker safety via atomic
  claim.
- complete / fail / cancel state transitions.
- AGENT_HEADLESS + USER tiers exist on ModelTier.
- Admission hooks compose; rejected entries land with audit row.
- Budget hook integrates: cap + cost source + check_budget +
  budget_admission_hook.

DESIGN.md §9.2 (queue), §9.4 (budget) are the spec.
"""

from __future__ import annotations

import pytest

from work_buddy.llm import budget, queue
from work_buddy.llm.tiers import ModelTier


@pytest.fixture
def fresh_queue(tmp_path, monkeypatch):
    db = tmp_path / "queue.db"
    monkeypatch.setattr(queue, "_db_path", lambda: db)
    queue.clear_admission_hooks()
    budget.clear_caller_budgets()
    yield db
    queue.clear_admission_hooks()
    budget.clear_caller_budgets()


# ---------------------------------------------------------------------------
# Tier registration (1.7)
# ---------------------------------------------------------------------------


class TestTiersV5:
    def test_agent_headless_registered(self):
        assert ModelTier.AGENT_HEADLESS.value == "agent_headless"

    def test_user_tier_registered(self):
        assert ModelTier.USER.value == "user"

    def test_resolve_tier_for_agent_headless_returns_subprocess_backend(self):
        from work_buddy.llm.tiers import resolve_tier
        binding = resolve_tier(ModelTier.AGENT_HEADLESS)
        assert binding.backend == "agent_subprocess"

    def test_resolve_tier_for_user_returns_clarification_backend(self):
        from work_buddy.llm.tiers import resolve_tier
        binding = resolve_tier(ModelTier.USER)
        assert binding.backend == "user_clarification"


# ---------------------------------------------------------------------------
# Schema + basic flow (1.6)
# ---------------------------------------------------------------------------


class TestSchema:
    def test_table_and_indexes_present(self, fresh_queue):
        conn = queue.get_connection()
        try:
            tbls = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            idxs = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
        finally:
            conn.close()
        assert "llm_call_queue" in tbls
        assert "idx_queue_pending_priority" in idxs
        assert "idx_queue_caller" in idxs
        assert "idx_queue_status" in idxs


class TestEnqueueDequeue:
    def test_basic_round_trip(self, fresh_queue):
        eid = queue.enqueue(
            caller_id="thread:t-1", caller_kind="thread", target="intent",
            priority=50, payload={"foo": "bar"},
        )
        entry = queue.get_entry(eid)
        assert entry.status == "pending"
        assert entry.payload == {"foo": "bar"}
        assert entry.caller_kind == "thread"

        claimed = queue.dequeue("worker-A")
        assert claimed.id == eid
        assert claimed.status == "in_flight"
        assert claimed.worker_id == "worker-A"

        assert queue.complete(eid, {"intent": "schedule"}) is True
        final = queue.get_entry(eid)
        assert final.status == "done"
        assert final.result == {"intent": "schedule"}

    def test_rejects_unknown_caller_kind(self, fresh_queue):
        with pytest.raises(ValueError):
            queue.enqueue(
                caller_id="x", caller_kind="garbage", target="intent",
            )

    def test_priority_ordering(self, fresh_queue):
        # Lower priority number = higher precedence
        a = queue.enqueue(caller_id="a", caller_kind="thread", target="i", priority=200)
        b = queue.enqueue(caller_id="b", caller_kind="thread", target="i", priority=10)
        c = queue.enqueue(caller_id="c", caller_kind="thread", target="i", priority=100)

        order = [queue.dequeue("w").id for _ in range(3)]
        assert order == [b, c, a]

    def test_dequeue_empty_returns_none(self, fresh_queue):
        assert queue.dequeue("worker") is None

    def test_dequeue_only_pending(self, fresh_queue):
        eid = queue.enqueue(caller_id="x", caller_kind="thread", target="i")
        queue.cancel(eid)
        assert queue.dequeue("w") is None

    def test_dequeue_filter_by_caller_kind(self, fresh_queue):
        thread_id = queue.enqueue(
            caller_id="thread:t", caller_kind="thread", target="i",
        )
        queue.enqueue(
            caller_id="job:j", caller_kind="scheduled_job", target="i",
        )
        thread_only = queue.dequeue("w", caller_kind="thread")
        assert thread_only.id == thread_id


class TestStateTransitions:
    def test_complete_only_from_in_flight(self, fresh_queue):
        eid = queue.enqueue(caller_id="x", caller_kind="thread", target="i")
        # Pending entry — complete should be a no-op
        assert queue.complete(eid, {"r": "ok"}) is False

    def test_fail_only_from_in_flight(self, fresh_queue):
        eid = queue.enqueue(caller_id="x", caller_kind="thread", target="i")
        assert queue.fail(eid, "boom") is False

    def test_cancel_only_pending(self, fresh_queue):
        eid = queue.enqueue(caller_id="x", caller_kind="thread", target="i")
        queue.dequeue("w")  # claims the entry → in_flight
        # cancel after claim is a no-op
        assert queue.cancel(eid) is False

    def test_fail_records_error(self, fresh_queue):
        eid = queue.enqueue(caller_id="x", caller_kind="thread", target="i")
        queue.dequeue("w")
        queue.fail(eid, "rate-limit")
        e = queue.get_entry(eid)
        assert e.status == "failed"
        assert e.error_text == "rate-limit"


class TestVisibility:
    def test_peek_pending_excludes_terminal(self, fresh_queue):
        a = queue.enqueue(caller_id="a", caller_kind="thread", target="i")
        b = queue.enqueue(caller_id="b", caller_kind="thread", target="i")
        queue.cancel(a)
        peeked = queue.peek_pending()
        assert {p.id for p in peeked} == {b}

    def test_status_for_caller(self, fresh_queue):
        for _ in range(3):
            queue.enqueue(caller_id="thread:t", caller_kind="thread", target="i")
        e = queue.enqueue(caller_id="thread:t", caller_kind="thread", target="i")
        queue.cancel(e)
        counts = queue.status_for_caller("thread:t")
        assert counts["pending"] == 3
        assert counts["cancelled"] == 1


# ---------------------------------------------------------------------------
# Admission hook composition (1.6)
# ---------------------------------------------------------------------------


class TestAdmissionHooks:
    def test_admit_true_hook_no_op(self, fresh_queue):
        queue.register_admission_hook(
            lambda **kw: queue.AdmissionDecision(admit=True),
        )
        eid = queue.enqueue(
            caller_id="x", caller_kind="thread", target="i",
        )
        assert queue.get_entry(eid).status == "pending"

    def test_admit_false_records_rejection_audit(self, fresh_queue):
        queue.register_admission_hook(
            lambda **kw: queue.AdmissionDecision(
                admit=False, reason="test reject",
            ),
        )
        with pytest.raises(queue.QueueRejected) as exc_info:
            queue.enqueue(
                caller_id="thread:t", caller_kind="thread", target="i",
            )
        assert "test reject" in str(exc_info.value)
        # Rejected entry IS recorded in the queue (for audit)
        counts = queue.status_for_caller("thread:t")
        assert counts.get("rejected") == 1

    def test_multiple_hooks_first_reject_wins(self, fresh_queue):
        queue.register_admission_hook(
            lambda **kw: queue.AdmissionDecision(admit=True),
        )
        queue.register_admission_hook(
            lambda **kw: queue.AdmissionDecision(admit=False, reason="hook2"),
        )
        queue.register_admission_hook(
            lambda **kw: queue.AdmissionDecision(admit=False, reason="hook3"),
        )
        with pytest.raises(queue.QueueRejected) as exc_info:
            queue.enqueue(caller_id="x", caller_kind="thread", target="i")
        assert "hook2" in str(exc_info.value)
        assert "hook3" not in str(exc_info.value)

    def test_no_hooks_means_always_admit(self, fresh_queue):
        eid = queue.enqueue(caller_id="x", caller_kind="thread", target="i")
        assert queue.get_entry(eid).status == "pending"


# ---------------------------------------------------------------------------
# Budget hook (1.8)
# ---------------------------------------------------------------------------


class TestBudgetHook:
    def test_check_budget_no_cap_set(self, fresh_queue):
        # No budget set for the caller → would_exceed False regardless
        result = budget.check_budget(
            caller_id="thread:no-cap", caller_kind="thread",
            estimated_cost_usd=10.0,
        )
        assert result.would_exceed is False
        assert result.budget_usd is None

    def test_check_budget_within_cap(self, fresh_queue):
        budget.set_caller_budget("thread:t", 1.00)
        result = budget.check_budget(
            caller_id="thread:t", caller_kind="thread",
            estimated_cost_usd=0.10,
        )
        assert result.would_exceed is False
        assert result.budget_usd == 1.00

    def test_check_budget_with_cumulative_cost_source(self, fresh_queue):
        budget.set_caller_budget("agent:a", 0.50)
        # Plug a custom cost source for this kind
        budget.register_cost_source("agent", lambda cid: 0.45)
        result = budget.check_budget(
            caller_id="agent:a", caller_kind="agent",
            estimated_cost_usd=0.10,
        )
        # 0.45 + 0.10 = 0.55 > 0.50 → would exceed
        assert result.would_exceed is True

    def test_admission_hook_admits_under_cap(self, fresh_queue):
        budget.set_caller_budget("thread:t", 1.00)
        queue.register_admission_hook(budget.budget_admission_hook)
        eid = queue.enqueue(
            caller_id="thread:t", caller_kind="thread",
            target="intent", estimated_cost_usd=0.05,
        )
        assert queue.get_entry(eid).status == "pending"

    def test_admission_hook_rejects_over_cap(self, fresh_queue):
        budget.set_caller_budget("agent:a", 0.10)
        budget.register_cost_source("agent", lambda cid: 0.09)
        queue.register_admission_hook(budget.budget_admission_hook)

        with pytest.raises(queue.QueueRejected) as exc_info:
            queue.enqueue(
                caller_id="agent:a", caller_kind="agent",
                target="intent", estimated_cost_usd=0.05,
            )
        msg = str(exc_info.value)
        assert "budget exceeded" in msg
        assert "0.0900" in msg or "0.09" in msg
        assert queue.status_for_caller("agent:a").get("rejected") == 1

    def test_thread_cost_source_default_registered(self):
        # The default registry maps 'thread' → _default_thread_cost_source
        # without any explicit setup. Sanity-check that.
        from work_buddy.llm.budget import cumulative_cost_for
        # No threads DB content → returns 0.0 (best-effort fallback)
        cost = cumulative_cost_for("thread:nonexistent", "thread")
        assert cost == 0.0

"""Invariants of the WorkItem base — the guards that keep the inversion
from eroding back into the v5 mistake (the base re-absorbing the FSM).

R2: the base never branches on subtype and carries no FSM machinery.
R3: the base must host a *hypothetical third subtype* with zero base
changes — proof the cut is genuinely N-ary, not secretly bimodal.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from typing import Optional

from work_buddy.threads import workitem as workitem_mod
from work_buddy.threads.models import Task, Thread
from work_buddy.threads.workitem import WorkItem


# ---------------------------------------------------------------------------
# R2 — no leak: the base is FSM-agnostic and never branches on subtype
# ---------------------------------------------------------------------------

# Identifiers that would mean the FSM/resolution machinery leaked into the
# base. Checked against *code* identifiers only (AST Name/Attribute), so
# the module docstring's prose ("FSM", "Thread", …) is correctly ignored.
_FSM_LEAK_IDENTIFIERS = {
    "fsm_state",
    "parent_event_id",
    "current_focus_thread_id",
    "parent_relationship",
    "originating_scrape_id",
    "TRANSITION_TABLE",
    "INFERRING_INTENT",
    "INFERRING_CONTEXT",
    "INFERRING_ACTION",
}


def _code_identifiers(module) -> set[str]:
    src = inspect.getsource(module)
    tree = ast.parse(src)
    ids: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            ids.add(node.id)
        elif isinstance(node, ast.Attribute):
            ids.add(node.attr)
    return ids


def test_base_carries_no_fsm_machinery():
    ids = _code_identifiers(workitem_mod)
    leaked = _FSM_LEAK_IDENTIFIERS & ids
    assert not leaked, f"FSM machinery leaked into WorkItem base: {leaked}"


def test_base_never_branches_on_subtype():
    """`if self.subtype == ...` is forbidden in the base. (`is_task`
    *returns* the comparison — that's an accessor, not a branch — so we
    specifically flag `If` nodes whose test inspects ``subtype``.)"""
    tree = ast.parse(inspect.getsource(workitem_mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            for sub in ast.walk(node.test):
                name = getattr(sub, "id", None) or getattr(sub, "attr", None)
                assert name != "subtype", (
                    "WorkItem base branches on subtype — forbidden (R2)"
                )


def test_thread_and_task_are_both_workitems():
    assert isinstance(Thread(), WorkItem)
    assert isinstance(Task(), WorkItem)


def test_only_thread_carries_the_fsm():
    assert hasattr(Thread(), "fsm_state")
    assert not hasattr(Task(), "fsm_state")
    # And they are siblings, not a subclass chain.
    assert not isinstance(Task(), Thread)
    assert not isinstance(Thread(), Task)


# ---------------------------------------------------------------------------
# R3 — third-subtype probe: a hypothetical new WorkItem kind must work
# with ZERO changes to the base. If this needs a base edit, the cut is
# secretly bimodal and must be fixed before it ossifies.
# ---------------------------------------------------------------------------


@dataclass
class _ReminderProbe(WorkItem):
    """A throwaway hypothetical third subtype — NOT shipped. Exists only
    to prove the base hosts arbitrary subtypes (anti-A3-bimodal guard)."""

    subtype: str = "reminder"
    remind_at: Optional[str] = None


def test_third_subtype_constructs_on_the_unchanged_base():
    probe = _ReminderProbe(remind_at="2026-06-01T09:00:00+00:00")
    assert isinstance(probe, WorkItem)
    assert not isinstance(probe, (Thread, Task))
    assert probe.subtype == "reminder"
    assert probe.is_task is False
    assert probe.remind_at == "2026-06-01T09:00:00+00:00"


def test_third_subtype_inherits_universal_serialization():
    probe = _ReminderProbe()
    d = probe.to_dict()
    # The base to_dict is the universal projection — it neither knows nor
    # leaks the probe's own field; subtypes that want it serialized would
    # override to_dict (as Thread does). The universal keys are present.
    assert d["subtype"] == "reminder"
    assert "thread_id" in d and "risk_profile" in d and "created_at" in d
    assert "remind_at" not in d  # base owns only universal fields
    assert "fsm_state" not in d  # no FSM anywhere near the base

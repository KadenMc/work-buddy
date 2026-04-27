"""Slice 1 bug fix: api_review_execute should only mark reviewed the
entries whose groups had decisions submitted — not the entire pool.

Prior behavior walked all groups in the presentation and stamped every
entry, so submitting one card via the per-group-submit frontend
(``perGroupSubmit: true`` in script_review.py) caused every other card
to silently disappear from the Review tab. Bug surfaced when raw
entries (Slice 1's verdict-pass-off mode) all clustered in the
``leave`` bucket, making the data-loss obvious.

This test exercises the filter logic in isolation without booting
the full Flask app.
"""

from __future__ import annotations


def _filter_keys(
    presentation: dict,
    decisions: dict,
    executed: dict | None = None,
) -> list[tuple[str, str]]:
    """Inline copy of the fixed filter logic so the test is hermetic.

    Mirrors the production logic in ``api_review_execute``; if the
    production logic changes shape, this test will need to follow.
    """
    decided_indices: set[int] = set()
    for gd in (decisions.get("group_decisions") or []):
        idx = gd.get("group_index")
        if isinstance(idx, int):
            decided_indices.add(idx)

    # Collect item_ids of successful ops from the executor result.
    # When ``executed`` is omitted, treat all decided items as
    # succeeded (legacy callers without success-tracking).
    succeeded_item_ids: set[str] | None = None
    if executed is not None:
        succeeded_item_ids = set()
        details = (executed or {}).get("details", {}) or {}
        for bucket_name in (
            "tasks_created", "tasks_recorded", "grouped",
            "closed", "left",
        ):
            for entry in details.get(bucket_name, []) or []:
                for iid in entry.get("item_ids", []) or []:
                    if iid:
                        succeeded_item_ids.add(iid)
                single = entry.get("item_id")
                if single:
                    succeeded_item_ids.add(single)

    keys: list[tuple[str, str]] = []
    for action_groups in presentation.get("groups_by_action", {}).values():
        for group in action_groups:
            if group.get("index") not in decided_indices:
                continue
            run_id = group.get("pool_run_id")
            if not run_id:
                continue
            for item in group.get("items", []) or []:
                iid = item.get("id")
                if not iid:
                    continue
                if (
                    succeeded_item_ids is not None
                    and iid not in succeeded_item_ids
                ):
                    continue
                keys.append((run_id, iid))
    return keys


def _make_presentation(n_groups: int = 6) -> dict:
    """A presentation with N groups, all bucketed under 'leave'
    (the raw-entry case)."""
    return {
        "groups_by_action": {
            "leave": [
                {
                    "index": i,
                    "pool_run_id": "bgt_TEST",
                    "items": [{"id": f"j_00{i}"}],
                }
                for i in range(n_groups)
            ],
            "create_task": [],
            "close": [],
            "group": [],
            "record_into_task": [],
        }
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_one_decision_marks_one_entry() -> None:
    """User submits decision on group 2 only — only entry j_002 stamped."""
    pres = _make_presentation(6)
    decisions = {
        "group_decisions": [{"group_index": 2, "action": "leave"}],
    }
    keys = _filter_keys(pres, decisions)
    assert keys == [("bgt_TEST", "j_002")]


def test_zero_decisions_marks_nothing() -> None:
    """No decisions submitted — no entries should be stamped."""
    pres = _make_presentation(6)
    decisions = {"group_decisions": []}
    keys = _filter_keys(pres, decisions)
    assert keys == []


def test_three_decisions_marks_three_entries() -> None:
    pres = _make_presentation(6)
    decisions = {
        "group_decisions": [
            {"group_index": 0, "action": "leave"},
            {"group_index": 3, "action": "create_task"},
            {"group_index": 5, "action": "close"},
        ],
    }
    keys = _filter_keys(pres, decisions)
    assert sorted(keys) == [
        ("bgt_TEST", "j_000"),
        ("bgt_TEST", "j_003"),
        ("bgt_TEST", "j_005"),
    ]


def test_decision_for_nonexistent_group_is_safely_ignored() -> None:
    """Decision references a group_index that's not in the presentation."""
    pres = _make_presentation(3)
    decisions = {"group_decisions": [{"group_index": 99, "action": "leave"}]}
    keys = _filter_keys(pres, decisions)
    assert keys == []


def test_decision_with_non_int_index_is_skipped() -> None:
    """Defensive: malformed payload with a string index doesn't blow up."""
    pres = _make_presentation(3)
    decisions = {
        "group_decisions": [
            {"group_index": "not-an-int", "action": "leave"},
            {"group_index": 1, "action": "leave"},
        ],
    }
    keys = _filter_keys(pres, decisions)
    assert keys == [("bgt_TEST", "j_001")]


def test_groups_in_different_action_buckets() -> None:
    """A presentation with verdicted entries spread across action buckets."""
    pres = {
        "groups_by_action": {
            "leave": [
                {"index": 0, "pool_run_id": "bgt_TEST", "items": [{"id": "j_000"}]},
            ],
            "create_task": [
                {"index": 1, "pool_run_id": "bgt_TEST", "items": [{"id": "j_001"}]},
            ],
            "close": [
                {"index": 2, "pool_run_id": "bgt_TEST", "items": [{"id": "j_002"}]},
            ],
            "record_into_task": [],
            "group": [],
        }
    }
    decisions = {
        "group_decisions": [
            {"group_index": 0, "action": "leave"},
            {"group_index": 2, "action": "close"},
        ],
    }
    keys = _filter_keys(pres, decisions)
    # Only groups 0 and 2 stamped, regardless of which bucket they're in.
    assert sorted(keys) == [
        ("bgt_TEST", "j_000"),
        ("bgt_TEST", "j_002"),
    ]


def test_group_without_pool_run_id_skipped() -> None:
    """Defensive: a group missing pool_run_id can't be stamped."""
    pres = {
        "groups_by_action": {
            "leave": [
                {"index": 0, "items": [{"id": "j_000"}]},  # no pool_run_id
                {"index": 1, "pool_run_id": "bgt_TEST", "items": [{"id": "j_001"}]},
            ]
        }
    }
    decisions = {
        "group_decisions": [
            {"group_index": 0, "action": "leave"},
            {"group_index": 1, "action": "leave"},
        ],
    }
    keys = _filter_keys(pres, decisions)
    assert keys == [("bgt_TEST", "j_001")]


def test_group_with_multiple_items() -> None:
    """If a group has multiple items, all of them get stamped together."""
    pres = {
        "groups_by_action": {
            "leave": [
                {
                    "index": 0,
                    "pool_run_id": "bgt_TEST",
                    "items": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                },
            ]
        }
    }
    decisions = {"group_decisions": [{"group_index": 0, "action": "leave"}]}
    keys = _filter_keys(pres, decisions)
    assert sorted(keys) == [("bgt_TEST", "a"), ("bgt_TEST", "b"), ("bgt_TEST", "c")]


# ---------------------------------------------------------------------------
# Success-only filter (the second-fix gap)
# ---------------------------------------------------------------------------


def test_failed_op_does_not_mark_reviewed() -> None:
    """When the executor reports a failure for an item, that item must
    NOT be marked reviewed — it stays pending so the user can retry.

    This is the bug that caused the user's "This is a test task!"
    submit to silently disappear: the bridge timed out writing the
    note, create_task raised ObsidianPostWriteUncertain, the executor
    caught it into errors[]; but the dashboard still stamped the
    entry reviewed because it didn't check success.
    """
    pres = _make_presentation(2)
    decisions = {
        "group_decisions": [
            {"group_index": 0, "action": "create_task"},
            {"group_index": 1, "action": "leave"},
        ],
    }
    # Executor result: group 0's create_task failed (not in any
    # success bucket; only in errors). Group 1's leave succeeded.
    executed = {
        "details": {
            "left": [{"item_ids": ["j_001"]}],
            "tasks_created": [],  # failed, no entry here
            "errors": [
                {"action": "create_task", "task_text": "...", "error": "bridge timed out"},
            ],
        }
    }
    keys = _filter_keys(pres, decisions, executed=executed)
    # j_000 (the failed create_task) should NOT be stamped.
    # j_001 (the successful leave) should be stamped.
    assert keys == [("bgt_TEST", "j_001")]


def test_successful_create_task_marks_reviewed() -> None:
    """Sanity: when create_task succeeds, the item IS stamped reviewed."""
    pres = _make_presentation(1)
    decisions = {
        "group_decisions": [{"group_index": 0, "action": "create_task"}],
    }
    executed = {
        "details": {
            "tasks_created": [
                {"item_ids": ["j_000"], "task_id": "t-NEW", "task_text": "..."},
            ],
            "errors": [],
        }
    }
    keys = _filter_keys(pres, decisions, executed=executed)
    assert keys == [("bgt_TEST", "j_000")]


def test_leave_action_marks_reviewed_singular_item_id() -> None:
    """The 'leave' bucket uses singular ``item_id`` (not ``item_ids``).
    The success-filter must accept that form, otherwise every Leave-As-Is
    submit would silently fail to mark the entry reviewed."""
    pres = _make_presentation(1)
    decisions = {"group_decisions": [{"group_index": 0, "action": "leave"}]}
    executed = {
        "details": {
            "left": [
                {"item_id": "j_000", "label": "..."},  # singular form!
            ],
            "errors": [],
        }
    }
    keys = _filter_keys(pres, decisions, executed=executed)
    assert keys == [("bgt_TEST", "j_000")]


def test_close_action_marks_reviewed_singular_item_id() -> None:
    """The 'closed' bucket also uses singular ``item_id`` (Chrome path)."""
    pres = _make_presentation(1)
    decisions = {"group_decisions": [{"group_index": 0, "action": "close"}]}
    executed = {
        "details": {
            "closed": [
                {"item_id": "j_000", "tab_id": 42, "label": "..."},
            ],
            "errors": [],
        }
    }
    keys = _filter_keys(pres, decisions, executed=executed)
    assert keys == [("bgt_TEST", "j_000")]


def test_skipped_stale_does_not_mark_reviewed() -> None:
    """``skipped_stale`` is intentionally excluded — the user should
    have a chance to re-decide stale entries instead of having them
    silently disappear."""
    pres = _make_presentation(1)
    decisions = {"group_decisions": [{"group_index": 0, "action": "close"}]}
    executed = {
        "details": {
            "closed": [],
            "skipped_stale": [
                {"item_id": "j_000", "tab_id": 42, "reason": "URL changed"},
            ],
            "errors": [],
        }
    }
    keys = _filter_keys(pres, decisions, executed=executed)
    assert keys == []


def test_partial_success_only_marks_succeeded_items() -> None:
    """A submit covering 3 cards: 2 succeed, 1 fails. Only the 2
    successes get marked reviewed."""
    pres = _make_presentation(3)
    decisions = {
        "group_decisions": [
            {"group_index": 0, "action": "leave"},
            {"group_index": 1, "action": "create_task"},
            {"group_index": 2, "action": "leave"},
        ],
    }
    executed = {
        "details": {
            "left": [
                {"item_ids": ["j_000"]},
                {"item_ids": ["j_002"]},
            ],
            "tasks_created": [],  # group 1's create_task failed
            "errors": [
                {"action": "create_task", "error": "consent denied"},
            ],
        }
    }
    keys = _filter_keys(pres, decisions, executed=executed)
    assert sorted(keys) == [("bgt_TEST", "j_000"), ("bgt_TEST", "j_002")]

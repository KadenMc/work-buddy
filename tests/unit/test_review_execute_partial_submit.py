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


def _filter_keys(presentation: dict, decisions: dict) -> list[tuple[str, str]]:
    """Inline copy of the fixed filter logic so the test is hermetic.

    Mirrors the production logic in ``api_review_execute``; if the
    production logic changes shape, this test will need to follow.
    """
    decided_indices: set[int] = set()
    for gd in (decisions.get("group_decisions") or []):
        idx = gd.get("group_index")
        if isinstance(idx, int):
            decided_indices.add(idx)

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
                if iid:
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

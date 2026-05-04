"""Tests for ``work_buddy.pipelines.actions`` — the ActionLibrary
primitive that backs both the LLM cluster-refinement step and the
dashboard action-chip dropdown.
"""

from __future__ import annotations

import pytest

from work_buddy.pipelines.actions import (
    CARDINALITY_PER_GROUP,
    CARDINALITY_PER_ITEM,
    CARDINALITY_UMBRELLA,
    ActionDescriptor,
    ActionLibrary,
)


def _desc(name: str, cardinality: str = CARDINALITY_PER_GROUP) -> ActionDescriptor:
    return ActionDescriptor(
        capability_name=name,
        label=name.replace("_", " ").title(),
        description=f"Test descriptor for {name}",
        cardinality=cardinality,
    )


class TestActionDescriptor:
    def test_valid_cardinality_accepted(self):
        for c in (CARDINALITY_PER_GROUP, CARDINALITY_PER_ITEM, CARDINALITY_UMBRELLA):
            _desc("x", c)  # no raise

    def test_invalid_cardinality_rejected(self):
        with pytest.raises(ValueError, match="Invalid cardinality"):
            ActionDescriptor(
                capability_name="x",
                label="x", description="x",
                cardinality="not_a_real_one",
            )

    def test_to_dict_round_trip(self):
        d = _desc("task_create")
        out = d.to_dict()
        assert out["capability_name"] == "task_create"
        assert out["cardinality"] == CARDINALITY_PER_GROUP
        assert out["default_params"] == {}


class TestActionLibrary:
    def test_empty_library(self):
        lib = ActionLibrary([])
        assert len(lib) == 0
        assert lib.all() == []
        assert lib.per_group_actions() == []
        assert lib.by_name("anything") is None
        assert not lib.has("anything")

    def test_lookup_by_name(self):
        lib = ActionLibrary([_desc("task_create"), _desc("dismiss")])
        assert lib.has("task_create")
        assert lib.by_name("task_create").label == "Task Create"
        assert lib.by_name("missing") is None

    def test_per_cardinality_split(self):
        lib = ActionLibrary([
            _desc("task_create", CARDINALITY_PER_GROUP),
            _desc("task_open_each", CARDINALITY_PER_ITEM),
            _desc("rewrite_notes", CARDINALITY_UMBRELLA),
        ])
        assert {d.capability_name for d in lib.per_group_actions()} == {"task_create"}
        assert {d.capability_name for d in lib.per_item_actions()} == {"task_open_each"}
        assert {d.capability_name for d in lib.umbrella_actions()} == {"rewrite_notes"}

    def test_merged_with_layers_and_overrides(self):
        universal = ActionLibrary([
            _desc("dismiss"),
            _desc("defer"),
        ])
        chrome = ActionLibrary([
            _desc("chrome_tab_close"),
            # Chrome can override "dismiss" with a Chrome-specific
            # description (e.g. "Stop watching these tabs").
            ActionDescriptor(
                capability_name="dismiss",
                label="Stop watching",
                description="Chrome-specific override",
                cardinality=CARDINALITY_PER_GROUP,
            ),
        ])
        merged = universal.merged_with(chrome)
        assert len(merged) == 3
        # Chrome override won.
        assert merged.by_name("dismiss").label == "Stop watching"
        # Original universal action survived.
        assert merged.by_name("defer") is not None

    def test_with_descriptor(self):
        lib = ActionLibrary([_desc("a")])
        bigger = lib.with_descriptor(_desc("b"))
        assert len(bigger) == 2
        # Original is unchanged (immutable semantics).
        assert len(lib) == 1

    def test_to_list_is_json_serialisable(self):
        import json
        lib = ActionLibrary([
            _desc("task_create"),
            _desc("dismiss"),
        ])
        # Round-trips through JSON cleanly.
        text = json.dumps(lib.to_list())
        parsed = json.loads(text)
        assert {entry["capability_name"] for entry in parsed} == {
            "task_create", "dismiss",
        }

    def test_iteration_preserves_registration_order(self):
        lib = ActionLibrary([
            _desc("first"), _desc("second"), _desc("third"),
        ])
        assert [d.capability_name for d in lib] == ["first", "second", "third"]

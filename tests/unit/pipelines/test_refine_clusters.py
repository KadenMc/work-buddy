"""Tests for ``work_buddy.pipelines.llm_cluster_refinement.refine_clusters``.

Stub the LLM to keep the unit tests offline. Cover:

- Happy path: well-formed LLM JSON → final clusters with proposed
  actions.
- Fallback to algorithmic clusters on LLM failure / unparseable JSON.
- Validation rejects: missing item_ids, duplicate item_ids,
  unknown capability, out-of-range confidence.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.pipelines.actions import (
    CARDINALITY_PER_GROUP,
    ActionDescriptor,
    ActionLibrary,
)
from work_buddy.pipelines.llm_cluster_refinement import refine_clusters
from work_buddy.pipelines.types import CapturedItem, ClusterSpec


def _items(n: int) -> list[CapturedItem]:
    return [
        CapturedItem(
            id=f"i{i}", source="journal_segment", type="todo_line",
            label=f"Item {i}", payload={"raw_text": f"text {i}"},
            summary=f"Summary of item {i}", tags=("wb/todo",),
        )
        for i in range(n)
    ]


def _library() -> ActionLibrary:
    return ActionLibrary([
        ActionDescriptor(
            capability_name="journal_route_to_tasks",
            label="Route to tasks",
            description="Each item becomes a task.",
            cardinality=CARDINALITY_PER_GROUP,
        ),
        ActionDescriptor(
            capability_name="thread_dismiss",
            label="Dismiss",
            description="Mark the group dismissed.",
            cardinality=CARDINALITY_PER_GROUP,
        ),
    ])


# ---------------------------------------------------------------------------
# Empty / passthrough
# ---------------------------------------------------------------------------


class TestEmptyInput:
    def test_empty_pre_returns_empty(self):
        out = refine_clusters(
            items=_items(2), pre=[],
            source_name="journal_backlog", action_library=_library(),
        )
        assert out == []

    def test_empty_items_returns_pre_unchanged(self):
        pre = [ClusterSpec(label="A", item_ids=("i0",))]
        out = refine_clusters(
            items=[], pre=pre,
            source_name="journal_backlog", action_library=_library(),
        )
        assert out == pre


# ---------------------------------------------------------------------------
# Happy path — well-formed LLM response
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_well_formed_response_produces_final_clusters(self):
        items = _items(4)
        pre = [
            ClusterSpec(label="Stub A", item_ids=("i0", "i1")),
            ClusterSpec(label="Stub B", item_ids=("i2", "i3")),
        ]
        good = {
            "content": "",
            "model": "claude-sonnet-4-5",
            "input_tokens": 10, "output_tokens": 50, "cached": False,
            "error": None,
            "parsed": {
                "clusters": [
                    {
                        "label": "Auto-extraction tooling",
                        "item_ids": ["i0", "i1"],
                        "proposed_action": {
                            "capability_name": "journal_route_to_tasks",
                            "rationale": "These are concrete TODOs.",
                            "confidence": 0.85,
                        },
                    },
                    {
                        "label": "ECG paper edits",
                        "item_ids": ["i2", "i3"],
                        "proposed_action": None,
                    },
                ],
            },
        }
        with patch(
            "work_buddy.llm.call.llm_call", return_value=good,
        ):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert len(out) == 2
        assert out[0].label == "Auto-extraction tooling"
        assert out[0].item_ids == ("i0", "i1")
        assert out[0].proposed_action.capability_name == "journal_route_to_tasks"
        assert out[0].proposed_action.confidence == 0.85
        assert out[1].proposed_action is None

    def test_llm_can_merge_clusters(self):
        items = _items(4)
        pre = [
            ClusterSpec(label="Stub A", item_ids=("i0", "i1")),
            ClusterSpec(label="Stub B", item_ids=("i2", "i3")),
        ]
        merged = {
            "content": "", "model": "x",
            "input_tokens": 0, "output_tokens": 0, "cached": False,
            "error": None,
            "parsed": {
                "clusters": [
                    {
                        "label": "All four",
                        "item_ids": ["i0", "i1", "i2", "i3"],
                        "proposed_action": None,
                    },
                ],
            },
        }
        with patch(
            "work_buddy.llm.call.llm_call", return_value=merged,
        ):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert len(out) == 1
        assert out[0].label == "All four"


# ---------------------------------------------------------------------------
# Failure → fallback to ``pre``
# ---------------------------------------------------------------------------


class TestFallback:
    def test_llm_error_falls_back_to_pre(self):
        items = _items(2)
        pre = [ClusterSpec(label="Stub A", item_ids=("i0", "i1"))]
        with patch(
            "work_buddy.llm.call.llm_call",
            return_value={
                "content": "", "model": "", "input_tokens": 0,
                "output_tokens": 0, "cached": False,
                "error": "timeout", "parsed": None,
            },
        ):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert out == pre

    def test_llm_call_raises_falls_back(self):
        items = _items(2)
        pre = [ClusterSpec(label="Stub A", item_ids=("i0", "i1"))]
        with patch(
            "work_buddy.llm.call.llm_call",
            side_effect=RuntimeError("boom"),
        ):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert out == pre

    def test_unparseable_response_falls_back(self):
        items = _items(2)
        pre = [ClusterSpec(label="Stub A", item_ids=("i0", "i1"))]
        with patch(
            "work_buddy.llm.call.llm_call",
            return_value={
                "content": "garbled", "model": "",
                "input_tokens": 0, "output_tokens": 0, "cached": False,
                "error": None, "parsed": None,
            },
        ):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert out == pre


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.fixture
    def items(self):
        return _items(3)

    @pytest.fixture
    def pre(self):
        return [ClusterSpec(label="Stub A", item_ids=("i0", "i1", "i2"))]

    def _make_response(self, parsed: dict) -> dict:
        return {
            "content": "", "model": "x", "input_tokens": 0,
            "output_tokens": 0, "cached": False, "error": None,
            "parsed": parsed,
        }

    def test_missing_item_ids_falls_back(self, items, pre):
        bad = self._make_response({
            "clusters": [
                {
                    "label": "Partial",
                    "item_ids": ["i0", "i1"],  # missing i2
                    "proposed_action": None,
                },
            ],
        })
        with patch("work_buddy.llm.call.llm_call", return_value=bad):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert out == pre

    def test_duplicate_item_id_falls_back(self, items, pre):
        bad = self._make_response({
            "clusters": [
                {"label": "A", "item_ids": ["i0", "i1"]},
                {"label": "B", "item_ids": ["i1", "i2"]},
            ],
        })
        with patch("work_buddy.llm.call.llm_call", return_value=bad):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert out == pre

    def test_unknown_capability_name_falls_back(self, items, pre):
        bad = self._make_response({
            "clusters": [
                {
                    "label": "All",
                    "item_ids": ["i0", "i1", "i2"],
                    "proposed_action": {
                        "capability_name": "nonexistent_capability",
                        "rationale": "fake",
                        "confidence": 0.5,
                    },
                },
            ],
        })
        with patch("work_buddy.llm.call.llm_call", return_value=bad):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert out == pre

    def test_confidence_out_of_range_falls_back(self, items, pre):
        bad = self._make_response({
            "clusters": [
                {
                    "label": "All",
                    "item_ids": ["i0", "i1", "i2"],
                    "proposed_action": {
                        "capability_name": "journal_route_to_tasks",
                        "rationale": "ok",
                        "confidence": 1.5,
                    },
                },
            ],
        })
        with patch("work_buddy.llm.call.llm_call", return_value=bad):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert out == pre

    def test_extra_item_id_falls_back(self, items, pre):
        bad = self._make_response({
            "clusters": [
                {
                    "label": "All plus extra",
                    "item_ids": ["i0", "i1", "i2", "i_extra"],
                    "proposed_action": None,
                },
            ],
        })
        with patch("work_buddy.llm.call.llm_call", return_value=bad):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert out == pre

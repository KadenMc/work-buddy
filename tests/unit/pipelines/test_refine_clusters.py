"""Tests for ``work_buddy.pipelines.llm_cluster_refinement.refine_clusters``.

Stub the LLM to keep the unit tests offline. Cover:

- Happy path: well-formed LLM JSON → final clusters with proposed
  actions.
- Fallback to algorithmic clusters on LLM failure / unparseable JSON.
- Validation rejects: missing item_ids, duplicate item_ids,
  unknown capability, out-of-range confidence.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from work_buddy.llm.response import LLMResponse
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


def _ok(parsed: dict) -> LLMResponse:
    return LLMResponse(structured_output=parsed, model="claude-sonnet-4-5")


def _err(msg: str = "timeout") -> LLMResponse:
    return LLMResponse(error=msg)


def _patch_runner(response: LLMResponse | None = None, *, side_effect=None):
    """Patch LLMRunner so its .call(...) returns a stub response.

    refine_clusters does ``from work_buddy.llm import LLMRunner`` inside
    its helper, so the patch target is the package-level name.
    """
    runner_instance = MagicMock()
    if side_effect is not None:
        runner_instance.call.side_effect = side_effect
    else:
        runner_instance.call.return_value = response

    runner_cls = MagicMock(return_value=runner_instance)
    return patch("work_buddy.llm.LLMRunner", runner_cls)


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
        good = _ok({
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
        })
        with _patch_runner(good):
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
        merged = _ok({
            "clusters": [
                {
                    "label": "All four",
                    "item_ids": ["i0", "i1", "i2", "i3"],
                    "proposed_action": None,
                },
            ],
        })
        with _patch_runner(merged):
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
        with _patch_runner(_err("timeout")):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert out == pre

    def test_llm_call_raises_falls_back(self):
        items = _items(2)
        pre = [ClusterSpec(label="Stub A", item_ids=("i0", "i1"))]
        with _patch_runner(side_effect=RuntimeError("boom")):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert out == pre

    def test_unparseable_response_falls_back(self):
        items = _items(2)
        pre = [ClusterSpec(label="Stub A", item_ids=("i0", "i1"))]
        # No structured_output and no error: caller can't extract a
        # parsed dict, so it should fall back. content is non-empty so
        # the warning logs the length.
        no_parsed = LLMResponse(content="garbled")
        with _patch_runner(no_parsed):
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

    def test_missing_item_ids_falls_back(self, items, pre):
        bad = _ok({
            "clusters": [
                {
                    "label": "Partial",
                    "item_ids": ["i0", "i1"],  # missing i2
                    "proposed_action": None,
                },
            ],
        })
        with _patch_runner(bad):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert out == pre

    def test_duplicate_item_id_falls_back(self, items, pre):
        bad = _ok({
            "clusters": [
                {"label": "A", "item_ids": ["i0", "i1"]},
                {"label": "B", "item_ids": ["i1", "i2"]},
            ],
        })
        with _patch_runner(bad):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert out == pre

    def test_unknown_capability_name_falls_back(self, items, pre):
        bad = _ok({
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
        with _patch_runner(bad):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert out == pre

    def test_confidence_out_of_range_falls_back(self, items, pre):
        bad = _ok({
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
        with _patch_runner(bad):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert out == pre

    def test_extra_item_id_falls_back(self, items, pre):
        bad = _ok({
            "clusters": [
                {
                    "label": "All plus extra",
                    "item_ids": ["i0", "i1", "i2", "i_extra"],
                    "proposed_action": None,
                },
            ],
        })
        with _patch_runner(bad):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
            )
        assert out == pre


# ---------------------------------------------------------------------------
# Tier-chain escalation
# ---------------------------------------------------------------------------


class TestTierChain:
    """Refinement walks the tier_chain on failure and short-circuits on success."""

    def _good_response(self) -> LLMResponse:
        return _ok({
            "clusters": [
                {
                    "label": "All",
                    "item_ids": ["i0", "i1"],
                    "proposed_action": None,
                },
            ],
        })

    def test_explicit_tier_chain_overrides_config(self):
        """Passing ``tier_chain=...`` skips ``load_triage_config``."""
        items = _items(2)
        pre = [ClusterSpec(label="A", item_ids=("i0", "i1"))]
        with _patch_runner(self._good_response()):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
                tier_chain=["frontier_balanced"],
            )
        assert len(out) == 1
        assert out[0].label == "All"

    def test_first_tier_succeeds_no_escalation(self):
        items = _items(2)
        pre = [ClusterSpec(label="A", item_ids=("i0", "i1"))]
        runner_instance = MagicMock()
        runner_instance.call.return_value = self._good_response()
        runner_cls = MagicMock(return_value=runner_instance)
        with patch("work_buddy.llm.LLMRunner", runner_cls):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
                tier_chain=["local_tool_calling", "frontier_balanced"],
            )
        # Only the first tier was tried; success short-circuits.
        assert runner_instance.call.call_count == 1
        assert len(out) == 1

    def test_escalates_on_llm_error(self):
        """First tier returns LLMResponse error → second tier called and wins."""
        items = _items(2)
        pre = [ClusterSpec(label="A", item_ids=("i0", "i1"))]
        runner_instance = MagicMock()
        runner_instance.call.side_effect = [
            _err("timeout"),
            self._good_response(),
        ]
        runner_cls = MagicMock(return_value=runner_instance)
        with patch("work_buddy.llm.LLMRunner", runner_cls):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
                tier_chain=["local_tool_calling", "frontier_balanced"],
            )
        assert runner_instance.call.call_count == 2
        assert len(out) == 1
        assert out[0].label == "All"

    def test_escalates_on_validation_failure(self):
        """First tier returns valid LLMResponse but bad cluster shape →
        validation fails → escalates to next tier which produces good output."""
        items = _items(2)
        pre = [ClusterSpec(label="A", item_ids=("i0", "i1"))]
        bad = _ok({
            "clusters": [
                {
                    "label": "Missing item",
                    "item_ids": ["i0"],  # missing i1
                    "proposed_action": None,
                },
            ],
        })
        runner_instance = MagicMock()
        runner_instance.call.side_effect = [bad, self._good_response()]
        runner_cls = MagicMock(return_value=runner_instance)
        with patch("work_buddy.llm.LLMRunner", runner_cls):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
                tier_chain=["local_tool_calling", "frontier_balanced"],
            )
        assert runner_instance.call.call_count == 2
        assert len(out) == 1
        assert out[0].label == "All"

    def test_all_tiers_exhausted_falls_back_to_pre(self):
        items = _items(2)
        pre = [ClusterSpec(label="A", item_ids=("i0", "i1"))]
        runner_instance = MagicMock()
        runner_instance.call.return_value = _err("timeout")
        runner_cls = MagicMock(return_value=runner_instance)
        with patch("work_buddy.llm.LLMRunner", runner_cls):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
                tier_chain=["local_tool_calling", "local_fast", "frontier_fast"],
            )
        assert runner_instance.call.call_count == 3
        assert out == pre

    def test_empty_tier_chain_falls_back_without_call(self):
        """Empty ``tier_chain`` short-circuits without invoking the LLM."""
        items = _items(2)
        pre = [ClusterSpec(label="A", item_ids=("i0", "i1"))]
        runner_instance = MagicMock()
        runner_cls = MagicMock(return_value=runner_instance)
        with patch("work_buddy.llm.LLMRunner", runner_cls):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
                tier_chain=[],
            )
        assert runner_instance.call.call_count == 0
        assert out == pre

    def test_continues_past_exception(self):
        """An exception (not just LLMResponse error) at one tier is logged
        and the next tier is tried."""
        items = _items(2)
        pre = [ClusterSpec(label="A", item_ids=("i0", "i1"))]
        runner_instance = MagicMock()
        runner_instance.call.side_effect = [
            RuntimeError("boom"),
            self._good_response(),
        ]
        runner_cls = MagicMock(return_value=runner_instance)
        with patch("work_buddy.llm.LLMRunner", runner_cls):
            out = refine_clusters(
                items=items, pre=pre,
                source_name="journal_backlog",
                action_library=_library(),
                tier_chain=["local_tool_calling", "frontier_balanced"],
            )
        assert runner_instance.call.call_count == 2
        assert len(out) == 1
        assert out[0].label == "All"

"""Tests for ``work_buddy.pipelines.runner.run_pipeline``.

A lightweight stand-in for SourcePipeline drives the runner end-to-
end without any LLM/embedding/Chrome/journal dependencies. Each test
exercises one stage's behaviour or a runner-level concern (empty
input, action proposal recording, etc.).
"""

from __future__ import annotations

import pytest

from work_buddy.pipelines.actions import (
    CARDINALITY_PER_GROUP,
    ActionDescriptor,
    ActionLibrary,
)
from work_buddy.pipelines.runner import run_pipeline
from work_buddy.pipelines.types import (
    ActionProposal,
    CapturedItem,
    ClusterSpec,
)
from work_buddy.threads import store


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Per-test threads DB."""
    threads_db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: threads_db)
    yield


# ---------------------------------------------------------------------------
# Test pipeline — predictable, deterministic, no I/O
# ---------------------------------------------------------------------------


class _StubPipeline:
    """Minimal SourcePipeline stand-in. Each stage is parameterised
    via constructor kwargs so tests can exercise different paths."""

    def __init__(
        self,
        *,
        items: list[CapturedItem] | None = None,
        clusters: list[ClusterSpec] | None = None,
        action_library: ActionLibrary | None = None,
        umbrella_summary_extra: dict | None = None,
    ):
        self.name = "test_source"
        self._items = items or []
        self._clusters = clusters or []
        self._action_library = action_library or ActionLibrary([
            ActionDescriptor(
                capability_name="test_action",
                label="Test action",
                description="A test per-group action",
                cardinality=CARDINALITY_PER_GROUP,
            ),
        ])
        self._umbrella_extra = umbrella_summary_extra or {}

    @property
    def action_library(self) -> ActionLibrary:
        return self._action_library

    def collect(self, **kwargs):
        return list(self._items)

    def annotate_items(self, items):
        # Tag everything for visibility in cluster labels.
        return [it.augment(tags=("annotated",)) for it in items]

    def precluster(self, items):
        return list(self._clusters)

    def umbrella_summary(self, run_metadata):
        return {
            "source": self.name,
            "title": "Test umbrella",
            **self._umbrella_extra,
        }


def _ctx(item_id: str) -> CapturedItem:
    return CapturedItem(
        id=item_id, source="test_source", type="test_item",
        label=f"Item {item_id}",
    )


# ---------------------------------------------------------------------------
# Empty + minimal runs
# ---------------------------------------------------------------------------


class TestEmptyAndMinimalRuns:
    def test_empty_collect_still_spawns_umbrella(self, fresh_db):
        pipeline = _StubPipeline(items=[], clusters=[])
        result = run_pipeline(
            pipeline, universal_actions=ActionLibrary([]),
        )
        assert result.umbrella_id != ""
        assert result.child_thread_ids == ()
        assert result.item_count == 0
        assert result.cluster_count == 0
        # Umbrella should be in MONITORING.
        from work_buddy.threads.enums import FSMState
        u = store.get_thread(result.umbrella_id)
        assert u.fsm_state == FSMState.MONITORING

    def test_one_item_one_cluster(self, fresh_db):
        items = [_ctx("i0")]
        clusters = [ClusterSpec(label="A", item_ids=("i0",))]
        pipeline = _StubPipeline(items=items, clusters=clusters)
        result = run_pipeline(
            pipeline, universal_actions=ActionLibrary([]),
        )
        assert len(result.child_thread_ids) == 1
        assert result.item_count == 1
        assert result.cluster_count == 1
        # Umbrella now flagged as group.
        u = store.get_thread(result.umbrella_id)
        assert u.parent_relationship == "group"


# ---------------------------------------------------------------------------
# Multi-cluster + action proposal recording
# ---------------------------------------------------------------------------


class TestActionProposalRecording:
    def test_proposed_action_lands_on_child_event_log(self, fresh_db):
        items = [_ctx(f"i{i}") for i in range(3)]
        clusters = [
            ClusterSpec(
                label="With action", item_ids=("i0", "i1"),
                proposed_action=ActionProposal(
                    capability_name="test_action",
                    parameters={"foo": "bar"},
                    rationale="because",
                    confidence=0.85,
                ),
            ),
            ClusterSpec(label="No action", item_ids=("i2",)),
        ]
        pipeline = _StubPipeline(items=items, clusters=clusters)
        # Stub refine_clusters so the real Sonnet call doesn't fire
        # and overwrite our prepared proposed_action. (Pre-Phase-D
        # the runner used a passthrough stub; now the real LLM call
        # is wired, so test mocking is needed.)
        from work_buddy.pipelines import runner as runner_mod
        original = runner_mod.refine_clusters
        runner_mod.refine_clusters = lambda items, pre, **kw: list(pre)
        try:
            result = run_pipeline(
                pipeline, universal_actions=ActionLibrary([]),
            )
        finally:
            runner_mod.refine_clusters = original
        assert len(result.action_proposals) == 1
        # The "With action" child has an action_inferred event.
        with_action_child_id = result.child_thread_ids[0]
        events = store.list_events(with_action_child_id)
        action_inferred = [
            e for e in events if e.kind == "action_inferred"
        ]
        assert len(action_inferred) == 1
        payload = action_inferred[0].data["payload"]
        assert payload["name"] == "test_action"
        assert payload["parameters"] == {"foo": "bar"}
        assert payload["rationale"] == "because"
        assert action_inferred[0].data["from_pipeline_proposal"] is True
        # The "No action" child has NO action_inferred event.
        no_action_child_id = result.child_thread_ids[1]
        no_action_events = store.list_events(no_action_child_id)
        assert not [
            e for e in no_action_events if e.kind == "action_inferred"
        ]


# ---------------------------------------------------------------------------
# Action library merging
# ---------------------------------------------------------------------------


class TestUniversalActionsLayering:
    def test_universal_actions_merged_with_pipeline_library(self, fresh_db):
        # Pipeline library has "test_action"; universal has "dismiss".
        # The runner merges them; both should be present in the final
        # library passed to refine_clusters.
        universal = ActionLibrary([
            ActionDescriptor(
                capability_name="dismiss",
                label="Dismiss",
                description="Universal dismiss action",
                cardinality=CARDINALITY_PER_GROUP,
            ),
        ])
        # We intercept refine_clusters to capture the library it sees.
        from work_buddy.pipelines import runner as runner_mod
        captured: dict = {}

        def fake_refine(items, pre, *, source_name, action_library):
            captured["library"] = action_library
            return list(pre)

        original = runner_mod.refine_clusters
        runner_mod.refine_clusters = fake_refine
        try:
            pipeline = _StubPipeline(
                items=[_ctx("i0")],
                clusters=[ClusterSpec(label="A", item_ids=("i0",))],
            )
            run_pipeline(pipeline, universal_actions=universal)
        finally:
            runner_mod.refine_clusters = original

        assert captured["library"].has("dismiss")
        assert captured["library"].has("test_action")


# ---------------------------------------------------------------------------
# Inciting summary / metadata flow
# ---------------------------------------------------------------------------


class TestUmbrellaSummary:
    def test_umbrella_summary_is_recorded(self, fresh_db):
        pipeline = _StubPipeline(
            items=[_ctx("i0")],
            clusters=[ClusterSpec(label="A", item_ids=("i0",))],
            umbrella_summary_extra={"custom_field": "custom_value"},
        )
        result = run_pipeline(
            pipeline, universal_actions=ActionLibrary([]),
        )
        u = store.get_thread(result.umbrella_id)
        assert u.inciting_event_summary["title"] == "Test umbrella"
        assert u.inciting_event_summary["custom_field"] == "custom_value"
        assert u.inciting_event_summary["source"] == "test_source"

    def test_collect_kwargs_forwarded_to_umbrella(self, fresh_db):
        captured_kwargs = {}

        class PassThroughPipeline(_StubPipeline):
            def collect(self, **kwargs):
                captured_kwargs.update(kwargs)
                return [_ctx("i0")]

        pipeline = PassThroughPipeline(
            items=[],
            clusters=[ClusterSpec(label="A", item_ids=("i0",))],
        )
        run_pipeline(
            pipeline,
            universal_actions=ActionLibrary([]),
            journal_date="2026-04-01",
            scrape_id="abc-123",
        )
        assert captured_kwargs == {
            "journal_date": "2026-04-01",
            "scrape_id": "abc-123",
        }

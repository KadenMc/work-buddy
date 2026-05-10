"""Tests for ``work_buddy.pipelines.inline.inline_capture``.

Stubs the LLM and the inline-selection adapter so the pipeline
exercises end-to-end without touching the verdict LLM, the segmenter,
or the user-context builders.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from work_buddy.clarify.items import TriageItem
from work_buddy.llm.response import LLMResponse
from work_buddy.pipelines.inline import (
    _action_payload_for_record,
    _confidence_for_record,
    _plan_summary_for_task,
    inline_capture,
)
from work_buddy.threads import store
from work_buddy.threads.enums import FSMState


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Per-test threads DB."""
    threads_db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: threads_db)
    yield


# ---------------------------------------------------------------------------
# Stubbing helpers
# ---------------------------------------------------------------------------


def _stub_collect(text: str = "Send Q4 status to Sarah by Friday") -> tuple:
    """Patch target for ``collect_inline_selection`` returning one TriageItem."""
    item = TriageItem(
        id="inline_abc",
        text=text,
        label=text[:40],
        source="inline",
        url=None,
        metadata={
            "file_path": "Daily/2026-05-09.md",
            "cursor_line": 12,
            "hint": "",
        },
    )

    def _fake_collect(**_kwargs):
        return [item], "hash_abc"

    return _fake_collect


def _patch_runner(verdict_dict: dict | LLMResponse):
    """Patch LLMRunner so its .call(...) returns a stub response."""
    if isinstance(verdict_dict, LLMResponse):
        resp = verdict_dict
    else:
        resp = LLMResponse(
            structured_output=verdict_dict, model="claude-sonnet-4-5",
        )
    runner_instance = MagicMock()
    runner_instance.call.return_value = resp
    runner_cls = MagicMock(return_value=runner_instance)
    return patch("work_buddy.llm.LLMRunner", runner_cls), runner_instance


def _patch_deadline_extract(hints: dict | None = None):
    """Patch deadline extraction so the inline pipeline doesn't try to call
    a real LLM for hints."""
    default_hints = {
        "has_deadline": False,
        "deadline_date": None,
        "has_dependency": False,
        "dependency_hint": None,
    }
    return patch(
        "work_buddy.clarify.deadline_extract.extract_deadline_hints",
        return_value=hints or default_hints,
    )


def _patch_pick_projects(candidates: list[dict] | None = None):
    """Patch the project-picker so the inline pipeline doesn't make a real
    LLM call for project candidates.

    Without this patch, ``pick_projects`` constructs its own LLMRunner via
    ``run_subcall`` (which imports LLMRunner from ``work_buddy.llm.runner_v2``
    directly, NOT from ``work_buddy.llm``) — so the existing
    ``_patch_runner`` patch on ``work_buddy.llm.LLMRunner`` doesn't reach
    it. Live tests showed inline-pipeline tests were leaking real
    project-picker LLM calls; this helper plugs the leak.
    """
    default_candidates = [
        {
            "project_tag": None,
            "confidence": 1.0,
            "rationale": "Test stub: no project signal.",
        },
    ]
    return patch(
        "work_buddy.clarify.project_picker.pick_projects",
        return_value={"candidates": candidates or default_candidates},
    )


# ---------------------------------------------------------------------------
# Action-payload mapping
# ---------------------------------------------------------------------------


class TestActionPayloadForRecord:
    def test_task_destination(self):
        record = {
            "destination": "task",
            "task_proposal": {
                "suggested_task_text": "Send Q4 status to Sarah",
                "definition_of_done": "Email sent and acknowledged",
                "has_deadline": True,
                "deadline_date": "2026-05-15",
            },
        }
        item = TriageItem(
            id="x", text="t", label="x", source="inline", metadata={},
        )
        payload, title = _action_payload_for_record(
            record=record, item=item,
            group_intent="Q4 update", rationale="Sarah asked.",
        )
        assert payload["kind"] == "standard"
        assert payload["name"] == "task_create"
        assert payload["parameters"]["task_text"] == "Send Q4 status to Sarah"
        assert payload["parameters"]["has_deadline"] is True
        assert payload["parameters"]["deadline_date"] == "2026-05-15"
        assert payload["parameters"]["creation_provenance"] == "inline-inferred"
        assert payload["parameters"]["user_involvement"] == "high"
        assert "Sarah" in title

    def test_reference_destination(self):
        record = {
            "destination": "reference",
            "reference_proposal": {
                "summary": "MIT paper draft notes",
                "suggested_path": "papers/mit-2026/draft-notes.md",
            },
        }
        item = TriageItem(
            id="x", text="t", label="x", source="inline", metadata={},
        )
        payload, title = _action_payload_for_record(
            record=record, item=item,
            group_intent="Reference filing", rationale="",
        )
        assert payload["kind"] == "suggestion"
        assert payload["name"] == "reference_capture_suggested"
        assert payload["parameters"]["summary"] == "MIT paper draft notes"
        assert "no reference-capture capability" in payload["blocked_on"].lower()
        assert title == "MIT paper draft notes"

    def test_calendar_destination(self):
        record = {
            "destination": "calendar_only",
            "calendar_proposal": {
                "title": "Sarah's birthday",
                "datetime": "2026-05-12",
                "all_day": True,
            },
        }
        item = TriageItem(
            id="x", text="t", label="x", source="inline", metadata={},
        )
        payload, title = _action_payload_for_record(
            record=record, item=item,
            group_intent="", rationale="",
        )
        assert payload["kind"] == "suggestion"
        assert payload["name"] == "calendar_event_suggested"
        assert payload["parameters"]["title"] == "Sarah's birthday"
        assert payload["parameters"]["datetime"] == "2026-05-12"
        assert payload["parameters"]["all_day"] is True
        assert title == "Sarah's birthday"

    def test_unknown_destination_returns_none(self):
        record = {"destination": "alien"}
        item = TriageItem(
            id="x", text="t", label="x", source="inline", metadata={},
        )
        payload, title = _action_payload_for_record(
            record=record, item=item, group_intent="", rationale="",
        )
        assert payload is None
        assert title == ""


class TestPlanSummary:
    def test_with_deadline(self):
        s = _plan_summary_for_task(
            {"has_deadline": True, "deadline_date": "2026-05-15"},
            "Do thing",
        )
        assert "2026-05-15" in s
        assert "Do thing" in s

    def test_with_dependency(self):
        s = _plan_summary_for_task(
            {"has_dependency": True, "dependency_hint": "Bob's review"},
            "Do thing",
        )
        assert "Bob's review" in s

    def test_kind_project(self):
        s = _plan_summary_for_task(
            {"kind": "project"},
            "Ship feature",
        )
        assert "(project)" in s


class TestConfidenceForRecord:
    def test_default_when_missing(self):
        assert _confidence_for_record({}) == 0.5

    def test_low_uncertainty_high_confidence(self):
        rec = {
            "task_proposal": {
                "risk_profile": {"inference_uncertainty": "low"},
            },
        }
        assert _confidence_for_record(rec) == 0.85

    def test_high_uncertainty_low_confidence(self):
        rec = {
            "task_proposal": {
                "risk_profile": {"inference_uncertainty": "high"},
            },
        }
        assert _confidence_for_record(rec) == 0.35


# ---------------------------------------------------------------------------
# Single-record paths
# ---------------------------------------------------------------------------


class TestSingleRecordPaths:
    def test_task_record_spawns_one_standalone_thread(self, fresh_db):
        verdict = {
            "rationale": "Sarah asked for a status by Friday.",
            "group_intent": "Q4 status to Sarah",
            "records": [
                {
                    "destination": "task",
                    "task_proposal": {
                        "suggested_task_text": "Send Q4 status to Sarah",
                        "creation_effort": "developed",
                    },
                },
            ],
        }
        runner_patch, _runner = _patch_runner(verdict)
        with (
            patch(
                "work_buddy.pipelines.inline._collect_inline_selection",
                side_effect=_stub_collect(),
            ),
            _patch_deadline_extract(),
            _patch_pick_projects(),
            patch(
                "work_buddy.clarify.recommend.build_triage_context",
                return_value={},
            ),
            runner_patch,
        ):
            result = inline_capture(
                file_path="Daily/2026-05-09.md",
                selection="Send Q4 status to Sarah by Friday",
                paragraph="",
                cursor_line=12,
                hint="",
                tier_chain=["frontier_balanced"],
            )

        assert result["status"] == "ok"
        assert result["umbrella_id"] is None
        assert result["single_thread_id"] is not None
        assert result["child_thread_ids"] == []
        assert result["dropped_count"] == 0

        # The spawned Thread is in AWAITING_CONFIRMATION with the
        # captured selection as a ContextItem.
        spawned = store.get_thread(result["single_thread_id"])
        assert spawned.fsm_state == FSMState.AWAITING_CONFIRMATION
        assert any(ci.source == "inline_selection" for ci in spawned.context_items)

    def test_all_delete_records_spawn_dismissed_thread(self, fresh_db):
        verdict = {
            "rationale": "Test ping; no action needed.",
            "group_intent": "Test ping",
            "records": [
                {
                    "destination": "delete",
                    "delete_reason": "Test ping with no signal.",
                },
            ],
        }
        runner_patch, _runner = _patch_runner(verdict)
        with (
            patch(
                "work_buddy.pipelines.inline._collect_inline_selection",
                side_effect=_stub_collect("..."),
            ),
            _patch_deadline_extract(),
            _patch_pick_projects(),
            patch(
                "work_buddy.clarify.recommend.build_triage_context",
                return_value={},
            ),
            runner_patch,
        ):
            result = inline_capture(
                file_path="Daily/2026-05-09.md",
                selection="...",
                tier_chain=["frontier_balanced"],
            )

        assert result["status"] == "ok"
        assert result["dropped_count"] == 1
        assert result["single_thread_id"] is not None

        spawned = store.get_thread(result["single_thread_id"])
        assert spawned.fsm_state == FSMState.DISMISSED


# ---------------------------------------------------------------------------
# Multi-record paths
# ---------------------------------------------------------------------------


class TestMultiRecordPaths:
    def test_two_actionable_records_spawn_umbrella_plus_children(self, fresh_db):
        verdict = {
            "rationale": "Birthday gift + the day itself.",
            "group_intent": "Sarah's birthday",
            "records": [
                {
                    "destination": "task",
                    "task_proposal": {
                        "suggested_task_text": "Buy gift for Sarah",
                    },
                },
                {
                    "destination": "calendar_only",
                    "calendar_proposal": {
                        "title": "Sarah's birthday",
                        "datetime": "2026-05-12",
                        "all_day": True,
                    },
                },
            ],
        }
        runner_patch, _runner = _patch_runner(verdict)
        with (
            patch(
                "work_buddy.pipelines.inline._collect_inline_selection",
                side_effect=_stub_collect("Buy gift for Sarah's birthday May 12"),
            ),
            _patch_deadline_extract(),
            _patch_pick_projects(),
            patch(
                "work_buddy.clarify.recommend.build_triage_context",
                return_value={},
            ),
            runner_patch,
        ):
            result = inline_capture(
                file_path="x.md",
                selection="Buy gift for Sarah's birthday May 12",
                tier_chain=["frontier_balanced"],
            )

        assert result["status"] == "ok"
        assert result["umbrella_id"] is not None
        assert len(result["child_thread_ids"]) == 2
        assert result["single_thread_id"] is None

        umbrella = store.get_thread(result["umbrella_id"])
        assert umbrella.fsm_state == FSMState.MONITORING
        # Singular-pattern marker (Stage 1 of the singular-fix). The
        # render layer keys on this to hoist children's actions onto
        # the umbrella's card. Distinguishes inline-singular from
        # cluster-group umbrellas (chrome/journal/email use 'group').
        assert umbrella.parent_relationship == "singular"

        for cid in result["child_thread_ids"]:
            child = store.get_thread(cid)
            assert child.parent_id == result["umbrella_id"]
            assert child.fsm_state == FSMState.AWAITING_CONFIRMATION

    def test_mixed_records_drop_count_tracks_deletes(self, fresh_db):
        verdict = {
            "rationale": "One real action; one stray fragment to drop.",
            "group_intent": "Mixed capture",
            "records": [
                {
                    "destination": "task",
                    "task_proposal": {"suggested_task_text": "Real task"},
                },
                {
                    "destination": "delete",
                    "delete_reason": "Stray fragment.",
                },
            ],
        }
        runner_patch, _runner = _patch_runner(verdict)
        with (
            patch(
                "work_buddy.pipelines.inline._collect_inline_selection",
                side_effect=_stub_collect(),
            ),
            _patch_deadline_extract(),
            _patch_pick_projects(),
            patch(
                "work_buddy.clarify.recommend.build_triage_context",
                return_value={},
            ),
            runner_patch,
        ):
            result = inline_capture(
                file_path="x.md", selection="Real task plus junk",
                tier_chain=["frontier_balanced"],
            )

        # 1 actionable record → standalone Thread (no umbrella).
        assert result["status"] == "ok"
        assert result["dropped_count"] == 1
        assert result["umbrella_id"] is None
        assert result["single_thread_id"] is not None


# ---------------------------------------------------------------------------
# Refusal path
# ---------------------------------------------------------------------------


class TestRefusal:
    def test_refusal_spawns_clarification_thread(self, fresh_db):
        verdict = {
            "rationale": "Need more context.",
            "group_intent": "Ambiguous capture",
            "refusal": {
                "question": "Which project does this belong to?",
                "missing_context": ["project"],
            },
        }
        runner_patch, _runner = _patch_runner(verdict)
        with (
            patch(
                "work_buddy.pipelines.inline._collect_inline_selection",
                side_effect=_stub_collect("Vague stuff"),
            ),
            _patch_deadline_extract(),
            _patch_pick_projects(),
            patch(
                "work_buddy.clarify.recommend.build_triage_context",
                return_value={},
            ),
            runner_patch,
        ):
            result = inline_capture(
                file_path="x.md", selection="Vague stuff",
                tier_chain=["frontier_balanced"],
            )

        assert result["status"] == "refusal"
        assert result["single_thread_id"] is not None
        spawned = store.get_thread(result["single_thread_id"])
        assert spawned.fsm_state == FSMState.AWAITING_INTENT_CLARIFICATION
        # Refusal payload is preserved on inciting summary.
        ie = spawned.inciting_event_summary or {}
        assert ie.get("refusal", {}).get("question") == "Which project does this belong to?"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    def test_collect_failure_returns_error(self, fresh_db):
        with (
            patch(
                "work_buddy.pipelines.inline._collect_inline_selection",
                return_value=([], None),
            ),
        ):
            result = inline_capture(
                file_path="x.md", selection="anything",
                tier_chain=["frontier_balanced"],
            )
        assert result["status"] == "error"
        assert "no items" in result["error"].lower()

    def test_all_tiers_exhausted_returns_error(self, fresh_db):
        # LLMRunner returns an error response on every call.
        runner_instance = MagicMock()
        runner_instance.call.return_value = LLMResponse(error="timeout")
        runner_cls = MagicMock(return_value=runner_instance)
        with (
            patch(
                "work_buddy.pipelines.inline._collect_inline_selection",
                side_effect=_stub_collect(),
            ),
            _patch_deadline_extract(),
            _patch_pick_projects(),
            patch(
                "work_buddy.clarify.recommend.build_triage_context",
                return_value={},
            ),
            patch("work_buddy.llm.LLMRunner", runner_cls),
        ):
            result = inline_capture(
                file_path="x.md", selection="anything",
                tier_chain=["local_tool_calling", "frontier_balanced"],
            )
        assert result["status"] == "error"
        assert runner_instance.call.call_count == 2

    def test_tier_chain_walks_to_success(self, fresh_db):
        """First tier errors; second tier returns a good verdict."""
        good = {
            "rationale": "ok",
            "group_intent": "test",
            "records": [
                {"destination": "task", "task_proposal": {"suggested_task_text": "X"}},
            ],
        }
        runner_instance = MagicMock()
        runner_instance.call.side_effect = [
            LLMResponse(error="timeout"),
            LLMResponse(structured_output=good, model="claude-sonnet-4-5"),
        ]
        runner_cls = MagicMock(return_value=runner_instance)
        with (
            patch(
                "work_buddy.pipelines.inline._collect_inline_selection",
                side_effect=_stub_collect(),
            ),
            _patch_deadline_extract(),
            _patch_pick_projects(),
            patch(
                "work_buddy.clarify.recommend.build_triage_context",
                return_value={},
            ),
            patch("work_buddy.llm.LLMRunner", runner_cls),
        ):
            result = inline_capture(
                file_path="x.md", selection="X",
                tier_chain=["local_tool_calling", "frontier_balanced"],
            )
        assert result["status"] == "ok"
        assert runner_instance.call.call_count == 2
        assert result["single_thread_id"] is not None

"""Tests for the project-picker SubCall.

The picker emits a hedged ranked-candidate list; this suite verifies:
- Schema-shaped output reaches the verdict prompt as expected.
- Slug validation drops candidates not in the active project registry.
- Dedup keeps the higher-confidence entry per slug.
- The null (no-project) candidate is always present (injected if absent).
- Sorting is by confidence descending.
- max_candidates caps the list, preserving null.
- Soft-fail on LLM exhaustion returns just the null candidate.
- The render block formats the candidate list for the verdict prompt.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from work_buddy.clarify.project_picker import (
    PROJECT_PICKER_SUBCALL,
    _validate_and_normalize_candidates,
    pick_projects,
    render_project_candidates_block,
)
from work_buddy.llm.response import ErrorKind, LLMResponse, TierAttempt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(structured: dict) -> LLMResponse:
    return LLMResponse(
        content="",
        structured_output=structured,
        tier_used="local_fast",
        tier_attempts=(
            TierAttempt(
                tier="local_fast", model="qwen3-4b",
                error_kind=None, error=None,
                elapsed_ms=42, outcome="success",
            ),
        ),
        model="qwen3-4b",
    )


def _err() -> LLMResponse:
    return LLMResponse(
        content="",
        structured_output=None,
        tier_used="frontier_fast",
        tier_attempts=(
            TierAttempt(
                tier="frontier_fast", model="claude-haiku",
                error_kind=ErrorKind.TIMEOUT, error="boom",
                elapsed_ms=99, outcome="backend_error",
            ),
        ),
        error="boom", error_kind=ErrorKind.TIMEOUT,
    )


_PROJECTS = [
    {"slug": "tka_paper", "name": "TKA Paper",
     "status": "active", "description": "ECG paper for ICML"},
    {"slug": "ifs_explorer", "name": "IFS Explorer",
     "status": "active", "description": "Internal Family Systems tool"},
    {"slug": "muse_eeg", "name": "Muse EEG",
     "status": "paused", "description": "Neuroadaptive focus sessions"},
]


# ---------------------------------------------------------------------------
# SubCall declaration sanity
# ---------------------------------------------------------------------------


def test_subcall_declaration() -> None:
    assert PROJECT_PICKER_SUBCALL.config_key == "triage.project_picker"
    assert PROJECT_PICKER_SUBCALL.fail_policy == "soft"
    default = PROJECT_PICKER_SUBCALL.soft_fail_default
    assert default is not None
    cands = default["candidates"]
    assert len(cands) == 1
    assert cands[0]["project_tag"] is None
    assert 0.0 <= cands[0]["confidence"] <= 1.0


# ---------------------------------------------------------------------------
# _validate_and_normalize_candidates — pure-logic
# ---------------------------------------------------------------------------


def test_validate_drops_unknown_slugs() -> None:
    raw = {"candidates": [
        {"project_tag": "tka_paper", "confidence": 0.8, "rationale": "..."},
        {"project_tag": "made_up", "confidence": 0.5, "rationale": "..."},
        {"project_tag": None, "confidence": 0.2, "rationale": "..."},
    ]}
    out = _validate_and_normalize_candidates(
        raw, active_slugs={"tka_paper", "ifs_explorer"},
        max_candidates=5,
    )
    slugs = [c["project_tag"] for c in out["candidates"]]
    assert "made_up" not in slugs
    assert "tka_paper" in slugs
    assert None in slugs


def test_validate_dedupes_keeps_higher_confidence() -> None:
    raw = {"candidates": [
        {"project_tag": "tka_paper", "confidence": 0.4, "rationale": "low"},
        {"project_tag": "tka_paper", "confidence": 0.8, "rationale": "high"},
        {"project_tag": None, "confidence": 0.1, "rationale": "..."},
    ]}
    out = _validate_and_normalize_candidates(
        raw, active_slugs={"tka_paper"}, max_candidates=5,
    )
    tka = [c for c in out["candidates"] if c["project_tag"] == "tka_paper"]
    assert len(tka) == 1
    assert tka[0]["confidence"] == pytest.approx(0.8)
    assert tka[0]["rationale"] == "high"


def test_validate_clamps_confidence_to_unit_interval() -> None:
    raw = {"candidates": [
        {"project_tag": "tka_paper", "confidence": 1.7, "rationale": "..."},
        {"project_tag": None, "confidence": -0.4, "rationale": "..."},
    ]}
    out = _validate_and_normalize_candidates(
        raw, active_slugs={"tka_paper"}, max_candidates=5,
    )
    by_tag = {c["project_tag"]: c for c in out["candidates"]}
    assert by_tag["tka_paper"]["confidence"] == 1.0
    assert by_tag[None]["confidence"] == 0.0


def test_validate_sorts_by_confidence_descending() -> None:
    raw = {"candidates": [
        {"project_tag": "tka_paper", "confidence": 0.3, "rationale": "..."},
        {"project_tag": "ifs_explorer", "confidence": 0.7, "rationale": "..."},
        {"project_tag": None, "confidence": 0.1, "rationale": "..."},
    ]}
    out = _validate_and_normalize_candidates(
        raw, active_slugs={"tka_paper", "ifs_explorer"}, max_candidates=5,
    )
    confs = [c["confidence"] for c in out["candidates"]]
    assert confs == sorted(confs, reverse=True)


def test_validate_caps_at_max_candidates_preserving_null() -> None:
    raw = {"candidates": [
        {"project_tag": "tka_paper", "confidence": 0.8, "rationale": "..."},
        {"project_tag": "ifs_explorer", "confidence": 0.7, "rationale": "..."},
        {"project_tag": "muse_eeg", "confidence": 0.6, "rationale": "..."},
        {"project_tag": "extra_one", "confidence": 0.5, "rationale": "..."},
        {"project_tag": "extra_two", "confidence": 0.4, "rationale": "..."},
        {"project_tag": "extra_three", "confidence": 0.3, "rationale": "..."},
        {"project_tag": None, "confidence": 0.05, "rationale": "..."},
    ]}
    out = _validate_and_normalize_candidates(
        raw,
        active_slugs={"tka_paper", "ifs_explorer", "muse_eeg",
                      "extra_one", "extra_two", "extra_three"},
        max_candidates=3,
    )
    assert len(out["candidates"]) == 3
    # Null must still appear despite being lowest-confidence.
    assert any(c["project_tag"] is None for c in out["candidates"])


def test_validate_injects_null_when_model_omitted_it() -> None:
    raw = {"candidates": [
        {"project_tag": "tka_paper", "confidence": 0.95, "rationale": "..."},
    ]}
    out = _validate_and_normalize_candidates(
        raw, active_slugs={"tka_paper"}, max_candidates=5,
    )
    null_entries = [c for c in out["candidates"] if c["project_tag"] is None]
    assert len(null_entries) == 1


def test_validate_passes_through_null_only_output() -> None:
    """The 'probably not project work' shape — just null at high conf."""
    raw = {"candidates": [
        {"project_tag": None, "confidence": 0.9, "rationale": "Passing thought."},
    ]}
    out = _validate_and_normalize_candidates(
        raw, active_slugs=set(), max_candidates=5,
    )
    assert len(out["candidates"]) == 1
    assert out["candidates"][0]["project_tag"] is None
    assert out["candidates"][0]["confidence"] == pytest.approx(0.9)


def test_validate_drops_malformed_entries() -> None:
    raw = {"candidates": [
        {"project_tag": "tka_paper", "confidence": 0.8, "rationale": "ok"},
        {"project_tag": 42, "confidence": 0.7, "rationale": "..."},  # tag not str/null
        {"project_tag": "tka_paper", "confidence": "huh", "rationale": "..."},  # conf non-numeric
        "not a dict",  # not a dict
        {"project_tag": None, "confidence": 0.1, "rationale": "..."},
    ]}
    out = _validate_and_normalize_candidates(
        raw, active_slugs={"tka_paper"}, max_candidates=5,
    )
    assert len(out["candidates"]) == 2
    tags = {c["project_tag"] for c in out["candidates"]}
    assert tags == {"tka_paper", None}


# ---------------------------------------------------------------------------
# pick_projects — full integration with mocked LLM
# ---------------------------------------------------------------------------


def test_pick_projects_empty_text_skips_llm() -> None:
    runner = MagicMock()
    out = pick_projects("", active_projects=_PROJECTS, runner=runner)
    runner.call.assert_not_called()
    assert len(out["candidates"]) == 1
    assert out["candidates"][0]["project_tag"] is None


def test_pick_projects_confident_single() -> None:
    runner = MagicMock()
    runner.call.return_value = _ok({
        "candidates": [
            {"project_tag": "tka_paper", "confidence": 0.92,
             "rationale": "Mentions 'TKA paper deadline'."},
            {"project_tag": None, "confidence": 0.08,
             "rationale": "Low chance text refers to something else."},
        ],
    })
    out = pick_projects(
        "Need to draft the TKA paper intro by Friday",
        active_projects=_PROJECTS,
        max_candidates=5,
        runner=runner,
    )
    assert out["candidates"][0]["project_tag"] == "tka_paper"
    assert out["candidates"][0]["confidence"] == pytest.approx(0.92)


def test_pick_projects_uncertain_multi() -> None:
    runner = MagicMock()
    runner.call.return_value = _ok({
        "candidates": [
            {"project_tag": "tka_paper", "confidence": 0.40,
             "rationale": "Could plausibly be related."},
            {"project_tag": "ifs_explorer", "confidence": 0.35,
             "rationale": "Also plausible."},
            {"project_tag": None, "confidence": 0.30,
             "rationale": "Maybe not project work at all."},
        ],
    })
    out = pick_projects(
        "Random thought about modeling decisions",
        active_projects=_PROJECTS,
        max_candidates=5,
        runner=runner,
    )
    assert len(out["candidates"]) == 3
    confs = [c["confidence"] for c in out["candidates"]]
    assert confs == sorted(confs, reverse=True)


def test_pick_projects_soft_fail_returns_null_only() -> None:
    runner = MagicMock()
    runner.call.return_value = _err()
    out = pick_projects(
        "anything",
        active_projects=_PROJECTS,
        max_candidates=5,
        runner=runner,
    )
    assert len(out["candidates"]) == 1
    assert out["candidates"][0]["project_tag"] is None


def test_pick_projects_runner_throws_returns_null_only() -> None:
    runner = MagicMock()
    runner.call.side_effect = RuntimeError("backend down")
    out = pick_projects(
        "anything",
        active_projects=_PROJECTS,
        max_candidates=5,
        runner=runner,
    )
    assert len(out["candidates"]) == 1
    assert out["candidates"][0]["project_tag"] is None


def test_pick_projects_filters_unknown_slugs_from_llm_output() -> None:
    """Even if the LLM hallucinates a slug, the post-validator drops it."""
    runner = MagicMock()
    runner.call.return_value = _ok({
        "candidates": [
            {"project_tag": "tka_paper", "confidence": 0.8, "rationale": "..."},
            {"project_tag": "phantom_project", "confidence": 0.7, "rationale": "..."},
            {"project_tag": None, "confidence": 0.2, "rationale": "..."},
        ],
    })
    out = pick_projects(
        "anything",
        active_projects=_PROJECTS,
        max_candidates=5,
        runner=runner,
    )
    slugs = [c["project_tag"] for c in out["candidates"]]
    assert "phantom_project" not in slugs
    assert "tka_paper" in slugs


def test_pick_projects_passes_active_projects_to_user_prompt() -> None:
    """The user-prompt builder should list each active project."""
    runner = MagicMock()
    runner.call.return_value = _ok({
        "candidates": [{"project_tag": None, "confidence": 1.0, "rationale": "..."}],
    })
    pick_projects(
        "scoring text",
        active_projects=_PROJECTS,
        runner=runner,
    )
    user_prompt = runner.call.call_args.kwargs["user"]
    assert "tka_paper" in user_prompt
    assert "ifs_explorer" in user_prompt
    assert "muse_eeg" in user_prompt
    # Project description should appear so the model can read it.
    assert "ECG paper" in user_prompt
    # The captured text should appear under its section.
    assert "scoring text" in user_prompt


def test_pick_projects_uses_hint() -> None:
    runner = MagicMock()
    runner.call.return_value = _ok({
        "candidates": [{"project_tag": None, "confidence": 1.0, "rationale": "..."}],
    })
    pick_projects(
        "text",
        active_projects=_PROJECTS,
        hint="this is for the TKA paper",
        runner=runner,
    )
    user_prompt = runner.call.call_args.kwargs["user"]
    assert "this is for the TKA paper" in user_prompt


# ---------------------------------------------------------------------------
# render_project_candidates_block
# ---------------------------------------------------------------------------


def test_render_block_lists_candidates_with_confidences() -> None:
    block = render_project_candidates_block([
        {"project_tag": "tka_paper", "confidence": 0.85,
         "rationale": "Mentions deadline."},
        {"project_tag": None, "confidence": 0.15,
         "rationale": "Could be unrelated."},
    ])
    assert "tka_paper" in block
    assert "0.85" in block
    assert "null (no project)" in block
    assert "Mentions deadline" in block


def test_render_block_empty_input_returns_empty_string() -> None:
    assert render_project_candidates_block(None) == ""
    assert render_project_candidates_block([]) == ""


def test_render_block_says_verdict_decides() -> None:
    """The block should make clear the verdict — not Python — picks."""
    block = render_project_candidates_block([
        {"project_tag": None, "confidence": 1.0, "rationale": "..."},
    ])
    # Loose check — the text should mention the verdict's role and bias toward null.
    assert "verdict" in block.lower() or "reasoning" in block.lower()
    assert "null" in block.lower() or "uncertain" in block.lower()

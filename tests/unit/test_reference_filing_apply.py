"""Slice 6 (completion): apply_reference_proposal write step + tier-aware exec.

The bridge.write_file is stubbed; the resolver consumes the
risk_profile_json kwarg + verdict.confidence to decide whether to
write or surface as a tier-1 suggestion.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from work_buddy.clarify import reference_filing as rf


@pytest.fixture
def stub_bridge(monkeypatch):
    """Replace bridge.write_file + read_file with a single MagicMock."""
    bridge = MagicMock()
    bridge.write_file.return_value = True
    bridge.read_file.return_value = "Existing file content."
    monkeypatch.setattr("work_buddy.obsidian.bridge.write_file", bridge.write_file)
    monkeypatch.setattr("work_buddy.obsidian.bridge.read_file", bridge.read_file)
    return bridge


def _verdict(action: str, *, confidence: float = 0.9) -> dict:
    return {
        "topic_label": "ECG augmentation",
        "candidate_paths": [
            {"path": "Research/ECG/aug.md", "action": action,
             "rationale": "existing file is the topical home"},
        ],
        "confidence": confidence,
        "namespace_tags": ["paper/ecg-classifier"],
    }


# ---------------------------------------------------------------------------
# Tier-aware behavior
# ---------------------------------------------------------------------------


def test_low_confidence_caps_at_tier_1_no_write(stub_bridge):
    res = rf.apply_reference_proposal(
        summary="Random snippet about augmentation.",
        verdict=_verdict("extend", confidence=0.2),
    )
    assert res.status == "suggested"
    assert res.tier == 1
    assert res.write_result is None
    stub_bridge.write_file.assert_not_called()


def test_high_confidence_writes_when_tier_3(stub_bridge):
    res = rf.apply_reference_proposal(
        summary="A new paragraph about augmentation.",
        verdict=_verdict("extend", confidence=0.9),
    )
    # Default risk profile resolves to tier 3 in the safe-profile world.
    assert res.status == "ok"
    assert res.tier >= 2
    assert res.write_result is True
    stub_bridge.write_file.assert_called_once()
    args, kwargs = stub_bridge.write_file.call_args
    assert args[0] == "Research/ECG/aug.md"
    # Body composed via _extend_existing_file: existing + section heading + body.
    written = args[1]
    assert "ECG augmentation" in written
    assert "Existing file content" in written


def test_new_file_action_writes_full_body(stub_bridge):
    stub_bridge.read_file.return_value = None  # file does not exist
    res = rf.apply_reference_proposal(
        summary="Brand new topic.",
        verdict=_verdict("new_file", confidence=0.95),
    )
    assert res.status == "ok"
    assert res.action == "new_file"
    args, _ = stub_bridge.write_file.call_args
    assert "Brand new topic" in args[1]
    assert "type: reference" in args[1]


def test_sibling_action_derives_path_from_neighbour(stub_bridge):
    res = rf.apply_reference_proposal(
        summary="Adjacent topic.",
        verdict=_verdict("sibling", confidence=0.9),
    )
    assert res.status == "ok"
    assert res.action == "sibling"
    args, _ = stub_bridge.write_file.call_args
    written_path = args[0]
    # Sibling derives a slug from topic_label next to the neighbour.
    assert written_path.startswith("Research/ECG/")
    assert written_path.endswith(".md")
    assert "ecg-augmentation" in written_path


def test_extend_falls_back_to_new_file_when_target_missing(stub_bridge):
    """PR #70 fix #6: silent extend->new_file degradation now surfaces
    via action='extended_as_new' in the result so the audit trail is
    honest about what landed."""
    stub_bridge.read_file.return_value = None
    res = rf.apply_reference_proposal(
        summary="topic body.",
        verdict=_verdict("extend", confidence=0.9),
    )
    assert res.status == "ok"
    # PR #70 fix #6: action surfaces the degradation.
    assert res.action == "extended_as_new"
    # write_file called even though action was 'extend' (degraded path).
    stub_bridge.write_file.assert_called_once()


def test_extend_keeps_action_as_extend_when_target_exists(stub_bridge):
    """Sanity: when extend target IS present, action stays 'extend'
    (the fix only changes the missing-target path)."""
    # Default stub_bridge.read_file returns the existing-content string.
    res = rf.apply_reference_proposal(
        summary="addendum.",
        verdict=_verdict("extend", confidence=0.9),
    )
    assert res.status == "ok"
    assert res.action == "extend"


# ---------------------------------------------------------------------------
# PR #70 fix #5: sibling/new_file slug collision
# ---------------------------------------------------------------------------


def test_new_file_collision_appends_n(stub_bridge):
    """When the chosen new_file path already exists, the resolver
    derives <stem>-2.md (then -3, -4, ...) instead of overwriting."""
    # First read (existence probe of the bare path) returns existing
    # content -- collision.  Second read (the -2 candidate) returns
    # None -- free.
    stub_bridge.read_file.side_effect = ["existing", None]
    res = rf.apply_reference_proposal(
        summary="x",
        verdict=_verdict("new_file", confidence=0.95),
    )
    args, _ = stub_bridge.write_file.call_args
    assert res.status == "ok"
    assert args[0].endswith("-2.md"), (
        f"expected -2 suffix on collision, got {args[0]}"
    )


def test_sibling_collision_appends_n(stub_bridge):
    """Same logic on the sibling path.  The slug derives from
    topic_label, then collision-resolves."""
    # The sibling path is derived from the neighbour ('Research/ECG/aug.md')
    # + topic_label ('ECG augmentation') -- yields 'Research/ECG/ecg-augmentation.md'.
    # First read = collision; second = free.
    stub_bridge.read_file.side_effect = ["existing sibling", None]
    res = rf.apply_reference_proposal(
        summary="x",
        verdict=_verdict("sibling", confidence=0.95),
    )
    args, _ = stub_bridge.write_file.call_args
    assert res.status == "ok"
    assert args[0].endswith("-2.md")


def test_new_file_no_collision_writes_at_base_path(stub_bridge):
    """When the path is free, no rename happens."""
    stub_bridge.read_file.return_value = None  # path is free
    res = rf.apply_reference_proposal(
        summary="x",
        verdict=_verdict("new_file", confidence=0.95),
    )
    args, _ = stub_bridge.write_file.call_args
    assert args[0] == "Research/ECG/aug.md"
    assert res.action == "new_file"


def test_no_candidates_fails(stub_bridge):
    res = rf.apply_reference_proposal(
        summary="x",
        verdict={"topic_label": "x", "candidate_paths": []},
    )
    assert res.status == "failed"


def test_dict_verdict_parsed_internally(stub_bridge):
    """Caller can pass a raw dict and the function parses it."""
    res = rf.apply_reference_proposal(
        summary="x",
        verdict=_verdict("new_file"),
    )
    assert res.status == "ok"


def test_write_failure_surfaces_status_failed(stub_bridge):
    stub_bridge.write_file.return_value = False
    res = rf.apply_reference_proposal(
        summary="x",
        verdict=_verdict("new_file", confidence=0.9),
    )
    assert res.status == "failed"
    assert res.write_result is False

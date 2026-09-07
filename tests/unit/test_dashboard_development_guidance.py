"""Dashboard guidance activation and proportional-scope contracts."""

from pathlib import Path

import pytest

from work_buddy.harness.sync import _instructions_for_harness
from work_buddy.knowledge.file_store import read_unit
from work_buddy.mcp_server.conductor import _validate_step_result


REPO = Path(__file__).resolve().parents[2]


def _read(path):
    return read_unit(REPO / "knowledge/store", path)


@pytest.mark.parametrize("harness", ["claudecode", "codexcli"])
def test_canonical_router_activates_for_dashboard_intent_without_a_slash_command(harness):
    instructions = _instructions_for_harness((REPO / "CLAUDE.md").read_text(encoding="utf-8"), harness)
    router = next(line for line in instructions.splitlines() if "dev/dashboard/ux-directions" in line)
    assert "When planning or implementing changes" in router
    assert "dashboard user tasks or user-visible behavior" in router
    assert "dev/dashboard/verification-directions" in router
    assert "before deciding the interaction design or opening a browser" in router
    assert "/wb-dev" not in router
    assert "Do not perform a dashboard review for unrelated work" in instructions


def test_orientation_declares_optional_surface_evidence_and_reassesses_generated_ui():
    orientation = _read("dev/dev-orient")
    step = orientation["steps"][0]
    schema = step["result_schema"]
    for key in ("affected_surfaces", "required_reviews", "review_scope"):
        assert schema["key_types"][key] == "list"
        assert key not in schema["required_keys"]
    instruction = orientation["step_instructions"]["orient"]
    assert "Python changes that alter generated surfaces" in instruction
    assert "Settings" in instruction
    assert "Reassess when scope changes" in instruction
    assert "For unrelated work leave these lists empty" in instruction
    assert "load `dev/dashboard/ux-directions` before deciding" in instruction

    evidence = {
        "units_read": ["services/dashboard/react"],
        "files_read": ["dashboard-react/src/apps/cowork/CoworkApp.tsx"],
        "wrappers_found": ["HttpCoworkProvider"],
        "affected_surfaces": ["dashboard:cowork"],
        "required_reviews": ["dashboard-ux"],
        "review_scope": ["Create a document and find it again after reload"],
    }
    assert _validate_step_result("orient", evidence, schema) is None
    for key in ("affected_surfaces", "required_reviews", "review_scope"):
        evidence[key] = []
    assert _validate_step_result("orient", evidence, schema) is None


def test_ux_depth_and_evidence_do_not_turn_unrelated_work_into_a_review():
    directions = _read("dev/dashboard/ux-directions")["content"]["full"]
    for change in ("Wording", "Moved or restyled control", "New or changed mutation", "Shared navigation", "Python registration"):
        assert change in directions
    assert "without approval stops at ordinary milestones" in directions
    assert "Do not invent personas" in directions
    for label in ("live-verified", "source-inferred", "design-hypothesis", "untested"):
        assert label in directions

from __future__ import annotations

from pathlib import Path

import yaml

from work_buddy.knowledge.capability_loader import load_declared_capabilities
from work_buddy.mcp_server.op_registry import clear_ops


EXPECTED = {
    "truth_store_create",
    "truth_store_list",
    "truth_evidence_capture",
    "truth_span_mark",
    "truth_claim_propose",
    "truth_claim_confirm",
    "truth_claim_reject",
    "truth_claim_challenge",
    "truth_claim_supersede",
    "truth_claim_redact",
    "truth_query",
    "truth_sweep",
}


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    _, raw, _ = text.split("---", 2)
    return yaml.safe_load(raw)


def test_all_truth_declarations_resolve_without_signature_issues() -> None:
    clear_ops()
    capabilities, issues = load_declared_capabilities()
    found = {item.name for item in capabilities if item.name.startswith("truth_")}
    truth_issues = [
        issue
        for issue in issues
        if issue["path"] == "truth" or issue["path"].startswith("truth/")
    ]
    assert found == EXPECTED
    assert truth_issues == []


def test_truth_declarations_are_manual_for_writes_and_searchable() -> None:
    root = Path("knowledge/store/truth")
    declarations = [_frontmatter(path) for path in root.glob("*.md")]
    assert {item["capability_name"] for item in declarations} == EXPECTED
    for item in declarations:
        assert item["parents"] == ["truth"]
        assert len(item["aliases"]) >= 4
        if item["capability_name"] in {"truth_store_list", "truth_query"}:
            assert item.get("mutates_state", False) is False
        else:
            assert item["mutates_state"] is True
            assert item["retry_policy"] == "manual"
            assert item["auto_retry"] is False


def test_exact_decisions_declare_only_per_claim_consent_operations() -> None:
    root = Path("knowledge/store/truth")
    by_name = {
        item["capability_name"]: item
        for item in (_frontmatter(path) for path in root.glob("*.md"))
    }
    assert by_name["truth_claim_confirm"]["consent_operations"] == [
        "truth.claim_confirm"
    ]
    assert by_name["truth_claim_reject"]["consent_operations"] == [
        "truth.claim_reject"
    ]
    assert by_name["truth_claim_redact"]["consent_operations"] == [
        "truth.claim_redact"
    ]
    for name in EXPECTED - {
        "truth_store_create",
        "truth_claim_confirm",
        "truth_claim_reject",
        "truth_claim_redact",
    }:
        assert "consent_operations" not in by_name[name]

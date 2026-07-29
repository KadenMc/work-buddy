"""Deterministic containment checks for private Verify revisions."""

from __future__ import annotations

import pytest

from work_buddy.cowork.verify_candidate_evaluation import (
    CandidateEvaluationError,
    evaluate_terminology_candidate,
    sanitize_candidate_evaluations,
)
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.identity import canonical_json


def _configuration(non_preferred: str = "Co-work scope") -> dict:
    return {
        "criteria": [
            {
                "stable_key": "terminology_exact_match",
                "operational_state": "active",
                "checks": [
                    {
                        "availability": {"state": "available"},
                        "binding": {
                            "selected": True,
                            "configuration": {
                                "terms": [
                                    {
                                        "non_preferred": non_preferred,
                                        "preferred": "document target",
                                    }
                                ]
                            },
                        },
                    }
                ],
            }
        ]
    }


def _evidence(text: str, exact: str) -> str:
    start = text.index(exact)
    return canonical_json(
        {
            "kind": "text_quote",
            "selector": CompositeSelector(
                exact=exact,
                start=start,
                end=start + len(exact),
            ).to_web_annotation(),
        }
    )


def test_candidate_passes_only_after_the_affected_term_is_removed():
    text = "The current Co-work scope should be named clearly."
    evidence = _evidence(text, "Co-work scope")

    passed = evaluate_terminology_candidate(
        projection=text,
        evaluation_result_id="result-1",
        evidence_selector_json=evidence,
        replacement="document target",
        effective_configuration=_configuration(),
    )
    failed = evaluate_terminology_candidate(
        projection=text,
        evaluation_result_id="result-1",
        evidence_selector_json=evidence,
        replacement="Co-work scope again",
        effective_configuration=_configuration(),
    )

    assert passed["status"] == "passed"
    assert passed["match_count"] == 0
    assert failed["status"] == "failed"
    assert failed["matches"][0]["non_preferred"] == "Co-work scope"


def test_candidate_evaluation_catches_a_term_created_across_edit_boundaries():
    text = "prefix Co-work Xscope suffix"
    evidence = _evidence(text, "X")

    result = evaluate_terminology_candidate(
        projection=text,
        evaluation_result_id="result-boundary",
        evidence_selector_json=evidence,
        replacement="",
        effective_configuration=_configuration(),
    )

    assert result["status"] == "failed"
    assert result["matches"][0]["candidate_relative_start"] < 0
    assert result["matches"][0]["candidate_relative_end"] > 0


def test_portable_candidate_proof_rejects_tampering():
    text = "The current Co-work scope should be named clearly."
    proof = evaluate_terminology_candidate(
        projection=text,
        evaluation_result_id="result-1",
        evidence_selector_json=_evidence(text, "Co-work scope"),
        replacement="document target",
        effective_configuration=_configuration(),
    )

    assert sanitize_candidate_evaluations([proof]) == [proof]
    with pytest.raises(
        CandidateEvaluationError,
        match="canonical hash does not match",
    ):
        sanitize_candidate_evaluations([{**proof, "status": "failed"}])

"""Deterministic affected-region evaluation for private Verify candidates."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from work_buddy.cowork.verify import terminology_exact_matches
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.identity import canonical_json, sha256_text


class CandidateEvaluationError(ValueError):
    """A candidate cannot be evaluated against its frozen evidence/config."""


def _digest(value: object, label: str) -> str:
    text = str(value or "")
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise CandidateEvaluationError(f"{label} must be a SHA-256 digest")
    return text


def _terminology_configuration(
    effective_configuration: Mapping[str, Any],
) -> Mapping[str, Any]:
    criteria = effective_configuration.get("criteria")
    if not isinstance(criteria, list):
        raise CandidateEvaluationError(
            "effective configuration has no criteria list"
        )
    admitted: list[Mapping[str, Any]] = []
    for criterion in criteria:
        if (
            not isinstance(criterion, Mapping)
            or criterion.get("stable_key") != "terminology_exact_match"
            or criterion.get("operational_state") != "active"
        ):
            continue
        checks = criterion.get("checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, Mapping):
                continue
            binding = check.get("binding")
            availability = check.get("availability")
            if (
                isinstance(binding, Mapping)
                and binding.get("selected") is True
                and isinstance(availability, Mapping)
                and availability.get("state") == "available"
                and isinstance(binding.get("configuration"), Mapping)
            ):
                admitted.append(binding["configuration"])
    if len(admitted) != 1:
        raise CandidateEvaluationError(
            "candidate evaluation requires one admitted terminology check"
        )
    return admitted[0]


def evaluate_terminology_candidate(
    *,
    projection: str,
    evaluation_result_id: str,
    evidence_selector_json: str,
    replacement: str,
    effective_configuration: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-evaluate the exact changed region, including boundary-spanning terms."""

    try:
        evidence = json.loads(evidence_selector_json)
        selector = CompositeSelector.from_web_annotation(evidence)
    except Exception as exc:
        raise CandidateEvaluationError(
            "candidate evidence selector is invalid"
        ) from exc
    if selector.start is None or selector.end is None:
        raise CandidateEvaluationError(
            "candidate evidence requires exact frozen positions"
        )
    if (
        selector.start < 0
        or selector.end < selector.start
        or selector.end > len(projection)
        or projection[selector.start : selector.end] != selector.exact
    ):
        raise CandidateEvaluationError(
            "candidate evidence no longer matches the frozen projection"
        )

    configuration = _terminology_configuration(effective_configuration)
    terms = configuration.get("terms")
    if not isinstance(terms, list) or not terms:
        raise CandidateEvaluationError(
            "terminology configuration has no terms"
        )
    max_term_length = max(
        (
            len(str(term.get("non_preferred") or ""))
            for term in terms
            if isinstance(term, Mapping)
        ),
        default=0,
    )
    if max_term_length <= 0:
        raise CandidateEvaluationError(
            "terminology configuration has an invalid term"
        )

    boundary = max_term_length - 1
    window_start = max(0, selector.start - boundary)
    window_end = min(len(projection), selector.end + boundary)
    prefix = projection[window_start : selector.start]
    suffix = projection[selector.end : window_end]
    candidate_window = f"{prefix}{replacement}{suffix}"
    replacement_start = len(prefix)
    replacement_end = replacement_start + len(replacement)
    affected_matches: list[dict[str, Any]] = []
    for match in terminology_exact_matches(candidate_window, configuration):
        start = int(match["start"])
        end = int(match["end"])
        overlaps = (
            start < replacement_end and end > replacement_start
            if replacement_start != replacement_end
            else start < replacement_start < end
        )
        if not overlaps:
            continue
        affected_matches.append(
            {
                "non_preferred": str(match["non_preferred"]),
                "preferred": str(match["preferred"]),
                "candidate_relative_start": start - replacement_start,
                "candidate_relative_end": end - replacement_start,
            }
        )

    status = "passed" if not affected_matches else "failed"
    payload = {
        "schema": "work-buddy.cowork-verify-candidate-evaluation/v1",
        "evaluation_result_id": evaluation_result_id,
        "criterion_key": "terminology_exact_match",
        "mechanism": "deterministic",
        "coverage": "changed_region_with_term_length_boundaries",
        "status": status,
        "candidate_sha256": sha256_text(replacement),
        "affected_input_sha256": sha256_text(candidate_window),
        "match_count": len(affected_matches),
        "matches": affected_matches,
    }
    return {
        **payload,
        "canonical_sha256": sha256_text(canonical_json(payload)),
    }


def sanitize_candidate_evaluations(value: object) -> list[dict[str, Any]]:
    """Validate the content-minimized proof retained in portable coordination."""

    if not isinstance(value, list):
        raise CandidateEvaluationError(
            "candidate evaluations must be a list"
        )
    sanitized: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected_fields = {
        "schema",
        "evaluation_result_id",
        "criterion_key",
        "mechanism",
        "coverage",
        "status",
        "candidate_sha256",
        "affected_input_sha256",
        "match_count",
        "matches",
        "canonical_sha256",
    }
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != expected_fields:
            raise CandidateEvaluationError(
                "candidate evaluation has unsupported fields"
            )
        result_id = str(raw.get("evaluation_result_id") or "")
        if not result_id or result_id in seen:
            raise CandidateEvaluationError(
                "candidate evaluation has an invalid or duplicate result id"
            )
        seen.add(result_id)
        if (
            raw.get("schema")
            != "work-buddy.cowork-verify-candidate-evaluation/v1"
            or raw.get("criterion_key") != "terminology_exact_match"
            or raw.get("mechanism") != "deterministic"
            or raw.get("coverage")
            != "changed_region_with_term_length_boundaries"
            or raw.get("status") not in {"passed", "failed"}
        ):
            raise CandidateEvaluationError(
                "candidate evaluation has an unsupported contract"
            )
        matches = raw.get("matches")
        if not isinstance(matches, list):
            raise CandidateEvaluationError(
                "candidate evaluation matches must be a list"
            )
        normalized_matches: list[dict[str, Any]] = []
        for match in matches:
            if not isinstance(match, Mapping) or set(match) != {
                "non_preferred",
                "preferred",
                "candidate_relative_start",
                "candidate_relative_end",
            }:
                raise CandidateEvaluationError(
                    "candidate evaluation match has unsupported fields"
                )
            start = match.get("candidate_relative_start")
            end = match.get("candidate_relative_end")
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or end <= start
            ):
                raise CandidateEvaluationError(
                    "candidate evaluation match has invalid offsets"
                )
            normalized_matches.append(
                {
                    "non_preferred": str(match.get("non_preferred") or ""),
                    "preferred": str(match.get("preferred") or ""),
                    "candidate_relative_start": start,
                    "candidate_relative_end": end,
                }
            )
        match_count = raw.get("match_count")
        if (
            not isinstance(match_count, int)
            or isinstance(match_count, bool)
            or match_count != len(normalized_matches)
        ):
            raise CandidateEvaluationError(
                "candidate evaluation match_count is invalid"
            )
        payload = {
            "schema": raw["schema"],
            "evaluation_result_id": result_id,
            "criterion_key": raw["criterion_key"],
            "mechanism": raw["mechanism"],
            "coverage": raw["coverage"],
            "status": raw["status"],
            "candidate_sha256": _digest(
                raw.get("candidate_sha256"),
                "candidate_sha256",
            ),
            "affected_input_sha256": _digest(
                raw.get("affected_input_sha256"),
                "affected_input_sha256",
            ),
            "match_count": match_count,
            "matches": normalized_matches,
        }
        canonical_sha256 = _digest(
            raw.get("canonical_sha256"),
            "canonical_sha256",
        )
        if sha256_text(canonical_json(payload)) != canonical_sha256:
            raise CandidateEvaluationError(
                "candidate evaluation canonical hash does not match"
            )
        sanitized.append({**payload, "canonical_sha256": canonical_sha256})
    return sanitized


__all__ = [
    "CandidateEvaluationError",
    "evaluate_terminology_candidate",
    "sanitize_candidate_evaluations",
]

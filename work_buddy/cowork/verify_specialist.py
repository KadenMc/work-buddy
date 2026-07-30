"""Strict normalization for narrow account-backed Verify check workers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from work_buddy.cowork.verify.service import (
    CheckEvaluationOutput,
    CheckResultDraft,
)
from work_buddy.truth.anchors import CompositeSelector, reanchor
from work_buddy.truth.contracts import AnchorError


class SpecialistOutputError(ValueError):
    """A specialist submission cannot become durable evaluation records."""


_RESULT_KINDS = frozenset({"conforming", "finding", "inconclusive"})
_SEVERITIES = frozenset({"info", "warning", "error"})
_COVERAGE = frozenset(
    {"complete_target_review", "partial_target_review", "not_assessed"}
)


def _text(
    value: object,
    label: str,
    *,
    maximum: int,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecialistOutputError(f"{label} must be nonempty text")
    if len(value) > maximum:
        raise SpecialistOutputError(f"{label} exceeds {maximum} characters")
    return value


def _limitations(value: object) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise SpecialistOutputError(
            "specialist limitations must be a list of nonempty text"
        )
    if len(value) > 20 or any(len(item) > 500 for item in value):
        raise SpecialistOutputError(
            "specialist limitations exceed the supported boundary"
        )
    return list(value)


def _evidence_selector(
    value: object,
    *,
    target_text: str,
    target_start: int,
    target_text_sha256: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "exact",
        "prefix",
        "suffix",
    }:
        raise SpecialistOutputError(
            "specialist evidence must contain exactly exact, prefix, and suffix"
        )
    exact = _text(value.get("exact"), "specialist evidence exact", maximum=4000)
    prefix = value.get("prefix", "")
    suffix = value.get("suffix", "")
    if (
        not isinstance(prefix, str)
        or not isinstance(suffix, str)
        or len(prefix) > 500
        or len(suffix) > 500
    ):
        raise SpecialistOutputError(
            "specialist evidence context must be text of at most 500 characters"
        )
    source_selector = CompositeSelector(
        exact=exact,
        prefix=prefix,
        suffix=suffix,
    )
    try:
        resolved = reanchor(
            target_text,
            source_selector,
            expected_snapshot_sha256=target_text_sha256,
        )
    except AnchorError as exc:
        raise SpecialistOutputError(
            "specialist evidence does not resolve uniquely inside the frozen target"
        ) from exc
    # The model's prefix/suffix are only disambiguation hints.  Once the exact
    # quote has been resolved against the frozen target, persist bounded
    # canonical context derived from that target rather than model-supplied
    # context that may be stale or internally inconsistent.
    canonical_prefix = target_text[max(0, resolved.start - 120) : resolved.start]
    canonical_suffix = target_text[resolved.end : min(len(target_text), resolved.end + 120)]
    projected = CompositeSelector(
        exact=resolved.exact,
        prefix=canonical_prefix,
        suffix=canonical_suffix,
        start=target_start + resolved.start,
        end=target_start + resolved.end,
    )
    return {
        "kind": "text_quote",
        "selector": projected.to_web_annotation(),
    }


def normalize_specialist_output(
    payload: Mapping[str, Any],
    *,
    target_text: str,
    target_start: int,
    target_text_sha256: str,
) -> CheckEvaluationOutput:
    """Validate one worker payload and bind every finding to frozen evidence."""

    if set(payload) != {"results", "summary"}:
        raise SpecialistOutputError(
            "specialist output must contain only results and summary"
        )
    summary = _text(payload.get("summary"), "specialist summary", maximum=4000)
    raw_results = payload.get("results")
    if (
        not isinstance(raw_results, list)
        or not raw_results
        or len(raw_results) > 50
    ):
        raise SpecialistOutputError(
            "specialist results must contain between 1 and 50 items"
        )

    drafts: list[CheckResultDraft] = []
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw in raw_results:
        if not isinstance(raw, Mapping) or set(raw) != {
            "result_kind",
            "severity",
            "message",
            "evidence",
            "coverage",
            "limitations",
        }:
            raise SpecialistOutputError(
                "each specialist result has an unsupported shape"
            )
        result_kind = raw.get("result_kind")
        severity = raw.get("severity")
        coverage = raw.get("coverage")
        if result_kind not in _RESULT_KINDS:
            raise SpecialistOutputError("specialist result_kind is unsupported")
        if severity not in _SEVERITIES:
            raise SpecialistOutputError("specialist severity is unsupported")
        if coverage not in _COVERAGE:
            raise SpecialistOutputError("specialist coverage is unsupported")
        message = _text(
            raw.get("message"),
            "specialist result message",
            maximum=2000,
        )
        limitations = _limitations(raw.get("limitations"))
        evidence = _evidence_selector(
            raw.get("evidence"),
            target_text=target_text,
            target_start=target_start,
            target_text_sha256=target_text_sha256,
        )
        if result_kind == "finding" and evidence is None:
            raise SpecialistOutputError(
                "a specialist finding requires exact frozen-target evidence"
            )
        if result_kind == "conforming" and evidence is not None:
            raise SpecialistOutputError(
                "a conforming specialist result cannot cite defect evidence"
            )
        if result_kind == "conforming" and severity != "info":
            raise SpecialistOutputError(
                "a conforming specialist result must use info severity"
            )
        if (
            result_kind == "conforming"
            and coverage != "complete_target_review"
        ):
            raise SpecialistOutputError(
                "a conforming specialist result requires complete target review"
            )
        evidence_key = "" if evidence is None else repr(evidence)
        identity = (str(result_kind), str(severity), message, evidence_key)
        if identity in seen:
            raise SpecialistOutputError(
                "specialist output contains a duplicate result"
            )
        seen.add(identity)
        result_payload = {
            "coverage": coverage,
            "limitations": limitations,
            "evaluation_basis": "account_backed_specialist",
        }
        drafts.append(
            CheckResultDraft(
                result_kind=str(result_kind),
                severity=str(severity),
                message=message,
                evidence_selector=evidence,
                payload=result_payload,
            )
        )
        normalized.append(
            {
                "result_kind": str(result_kind),
                "severity": str(severity),
                "message": message,
                "evidence_selector": evidence,
                "payload": result_payload,
            }
        )

    kinds = {draft.result_kind for draft in drafts}
    if "conforming" in kinds and len(kinds) != 1:
        raise SpecialistOutputError(
            "a conforming result cannot be mixed with findings or inconclusive results"
        )
    if sum(draft.result_kind == "conforming" for draft in drafts) > 1:
        raise SpecialistOutputError(
            "specialist output may contain at most one conforming result"
        )
    normalized_output = {
        "results": normalized,
        "summary": summary,
    }
    return CheckEvaluationOutput(
        output=normalized_output,
        diagnostics={
            "result_count": len(normalized),
            "coverage_states": sorted(
                {str(item["payload"]["coverage"]) for item in normalized}
            ),
        },
        results=tuple(drafts),
    )


__all__ = [
    "SpecialistOutputError",
    "normalize_specialist_output",
]

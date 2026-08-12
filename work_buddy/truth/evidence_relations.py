"""Validated claim-to-evidence relation contracts.

Schema-v9 keeps an evidence item's semantic effect on a claim separate from
the way the claim was derived from that evidence.  Older ``supports_span``
links remain readable compatibility records; arbitrary legacy ``role_json``
is never reinterpreted as a v1 relation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.identity import canonical_json


CLAIM_EVIDENCE_SCHEMA = "claim-evidence/v1"
EVIDENTIAL_EFFECTS = frozenset(
    {
        "supports",
        "partially_supports",
        "contradicts",
        "mentions",
        "does_not_address",
        "inconclusive",
    }
)
DERIVATION_RELATIONSHIPS = frozenset(
    {"direct_statement", "paraphrase", "inference", "context"}
)
POSITIVE_EVIDENTIAL_EFFECTS = frozenset({"supports", "partially_supports"})
_ROLE_KEYS = frozenset(
    {"schema", "evidential_effect", "derivation_relationship", "diagnostics"}
)


@dataclass(frozen=True, slots=True)
class ClaimEvidenceRelation:
    """One validated or conservatively classified claim-evidence edge."""

    classification: str
    evidential_effect: str | None
    derivation_relationship: str | None
    diagnostics: Mapping[str, Any] | None = None

    @property
    def is_positive(self) -> bool:
        return self.evidential_effect in POSITIVE_EVIDENTIAL_EFFECTS

    def to_role(self) -> dict[str, Any]:
        if self.classification != "validated":
            raise InvariantViolation(
                "only validated claim-evidence relations have a canonical role"
            )
        value: dict[str, Any] = {
            "schema": CLAIM_EVIDENCE_SCHEMA,
            "evidential_effect": self.evidential_effect,
            "derivation_relationship": self.derivation_relationship,
        }
        if self.diagnostics is not None:
            value["diagnostics"] = dict(self.diagnostics)
        return value


def validate_claim_evidence_role(
    value: Mapping[str, Any],
) -> ClaimEvidenceRelation:
    """Validate the exact ``claim-evidence/v1`` role shape.

    Diagnostics are deliberately opaque canonical JSON metadata.  Confidence
    and model-specific diagnostics belong there; they do not affect whether
    an edge counts as support.
    """

    if not isinstance(value, Mapping):
        raise InvariantViolation("claim-evidence role must be an object")
    unknown = set(value) - _ROLE_KEYS
    required = {"schema", "evidential_effect", "derivation_relationship"}
    missing = required - set(value)
    if missing or unknown:
        raise InvariantViolation(
            "claim-evidence role fields do not match claim-evidence/v1"
        )
    if value.get("schema") != CLAIM_EVIDENCE_SCHEMA:
        raise InvariantViolation("claim-evidence role has an unsupported schema")
    effect = value.get("evidential_effect")
    if effect not in EVIDENTIAL_EFFECTS:
        raise InvariantViolation("claim-evidence role has an invalid evidential effect")
    relationship = value.get("derivation_relationship")
    if relationship not in DERIVATION_RELATIONSHIPS:
        raise InvariantViolation(
            "claim-evidence role has an invalid derivation relationship"
        )
    diagnostics = value.get("diagnostics")
    if diagnostics is not None:
        if not isinstance(diagnostics, Mapping):
            raise InvariantViolation("claim-evidence diagnostics must be an object")
        try:
            # Prove the diagnostics are finite/canonical JSON data now rather
            # than allowing export to discover an unportable payload later.
            canonical_json(dict(diagnostics))
        except (TypeError, ValueError) as exc:
            raise InvariantViolation(
                "claim-evidence diagnostics must be canonical JSON data"
            ) from exc
    return ClaimEvidenceRelation(
        classification="validated",
        evidential_effect=str(effect),
        derivation_relationship=str(relationship),
        diagnostics=(None if diagnostics is None else dict(diagnostics)),
    )


def classify_claim_evidence_role(
    *,
    link_type: str,
    role_json: str | None,
) -> ClaimEvidenceRelation:
    """Project a stored edge without guessing at legacy metadata."""

    if link_type == "supports_span":
        return ClaimEvidenceRelation(
            classification="legacy_positive",
            evidential_effect="supports",
            derivation_relationship=None,
        )
    if link_type != "evidence_relation":
        return ClaimEvidenceRelation(
            classification="legacy_unspecified",
            evidential_effect=None,
            derivation_relationship=None,
        )
    try:
        parsed = json.loads(role_json or "null")
        if not isinstance(parsed, Mapping):
            raise InvariantViolation("claim-evidence role must be an object")
        return validate_claim_evidence_role(parsed)
    except (json.JSONDecodeError, InvariantViolation):
        # Read paths remain conservative for historical or externally altered
        # stores. New writes and imports reject this shape.
        return ClaimEvidenceRelation(
            classification="legacy_unspecified",
            evidential_effect=None,
            derivation_relationship=None,
        )


__all__ = [
    "CLAIM_EVIDENCE_SCHEMA",
    "DERIVATION_RELATIONSHIPS",
    "EVIDENTIAL_EFFECTS",
    "POSITIVE_EVIDENTIAL_EFFECTS",
    "ClaimEvidenceRelation",
    "classify_claim_evidence_role",
    "validate_claim_evidence_role",
]

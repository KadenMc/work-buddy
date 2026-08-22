"""Canonical validation for append-only Co-work provenance attestations.

Authorship, human review, and the identity of the person making the
attestation are deliberately independent dimensions.  An attestation records
what a human says about a frozen document target; it does not turn an
unverified name into an account identity or certify the content as correct.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from work_buddy.truth.contracts import InvariantViolation, VALID_ACTOR_KINDS
from work_buddy.truth.identity import canonical_json, sha256_text


ATTESTATION_SCHEMA = "document-provenance-attestation/v1"
PROVENANCE_TARGET_KINDS = frozenset({"document_version", "document_span"})
PROVENANCE_AUTHORSHIP_KINDS = frozenset({"human", "ai", "mixed", "unknown"})
PROVENANCE_REVIEW_STATUSES = frozenset(
    {"reviewed", "not_reviewed", "not_applicable", "unknown"}
)
PROVENANCE_SOURCE_KINDS = frozenset(
    {
        "file_import",
        "paste",
        "direct_entry",
        "proposal_acceptance",
        "legacy",
    }
)
PROVENANCE_BASIS_KINDS = frozenset(
    {
        "user_attestation",
        "automatic_short_text_attribution",
        "automatic_direct_entry_attribution",
        "proposal_acceptance",
        "migration_backfill",
        "legacy",
    }
)
PROVENANCE_SOURCE_BASIS_PAIRS = frozenset(
    {
        ("file_import", "user_attestation"),
        ("file_import", "migration_backfill"),
        ("paste", "user_attestation"),
        ("paste", "automatic_short_text_attribution"),
        ("direct_entry", "automatic_direct_entry_attribution"),
        ("proposal_acceptance", "proposal_acceptance"),
        ("proposal_acceptance", "user_attestation"),
        ("legacy", "user_attestation"),
        ("legacy", "legacy"),
    }
)
PROVENANCE_SPAN_SOURCE_BASIS_PAIRS = frozenset(
    {
        ("paste", "automatic_short_text_attribution"),
        ("paste", "user_attestation"),
        ("direct_entry", "automatic_direct_entry_attribution"),
        ("legacy", "user_attestation"),
    }
)
PERSON_IDENTITY_STATUSES = frozenset(
    {"local_actor_ref", "account_ref", "claimed_name"}
)


def _required_text(value: Any, label: str, *, maximum: int = 32_767) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvariantViolation(f"{label} must be a nonempty string")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise InvariantViolation(
            f"{label} must contain at most {maximum} characters"
        )
    return normalized


def _optional_text(
    value: Any,
    label: str,
    *,
    maximum: int = 32_767,
) -> str | None:
    if value is None:
        return None
    return _required_text(value, label, maximum=maximum)


def _json_value(value: Any, label: str) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise InvariantViolation(f"{label} must be valid JSON") from exc
    return value


def normalize_people(value: Any, label: str) -> list[dict[str, Any]]:
    """Return a canonical list of human identities without upgrading claims.

    A durable local/account reference and a typed display name remain visibly
    different identity strengths. The typed name is only a claim.
    """

    parsed = _json_value(value, label)
    if (
        not isinstance(parsed, Sequence)
        or isinstance(parsed, (str, bytes, bytearray))
    ):
        raise InvariantViolation(f"{label} must be a JSON array")
    people: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(parsed):
        item_label = f"{label}[{index}]"
        if not isinstance(item, Mapping):
            raise InvariantViolation(f"{item_label} must be an object")
        unexpected = set(item) - {
            "kind",
            "ref",
            "display_name",
            "identity_status",
        }
        if unexpected:
            raise InvariantViolation(
                f"{item_label} contains unsupported fields: {sorted(unexpected)}"
            )
        if item.get("kind") != "human":
            raise InvariantViolation(f"{item_label}.kind must be human")
        ref = _optional_text(item.get("ref"), f"{item_label}.ref", maximum=512)
        display_name = _optional_text(
            item.get("display_name"),
            f"{item_label}.display_name",
            maximum=160,
        )
        if ref is None and display_name is None:
            raise InvariantViolation(
                f"{item_label} requires an actor/account ref or display name"
            )
        identity_status = item.get("identity_status")
        if identity_status is not None:
            identity_status = _required_text(
                identity_status,
                f"{item_label}.identity_status",
                maximum=40,
            )
            if identity_status not in PERSON_IDENTITY_STATUSES:
                raise InvariantViolation(
                    f"{item_label}.identity_status is invalid"
                )
        if ref is None and identity_status not in {None, "claimed_name"}:
            raise InvariantViolation(
                f"{item_label} without a ref must remain a claimed name"
            )
        if ref is not None and identity_status not in {
            None,
            "local_actor_ref",
            "account_ref",
        }:
            raise InvariantViolation(
                f"{item_label} with a ref must identify its reference strength"
            )
        person: dict[str, Any] = {
            "kind": "human",
            "identity_status": (
                identity_status
                or ("local_actor_ref" if ref is not None else "claimed_name")
            ),
        }
        if ref is not None:
            person["ref"] = ref
        if display_name is not None:
            person["display_name"] = display_name
        fingerprint = canonical_json(person)
        if fingerprint in seen:
            raise InvariantViolation(f"{label} contains a duplicate person")
        seen.add(fingerprint)
        people.append(person)
    return people


def normalize_source(
    value: Any,
    *,
    expected_kind: str | None = None,
) -> dict[str, Any]:
    parsed = _json_value(value, "source")
    if not isinstance(parsed, Mapping):
        raise InvariantViolation("source must be a JSON object")
    source = dict(parsed)
    kind = _required_text(source.get("kind"), "source.kind", maximum=80)
    if kind not in PROVENANCE_SOURCE_KINDS:
        raise InvariantViolation(
            f"source.kind must be one of {sorted(PROVENANCE_SOURCE_KINDS)}"
        )
    if expected_kind is not None and kind != expected_kind:
        raise InvariantViolation("source_kind does not match source.kind")
    for field, maximum in (
        ("format", 80),
        ("media_type", 160),
        ("path", 32_767),
    ):
        if field in source and source[field] is not None:
            source[field] = _required_text(
                source[field],
                f"source.{field}",
                maximum=maximum,
            )
    if source.get("sha256") is not None:
        digest = _required_text(source["sha256"], "source.sha256", maximum=64)
        if len(digest) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in digest):
            raise InvariantViolation(
                "source.sha256 must be a 64-character hexadecimal digest"
            )
        source["sha256"] = digest.lower()
    # Ensure every extension field is portable JSON and reject non-finite
    # numbers through the canonical serializer.
    try:
        canonical_json(source)
    except (TypeError, ValueError) as exc:
        raise InvariantViolation("source must contain portable JSON values") from exc
    return source


def normalize_attester_meta(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    parsed = _json_value(value, "attested_by_meta_json")
    if not isinstance(parsed, Mapping):
        raise InvariantViolation("attested_by_meta_json must contain an object")
    result = dict(parsed)
    try:
        canonical_json(result)
    except (TypeError, ValueError) as exc:
        raise InvariantViolation(
            "attested_by_meta_json must contain portable JSON values"
        ) from exc
    return result


def validate_attestation_components(
    *,
    target_kind: str,
    document_version_id: str | None,
    document_span_id: str | None,
    authorship_kind: str,
    human_contributors: Any,
    review_status: str,
    human_reviewers: Any,
    source_kind: str,
    source: Any,
    basis_kind: str,
    basis_ref: str | None,
    attested_by_kind: str,
    attested_by_ref: str | None,
    attested_by_meta: Any,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any] | None,
]:
    """Validate cross-field semantics and return normalized JSON values."""

    if target_kind not in PROVENANCE_TARGET_KINDS:
        raise InvariantViolation(
            f"target_kind must be one of {sorted(PROVENANCE_TARGET_KINDS)}"
        )
    if target_kind == "document_version":
        if document_version_id is None or document_span_id is not None:
            raise InvariantViolation(
                "document_version target requires only document_version_id"
            )
    elif document_span_id is None or document_version_id is not None:
        raise InvariantViolation(
            "document_span target requires only document_span_id"
        )

    if authorship_kind not in PROVENANCE_AUTHORSHIP_KINDS:
        raise InvariantViolation(
            "authorship_kind must be one of "
            f"{sorted(PROVENANCE_AUTHORSHIP_KINDS)}"
        )
    contributors = normalize_people(
        human_contributors,
        "human_contributors_json",
    )
    if authorship_kind in {"human", "mixed"} and not contributors:
        raise InvariantViolation(
            f"{authorship_kind} authorship requires a human contributor"
        )
    if authorship_kind in {"ai", "unknown"} and contributors:
        raise InvariantViolation(
            f"{authorship_kind} authorship cannot name human contributors"
        )

    if review_status not in PROVENANCE_REVIEW_STATUSES:
        raise InvariantViolation(
            "review_status must be one of "
            f"{sorted(PROVENANCE_REVIEW_STATUSES)}"
        )
    reviewers = normalize_people(human_reviewers, "human_reviewers_json")
    if review_status == "reviewed" and not reviewers:
        raise InvariantViolation("reviewed content requires a human reviewer")
    if review_status != "reviewed" and reviewers:
        raise InvariantViolation(
            f"{review_status} content cannot name human reviewers"
        )

    if source_kind not in PROVENANCE_SOURCE_KINDS:
        raise InvariantViolation(
            f"source_kind must be one of {sorted(PROVENANCE_SOURCE_KINDS)}"
        )
    normalized_source = normalize_source(source, expected_kind=source_kind)
    if basis_kind not in PROVENANCE_BASIS_KINDS:
        raise InvariantViolation(
            f"basis_kind must be one of {sorted(PROVENANCE_BASIS_KINDS)}"
        )
    if (source_kind, basis_kind) not in PROVENANCE_SOURCE_BASIS_PAIRS:
        raise InvariantViolation(
            "source_kind and basis_kind are not an allowed provenance pair"
        )
    _optional_text(basis_ref, "basis_ref", maximum=2_048)

    if attested_by_kind not in VALID_ACTOR_KINDS:
        raise InvariantViolation("attested_by_kind is invalid")
    normalized_attester_ref = _optional_text(
        attested_by_ref,
        "attested_by_ref",
        maximum=512,
    )
    if attested_by_kind in {"human", "agent_run"} and normalized_attester_ref is None:
        raise InvariantViolation(
            f"{attested_by_kind} attester requires attested_by_ref"
        )
    attester_meta = normalize_attester_meta(attested_by_meta)
    return contributors, reviewers, normalized_source, attester_meta


def attestation_canonical_payload(
    *,
    document_id: str,
    target_kind: str,
    document_version_id: str | None,
    document_span_id: str | None,
    target_structured_head_sha256: str,
    authorship_kind: str,
    human_contributors: Any,
    review_status: str,
    human_reviewers: Any,
    source_kind: str,
    source: Any,
    basis_kind: str,
    basis_ref: str | None,
    supersedes_id: str | None,
    attested_by_kind: str,
    attested_by_ref: str | None,
    attested_by_meta: Any,
) -> dict[str, Any]:
    contributors, reviewers, normalized_source, attester_meta = (
        validate_attestation_components(
            target_kind=target_kind,
            document_version_id=document_version_id,
            document_span_id=document_span_id,
            authorship_kind=authorship_kind,
            human_contributors=human_contributors,
            review_status=review_status,
            human_reviewers=human_reviewers,
            source_kind=source_kind,
            source=source,
            basis_kind=basis_kind,
            basis_ref=basis_ref,
            attested_by_kind=attested_by_kind,
            attested_by_ref=attested_by_ref,
            attested_by_meta=attested_by_meta,
        )
    )
    return {
        "schema": ATTESTATION_SCHEMA,
        "document_id": document_id,
        "target": {
            "kind": target_kind,
            "document_version_id": document_version_id,
            "document_span_id": document_span_id,
            "structured_head_sha256": target_structured_head_sha256,
        },
        "authorship": {
            "kind": authorship_kind,
            "human_contributors": contributors,
        },
        "human_review": {
            "status": review_status,
            "human_reviewers": reviewers,
        },
        "source": normalized_source,
        "basis": {"kind": basis_kind, "ref": basis_ref},
        "supersedes_id": supersedes_id,
        "attested_by": {
            "kind": attested_by_kind,
            "ref": attested_by_ref,
            "meta": attester_meta,
        },
    }


def attestation_canonical_sha256(**values: Any) -> str:
    return sha256_text(canonical_json(attestation_canonical_payload(**values)))


def attestation_canonical_sha256_from_record(
    row: Mapping[str, Any],
) -> str:
    return attestation_canonical_sha256(
        document_id=row["document_id"],
        target_kind=row["target_kind"],
        document_version_id=row["document_version_id"],
        document_span_id=row["document_span_id"],
        target_structured_head_sha256=row["target_structured_head_sha256"],
        authorship_kind=row["authorship_kind"],
        human_contributors=row["human_contributors_json"],
        review_status=row["review_status"],
        human_reviewers=row["human_reviewers_json"],
        source_kind=row["source_kind"],
        source=row["source_json"],
        basis_kind=row["basis_kind"],
        basis_ref=row["basis_ref"],
        supersedes_id=row["supersedes_id"],
        attested_by_kind=row["attested_by_kind"],
        attested_by_ref=row["attested_by_ref"],
        attested_by_meta=row["attested_by_meta_json"],
    )


__all__ = [
    "ATTESTATION_SCHEMA",
    "PERSON_IDENTITY_STATUSES",
    "PROVENANCE_AUTHORSHIP_KINDS",
    "PROVENANCE_BASIS_KINDS",
    "PROVENANCE_REVIEW_STATUSES",
    "PROVENANCE_SOURCE_BASIS_PAIRS",
    "PROVENANCE_SOURCE_KINDS",
    "PROVENANCE_SPAN_SOURCE_BASIS_PAIRS",
    "PROVENANCE_TARGET_KINDS",
    "attestation_canonical_payload",
    "attestation_canonical_sha256",
    "attestation_canonical_sha256_from_record",
    "normalize_attester_meta",
    "normalize_people",
    "normalize_source",
    "validate_attestation_components",
]

"""Deterministic review payloads for human Truth decisions.

Transport layers use this module to render the exact durable claim and its
active receipts before asking a human to confirm, reject, or redact it. The
result carries separate hashes for the claim payload, displayed receipt
context, and whole decision request so consent can fail closed on drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.identity import (
    canonical_claim_payload,
    canonical_json,
    claim_sha256,
)
from work_buddy.truth.lifecycle import hash_context
from work_buddy.truth.store import TruthStore


_ACTIONS = frozenset({"confirm", "reject", "redact"})


@dataclass(frozen=True, slots=True)
class ReviewReceipt:
    """One active evidence span shown with a claim decision."""

    link_id: str
    span_id: str
    span_sha256: str
    quote: str | None
    evidence_id: str
    evidence_kind: str
    source_locator: str
    content_sha256: str
    trust_class: str
    author_kind: str | None
    author_ref: str | None
    derived_from_store: str | None
    span_redacted_at: str | None
    evidence_redacted_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_id": self.link_id,
            "span_id": self.span_id,
            "span_sha256": self.span_sha256,
            "quote": self.quote,
            "evidence_id": self.evidence_id,
            "evidence_kind": self.evidence_kind,
            "source_locator": self.source_locator,
            "content_sha256": self.content_sha256,
            "trust_class": self.trust_class,
            "author_kind": self.author_kind,
            "author_ref": self.author_ref,
            "derived_from_store": self.derived_from_store,
            "span_redacted_at": self.span_redacted_at,
            "evidence_redacted_at": self.evidence_redacted_at,
        }


@dataclass(frozen=True, slots=True)
class ClaimReviewPayload:
    """Server-composed content for one exact claim decision."""

    action: str
    claim_id: str
    proposition: str
    claim_payload: Mapping[str, Any]
    payload_sha256: str
    context_sha256: str
    request_fingerprint: str
    receipts: tuple[ReviewReceipt, ...]
    agent_authored_only: bool
    decision: Mapping[str, Any]
    body: str


def _active_receipts(store: TruthStore, claim_id: str) -> tuple[ReviewReceipt, ...]:
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT l.id AS link_id,
                   s.id AS span_id,
                   s.span_sha256,
                   s.quote_exact,
                   e.id AS evidence_id,
                   e.kind AS evidence_kind,
                   e.source_locator,
                   e.content_sha256,
                   e.trust_class,
                   s.author_kind,
                   s.author_ref,
                   e.derived_from_store,
                   s.redacted_at AS span_redacted_at,
                   e.redacted_at AS evidence_redacted_at
              FROM claim_links AS l
              JOIN evidence_spans AS s
                ON l.to_kind = 'evidence_span' AND l.to_ref = s.id
              JOIN evidence AS e ON e.id = s.evidence_id
              LEFT JOIN link_retractions AS r ON r.link_id = l.id
             WHERE l.from_claim_id = ?
               AND l.link_type = 'supports_span'
               AND r.link_id IS NULL
             ORDER BY l.created_at, l.id
            """,
            (claim_id,),
        ).fetchall()
    return tuple(
        ReviewReceipt(
            link_id=row["link_id"],
            span_id=row["span_id"],
            span_sha256=row["span_sha256"],
            quote=row["quote_exact"],
            evidence_id=row["evidence_id"],
            evidence_kind=row["evidence_kind"],
            source_locator=row["source_locator"],
            content_sha256=row["content_sha256"],
            trust_class=row["trust_class"],
            author_kind=row["author_kind"],
            author_ref=row["author_ref"],
            derived_from_store=row["derived_from_store"],
            span_redacted_at=row["span_redacted_at"],
            evidence_redacted_at=row["evidence_redacted_at"],
        )
        for row in rows
    )


def compose_claim_review(
    store: TruthStore,
    claim_id: str,
    *,
    action: str,
    decision: Mapping[str, Any] | None = None,
) -> ClaimReviewPayload:
    """Compose and hash the exact claim decision a human will see."""

    action_name = str(action).strip().lower()
    if action_name not in _ACTIONS:
        raise ValueError(f"unsupported Truth review action: {action!r}")
    claim = store.get_claim(claim_id)
    if claim is None:
        raise InvariantViolation(f"claim does not exist: {claim_id}")

    claim_payload = canonical_claim_payload(
        proposition=claim.proposition,
        claim_kind=claim.claim_kind,
        structured=claim.structured_json,
        scope=claim.scope,
        valid_from=claim.valid_from,
        valid_to=claim.valid_to,
    )
    recomputed_sha256 = claim_sha256(
        proposition=claim.proposition,
        claim_kind=claim.claim_kind,
        structured=claim.structured_json,
        scope=claim.scope,
        valid_from=claim.valid_from,
        valid_to=claim.valid_to,
    )
    if recomputed_sha256 != claim.canonical_sha256:
        raise InvariantViolation(
            f"claim canonical payload hash mismatch: {claim.id}"
        )

    receipts = _active_receipts(store, claim.id)
    usable_receipts = tuple(
        item
        for item in receipts
        if item.derived_from_store is None
        and item.span_redacted_at is None
        and item.evidence_redacted_at is None
    )
    agent_authored_only = bool(usable_receipts) and all(
        item.trust_class == "agent_authored" or item.author_kind == "agent_run"
        for item in usable_receipts
    )
    receipt_context = {
        "claim_id": claim.id,
        "receipts": [item.to_dict() for item in receipts],
    }
    context_sha256 = hash_context(receipt_context)
    decision_data = dict(decision or {})
    request_fingerprint = hash_context(
        {
            "action": action_name,
            "claim_id": claim.id,
            "claim_payload": claim_payload,
            "payload_sha256": claim.canonical_sha256,
            "context_sha256": context_sha256,
            "decision": decision_data,
        }
    )

    lines = [
        f"Truth decision: {action_name}",
        f"Claim ID: {claim.id}",
        f"Claim hash: {claim.canonical_sha256}",
        "",
        "Canonical claim payload (exact content being reviewed):",
        canonical_json(claim_payload),
        "",
        f"Active receipts: {len(receipts)}",
    ]
    if agent_authored_only:
        lines.extend(
            [
                "WARNING: This claim is supported only by agent-authored evidence.",
                "Confirming it ratifies an agent's claim against agent-authored support.",
            ]
        )
    for index, receipt in enumerate(receipts, start=1):
        quote = receipt.quote or "[no quoted text]"
        author = receipt.author_kind or "unknown"
        if receipt.author_ref:
            author += f":{receipt.author_ref}"
        derived = receipt.derived_from_store or "none"
        span_redacted = receipt.span_redacted_at or "no"
        evidence_redacted = receipt.evidence_redacted_at or "no"
        lines.extend(
            [
                f"{index}. {receipt.source_locator}",
                f"   trust: {receipt.trust_class}",
                f"   author: {author}",
                f"   derived from store: {derived}",
                f"   span redacted at: {span_redacted}",
                f"   evidence redacted at: {evidence_redacted}",
                f"   span: {receipt.span_id} ({receipt.span_sha256})",
                f"   quote: {quote}",
            ]
        )
    if decision_data:
        lines.extend(["", "Decision details:", canonical_json(decision_data)])

    return ClaimReviewPayload(
        action=action_name,
        claim_id=claim.id,
        proposition=claim.proposition,
        claim_payload=claim_payload,
        payload_sha256=claim.canonical_sha256,
        context_sha256=context_sha256,
        request_fingerprint=request_fingerprint,
        receipts=receipts,
        agent_authored_only=agent_authored_only,
        decision=decision_data,
        body="\n".join(lines),
    )

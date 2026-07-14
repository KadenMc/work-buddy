"""Targeted, provenance-aware truth stores.

The truth package owns the append-only ledger and its invariant-enforcing
engine. User-facing transports such as the MCP gateway, CLI, and dashboard
delegate to this package instead of reimplementing truth semantics.
"""

from work_buddy.truth.identity import (
    TruthRef,
    canonical_claim_payload,
    canonical_json,
    claim_sha256,
    entity_uri,
    new_id,
    parse_truth_uri,
    truth_uri,
    utc_now,
)

__all__ = [
    "TruthRef",
    "canonical_claim_payload",
    "canonical_json",
    "claim_sha256",
    "entity_uri",
    "new_id",
    "parse_truth_uri",
    "truth_uri",
    "utc_now",
]

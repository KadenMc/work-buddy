"""Stable identities, canonical hashes, and cross-store references."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlparse


_UUID4_HEX = re.compile(r"^[0-9a-f]{32}$")
TRUTH_RECORD_KINDS = frozenset({"claim", "evidence", "span", "derivation"})


def new_id() -> str:
    """Return a lowercase uuid4 hex identifier using the house convention."""
    return uuid.uuid4().hex


def utc_now() -> str:
    """Return a millisecond-precision ISO 8601 timestamp in UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _normalize_text(value: str) -> str:
    """Normalize semantic whitespace without changing punctuation or case."""
    return " ".join(value.strip().split())


def _normalize_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_json_value(item) for item in value]
    if isinstance(value, str):
        return _normalize_text(value)
    return value


def canonical_json(value: Any) -> str:
    """Serialize a value deterministically for hashing and committed exports."""
    return json.dumps(
        _normalize_json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_claim_payload(
    *,
    proposition: str,
    claim_kind: str,
    structured: Mapping[str, Any] | str | None,
    scope: str = "store",
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> dict[str, Any]:
    """Build the exact assertion payload bound by gestures and deduplication."""
    proposition_norm = _normalize_text(proposition)
    if not proposition_norm:
        raise ValueError("proposition cannot be empty")
    claim_kind_norm = claim_kind.strip()
    if not claim_kind_norm:
        raise ValueError("claim_kind cannot be empty")
    scope_norm = scope.strip()
    if not scope_norm:
        raise ValueError("scope cannot be empty")

    structured_value: Mapping[str, Any] | None
    if isinstance(structured, str):
        parsed = json.loads(structured)
        if not isinstance(parsed, Mapping):
            raise ValueError("structured JSON must contain an object")
        structured_value = parsed
    else:
        structured_value = structured

    return {
        "proposition": proposition_norm,
        "claim_kind": claim_kind_norm,
        "structured_json": (
            _normalize_json_value(structured_value)
            if structured_value is not None
            else None
        ),
        "scope": scope_norm,
        "valid_from": valid_from,
        "valid_to": valid_to,
    }


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase SHA-256 hex digest for bytes."""
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    """Return the UTF-8 SHA-256 hex digest for text."""
    return sha256_bytes(value.encode("utf-8"))


def claim_sha256(
    *,
    proposition: str,
    claim_kind: str,
    structured: Mapping[str, Any] | str | None,
    scope: str = "store",
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> str:
    """Hash the canonical claim payload defined by the truth contract."""
    payload = canonical_claim_payload(
        proposition=proposition,
        claim_kind=claim_kind,
        structured=structured,
        scope=scope,
        valid_from=valid_from,
        valid_to=valid_to,
    )
    return sha256_text(canonical_json(payload))


def _validate_hex_id(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not _UUID4_HEX.fullmatch(normalized):
        raise ValueError(f"{label} must be a 32-character uuid4 hex id")
    return normalized


def truth_uri(store_id: str, kind: str, record_id: str) -> str:
    """Build a permanent ``wb-truth`` reference URI."""
    store = _validate_hex_id(store_id, "store_id")
    record = _validate_hex_id(record_id, "record_id")
    kind_norm = kind.strip().lower()
    if kind_norm not in TRUTH_RECORD_KINDS:
        raise ValueError(
            f"kind must be one of {sorted(TRUTH_RECORD_KINDS)}, got {kind!r}"
        )
    return f"wb-truth://{store}/{kind_norm}/{record}"


def entity_uri(entity_id: str | int) -> str:
    """Build a soft URI into the existing entity-resolution registry."""
    value = str(entity_id).strip()
    if not value or "/" in value:
        raise ValueError("entity_id must be a non-empty path-safe value")
    return f"wb-entity://{value}"


@dataclass(frozen=True, slots=True)
class TruthRef:
    """A parsed permanent cross-store truth reference."""

    store_id: str
    kind: str
    record_id: str

    @property
    def uri(self) -> str:
        return truth_uri(self.store_id, self.kind, self.record_id)


def parse_truth_uri(value: str) -> TruthRef:
    """Parse and validate a ``wb-truth`` reference URI."""
    parsed = urlparse(value)
    if parsed.scheme != "wb-truth" or not parsed.netloc:
        raise ValueError("not a wb-truth URI")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("wb-truth URI must contain exactly kind and record id")
    kind, record_id = parts
    normalized = truth_uri(parsed.netloc, kind, record_id)
    parsed_normalized = urlparse(normalized)
    normalized_parts = [part for part in parsed_normalized.path.split("/") if part]
    return TruthRef(
        store_id=parsed_normalized.netloc,
        kind=normalized_parts[0],
        record_id=normalized_parts[1],
    )

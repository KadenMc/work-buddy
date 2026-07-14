"""Doorstop-style target fingerprints for mutable truth links.

The algorithm is a first-party implementation of the Doorstop design pattern:
store a mutable target's SHA-256 when reviewed and compare it with the target's
current SHA-256. No Doorstop source code is copied here.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from enum import Enum
from typing import Any

from work_buddy.truth.identity import canonical_json


MUTABLE_LINK_TYPES = frozenset({"about_entity", "cites_external"})
IMMUTABLE_LINK_TYPES = frozenset(
    {
        "supports_span",
        "supersedes",
        "conflicts_with",
        "refutes",
        "relates_to",
    }
)
KNOWN_LINK_TYPES = MUTABLE_LINK_TYPES | IMMUTABLE_LINK_TYPES

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FingerprintStatus(str, Enum):
    """Review status for a link's target fingerprint."""

    NOT_APPLICABLE = "not_applicable"
    UNREVIEWED = "unreviewed"
    CURRENT = "current"
    STALE = "stale"


def _validate_link_type(link_type: str) -> str:
    if not isinstance(link_type, str):
        raise ValueError("link_type must be a string")
    normalized = link_type.strip()
    if normalized not in KNOWN_LINK_TYPES:
        raise ValueError(
            f"unsupported link_type {link_type!r}. Known types are "
            f"{sorted(KNOWN_LINK_TYPES)}"
        )
    return normalized


def _normalize_digest(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a hexadecimal string or None")
    normalized = value.strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{label} must be a 64-character hexadecimal SHA-256 digest")
    return normalized


def compute_target_fingerprint(
    link_type: str,
    target_content: Any = None,
) -> str | None:
    """Hash mutable target content and return ``None`` for immutable links.

    Text and byte content hash their UTF-8 or raw bytes directly. Structured
    JSON-compatible content uses the truth package's canonical JSON encoding so
    key ordering and semantic whitespace do not create false drift.
    """
    normalized_type = _validate_link_type(link_type)
    if normalized_type in IMMUTABLE_LINK_TYPES:
        return None
    if target_content is None:
        raise ValueError(f"{normalized_type} requires target content to fingerprint")

    if isinstance(target_content, str):
        payload = target_content.encode("utf-8")
    elif isinstance(target_content, (bytes, bytearray, memoryview)):
        payload = bytes(target_content)
    else:
        try:
            payload = canonical_json(target_content).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "target content must be text, bytes, or JSON-compatible data"
            ) from exc
    return hashlib.sha256(payload).hexdigest()


def fingerprint_status(
    link_type: str,
    stored_fingerprint: str | None,
    current_target_fingerprint: str | None,
) -> FingerprintStatus:
    """Classify a stored review fingerprint against the current target hash."""
    normalized_type = _validate_link_type(link_type)
    if normalized_type in IMMUTABLE_LINK_TYPES:
        return FingerprintStatus.NOT_APPLICABLE

    stored = _normalize_digest(stored_fingerprint, "stored_fingerprint")
    current = _normalize_digest(
        current_target_fingerprint,
        "current_target_fingerprint",
    )
    if stored is None:
        return FingerprintStatus.UNREVIEWED
    if current is None or not hmac.compare_digest(stored, current):
        return FingerprintStatus.STALE
    return FingerprintStatus.CURRENT


def is_fingerprint_reviewed(
    link_type: str,
    stored_fingerprint: str | None,
    current_target_fingerprint: str | None,
) -> bool:
    """Return whether a mutable link is reviewed against its current target."""
    return (
        fingerprint_status(
            link_type,
            stored_fingerprint,
            current_target_fingerprint,
        )
        is FingerprintStatus.CURRENT
    )


def is_fingerprint_current(
    link_type: str,
    stored_fingerprint: str | None,
    current_target_fingerprint: str | None,
) -> bool:
    """Alias the reviewed check with explicit currency wording."""
    return is_fingerprint_reviewed(
        link_type,
        stored_fingerprint,
        current_target_fingerprint,
    )


def is_fingerprint_stale(
    link_type: str,
    stored_fingerprint: str | None,
    current_target_fingerprint: str | None,
) -> bool:
    """Return whether a reviewed mutable target changed or disappeared."""
    return (
        fingerprint_status(
            link_type,
            stored_fingerprint,
            current_target_fingerprint,
        )
        is FingerprintStatus.STALE
    )

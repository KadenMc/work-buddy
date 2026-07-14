"""Deterministic, lossless JSONL transport for one truth ledger.

The committed export is a recovery format, not a projection. It carries the
profile, every globally ordered ledger row, sanctioned mutation state, and each
live content-addressed blob. Import validates the complete stream before it
writes into a staged sidecar and then publishes that sidecar with one rename.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from work_buddy.artifacts.io import atomic_write_bytes
from work_buddy.truth.contracts import (
    InvariantViolation,
    StorePaths,
    VALID_ACTOR_KINDS,
    VALID_STATUSES,
)
from work_buddy.truth.identity import (
    canonical_json,
    claim_sha256,
    parse_truth_uri,
    sha256_bytes,
)
from work_buddy.truth.migrations import SCHEMA_VERSION, migrate
from work_buddy.truth.profiles import (
    StoreProfile,
    dump_profile,
    normalize_store_id,
    validate_profile,
)
from work_buddy.truth.store import TruthStore


FORMAT_NAME = "work-buddy.truth-ledger"
FORMAT_VERSION = 2
OLDEST_FORMAT_VERSION = 1

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECORD_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class TruthExportError(InvariantViolation):
    """A live store cannot be represented losslessly."""


class TruthImportError(InvariantViolation):
    """An export stream cannot safely rebuild a truth store."""


class StoreIdentityCollision(TruthImportError):
    """The imported store identity is already live at another path."""


class StoreRegistry(Protocol):
    """Read seam that K1's machine-level truth registry will implement."""

    def paths_for_store_id(self, store_id: str) -> Iterable[str | Path]:
        """Return every registered path carrying ``store_id``."""


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    sha256: str
    record_count: int
    blob_count: int
    last_seq: int


@dataclass(frozen=True, slots=True)
class ImportResult:
    store: TruthStore
    source_format_version: int
    record_count: int
    blob_count: int


@dataclass(frozen=True, slots=True)
class _DataRecord:
    seq: int
    record_type: str
    record_key: str
    record: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _BlobRecord:
    content_sha256: str
    content: bytes


@dataclass(frozen=True, slots=True)
class _Bundle:
    source_format_version: int
    store_info: Mapping[str, Any]
    profile: Mapping[str, Any]
    records: tuple[_DataRecord, ...]
    blobs: tuple[_BlobRecord, ...]


_RECORD_COLUMNS: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "evidence": (
        "evidence",
        (
            "id",
            "kind",
            "source_locator",
            "content_sha256",
            "content",
            "content_path",
            "media_type",
            "acquired_at",
            "acquired_by_kind",
            "acquired_by_ref",
            "acquisition_method",
            "trust_class",
            "derived_from_store",
            "meta_json",
            "redacted_at",
            "created_at",
        ),
    ),
    "evidence_span": (
        "evidence_spans",
        (
            "id",
            "evidence_id",
            "selector_json",
            "quote_exact",
            "span_sha256",
            "author_kind",
            "author_ref",
            "redacted_at",
            "created_at",
            "created_by_kind",
            "created_by_ref",
        ),
    ),
    "claim": (
        "claims",
        (
            "id",
            "proposition",
            "canonical_sha256",
            "claim_kind",
            "structured_json",
            "scope",
            "valid_from",
            "valid_to",
            "confidence_extraction",
            "meta_json",
            "redacted_at",
            "created_at",
            "created_by_kind",
            "created_by_ref",
        ),
    ),
    "derivation": (
        "derivations",
        (
            "id",
            "claim_id",
            "method",
            "producer_kind",
            "producer_ref",
            "confidence",
            "rationale",
            "created_at",
        ),
    ),
    "derivation_premise": (
        "derivation_premises",
        ("derivation_id", "premise_kind", "premise_ref"),
    ),
    "claim_link": (
        "claim_links",
        (
            "id",
            "from_claim_id",
            "link_type",
            "to_kind",
            "to_ref",
            "role_json",
            "target_fingerprint",
            "fingerprint_reviewed_at",
            "created_at",
            "created_by_kind",
            "created_by_ref",
        ),
    ),
    "link_retraction": (
        "link_retractions",
        ("link_id", "at", "actor_kind", "actor_ref", "reason"),
    ),
    "claim_status_event": (
        "claim_status_events",
        (
            "seq",
            "id",
            "claim_id",
            "status",
            "at",
            "actor_kind",
            "actor_ref",
            "basis_kind",
            "basis_ref",
            "note",
        ),
    ),
    "gesture": (
        "gestures",
        (
            "id",
            "at",
            "surface",
            "actor_ref",
            "kind",
            "subject_ref",
            "payload_sha256",
            "payload_excerpt",
            "context_sha256",
            "expires_at",
            "consumed_at",
        ),
    ),
    "redaction_event": (
        "redaction_events",
        (
            "id",
            "subject_kind",
            "subject_ref",
            "at",
            "actor_ref",
            "basis_kind",
            "basis_ref",
            "reason",
        ),
    ),
    "sweep": (
        "sweeps",
        ("id", "kind", "at", "params_json"),
    ),
    "sweep_finding": (
        "sweep_findings",
        (
            "id",
            "sweep_id",
            "subject_kind",
            "subject_ref",
            "finding",
            "resolved_at",
            "resolved_by_ref",
        ),
    ),
}

_ID_KEY_TYPES = frozenset(
    {
        "evidence",
        "evidence_span",
        "claim",
        "derivation",
        "claim_link",
        "claim_status_event",
        "gesture",
        "redaction_event",
        "sweep",
        "sweep_finding",
    }
)

_LINK_TARGETS: Mapping[str, frozenset[str]] = {
    "supports_span": frozenset({"evidence_span"}),
    "about_entity": frozenset({"entity"}),
    "supersedes": frozenset({"claim"}),
    "conflicts_with": frozenset({"claim"}),
    "refutes": frozenset({"claim"}),
    "cites_external": frozenset({"external_uri"}),
    "relates_to": frozenset({"claim", "entity", "external_uri"}),
}


def _canonical_line(value: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise TruthExportError("export data is not canonical JSON") from exc
    return text.encode("utf-8") + b"\n"


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TruthImportError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise TruthImportError(f"{label} keys must be strings")
    return dict(value)


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: Iterable[str],
    label: str,
) -> None:
    expected_set = set(expected)
    actual = set(value)
    if actual != expected_set:
        missing = sorted(expected_set - actual)
        extra = sorted(actual - expected_set)
        detail: list[str] = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unexpected {extra}")
        raise TruthImportError(f"{label} has invalid keys: {', '.join(detail)}")


def _positive_int(value: Any, label: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TruthImportError(f"{label} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise TruthImportError(f"{label} must be at least {minimum}")
    return value


def _record_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _RECORD_ID_RE.fullmatch(value) is None:
        raise TruthImportError(f"{label} must be a lowercase 32-hex id")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise TruthImportError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TruthImportError(f"{label} must be a nonempty string")
    return value


def _json_value(value: Any, label: str, *, mapping: bool = False) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TruthImportError(f"{label} must be JSON text or null")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TruthImportError(f"{label} is invalid JSON") from exc
    if mapping and not isinstance(parsed, dict):
        raise TruthImportError(f"{label} must contain a JSON object")
    return parsed


def _finite_confidence(value: Any, label: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TruthImportError(f"{label} must be a number from 0 to 1")
    if not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
        raise TruthImportError(f"{label} must be a finite number from 0 to 1")


def _timestamp(value: Any, label: str) -> None:
    text = _nonempty_text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TruthImportError(f"{label} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TruthImportError(f"{label} must carry a UTC offset")


def _record_key(record_type: str, row: Mapping[str, Any]) -> str:
    if record_type in _ID_KEY_TYPES:
        return _record_id(row.get("id"), f"{record_type}.id")
    if record_type == "link_retraction":
        return _record_id(row.get("link_id"), "link_retraction.link_id")
    if record_type == "derivation_premise":
        derivation_id = _record_id(
            row.get("derivation_id"), "derivation_premise.derivation_id"
        )
        premise_ref = _nonempty_text(
            row.get("premise_ref"), "derivation_premise.premise_ref"
        )
        return canonical_json(
            {"derivation_id": derivation_id, "premise_ref": premise_ref}
        )
    raise TruthImportError(f"unsupported ledger record type {record_type!r}")


def _validate_header(bundle: _Bundle) -> StoreProfile:
    info = _require_mapping(bundle.store_info, "store_info")
    _require_exact_keys(
        info,
        {"store_id", "profile", "schema_version", "title", "created_at"},
        "store_info",
    )
    try:
        normalized_store_id = normalize_store_id(info["store_id"])
    except InvariantViolation as exc:
        raise TruthImportError(str(exc)) from exc
    if info["store_id"] != normalized_store_id:
        raise TruthImportError("store_info.store_id must use lowercase UUID hex")
    profile_name = _nonempty_text(info["profile"], "store_info.profile")
    schema_version = _positive_int(info["schema_version"], "schema_version")
    if schema_version > SCHEMA_VERSION:
        raise TruthImportError(
            f"store schema v{schema_version} is newer than supported v{SCHEMA_VERSION}"
        )
    if info["title"] is not None and not isinstance(info["title"], str):
        raise TruthImportError("store_info.title must be text or null")
    _timestamp(info["created_at"], "store_info.created_at")
    try:
        profile = validate_profile(bundle.profile)
    except InvariantViolation as exc:
        raise TruthImportError(str(exc)) from exc
    if (
        profile.store_id != normalized_store_id
        or profile.profile != profile_name
        or profile.title != info["title"]
    ):
        raise TruthImportError("profile identity does not match store_info")
    return profile


def _validate_record_values(item: _DataRecord) -> None:
    row = item.record
    record_type = item.record_type
    _, columns = _RECORD_COLUMNS[record_type]
    _require_exact_keys(row, columns, f"{record_type} record")
    computed_key = _record_key(record_type, row)
    if computed_key != item.record_key:
        raise TruthImportError(
            f"{record_type} record_key does not match its primary key"
        )

    if record_type == "evidence":
        digest = _digest(row["content_sha256"], "evidence.content_sha256")
        _nonempty_text(row["kind"], "evidence.kind")
        _nonempty_text(row["source_locator"], "evidence.source_locator")
        if not urlparse(row["source_locator"]).scheme:
            raise TruthImportError("evidence.source_locator requires a URI scheme")
        content = row["content"]
        content_path = row["content_path"]
        if content is not None and not isinstance(content, str):
            raise TruthImportError("evidence.content must be text or null")
        if content is not None and content_path is not None:
            raise TruthImportError("evidence cannot contain inline and blob content")
        if content is not None and sha256_bytes(content.encode("utf-8")) != digest:
            raise TruthImportError("inline evidence does not match content_sha256")
        if content_path is not None and content_path != f"blobs/{digest}":
            raise TruthImportError("evidence.content_path must match content_sha256")
        if row["redacted_at"] is not None:
            if content is not None or content_path is not None:
                raise TruthImportError("redacted evidence cannot retain content")
            _timestamp(row["redacted_at"], "evidence.redacted_at")
        if row["derived_from_store"] is not None:
            try:
                derived = normalize_store_id(row["derived_from_store"])
            except InvariantViolation as exc:
                raise TruthImportError(str(exc)) from exc
            if derived != row["derived_from_store"]:
                raise TruthImportError("derived_from_store must use lowercase UUID hex")
        _json_value(row["meta_json"], "evidence.meta_json", mapping=True)
        _timestamp(row["acquired_at"], "evidence.acquired_at")
        _timestamp(row["created_at"], "evidence.created_at")
        return

    if record_type == "evidence_span":
        digest = _digest(row["span_sha256"], "evidence_span.span_sha256")
        _json_value(row["selector_json"], "evidence_span.selector_json")
        quote = row["quote_exact"]
        if quote is not None and not isinstance(quote, str):
            raise TruthImportError("evidence_span.quote_exact must be text or null")
        if quote is not None and sha256_bytes(quote.encode("utf-8")) != digest:
            raise TruthImportError("evidence span quote does not match span_sha256")
        if row["redacted_at"] is not None:
            if quote is not None:
                raise TruthImportError("redacted evidence span cannot retain its quote")
            _timestamp(row["redacted_at"], "evidence_span.redacted_at")
        elif quote is None:
            raise TruthImportError("live evidence span must retain its exact quote")
        _timestamp(row["created_at"], "evidence_span.created_at")
        return

    if record_type == "claim":
        digest = _digest(row["canonical_sha256"], "claim.canonical_sha256")
        proposition = _nonempty_text(row["proposition"], "claim.proposition")
        structured = _json_value(
            row["structured_json"], "claim.structured_json", mapping=True
        )
        _json_value(row["meta_json"], "claim.meta_json", mapping=True)
        if row["redacted_at"] is not None:
            if proposition != "[redacted]" or structured is not None:
                raise TruthImportError("redacted claim has retained claim content")
            _timestamp(row["redacted_at"], "claim.redacted_at")
        else:
            try:
                expected = claim_sha256(
                    proposition=proposition,
                    claim_kind=row["claim_kind"],
                    structured=structured,
                    scope=row["scope"],
                    valid_from=row["valid_from"],
                    valid_to=row["valid_to"],
                )
            except (TypeError, ValueError) as exc:
                raise TruthImportError("claim canonical payload is invalid") from exc
            if expected != digest:
                raise TruthImportError("claim content does not match canonical_sha256")
        _finite_confidence(row["confidence_extraction"], "claim confidence")
        _timestamp(row["created_at"], "claim.created_at")
        return

    if record_type == "derivation":
        _finite_confidence(row["confidence"], "derivation confidence")
        _timestamp(row["created_at"], "derivation.created_at")
        return

    if record_type == "derivation_premise":
        if row["premise_kind"] not in {"local", "uri"}:
            raise TruthImportError("derivation premise kind must be local or uri")
        if row["premise_kind"] == "uri":
            try:
                parsed = parse_truth_uri(row["premise_ref"])
            except ValueError as exc:
                raise TruthImportError("derivation premise URI is malformed") from exc
            if parsed.kind != "claim":
                raise TruthImportError("derivation URI premises must target claims")
        return

    if record_type == "claim_link":
        link_type = _nonempty_text(row["link_type"], "claim_link.link_type")
        to_kind = _nonempty_text(row["to_kind"], "claim_link.to_kind")
        if link_type not in _LINK_TARGETS or to_kind not in _LINK_TARGETS[link_type]:
            raise TruthImportError("claim link type and target kind are incompatible")
        _json_value(row["role_json"], "claim_link.role_json", mapping=True)
        if row["target_fingerprint"] is not None:
            _digest(row["target_fingerprint"], "claim_link.target_fingerprint")
        if to_kind == "external_uri" and not urlparse(row["to_ref"]).scheme:
            raise TruthImportError("external claim links require a URI target")
        _timestamp(row["created_at"], "claim_link.created_at")
        return

    if record_type == "link_retraction":
        _timestamp(row["at"], "link_retraction.at")
        return

    if record_type == "claim_status_event":
        _positive_int(row["seq"], "claim_status_event.seq")
        if row["status"] not in VALID_STATUSES:
            raise TruthImportError("claim status event has an invalid status")
        if row["actor_kind"] not in VALID_ACTOR_KINDS:
            raise TruthImportError("claim status event has an invalid actor kind")
        _timestamp(row["at"], "claim_status_event.at")
        return

    if record_type == "gesture":
        _digest(row["payload_sha256"], "gesture.payload_sha256")
        if row["context_sha256"] is not None:
            _digest(row["context_sha256"], "gesture.context_sha256")
        _timestamp(row["at"], "gesture.at")
        if row["expires_at"] is not None:
            _timestamp(row["expires_at"], "gesture.expires_at")
        if row["consumed_at"] is not None:
            _timestamp(row["consumed_at"], "gesture.consumed_at")
        return

    if record_type == "redaction_event":
        if row["subject_kind"] not in {"claim", "evidence", "span"}:
            raise TruthImportError("redaction subject kind is invalid")
        if row["basis_kind"] not in {"gesture", "policy"}:
            raise TruthImportError("redaction basis kind is invalid")
        _timestamp(row["at"], "redaction_event.at")
        return

    if record_type == "sweep":
        _json_value(row["params_json"], "sweep.params_json", mapping=True)
        _timestamp(row["at"], "sweep.at")
        return

    if record_type == "sweep_finding":
        if row["resolved_at"] is not None:
            _timestamp(row["resolved_at"], "sweep_finding.resolved_at")
        return


def _validate_foreign_refs(records: tuple[_DataRecord, ...]) -> None:
    index = {(item.record_type, item.record_key): item.seq for item in records}

    def require_prior(
        record_type: str,
        key: Any,
        before: int,
        label: str,
    ) -> None:
        if record_type in _ID_KEY_TYPES or record_type == "link_retraction":
            normalized_key = _record_id(key, label)
        else:
            normalized_key = _nonempty_text(key, label)
        seq = index.get((record_type, normalized_key))
        if seq is None:
            raise TruthImportError(f"{label} references a missing {record_type}")
        if seq >= before:
            raise TruthImportError(f"{label} must reference an earlier ledger record")

    for item in records:
        row = item.record
        if item.record_type == "evidence_span":
            require_prior("evidence", row["evidence_id"], item.seq, "evidence_id")
        elif item.record_type == "derivation":
            require_prior("claim", row["claim_id"], item.seq, "derivation.claim_id")
        elif item.record_type == "derivation_premise":
            require_prior(
                "derivation",
                row["derivation_id"],
                item.seq,
                "derivation_premise.derivation_id",
            )
            if row["premise_kind"] == "local":
                require_prior(
                    "claim",
                    row["premise_ref"],
                    item.seq,
                    "derivation_premise.premise_ref",
                )
        elif item.record_type == "claim_link":
            require_prior(
                "claim", row["from_claim_id"], item.seq, "claim_link.from_claim_id"
            )
            if row["to_kind"] == "claim":
                require_prior("claim", row["to_ref"], item.seq, "claim_link.to_ref")
            elif row["to_kind"] == "evidence_span":
                require_prior(
                    "evidence_span", row["to_ref"], item.seq, "claim_link.to_ref"
                )
        elif item.record_type == "link_retraction":
            require_prior(
                "claim_link", row["link_id"], item.seq, "link_retraction.link_id"
            )
        elif item.record_type == "claim_status_event":
            require_prior("claim", row["claim_id"], item.seq, "status.claim_id")
            if row["basis_kind"] == "gesture" and row["basis_ref"] is not None:
                require_prior("gesture", row["basis_ref"], item.seq, "status.basis_ref")
        elif item.record_type == "redaction_event":
            subject_type = {
                "claim": "claim",
                "evidence": "evidence",
                "span": "evidence_span",
            }[row["subject_kind"]]
            require_prior(
                subject_type, row["subject_ref"], item.seq, "redaction subject"
            )
            if row["basis_kind"] == "gesture":
                require_prior("gesture", row["basis_ref"], item.seq, "redaction basis")
        elif item.record_type == "sweep_finding":
            require_prior("sweep", row["sweep_id"], item.seq, "sweep_finding.sweep_id")


def _validate_bundle(bundle: _Bundle) -> StoreProfile:
    profile = _validate_header(bundle)
    previous_seq = 0
    seen_pairs: set[tuple[str, str]] = set()
    status_seqs: set[int] = set()
    for item in bundle.records:
        if item.record_type not in _RECORD_COLUMNS:
            raise TruthImportError(
                f"unsupported ledger record type {item.record_type!r}"
            )
        seq = _positive_int(item.seq, "ledger seq")
        if seq <= previous_seq:
            raise TruthImportError("ledger records must be strictly ordered by seq")
        previous_seq = seq
        pair = (item.record_type, item.record_key)
        if pair in seen_pairs:
            raise TruthImportError("duplicate ledger record key")
        seen_pairs.add(pair)
        _validate_record_values(item)
        if item.record_type == "claim_status_event":
            status_seq = int(item.record["seq"])
            if status_seq in status_seqs:
                raise TruthImportError("duplicate claim status seq")
            status_seqs.add(status_seq)

    blob_map: dict[str, bytes] = {}
    previous_digest = ""
    for blob in bundle.blobs:
        digest = _digest(blob.content_sha256, "blob content_sha256")
        if digest <= previous_digest:
            raise TruthImportError("blob records must be unique and sorted by digest")
        previous_digest = digest
        if sha256_bytes(blob.content) != digest:
            raise TruthImportError("blob bytes do not match content_sha256")
        blob_map[digest] = blob.content

    referenced_blobs: set[str] = set()
    for item in bundle.records:
        if item.record_type != "evidence":
            continue
        row = item.record
        if row["redacted_at"] is None and row["content_path"] is not None:
            referenced_blobs.add(row["content_sha256"])
    if referenced_blobs != set(blob_map):
        missing = sorted(referenced_blobs - set(blob_map))
        extra = sorted(set(blob_map) - referenced_blobs)
        if missing:
            raise TruthImportError(f"export is missing live blobs: {missing}")
        raise TruthImportError(f"export contains unreferenced blobs: {extra}")

    _validate_foreign_refs(bundle.records)
    return profile


def _fetch_row(
    conn: sqlite3.Connection,
    record_type: str,
    record_key: str,
) -> dict[str, Any]:
    table, columns = _RECORD_COLUMNS[record_type]
    selected = ", ".join(columns)
    if record_type in _ID_KEY_TYPES:
        key_column = "id"
        params = (record_key,)
    elif record_type == "link_retraction":
        key_column = "link_id"
        params = (record_key,)
    else:
        try:
            key = json.loads(record_key)
        except json.JSONDecodeError as exc:
            raise TruthExportError(
                "derivation premise ledger key is malformed"
            ) from exc
        if not isinstance(key, dict) or set(key) != {"derivation_id", "premise_ref"}:
            raise TruthExportError("derivation premise ledger key is malformed")
        key_column = "derivation_id = ? AND premise_ref"
        params = (key["derivation_id"], key["premise_ref"])
    sql = f"SELECT {selected} FROM {table} WHERE {key_column} = ?"
    rows = conn.execute(sql, params).fetchall()
    if len(rows) != 1:
        raise TruthExportError(
            f"ledger record {record_type}:{record_key} has no unique source row"
        )
    row = dict(rows[0])
    try:
        computed = _record_key(record_type, row)
    except TruthImportError as exc:
        raise TruthExportError(str(exc)) from exc
    if computed != record_key:
        raise TruthExportError("ledger record key does not match its source row")
    return row


def _assert_all_rows_are_ordered(
    conn: sqlite3.Connection,
    ordered_keys: set[tuple[str, str]],
) -> None:
    for record_type, (table, columns) in _RECORD_COLUMNS.items():
        selected = ", ".join(columns)
        for raw in conn.execute(f"SELECT {selected} FROM {table}"):
            row = dict(raw)
            try:
                key = _record_key(record_type, row)
            except TruthImportError as exc:
                raise TruthExportError(str(exc)) from exc
            if (record_type, key) not in ordered_keys:
                raise TruthExportError(
                    f"{table} contains a row missing from ledger_records"
                )


def _collect_export_bundle(
    store: TruthStore,
    *,
    conn: sqlite3.Connection | None = None,
) -> _Bundle:
    profile = store.profile.to_dict()
    export_conn = store.connect() if conn is None else conn
    owns_transaction = conn is None
    if conn is not None:
        store._validate_connection_target(conn)
        store._require_transaction(conn)
    try:
        if owns_transaction:
            export_conn.execute("BEGIN IMMEDIATE")
        info_rows = export_conn.execute("SELECT * FROM store_info").fetchall()
        if len(info_rows) != 1:
            raise TruthExportError("store_info must contain exactly one row")
        store_info = dict(info_rows[0])
        records: list[_DataRecord] = []
        ordered_keys: set[tuple[str, str]] = set()
        previous_seq = 0
        for ledger in export_conn.execute(
            "SELECT seq, record_type, record_key FROM ledger_records ORDER BY seq"
        ):
            seq = int(ledger["seq"])
            record_type = str(ledger["record_type"])
            record_key = str(ledger["record_key"])
            if seq <= previous_seq:
                raise TruthExportError("ledger_records is not strictly ordered")
            previous_seq = seq
            if record_type not in _RECORD_COLUMNS:
                raise TruthExportError(
                    f"ledger contains unsupported record type {record_type!r}"
                )
            pair = (record_type, record_key)
            if pair in ordered_keys:
                raise TruthExportError("ledger contains a duplicate record key")
            ordered_keys.add(pair)
            records.append(
                _DataRecord(
                    seq=seq,
                    record_type=record_type,
                    record_key=record_key,
                    record=_fetch_row(export_conn, record_type, record_key),
                )
            )
        _assert_all_rows_are_ordered(export_conn, ordered_keys)

        blobs: dict[str, bytes] = {}
        for item in records:
            if item.record_type != "evidence":
                continue
            row = item.record
            if row["redacted_at"] is not None or row["content_path"] is None:
                continue
            digest = str(row["content_sha256"])
            path = store.resolve_blob_path(str(row["content_path"]))
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise TruthExportError(
                    f"live evidence blob is unavailable: {path}"
                ) from exc
            if sha256_bytes(content) != digest:
                raise TruthExportError(
                    "live evidence blob does not match content_sha256"
                )
            blobs[digest] = content
        if owns_transaction:
            export_conn.execute("COMMIT")
    except Exception:
        if owns_transaction and export_conn.in_transaction:
            export_conn.execute("ROLLBACK")
        raise
    finally:
        if owns_transaction:
            export_conn.close()

    bundle = _Bundle(
        source_format_version=FORMAT_VERSION,
        store_info=store_info,
        profile=profile,
        records=tuple(records),
        blobs=tuple(
            _BlobRecord(content_sha256=digest, content=content)
            for digest, content in sorted(blobs.items())
        ),
    )
    try:
        _validate_bundle(bundle)
    except TruthImportError as exc:
        raise TruthExportError(str(exc)) from exc
    return bundle


def _serialize_bundle(bundle: _Bundle) -> bytes:
    header = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "profile": dict(bundle.profile),
        "record_type": "header",
        "store_info": dict(bundle.store_info),
    }
    items: list[Mapping[str, Any]] = [header]
    items.extend(
        {
            "record": dict(item.record),
            "record_key": item.record_key,
            "record_type": item.record_type,
            "seq": item.seq,
        }
        for item in bundle.records
    )
    items.extend(
        {
            "content_base64": base64.b64encode(blob.content).decode("ascii"),
            "content_sha256": blob.content_sha256,
            "record_type": "blob",
        }
        for blob in bundle.blobs
    )
    prefix = b"".join(_canonical_line(item) for item in items)
    footer = {
        "blob_count": len(bundle.blobs),
        "last_seq": bundle.records[-1].seq if bundle.records else 0,
        "record_count": len(bundle.records),
        "record_type": "end",
        "stream_sha256": sha256_bytes(prefix),
    }
    return prefix + _canonical_line(footer)


def export_store(
    store: TruthStore,
    destination: str | Path | None = None,
) -> ExportResult:
    """Write a deterministic, atomic recovery export for ``store``."""
    if not isinstance(store, TruthStore):
        raise TypeError("store must be a TruthStore")
    path = (
        store.paths.claims_export
        if destination is None
        else Path(destination).expanduser().resolve()
    )
    # Keep the store's cross-process SQLite writer lock until the atomic file
    # publication completes. Without this, an older post-commit hook can
    # collect seq N, pause, and overwrite a newer seq N+K export after the
    # newer writer has published it.
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        bundle = _collect_export_bundle(store, conn=conn)
        payload = _serialize_bundle(bundle)
        atomic_write_bytes(path, payload)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return ExportResult(
        path=path,
        sha256=sha256_bytes(payload),
        record_count=len(bundle.records),
        blob_count=len(bundle.blobs),
        last_seq=bundle.records[-1].seq if bundle.records else 0,
    )


class _DuplicateJsonKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON value {value}")


def _read_objects(
    source: str | Path | bytes | bytearray | memoryview,
) -> list[dict[str, Any]]:
    if isinstance(source, (bytes, bytearray, memoryview)):
        payload = bytes(source)
    else:
        try:
            payload = Path(source).expanduser().resolve().read_bytes()
        except OSError as exc:
            raise TruthImportError(f"cannot read truth export: {source}") from exc
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TruthImportError("truth export must be UTF-8") from exc
    lines = text.splitlines()
    if not lines:
        raise TruthImportError("truth export is empty")
    if any(not line.strip() for line in lines):
        raise TruthImportError("truth export contains a blank record")
    objects: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        try:
            value = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TruthImportError(f"malformed JSON record on line {number}") from exc
        if not isinstance(value, dict):
            raise TruthImportError(f"line {number} must contain a JSON object")
        objects.append(value)
    end_positions = [
        index
        for index, value in enumerate(objects)
        if value.get("record_type") == "end"
    ]
    if not end_positions:
        raise TruthImportError("truth export is missing its end record")
    if len(end_positions) != 1 or end_positions[0] != len(objects) - 1:
        raise TruthImportError("truth export contains duplicate or trailing records")
    return objects


def _parse_header(objects: list[dict[str, Any]]) -> int:
    header = objects[0]
    _require_exact_keys(
        header,
        {"format", "format_version", "profile", "record_type", "store_info"},
        "format header",
    )
    if header["record_type"] != "header" or header["format"] != FORMAT_NAME:
        raise TruthImportError("truth export has an invalid format header")
    version = _positive_int(header["format_version"], "format_version")
    if version > FORMAT_VERSION:
        raise TruthImportError(
            f"truth export format v{version} is newer than supported v{FORMAT_VERSION}"
        )
    if version < OLDEST_FORMAT_VERSION:
        raise TruthImportError(f"truth export format v{version} is unsupported")
    return version


def _parse_v1(objects: list[dict[str, Any]]) -> _Bundle:
    header = objects[0]
    footer = objects[-1]
    _require_exact_keys(footer, {"record_count", "record_type"}, "v1 end record")
    records: list[_DataRecord] = []
    for number, value in enumerate(objects[1:-1], start=2):
        record_type = value.get("record_type")
        if record_type not in _RECORD_COLUMNS:
            raise TruthImportError(f"v1 line {number} has an unknown record type")
        _require_exact_keys(
            value, {"record", "record_type", "seq"}, f"v1 line {number}"
        )
        row = _require_mapping(value["record"], f"v1 line {number} record")
        records.append(
            _DataRecord(
                seq=_positive_int(value["seq"], f"v1 line {number} seq"),
                record_type=record_type,
                record_key=_record_key(record_type, row),
                record=row,
            )
        )
    expected_count = _positive_int(
        footer["record_count"], "v1 record_count", allow_zero=True
    )
    if expected_count != len(records):
        raise TruthImportError("v1 end record count does not match the stream")
    bundle = _Bundle(
        source_format_version=1,
        store_info=_require_mapping(header["store_info"], "store_info"),
        profile=_require_mapping(header["profile"], "profile"),
        records=tuple(records),
        blobs=(),
    )
    _validate_bundle(bundle)
    return bundle


def _parse_v2(objects: list[dict[str, Any]]) -> _Bundle:
    header = objects[0]
    footer = objects[-1]
    _require_exact_keys(
        footer,
        {
            "blob_count",
            "last_seq",
            "record_count",
            "record_type",
            "stream_sha256",
        },
        "end record",
    )
    expected_stream_hash = _digest(footer["stream_sha256"], "stream_sha256")
    canonical_prefix = b"".join(_canonical_line(item) for item in objects[:-1])
    if sha256_bytes(canonical_prefix) != expected_stream_hash:
        raise TruthImportError("truth export stream hash does not match")

    records: list[_DataRecord] = []
    blobs: list[_BlobRecord] = []
    in_blob_section = False
    for number, value in enumerate(objects[1:-1], start=2):
        record_type = value.get("record_type")
        if record_type == "blob":
            in_blob_section = True
            _require_exact_keys(
                value,
                {"content_base64", "content_sha256", "record_type"},
                f"blob line {number}",
            )
            digest = _digest(value["content_sha256"], "blob content_sha256")
            encoded = value["content_base64"]
            if not isinstance(encoded, str):
                raise TruthImportError("blob content_base64 must be text")
            try:
                content = base64.b64decode(encoded.encode("ascii"), validate=True)
            except (UnicodeEncodeError, binascii.Error) as exc:
                raise TruthImportError("blob content_base64 is malformed") from exc
            blobs.append(_BlobRecord(digest, content))
            continue
        if record_type not in _RECORD_COLUMNS:
            raise TruthImportError(f"line {number} has an unknown record type")
        if in_blob_section:
            raise TruthImportError("ledger data records cannot follow blob records")
        _require_exact_keys(
            value,
            {"record", "record_key", "record_type", "seq"},
            f"data line {number}",
        )
        records.append(
            _DataRecord(
                seq=_positive_int(value["seq"], f"line {number} seq"),
                record_type=record_type,
                record_key=_nonempty_text(
                    value["record_key"], f"line {number} record_key"
                ),
                record=_require_mapping(value["record"], f"line {number} record"),
            )
        )

    record_count = _positive_int(
        footer["record_count"], "record_count", allow_zero=True
    )
    blob_count = _positive_int(footer["blob_count"], "blob_count", allow_zero=True)
    last_seq = _positive_int(footer["last_seq"], "last_seq", allow_zero=True)
    if record_count != len(records) or blob_count != len(blobs):
        raise TruthImportError("end record counts do not match the stream")
    observed_last = records[-1].seq if records else 0
    if last_seq != observed_last:
        raise TruthImportError("end record last_seq does not match the stream")
    bundle = _Bundle(
        source_format_version=2,
        store_info=_require_mapping(header["store_info"], "store_info"),
        profile=_require_mapping(header["profile"], "profile"),
        records=tuple(records),
        blobs=tuple(blobs),
    )
    _validate_bundle(bundle)
    return bundle


def _parse_bundle(source: str | Path | bytes | bytearray | memoryview) -> _Bundle:
    objects = _read_objects(source)
    version = _parse_header(objects)
    bundle = _parse_v1(objects) if version == 1 else _parse_v2(objects)
    source_schema = int(bundle.store_info["schema_version"])
    if source_schema == SCHEMA_VERSION:
        return bundle

    # The JSONL format, not SQLite's internal schema version, governs the
    # portable record contract. Older streams have already been transport-
    # upcast and validated against the current record shapes above, so rebuild
    # them directly into the current schema and publish a current header.
    store_info = dict(bundle.store_info)
    store_info["schema_version"] = SCHEMA_VERSION
    return _Bundle(
        source_format_version=bundle.source_format_version,
        store_info=store_info,
        profile=bundle.profile,
        records=bundle.records,
        blobs=bundle.blobs,
    )


def _preflight_target(
    paths: StorePaths,
    store_id: str,
    registry: StoreRegistry,
) -> bool:
    if not paths.root.is_dir():
        raise TruthImportError("import target scope root must already exist")
    existed_empty = False
    if paths.sidecar.exists():
        if not paths.sidecar.is_dir():
            raise TruthImportError("import target sidecar path is not a directory")
        if any(paths.sidecar.iterdir()):
            raise TruthImportError("truth import target must be empty")
        existed_empty = True
    try:
        registered_paths = registry.paths_for_store_id(store_id)
    except AttributeError as exc:
        raise TruthImportError(
            "registry does not implement paths_for_store_id"
        ) from exc
    target = paths.sidecar.resolve()
    for registered in registered_paths:
        existing = StorePaths.from_root(registered).sidecar.resolve()
        if existing != target:
            raise StoreIdentityCollision(
                f"store_id {store_id} is already registered at {existing}"
            )
    return existed_empty


def _insert_records(store: TruthStore, bundle: _Bundle) -> None:
    conn = store.connect()
    try:
        migrate(conn, store.paths.db)
        conn.execute("BEGIN IMMEDIATE")
        info = bundle.store_info
        conn.execute(
            "INSERT INTO store_info "
            "(store_id, profile, schema_version, title, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                info["store_id"],
                info["profile"],
                SCHEMA_VERSION,
                info["title"],
                info["created_at"],
            ),
        )
        for item in bundle.records:
            table, columns = _RECORD_COLUMNS[item.record_type]
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(item.record[column] for column in columns),
            )
            store._insert_ledger_record_locked(
                conn,
                item.record_type,
                item.record_key,
                seq=item.seq,
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _build_staged_store(
    container: Path,
    bundle: _Bundle,
    profile: StoreProfile,
) -> TruthStore:
    paths = StorePaths.from_root(container)
    paths.sidecar.mkdir(parents=True)
    paths.blobs.mkdir()
    paths.export_dir.mkdir()
    dump_profile(profile, paths.config)
    staged = TruthStore(paths)
    for blob in bundle.blobs:
        atomic_write_bytes(paths.blobs / blob.content_sha256, blob.content)
    _insert_records(staged, bundle)
    expected = _serialize_bundle(bundle)
    result = export_store(staged)
    if result.path.read_bytes() != expected:
        raise TruthImportError("staged store does not reproduce the validated export")
    TruthStore.open(paths.sidecar)
    return staged


def _remove_staging(container: Path, allowed_parent: Path) -> None:
    if not container.exists():
        return
    resolved = container.resolve()
    parent = allowed_parent.resolve()
    if resolved.parent != parent or not resolved.name.startswith(".wb-truth-import-"):
        raise RuntimeError("refusing to remove an unexpected import staging path")
    shutil.rmtree(resolved)


def import_store(
    source: str | Path | bytes | bytearray | memoryview,
    target: str | Path,
    *,
    registry: StoreRegistry,
) -> ImportResult:
    """Preflight and atomically rebuild one empty target from JSONL."""
    bundle = _parse_bundle(source)
    profile = _validate_bundle(bundle)
    target_paths = StorePaths.from_root(target)
    _preflight_target(target_paths, profile.store_id, registry)

    container = Path(
        tempfile.mkdtemp(prefix=".wb-truth-import-", dir=target_paths.root)
    )
    removed_empty_target = False
    try:
        staged = _build_staged_store(container, bundle, profile)
        staged_sidecar = staged.paths.sidecar.resolve()
        if staged_sidecar.parent != container.resolve():
            raise TruthImportError("staged sidecar escaped its import container")
        if target_paths.sidecar.exists():
            if any(target_paths.sidecar.iterdir()):
                raise TruthImportError("truth import target changed during import")
            target_paths.sidecar.rmdir()
            removed_empty_target = True
        os.replace(staged_sidecar, target_paths.sidecar)
    except Exception:
        if removed_empty_target and not target_paths.sidecar.exists():
            target_paths.sidecar.mkdir()
        raise
    finally:
        _remove_staging(container, target_paths.root)

    restored = TruthStore.open(target_paths.sidecar)
    return ImportResult(
        store=restored,
        source_format_version=bundle.source_format_version,
        record_count=len(bundle.records),
        blob_count=len(bundle.blobs),
    )


__all__ = [
    "ExportResult",
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "ImportResult",
    "OLDEST_FORMAT_VERSION",
    "StoreIdentityCollision",
    "StoreRegistry",
    "TruthExportError",
    "TruthImportError",
    "export_store",
    "import_store",
]

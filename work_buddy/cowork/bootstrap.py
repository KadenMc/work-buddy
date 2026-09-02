"""Recoverable two-phase create/import/repair for Co-work documents."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from work_buddy.artifacts.io import atomic_write_bytes
from work_buddy.cowork import provenance
from work_buddy.cowork.file_importers import (
    DEFAULT_FILE_IMPORTERS,
    MARKDOWN_IMPORTER_ID,
    MARKDOWN_MAX_SOURCE_BYTES,
    MARKDOWN_MEDIA_TYPE,
    FileImporter,
)
from work_buddy.cowork.paths import (
    CoworkPathError,
    resolve_document_source_path,
    resolve_markdown_path,
    resolve_relative_file_path,
)
from work_buddy.cowork.truth_activation import (
    LEGACY_FULL_COWORK_CONTRACT,
    provision_document_policy,
)
from work_buddy.cowork.readiness import classify_document
from work_buddy.cowork.source_observation import (
    SourceObservationError,
    read_bounded_regular_file,
)
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.identity import canonical_json, new_id, sha256_bytes
from work_buddy.truth.store import TruthStore, _valid_digest


YDOC_SCHEMA = "cowork-yjs/v1"
# Compatibility alias for callers that predate importer-specific source limits.
MAX_SOURCE_BYTES = MARKDOWN_MAX_SOURCE_BYTES
MAX_CANONICAL_PROJECTION_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_BYTES = ydoc_store.MAX_OPAQUE_SEGMENT_BYTES
BOOTSTRAP_TTL = timedelta(minutes=30)
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


class BootstrapError(InvariantViolation):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = dict(details or {})
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class BootstrapIntent:
    id: str
    idempotency_key: str
    actor_ref: str
    request_sha256: str
    mode: str
    state: str
    document_id: str
    normalized_path: str
    path_key: str
    title: str | None
    document_class: str
    source_sha256: str
    source_byte_length: int
    expected_file_sha256: str | None
    importer_id: str | None
    source_media_type: str | None
    import_attestation_sha256: str | None
    snapshot_sha256: str | None
    structured_head_sha256: str | None
    staged_path: str | None
    created_at: str
    updated_at: str
    expires_at: str
    committed_at: str | None
    receipt_json: str | None
    recovery_detail: str | None

    @property
    def receipt(self) -> dict[str, Any] | None:
        if self.receipt_json is None:
            return None
        value = json.loads(self.receipt_json)
        return value if isinstance(value, dict) else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def _actor_ref(actor: Actor) -> str:
    if actor.kind != "human" or not actor.ref:
        raise BootstrapError("actor_forbidden", "bootstrap requires a human actor", status=403)
    return actor.ref


def _idempotency_key(value: Any) -> str:
    key = str(value or "").strip()
    if not _KEY_RE.fullmatch(key):
        raise BootstrapError(
            "invalid_idempotency_key",
            "idempotency_key must be 8-128 safe characters",
        )
    return key


def _stage_relative(intent_id: str) -> str:
    return f"runtime/bootstrap/{intent_id}/source.bin"


def _stage_path(store: TruthStore, intent_id: str) -> Path:
    return store.paths.sidecar / _stage_relative(intent_id)


def _attestation_stage_path(store: TruthStore, intent_id: str) -> Path:
    return _stage_path(store, intent_id).with_name("authorship-attestation.json")


def _default_import_attestation() -> dict[str, Any]:
    return {
        "schema": provenance.INPUT_ATTESTATION_SCHEMA,
        "authorship": {"kind": "unknown", "contributors": []},
        "human_review": {"status": "not_applicable", "reviewers": []},
    }


def _import_attestation(metadata: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
    raw = metadata.get("authorship_attestation")
    if raw is None:
        return _default_import_attestation()
    if not isinstance(raw, Mapping):
        raise BootstrapError(
            "invalid_authorship_attestation",
            "authorship_attestation must be an object",
        )
    # Validate now, before staging or file I/O. Keep the frozen client form in
    # staging so commit can revalidate the exact actor binding rather than
    # resolving ``current_user`` against whichever actor happens to replay it.
    try:
        provenance.normalize_attestation(raw, actor=actor)
    except provenance.ProvenanceActorBindingError as exc:
        raise BootstrapError(exc.code, str(exc), status=exc.status) from exc
    except InvariantViolation as exc:
        raise BootstrapError("invalid_authorship_attestation", str(exc)) from exc
    return dict(raw)


def _read_staged_attestation(
    store: TruthStore,
    intent: BootstrapIntent,
) -> dict[str, Any]:
    path = _attestation_stage_path(store, intent.id)
    if not path.is_file():
        if intent.import_attestation_sha256 is None:
            # Compatibility only for prepared imports created before v8 added
            # provenance-aware staging. New imports always bind this sidecar.
            return _default_import_attestation()
        raise BootstrapError(
            "staged_attestation_missing",
            "The staged authorship details are unavailable.",
            status=409,
        )
    try:
        payload = path.read_bytes()
        if (
            intent.import_attestation_sha256 is None
            or sha256_bytes(payload) != intent.import_attestation_sha256
        ):
            raise BootstrapError(
                "staged_attestation_corrupt",
                "The staged authorship details failed integrity verification.",
                status=409,
            )
        value = json.loads(payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError(
            "staged_attestation_corrupt",
            "The staged authorship details failed integrity verification.",
            status=409,
        ) from exc
    if not isinstance(value, dict):
        raise BootstrapError(
            "staged_attestation_corrupt",
            "The staged authorship details failed integrity verification.",
            status=409,
        )
    return value


def _intent_from_row(row: Any) -> BootstrapIntent:
    return BootstrapIntent(**dict(row))


def get_intent(store: TruthStore, bootstrap_id: str) -> BootstrapIntent:
    with store._read_connection() as conn:
        row = conn.execute(
            "SELECT * FROM cowork_bootstrap_intents WHERE id = ?",
            (bootstrap_id,),
        ).fetchone()
    if row is None:
        raise BootstrapError("bootstrap_not_found", "bootstrap intent does not exist", status=404)
    return _intent_from_row(row)


def _authorize_profile(store: TruthStore) -> None:
    surface = store.profile.document_surface
    if not surface.enabled:
        raise BootstrapError(
            "policy_forbidden", "Co-work is not enabled for this folder", status=403
        )
    allowed = surface.allowed_document_classes
    if allowed and "co_authored" not in allowed:
        raise BootstrapError(
            "policy_forbidden",
            "This folder does not admit co-authored documents",
            status=403,
        )


def _importer_for_request(
    path: str,
    metadata: Mapping[str, Any],
) -> FileImporter:
    """Resolve browser assertions through the authoritative importer registry."""

    importer_id = str(
        metadata.get("importer_id") or MARKDOWN_IMPORTER_ID
    ).strip()
    importer = DEFAULT_FILE_IMPORTERS.importer_by_id(importer_id)
    if importer is None:
        raise BootstrapError(
            "unsupported_file_type",
            "This version of Co-work does not support that file importer.",
            status=415,
            details={"importer_id": importer_id},
        )
    if DEFAULT_FILE_IMPORTERS.resolve_binding(
        path,
        importer_id=importer.importer_id,
    ) is None:
        raise BootstrapError(
            "importer_path_mismatch",
            "The selected file does not match its declared importer.",
            status=415,
            details={
                "importer_id": importer.importer_id,
                "supported_suffixes": list(importer.suffixes),
            },
        )
    claimed_media_type = str(metadata.get("source_media_type") or "").strip()
    if claimed_media_type and claimed_media_type != importer.media_type:
        raise BootstrapError(
            "importer_media_type_mismatch",
            "The selected file media type does not match its importer.",
            status=415,
            details={
                "importer_id": importer.importer_id,
                "expected_media_type": importer.media_type,
            },
        )
    return importer


def _importer_for_intent(intent: BootstrapIntent) -> FileImporter:
    importer_id = intent.importer_id or MARKDOWN_IMPORTER_ID
    importer = DEFAULT_FILE_IMPORTERS.importer_by_id(importer_id)
    if importer is None:
        raise BootstrapError(
            "importer_unavailable",
            "The importer used to prepare this file is no longer available.",
            status=409,
            details={"importer_id": importer_id},
        )
    if (
        DEFAULT_FILE_IMPORTERS.resolve_binding(
            intent.normalized_path,
            importer_id=importer.importer_id,
        )
        is None
        or (intent.source_media_type or importer.media_type)
        != importer.media_type
    ):
        raise BootstrapError(
            "staged_importer_mismatch",
            "The prepared importer binding failed integrity validation.",
            status=409,
        )
    return importer


def importer_descriptor(intent: BootstrapIntent) -> dict[str, object] | None:
    """Return the authoritative source descriptor frozen by an import intent."""

    if intent.mode != "import":
        return None
    return _importer_for_intent(intent).descriptor()


def maximum_source_upload_bytes() -> int:
    """Bound HTTP staging before importer-specific admission runs."""

    return max(MAX_SOURCE_BYTES, DEFAULT_FILE_IMPORTERS.maximum_source_bytes)


def _resolve_intent_path(
    store: TruthStore,
    intent: BootstrapIntent,
):
    if intent.mode == "import":
        _importer_for_intent(intent)
        return resolve_relative_file_path(store, intent.normalized_path)
    if intent.mode == "repair":
        try:
            document = documents.get_document(store, intent.document_id)
        except InvariantViolation:
            return resolve_markdown_path(store, intent.normalized_path)
        return resolve_document_source_path(store, document)
    return resolve_markdown_path(
        store,
        intent.normalized_path,
        for_create=True,
    )


def _read_bounded_regular_file(
    path: Path,
    *,
    maximum: int,
    source_label: str,
) -> bytes:
    """Adapt the shared external-source reader to the bootstrap error contract."""

    try:
        result = read_bounded_regular_file(
            path,
            maximum=maximum,
            source_label=source_label,
        )
    except SourceObservationError as exc:
        raise BootstrapError(
            exc.code,
            str(exc),
            status=exc.status,
            details=exc.details,
            retryable=exc.retryable,
        ) from exc
    assert result.data is not None
    return result.data


def _source_for_prepare(
    store: TruthStore,
    *,
    mode: str,
    resolved: Any,
    metadata: Mapping[str, Any],
    source: bytes | None,
    max_source_bytes: int,
    source_label: str,
) -> tuple[bytes, str | None, str | None]:
    declared = str(metadata.get("initial_source_sha256") or "").strip().lower()
    expected = str(metadata.get("expected_file_sha256") or "").strip().lower() or None
    if expected is not None:
        expected = _valid_digest(expected, "expected_file_sha256")

    if mode == "create":
        payload = bytes(source or b"")
        if len(payload) > max_source_bytes:
            raise BootstrapError(
                "source_too_large",
                f"{source_label} exceeds the size limit",
                status=413,
                details={
                    "max_source_bytes": max_source_bytes,
                    "source_byte_length": len(payload),
                },
            )
        if resolved.path.exists():
            raise BootstrapError("path_conflict", "document path already exists", status=409)
        digest = sha256_bytes(payload)
        if declared and declared != digest:
            raise BootstrapError("source_hash_mismatch", "create source hash does not match")
        return payload, digest, None

    if not resolved.path.is_file():
        raise BootstrapError(
            "source_not_found",
            f"{source_label} does not exist",
            status=404,
        )
    payload = _read_bounded_regular_file(
        resolved.path,
        maximum=max_source_bytes,
        source_label=source_label,
    )
    digest = sha256_bytes(payload)
    if expected is not None and digest != expected:
        raise BootstrapError(
            "source_changed",
            f"{source_label} changed before bootstrap",
            status=409,
            details={"current_file_sha256": digest},
        )
    return payload, digest, digest


def prepare_bootstrap(
    store: TruthStore,
    *,
    metadata: Mapping[str, Any],
    source: bytes | None,
    actor: Actor,
) -> tuple[BootstrapIntent, bool]:
    """Stage source while excluding a concurrent legacy/canonical store move."""

    with store.migration_write_lock():
        intent, created = _prepare_bootstrap_locked(
            store,
            metadata=metadata,
            source=source,
            actor=actor,
        )
    if not created and intent.state in {"publishing", "committed"}:
        intent = recover_bootstrap_intent(store, intent.id)
    return intent, created


def _prepare_bootstrap_locked(
    store: TruthStore,
    *,
    metadata: Mapping[str, Any],
    source: bytes | None,
    actor: Actor,
) -> tuple[BootstrapIntent, bool]:
    """Implementation for prepare_bootstrap with the external lock held."""

    _authorize_profile(store)
    actor_ref = _actor_ref(actor)
    mode = str(metadata.get("mode") or "").strip().lower()
    if mode not in {"create", "import", "repair"}:
        raise BootstrapError("invalid_mode", "mode must be create, import, or repair")
    supplied_class = str(metadata.get("document_class") or "").strip()
    if supplied_class == "generated":
        raise BootstrapError(
            "reserved_document_class",
            "generated documents require a dedicated generation workflow",
            status=409,
        )
    if supplied_class not in {"", "co_authored"}:
        raise BootstrapError("invalid_document_class", "ordinary documents are co-authored")

    path = str(metadata.get("path") or "")
    existing_document_id = str(metadata.get("document_id") or "").strip() or None
    document = None
    importer = None
    if mode == "repair":
        if existing_document_id is None:
            raise BootstrapError("document_id_required", "repair requires document_id")
        document = documents.get_document(store, existing_document_id)
        try:
            resolved = resolve_document_source_path(store, document)
        except CoworkPathError as exc:
            raise BootstrapError("invalid_path", str(exc)) from exc
        if path != resolved.normalized:
            raise BootstrapError("path_mismatch", "repair path does not match document")
    else:
        if existing_document_id is not None:
            raise BootstrapError(
                "unexpected_document_id",
                "create/import mints the document identifier",
            )
        if mode == "import":
            importer = _importer_for_request(path, metadata)
        try:
            resolved = (
                resolve_relative_file_path(store, path)
                if importer is not None
                else resolve_markdown_path(store, path, for_create=True)
            )
        except CoworkPathError as exc:
            raise BootstrapError("invalid_path", str(exc)) from exc
    title_raw = metadata.get("title")
    title = None if title_raw is None else str(title_raw).strip()
    if title is not None and (not title or len(title) > 240):
        raise BootstrapError("invalid_title", "title must contain 1-240 characters")
    key = _idempotency_key(metadata.get("idempotency_key"))

    if mode == "repair":
        assert document is not None
        readiness = classify_document(store, document)
        if readiness.initialization_state != "bootstrap_required":
            raise BootstrapError(
                "repair_not_safe",
                f"document is {readiness.initialization_state}, not safely repairable",
                status=409,
            )
        if (
            documents.source_is_detached(document)
            and documents.retained_file_import_source_sha256(
                document.meta_json
            )
            != document.content_sha256
        ):
            raise BootstrapError(
                "repair_not_supported",
                (
                    "This imported document cannot be repaired from its source "
                    "file because its managed copy was normalized during import."
                ),
                status=409,
                details={
                    "source_writeback": documents.SOURCE_WRITEBACK_NEVER,
                    "normalized_projection": True,
                },
            )
        document_id = document.id
    else:
        with store._read_connection() as conn:
            occupied = conn.execute(
                "SELECT d.id FROM documents d JOIN document_path_keys k "
                "ON k.document_id = d.id WHERE k.path_key = ?",
                (resolved.path_key,),
            ).fetchone()
            occupied_lifecycle = (
                None
                if occupied is None
                else documents.current_lifecycle(store, occupied[0], conn=conn)
            )
        if occupied is not None:
            if occupied_lifecycle == "retired":
                raise BootstrapError(
                    "retired_path",
                    (
                        "This file already has a retired Co-work copy. Its "
                        "history is preserved, so this path cannot be reused."
                    ),
                    status=409,
                    details={
                        "document_id": occupied[0],
                        "lifecycle": "retired",
                        "path_reuse": "forbidden",
                        "recovery_action": "choose_different_path",
                    },
                )
            raise BootstrapError(
                "already_registered",
                "That file is already registered",
                status=409,
                details={"document_id": occupied[0]},
            )
        document_id = new_id()

    source_limit = (
        importer.max_source_bytes
        if importer is not None
        else MAX_SOURCE_BYTES
    )
    source_label = "The selected source file" if mode == "import" else "Markdown source"
    payload, source_digest, expected_file = _source_for_prepare(
        store,
        mode=mode,
        resolved=resolved,
        metadata=metadata,
        source=source,
        max_source_bytes=source_limit,
        source_label=source_label,
    )
    if mode == "repair" and source_digest != document.content_sha256:
        raise BootstrapError(
            "repair_not_safe",
            "current file no longer matches the document projection pointer",
            status=409,
        )
    assert source_digest is not None
    importer_id = None
    source_media_type = None
    if mode == "import":
        assert importer is not None
        importer_id = importer.importer_id
        source_media_type = importer.media_type
    canonical_request = {
        "mode": mode,
        "path": resolved.normalized,
        "title": title,
        "document_id": document_id if mode == "repair" else None,
        "document_class": "co_authored",
        "source_sha256": source_digest,
        "expected_file_sha256": expected_file,
    }
    import_attestation = (
        _import_attestation(metadata, actor) if mode == "import" else None
    )
    if import_attestation is not None:
        assert importer is not None
        canonical_request["importer_id"] = importer_id
        canonical_request["source_media_type"] = source_media_type
        canonical_request["source_format"] = importer.source_format
        canonical_request["max_source_bytes"] = importer.max_source_bytes
        canonical_request["authorship_attestation"] = import_attestation
    request_digest = sha256_bytes(canonical_json(canonical_request).encode("utf-8"))

    with store._read_connection() as conn:
        row = conn.execute(
            "SELECT * FROM cowork_bootstrap_intents "
            "WHERE actor_ref = ? AND idempotency_key = ?",
            (actor_ref, key),
        ).fetchone()
    if row is not None:
        existing = _intent_from_row(row)
        if existing.request_sha256 != request_digest:
            raise BootstrapError(
                "idempotency_conflict",
                "idempotency key was already used for a different bootstrap",
                status=409,
            )
        return existing, False

    intent_id = new_id()
    now = _now()
    created = _timestamp(now)
    expires = _timestamp(now + BOOTSTRAP_TTL)
    stage = _stage_path(store, intent_id)
    attestation_payload = (
        canonical_json(import_attestation).encode("utf-8")
        if import_attestation is not None
        else None
    )
    attestation_sha256 = (
        sha256_bytes(attestation_payload)
        if attestation_payload is not None
        else None
    )
    try:
        stage.parent.mkdir(parents=True, exist_ok=False)
        atomic_write_bytes(stage, payload)
        if attestation_payload is not None:
            atomic_write_bytes(
                _attestation_stage_path(store, intent_id),
                attestation_payload,
            )
        with store.write_transaction() as conn:
            conn.execute(
                "INSERT INTO cowork_bootstrap_intents ("
                "id, idempotency_key, actor_ref, request_sha256, mode, state, "
                "document_id, normalized_path, path_key, title, document_class, "
                "source_sha256, source_byte_length, expected_file_sha256, "
                "importer_id, source_media_type, import_attestation_sha256, "
                "snapshot_sha256, structured_head_sha256, staged_path, created_at, "
                "updated_at, expires_at, committed_at, receipt_json, recovery_detail"
                ") VALUES (?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, 'co_authored', "
                "?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, NULL, NULL, NULL)",
                (
                    intent_id,
                    key,
                    actor_ref,
                    request_digest,
                    mode,
                    document_id,
                    resolved.normalized,
                    resolved.path_key,
                    title,
                    source_digest,
                    len(payload),
                    expected_file,
                    importer_id,
                    source_media_type,
                    attestation_sha256,
                    _stage_relative(intent_id),
                    created,
                    created,
                    expires,
                ),
            )
    except Exception:
        stage.unlink(missing_ok=True)
        _attestation_stage_path(store, intent_id).unlink(missing_ok=True)
        try:
            stage.parent.rmdir()
        except OSError:
            pass
        raise
    return get_intent(store, intent_id), True


def read_staged_source(
    store: TruthStore,
    *,
    bootstrap_id: str,
    actor: Actor,
) -> tuple[BootstrapIntent, bytes]:
    intent = get_intent(store, bootstrap_id)
    if intent.actor_ref != _actor_ref(actor):
        raise BootstrapError("bootstrap_not_found", "bootstrap intent does not exist", status=404)
    if intent.state not in {"prepared", "publishing", "committed"}:
        raise BootstrapError("bootstrap_unavailable", "bootstrap source is no longer available", status=410)
    path = _stage_path(store, intent.id)
    if not path.is_file():
        raise BootstrapError("staged_source_missing", "staged source is unavailable", status=409)
    payload = path.read_bytes()
    if len(payload) != intent.source_byte_length or sha256_bytes(payload) != intent.source_sha256:
        raise BootstrapError("staged_source_corrupt", "staged source failed integrity verification", status=409)
    return intent, payload


def _publish_create(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise BootstrapError("path_conflict", "document path appeared before commit", status=409) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if path.is_file() and sha256_bytes(path.read_bytes()) == sha256_bytes(payload):
            path.unlink(missing_ok=True)
        raise


def _remove_stage(store: TruthStore, intent_id: str) -> None:
    path = _stage_path(store, intent_id)
    path.unlink(missing_ok=True)
    _attestation_stage_path(store, intent_id).unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _stage_bytes(store: TruthStore, intent: BootstrapIntent) -> bytes | None:
    path = _stage_path(store, intent.id)
    if not path.is_file():
        return None
    payload = path.read_bytes()
    if (
        len(payload) != intent.source_byte_length
        or sha256_bytes(payload) != intent.source_sha256
    ):
        return None
    return payload


def _recovered_receipt(
    store: TruthStore,
    intent: BootstrapIntent,
) -> dict[str, Any] | None:
    try:
        document = documents.get_document(store, intent.document_id)
    except InvariantViolation:
        return None
    version = documents.current_document_version(store, document.id)
    source_matches = document.content_sha256 == intent.source_sha256
    if intent.mode == "import":
        try:
            document_meta = (
                json.loads(document.meta_json) if document.meta_json else {}
            )
        except (TypeError, json.JSONDecodeError):
            document_meta = {}
        source_meta = (
            document_meta.get("source")
            if isinstance(document_meta, dict)
            else None
        )
        source_matches = (
            isinstance(source_meta, dict)
            and source_meta.get("sha256") == intent.source_sha256
            and source_meta.get("writeback_policy")
            == documents.SOURCE_WRITEBACK_NEVER
            and source_meta.get("importer_id")
            == (intent.importer_id or MARKDOWN_IMPORTER_ID)
        )
    if (
        version is None
        or not source_matches
        or document.ydoc_snapshot_sha256 != intent.snapshot_sha256
        or version.projection_sha256 != document.content_sha256
        or version.ydoc_snapshot_sha256 != document.ydoc_snapshot_sha256
        or version.structured_head_sha256 != intent.structured_head_sha256
    ):
        return None
    recovered_importer = (
        _importer_for_intent(intent)
        if intent.mode == "import"
        else None
    )
    return {
        "ok": True,
        "mode": intent.mode,
        "document_id": document.id,
        "path": document.path,
        "title": document.title or "",
        "document_class": document.document_class,
        "initialization_state": "ready",
        "snapshot_sha256": document.ydoc_snapshot_sha256,
        "structured_head_sha256": version.structured_head_sha256,
        "projection_sha256": document.content_sha256,
        # Compatibility field: detached documents use their internal
        # projection head as the sitting CAS baseline. The external source is
        # exposed separately and is never a writeback target.
        "current_file_sha256": document.content_sha256,
        "source_file_sha256": intent.source_sha256,
        "source_importer": (
            recovered_importer.descriptor()
            if recovered_importer is not None
            else None
        ),
        "document_version_id": version.id,
        "drift_state": "clean",
        "source_writeback": documents.source_writeback_policy(document),
        "permissions": {
            "open": True,
            "edit": True,
            "materialize": not documents.source_is_detached(document),
            "repair": False,
            "retire": True,
        },
    }


def _recover_bootstrap_locked(
    store: TruthStore,
    intent: BootstrapIntent,
) -> BootstrapIntent:
    """Recover one bootstrap while its path/document operation lock is held."""

    intent = get_intent(store, intent.id)
    now = _timestamp(_now())
    if intent.state == "committed":
        receipt = intent.receipt or _recovered_receipt(store, intent)
        if receipt is None:
            raise BootstrapError(
                "recovery_required",
                "The completed Co-work document needs manual recovery.",
                status=409,
                retryable=True,
            )
        stage = _stage_bytes(store, intent)
        if stage is not None:
            _remove_stage(store, intent.id)
        with store.write_transaction() as conn:
            conn.execute(
                "UPDATE cowork_bootstrap_intents SET receipt_json = ?, "
                "staged_path = CASE WHEN ? THEN NULL ELSE staged_path END, "
                "updated_at = ?, recovery_detail = CASE WHEN ? THEN NULL "
                "ELSE 'committed_stage_requires_cleanup' END WHERE id = ?",
                (
                    canonical_json(receipt),
                    int(stage is not None or not _stage_path(store, intent.id).exists()),
                    now,
                    int(stage is not None or not _stage_path(store, intent.id).exists()),
                    intent.id,
                ),
            )
        return get_intent(store, intent.id)
    if intent.state != "publishing":
        return intent

    receipt = _recovered_receipt(store, intent)
    if receipt is not None:
        with store.write_transaction() as conn:
            conn.execute(
                "UPDATE cowork_bootstrap_intents SET state = 'committed', "
                "receipt_json = ?, committed_at = COALESCE(committed_at, ?), "
                "updated_at = ?, recovery_detail = NULL WHERE id = ? "
                "AND state = 'publishing'",
                (canonical_json(receipt), now, now, intent.id),
            )
        return _recover_bootstrap_locked(store, get_intent(store, intent.id))

    stage = _stage_bytes(store, intent)
    if stage is None:
        with store.write_transaction() as conn:
            conn.execute(
                "UPDATE cowork_bootstrap_intents SET state = 'failed', "
                "updated_at = ?, recovery_detail = 'recovery_required:staged_source' "
                "WHERE id = ? AND state = 'publishing'",
                (now, intent.id),
            )
        return get_intent(store, intent.id)
    resolved = _resolve_intent_path(store, intent)
    safe = False
    if intent.mode == "create":
        if not resolved.path.exists():
            safe = True
        else:
            try:
                current = _read_bounded_regular_file(
                    resolved.path,
                    maximum=MAX_SOURCE_BYTES,
                    source_label="The created Markdown file",
                )
            except BootstrapError:
                pass
            else:
                if sha256_bytes(current) == intent.source_sha256:
                    resolved.path.unlink()
                    safe = True
    else:
        source_limit = (
            _importer_for_intent(intent).max_source_bytes
            if intent.mode == "import"
            else MAX_SOURCE_BYTES
        )
        try:
            current = _read_bounded_regular_file(
                resolved.path,
                maximum=source_limit,
                source_label=(
                    "The selected source file"
                    if intent.mode == "import"
                    else "Markdown source"
                ),
            )
        except BootstrapError:
            pass
        else:
            safe = sha256_bytes(current) == intent.source_sha256
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE cowork_bootstrap_intents SET state = ?, updated_at = ?, "
            "recovery_detail = ? WHERE id = ? AND state = 'publishing'",
            (
                "prepared" if safe else "failed",
                now,
                "retry_safe" if safe else "recovery_required:external_state",
                intent.id,
            ),
        )
    return get_intent(store, intent.id)


def recover_bootstrap_intent(
    store: TruthStore,
    bootstrap_id: str,
) -> BootstrapIntent:
    initial = get_intent(store, bootstrap_id)
    with ydoc_store.document_lock(
        store,
        bootstrap_id,
        path_key=initial.path_key,
    ):
        return _recover_bootstrap_locked(store, get_intent(store, bootstrap_id))


def commit_bootstrap(
    store: TruthStore,
    *,
    bootstrap_id: str,
    snapshot: bytes,
    source_sha256: str,
    snapshot_sha256: str,
    ydoc_schema: str,
    actor: Actor,
    projection: bytes | None = None,
    projection_sha256: str | None = None,
) -> dict[str, Any]:
    """Commit an initialized opaque snapshot and make the document visible."""

    _authorize_profile(store)
    actor_ref = _actor_ref(actor)
    if ydoc_schema != YDOC_SCHEMA:
        raise BootstrapError("unsupported_ydoc_schema", f"Y.Doc schema must be {YDOC_SCHEMA}")
    if not isinstance(snapshot, (bytes, bytearray, memoryview)) or not snapshot:
        raise BootstrapError("invalid_snapshot", "initialized Y.Doc snapshot cannot be empty")
    snapshot_bytes = bytes(snapshot)
    if len(snapshot_bytes) > MAX_SNAPSHOT_BYTES:
        raise BootstrapError("snapshot_too_large", "Y.Doc snapshot exceeds the size limit", status=413)
    declared_source = _valid_digest(source_sha256, "source_sha256")
    declared_snapshot = _valid_digest(snapshot_sha256, "snapshot_sha256")
    if sha256_bytes(snapshot_bytes) != declared_snapshot:
        raise BootstrapError("snapshot_hash_mismatch", "Y.Doc snapshot hash does not match")
    projection_bytes = None if projection is None else bytes(projection)
    if projection_bytes is not None:
        if len(projection_bytes) > MAX_CANONICAL_PROJECTION_BYTES:
            raise BootstrapError(
                "projection_too_large",
                "Co-work document projection exceeds the size limit",
                status=413,
            )
        declared_projection = _valid_digest(
            projection_sha256 or "",
            "projection_sha256",
        )
        if sha256_bytes(projection_bytes) != declared_projection:
            raise BootstrapError(
                "projection_hash_mismatch",
                "Co-work projection hash does not match",
            )

    initial_intent = get_intent(store, bootstrap_id)
    with ydoc_store.document_lock(
        store,
        bootstrap_id,
        path_key=initial_intent.path_key,
    ):
        intent = get_intent(store, bootstrap_id)
        if intent.actor_ref != actor_ref:
            raise BootstrapError("bootstrap_not_found", "bootstrap intent does not exist", status=404)
        if intent.state in {"publishing", "committed"}:
            intent = _recover_bootstrap_locked(store, intent)
        if intent.state == "committed" and intent.receipt is not None:
            return intent.receipt
        if intent.state != "prepared":
            raise BootstrapError(
                "bootstrap_not_committable",
                f"bootstrap is {intent.state}",
                status=409,
            )
        if declared_source != intent.source_sha256:
            raise BootstrapError("source_hash_mismatch", "source hash does not match prepared bytes", status=409)
        intent, source = read_staged_source(store, bootstrap_id=bootstrap_id, actor=actor)
        if projection_bytes is None:
            # Compatibility for older bootstrap clients, whose initialized
            # projection was required to be byte-identical to the source.
            projection_bytes = source
            declared_projection = intent.source_sha256
        if intent.mode != "import" and projection_bytes != source:
            raise BootstrapError(
                "projection_not_lossless",
                "Create and repair require a projection identical to the source bytes.",
                status=409,
            )
        import_attestation = (
            _read_staged_attestation(store, intent)
            if intent.mode == "import"
            else None
        )
        if import_attestation is not None:
            try:
                provenance.normalize_attestation(
                    import_attestation,
                    actor=actor,
                )
            except provenance.ProvenanceActorBindingError as exc:
                raise BootstrapError(
                    exc.code,
                    str(exc),
                    status=exc.status,
                ) from exc
            except InvariantViolation as exc:
                # A new prepared intent was validated before its hash-bound
                # sidecar was staged. Failure here therefore means a legacy or
                # otherwise unusable staged determination, not user input that
                # is safe to reinterpret.
                raise BootstrapError(
                    "staged_attestation_corrupt",
                    "The staged authorship details failed validation.",
                    status=409,
                ) from exc
        try:
            resolved = _resolve_intent_path(store, intent)
        except CoworkPathError as exc:
            raise BootstrapError("invalid_path", str(exc), status=409) from exc
        if resolved.path_key != intent.path_key:
            raise BootstrapError("path_changed", "document path identity changed", status=409)
        if intent.mode != "create":
            if not resolved.path.is_file():
                raise BootstrapError(
                    "source_not_found",
                    "The source file disappeared before import.",
                    status=409,
                )
            source_limit = (
                _importer_for_intent(intent).max_source_bytes
                if intent.mode == "import"
                else MAX_SOURCE_BYTES
            )
            current_bytes = _read_bounded_regular_file(
                resolved.path,
                maximum=source_limit,
                source_label=(
                    "The selected source file"
                    if intent.mode == "import"
                    else "Markdown source"
                ),
            )
            current = sha256_bytes(current_bytes)
            if current != intent.source_sha256:
                raise BootstrapError(
                    "source_changed",
                    (
                        "The selected source file changed before import."
                        if intent.mode == "import"
                        else "The Markdown source changed before repair."
                    ),
                    status=409,
                    details={"current_file_sha256": current},
                )
        elif resolved.path.exists():
            raise BootstrapError("path_conflict", "document path appeared before commit", status=409)

        ydoc_store.write_snapshot(
            store, snapshot=snapshot_bytes, expected_sha256=declared_snapshot
        )
        # Imported files are detached acquisition sources. Retain their exact
        # bytes as a source artifact even when the structured projection was
        # intelligently normalized during conversion.
        if intent.mode == "import" and intent.source_sha256 != declared_projection:
            store._store_blob_bytes(intent.source_sha256, source)
        structured_head = ydoc_store.structured_head_from_segments(snapshot_bytes, ())
        now = _timestamp(_now())
        with store.write_transaction() as conn:
            conn.execute(
                "UPDATE cowork_bootstrap_intents SET state = 'publishing', "
                "snapshot_sha256 = ?, structured_head_sha256 = ?, updated_at = ? "
                "WHERE id = ? AND state = 'prepared'",
                (declared_snapshot, structured_head, now, intent.id),
            )

        created_file = False
        if intent.mode == "create":
            # Re-resolve while holding the operation lock immediately before I/O.
            resolved = resolve_markdown_path(store, intent.normalized_path, for_create=True)
            _publish_create(resolved.path, source)
            created_file = True
            try:
                published_sha256 = sha256_bytes(resolved.path.read_bytes())
            except OSError as exc:
                raise BootstrapError(
                    "create_publication_changed",
                    "The newly created file could not be verified before registration.",
                    status=409,
                ) from exc
            if published_sha256 != intent.source_sha256:
                raise BootstrapError(
                    "create_publication_changed",
                    "The newly created file changed before registration.",
                    status=409,
                    details={"current_file_sha256": published_sha256},
                )

        conn = store.connect()
        committed = False
        import_attestation_record = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            if intent.mode in {"create", "import"}:
                committed_importer = (
                    _importer_for_intent(intent)
                    if intent.mode == "import"
                    else None
                )
                document_meta = (
                    {
                        "source": {
                            "kind": "file_import",
                            "path": intent.normalized_path,
                            "sha256": intent.source_sha256,
                            "writeback_policy": documents.SOURCE_WRITEBACK_NEVER,
                            "importer_id": committed_importer.importer_id,
                            "format": committed_importer.source_format,
                            "media_type": committed_importer.media_type,
                        }
                    }
                    if intent.mode == "import"
                    else None
                )
                record, version, _ = documents.register_ready_document(
                    store,
                    path=intent.normalized_path,
                    title=intent.title,
                    document_class="co_authored",
                    projection_bytes=projection_bytes,
                    ydoc_snapshot_sha256=declared_snapshot,
                    structured_head_sha256=structured_head,
                    actor=actor,
                    mode=intent.mode,
                    document_meta=document_meta,
                    document_id=intent.document_id,
                    conn=conn,
                )
                provision_document_policy(
                    store,
                    document_id=record.id,
                    interaction_contract_id=LEGACY_FULL_COWORK_CONTRACT,
                    initial_activation="enabled",
                    explicit_truth_acknowledged=True,
                    actor=actor,
                    intent_id=f"bootstrap:{intent.id}:truth-policy",
                    conn=conn,
                )
                if intent.mode == "import":
                    assert import_attestation is not None
                    assert committed_importer is not None
                    import_attestation_record = provenance.record_document_attestation(
                        store,
                        document_id=record.id,
                        document_version_id=version.id,
                        attestation=import_attestation,
                        source={
                            "kind": "file_import",
                            "format": committed_importer.source_format,
                            "media_type": committed_importer.media_type,
                            "path": intent.normalized_path,
                            "sha256": intent.source_sha256,
                        },
                        actor=actor,
                        idempotency_key=f"bootstrap:{intent.id}",
                        conn=conn,
                    )
            else:
                record, version, _ = documents.repair_document_snapshot(
                    store,
                    document_id=intent.document_id,
                    projection_bytes=source,
                    ydoc_snapshot_sha256=declared_snapshot,
                    structured_head_sha256=structured_head,
                    actor=actor,
                    conn=conn,
                )
            receipt = {
                "ok": True,
                "mode": intent.mode,
                "document_id": record.id,
                "path": record.path,
                "title": record.title or "",
                "document_class": record.document_class,
                "initialization_state": "ready",
                "snapshot_sha256": declared_snapshot,
                "structured_head_sha256": structured_head,
                "projection_sha256": record.content_sha256,
                "current_file_sha256": record.content_sha256,
                "source_file_sha256": (
                    intent.source_sha256 if intent.mode == "import" else None
                ),
                "source_importer": (
                    committed_importer.descriptor()
                    if committed_importer is not None
                    else None
                ),
                "document_version_id": version.id,
                "authorship_attestation_id": (
                    import_attestation_record.id
                    if import_attestation_record is not None
                    else None
                ),
                "drift_state": "clean",
                "source_writeback": documents.source_writeback_policy(record),
                "permissions": {
                    "open": True,
                    "edit": True,
                    "materialize": not documents.source_is_detached(record),
                    "repair": False,
                    "retire": True,
                },
            }
            receipt_json = canonical_json(receipt)
            cursor = conn.execute(
                "UPDATE cowork_bootstrap_intents SET state = 'committed', "
                "snapshot_sha256 = ?, structured_head_sha256 = ?, updated_at = ?, "
                "committed_at = ?, receipt_json = ?, recovery_detail = NULL "
                "WHERE id = ? AND state = 'publishing'",
                (
                    declared_snapshot,
                    structured_head,
                    now,
                    now,
                    receipt_json,
                    intent.id,
                ),
            )
            if cursor.rowcount != 1:
                raise BootstrapError("bootstrap_state_conflict", "bootstrap state changed", status=409)
            conn.execute("COMMIT")
            committed = True
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if created_file and resolved.path.is_file():
                try:
                    if sha256_bytes(resolved.path.read_bytes()) == intent.source_sha256:
                        resolved.path.unlink()
                except OSError:
                    pass
            with store.write_transaction() as failed_conn:
                failed_conn.execute(
                    "UPDATE cowork_bootstrap_intents SET state = 'failed', "
                    "updated_at = ?, recovery_detail = ? WHERE id = ? "
                    "AND state = 'publishing'",
                    (_timestamp(_now()), "commit_failed", intent.id),
                )
            raise
        finally:
            conn.close()
        if committed:
            _remove_stage(store, intent.id)
            with store.write_transaction() as cleanup_conn:
                cleanup_conn.execute(
                    "UPDATE cowork_bootstrap_intents SET staged_path = NULL "
                    "WHERE id = ? AND state = 'committed'",
                    (intent.id,),
                )
            store._run_on_commit()
        return receipt


def cancel_bootstrap(
    store: TruthStore,
    *,
    bootstrap_id: str,
    actor: Actor,
) -> bool:
    actor_ref = _actor_ref(actor)
    initial_intent = get_intent(store, bootstrap_id)
    with ydoc_store.document_lock(
        store,
        bootstrap_id,
        path_key=initial_intent.path_key,
    ):
        intent = get_intent(store, bootstrap_id)
        if intent.actor_ref != actor_ref:
            raise BootstrapError("bootstrap_not_found", "bootstrap intent does not exist", status=404)
        if intent.state == "committed":
            return False
        if intent.state == "cancelled":
            return True
        if intent.state == "publishing":
            raise BootstrapError("bootstrap_busy", "bootstrap is publishing", status=409)
        with store.write_transaction() as conn:
            conn.execute(
                "UPDATE cowork_bootstrap_intents SET state = 'cancelled', "
                "updated_at = ? WHERE id = ?",
                (_timestamp(_now()), intent.id),
            )
        _remove_stage(store, intent.id)
        return True


def recover_bootstrap_intents(store: TruthStore) -> dict[str, int]:
    """Recover abandoned work only after acquiring its normal operation lock."""

    counts = {"cancelled": 0, "committed": 0, "recovery_required": 0}
    now = _timestamp(_now())
    with store._read_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM cowork_bootstrap_intents WHERE "
            "state = 'publishing' OR "
            "(state = 'prepared' AND expires_at < ?) OR "
            "(state = 'committed' AND (staged_path IS NOT NULL OR "
            "recovery_detail IS NOT NULL)) ORDER BY created_at",
            (now,),
        ).fetchall()
    for raw in rows:
        candidate = _intent_from_row(raw)
        try:
            with ydoc_store.document_lock(
                store,
                candidate.id,
                path_key=candidate.path_key,
                timeout=0.01,
            ):
                intent = get_intent(store, candidate.id)
                if intent.state == "prepared" and intent.expires_at < now:
                    with store.write_transaction() as conn:
                        conn.execute(
                            "UPDATE cowork_bootstrap_intents SET state = 'cancelled', "
                            "updated_at = ? WHERE id = ? AND state = 'prepared'",
                            (now, intent.id),
                        )
                    _remove_stage(store, intent.id)
                    counts["cancelled"] += 1
                    continue
                if intent.state not in {"publishing", "committed"}:
                    continue
                recovered = _recover_bootstrap_locked(store, intent)
                if recovered.state == "committed":
                    counts["committed"] += 1
                elif recovered.state == "failed":
                    counts["recovery_required"] += 1
        except TimeoutError:
            # A live publisher still owns the lock. Never relabel its state.
            continue
        except BootstrapError:
            counts["recovery_required"] += 1
    return counts


__all__ = [
    "BOOTSTRAP_TTL",
    "BootstrapError",
    "BootstrapIntent",
    "MAX_CANONICAL_PROJECTION_BYTES",
    "MAX_SNAPSHOT_BYTES",
    "MAX_SOURCE_BYTES",
    "YDOC_SCHEMA",
    "cancel_bootstrap",
    "commit_bootstrap",
    "get_intent",
    "importer_descriptor",
    "maximum_source_upload_bytes",
    "prepare_bootstrap",
    "read_staged_source",
    "recover_bootstrap_intent",
    "recover_bootstrap_intents",
]

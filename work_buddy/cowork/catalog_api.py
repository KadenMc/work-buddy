"""Read-only Co-work catalog candidate and exact-source HTTP routes."""

from __future__ import annotations

import base64
import os
import threading
import time
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

from work_buddy.cowork.paths import (
    CoworkPathError,
    resolve_markdown_path,
)
from work_buddy.cowork.source_observation import (
    SourceObservationError,
    read_document_source,
)
from work_buddy.truth import documents
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.identity import sha256_bytes


catalog_blueprint = Blueprint("cowork_catalog", __name__)

_SKIP_DIRECTORIES = frozenset(
    {".git", ".wbuddy", "node_modules", ".venv", "__pycache__"}
)
_MAX_QUERY_LENGTH = 200
_MAX_SCAN_ENTRIES = 20_000
_SCAN_CACHE_TTL_SECONDS = 10.0
_SCAN_CACHE_LOCK = threading.Lock()
_SCAN_CACHE: dict[str, tuple[float, tuple[Path, ...], bool]] = {}


def _registry():
    # Reuse the parent HTTP surface seam so tests and configured deployments
    # resolve the same machine registry without importing Flask at module load.
    from work_buddy.cowork.api import _registry as parent_registry

    return parent_registry()


def _error(
    code: str,
    message: str,
    status: int,
    *,
    field: str | None = None,
    retryable: bool = False,
    details: dict | None = None,
):
    payload: dict = {
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    if field is not None:
        payload["field"] = field
    if details:
        payload["details"] = details
    return jsonify({"ok": False, "error": payload}), status


def _store_from_request():
    store_id = str(request.args.get("store_id") or "").strip()
    if not store_id:
        return None, _error(
            "invalid_request", "store_id is required", 400, field="store_id"
        )
    try:
        store = _registry().open_store(store_id)
    except Exception:  # noqa: BLE001 - registry details stay server-side
        return None, _error(
            "folder_unreachable",
            "The selected folder is not reachable by Co-work.",
            503,
            retryable=True,
        )
    if not store.profile.document_surface.enabled:
        return None, _error(
            "document_surface_disabled",
            "Co-work documents are not enabled for this folder.",
            403,
        )
    return store, None


def _decode_cursor(value: str | None) -> int:
    if not value:
        return 0
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii") + b"===")
        offset = int(decoded.decode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("cursor is invalid") from exc
    if offset < 0:
        raise ValueError("cursor is invalid")
    return offset


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii").rstrip("=")


def _scan_candidate_paths(root: Path) -> tuple[tuple[Path, ...], bool]:
    found: list[Path] = []
    visited = 0
    truncated = False
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        kept: list[str] = []
        for name in directory_names:
            candidate = current_path / name
            if name.casefold() in _SKIP_DIRECTORIES:
                continue
            if candidate.is_symlink():
                continue
            kept.append(name)
        directory_names[:] = kept
        for name in file_names:
            visited += 1
            if visited > _MAX_SCAN_ENTRIES:
                truncated = True
                break
            candidate = current_path / name
            if candidate.is_symlink() or candidate.suffix.casefold() not in {".md", ".markdown"}:
                continue
            found.append(candidate)
        if truncated:
            break
    found.sort(key=lambda item: item.relative_to(root).as_posix().casefold())
    return tuple(found), truncated


def _candidate_paths(root: Path) -> tuple[tuple[Path, ...], bool]:
    """Return one bounded scan, shared briefly across picker keystrokes.

    The picker issues a new filtered query as the user types. Rewalking a large
    folder for every keypress makes the debounce cosmetic, so the machine-local
    catalog reuses an immutable bounded result for a short window. Registration
    membership and path containment are still re-evaluated on every response.
    """

    key = os.path.normcase(str(root.resolve()))
    now = time.monotonic()
    with _SCAN_CACHE_LOCK:
        cached = _SCAN_CACHE.get(key)
        if cached is not None and now - cached[0] < _SCAN_CACHE_TTL_SECONDS:
            return cached[1], cached[2]
    paths, truncated = _scan_candidate_paths(root)
    with _SCAN_CACHE_LOCK:
        _SCAN_CACHE[key] = (now, paths, truncated)
        # The dashboard normally has only a handful of open Folders. Bound the
        # process cache defensively if tests or automation churn through many.
        if len(_SCAN_CACHE) > 32:
            oldest = min(_SCAN_CACHE, key=lambda item: _SCAN_CACHE[item][0])
            if oldest != key:
                _SCAN_CACHE.pop(oldest, None)
    return paths, truncated


@catalog_blueprint.get("/api/truth/doc/candidates")
def api_doc_candidates():
    store, failure = _store_from_request()
    if failure:
        return failure
    query = str(request.args.get("query") or "").strip().casefold()
    if len(query) > _MAX_QUERY_LENGTH:
        return _error(
            "invalid_request",
            f"query cannot exceed {_MAX_QUERY_LENGTH} characters",
            400,
            field="query",
        )
    try:
        limit = int(request.args.get("limit") or 25)
        cursor = _decode_cursor(request.args.get("cursor"))
    except ValueError as exc:
        return _error("invalid_request", str(exc), 400, field="cursor")
    if not 1 <= limit <= 50:
        return _error("invalid_request", "limit must be between 1 and 50", 400, field="limit")

    with store._read_connection() as conn:
        registered = {
            row["path_key"]
            for row in conn.execute("SELECT path_key FROM document_path_keys").fetchall()
        }
    paths, truncated = _candidate_paths(store.paths.root)
    entries: list[dict] = []
    for candidate in paths:
        relative = candidate.relative_to(store.paths.root).as_posix()
        try:
            resolved = resolve_markdown_path(store, relative)
            stat_result = resolved.path.stat()
        except (CoworkPathError, OSError):
            continue
        if resolved.path_key in registered:
            continue
        if query and query not in relative.casefold() and query not in candidate.stem.casefold():
            continue
        entries.append(
            {
                "path": resolved.normalized,
                "title": candidate.stem,
                "byte_size": stat_result.st_size,
                "mtime": stat_result.st_mtime,
                "already_registered": False,
            }
        )

    page = entries[cursor : cursor + limit]
    next_offset = cursor + len(page)
    next_cursor = _encode_cursor(next_offset) if next_offset < len(entries) else None
    return jsonify(
        {
            "ok": True,
            "candidates": page,
            "next_cursor": next_cursor,
            "scan_truncated": truncated,
        }
    )


def _source_headers(data: bytes) -> dict[str, str]:
    digest = sha256_bytes(data)
    has_bom = data.startswith(b"\xef\xbb\xbf")
    try:
        (data[3:] if has_bom else data).decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        encoding = "binary"
    return {
        "ETag": f'"{digest}"',
        "X-WB-Source-Sha256": digest,
        "X-WB-Source-Byte-Length": str(len(data)),
        "X-WB-Source-Encoding": encoding,
        "X-WB-Source-BOM": "utf-8" if has_bom else "none",
    }


@catalog_blueprint.get("/api/truth/doc/<document_id>/source")
def api_doc_source(document_id: str):
    store, failure = _store_from_request()
    if failure:
        return failure
    try:
        document = documents.get_document(store, document_id)
    except InvariantViolation:
        return _error("document_not_found", "Document does not exist.", 404)
    version = str(request.args.get("version") or "current").strip()
    if version not in {"current", "materialized"}:
        return _error(
            "invalid_request",
            "version must be current or materialized",
            400,
            field="version",
        )

    if version == "materialized":
        target = store.resolve_blob_path(f"blobs/{document.content_sha256}")
        if not target.is_file():
            return _error(
                "baseline_unavailable",
                "The retained materialized baseline is unavailable.",
                404,
            )
        try:
            data = target.read_bytes()
        except OSError:
            return _error(
                "folder_unreachable",
                "Co-work could not read that source file.",
                503,
                retryable=True,
            )
    else:
        try:
            source = read_document_source(store, document)
        except SourceObservationError as exc:
            return _error(
                exc.code,
                str(exc),
                exc.status,
                retryable=exc.retryable,
                details=exc.details,
            )
        assert source.data is not None
        data = source.data
    if version == "materialized" and sha256_bytes(data) != document.content_sha256:
        return _error("corrupt_document", "Materialized baseline failed its digest check.", 422)
    response = Response(data, mimetype="application/octet-stream")
    for name, value in _source_headers(data).items():
        response.headers[name] = value
    return response


def register_catalog_routes(app):
    app.register_blueprint(catalog_blueprint)
    return app


__all__ = ["catalog_blueprint", "register_catalog_routes"]

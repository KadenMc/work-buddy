"""Standalone Flask blueprint for Co-work Folder discovery and setup."""

from __future__ import annotations

import json
import os
import time
import uuid
from itertools import islice
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from flask import Blueprint, jsonify, request

from work_buddy.cowork.project_store import (
    DEFAULT_TOKEN_TTL_SECONDS,
    FolderLifecycleError,
    ProjectStoreManager,
    folder_summary,
)
from work_buddy.cowork.native_folder_chooser import (
    NativeFolderChooserError,
    default_host_folder_chooser,
)
from work_buddy.truth.registry import TruthStoreRegistry


class HostFolderChooser(Protocol):
    """Select a directory on the machine hosting Work Buddy, or cancel."""

    def __call__(self) -> str | Path | None: ...


class FolderAccessPolicy:
    """Admission policy for server-host paths supplied by trusted API callers."""

    def __init__(self, allowed_roots: tuple[str | Path, ...] = ()) -> None:
        self.allowed_roots = tuple(
            Path(item).expanduser().resolve() for item in allowed_roots
        )

    def admit(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise FolderLifecycleError(
                "invalid_path", "Folder paths must be absolute host paths.", status=400
            )
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise FolderLifecycleError(
                "folder_not_found", "The selected Folder does not exist.", status=404
            ) from exc
        if not resolved.is_dir():
            raise FolderLifecycleError(
                "folder_not_found", "The selected path is not a Folder.", status=404
            )
        if self.allowed_roots:
            admitted = False
            for root in self.allowed_roots:
                try:
                    admitted = Path(os.path.commonpath((str(root), str(resolved)))) == root
                except ValueError:
                    admitted = False
                if admitted:
                    break
            if not admitted:
                raise FolderLifecycleError(
                    "folder_disallowed",
                    "That host Folder is outside the configured allowed roots.",
                    status=403,
                )
        return resolved


class FolderTokenStore:
    """Short-lived opaque server-side tokens; no host path enters a URL."""

    def __init__(self, root: str | Path, *, ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS) -> None:
        self.root = Path(root).expanduser().resolve()
        self.ttl_seconds = max(30, int(ttl_seconds))

    def issue(self, kind: str, data: Mapping[str, Any]) -> str:
        self._prune_expired()
        token = uuid.uuid4().hex
        body = {
            "kind": kind,
            "expires_at": time.time() + self.ttl_seconds,
            "data": dict(data),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{token}.json"
        fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, json.dumps(body, separators=(",", ":")).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return token

    def _prune_expired(self) -> None:
        """Bound retention of host paths carried by abandoned opaque tokens."""

        if not self.root.is_dir():
            return
        now = time.time()
        try:
            candidates = tuple(islice(self.root.glob("*.json"), 256))
        except OSError:
            return
        for path in candidates:
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
                expired = float(body.get("expires_at") or 0) <= now
            except (OSError, ValueError, TypeError):
                expired = True
            if expired:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def resolve(self, token: Any, *, kind: str) -> Mapping[str, Any]:
        if not isinstance(token, str) or len(token) != 32:
            raise FolderLifecycleError(
                "invalid_request", f"A valid {kind} token is required.", status=400
            )
        try:
            uuid.UUID(hex=token)
            path = self.root / f"{token}.json"
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise FolderLifecycleError(
                "selection_expired",
                "The Folder selection expired; open the Folder again.",
                status=409,
                retryable=True,
            ) from exc
        if body.get("kind") != kind:
            raise FolderLifecycleError(
                "invalid_request", "The Folder token is for a different action.", status=400
            )
        if float(body.get("expires_at") or 0) <= time.time():
            path.unlink(missing_ok=True)
            raise FolderLifecycleError(
                "selection_expired",
                "The Folder selection expired; open the Folder again.",
                status=409,
                retryable=True,
            )
        data = body.get("data")
        if not isinstance(data, Mapping):
            raise FolderLifecycleError(
                "invalid_request", "The Folder token is malformed.", status=400
            )
        return data


def _dashboard_read_only() -> bool:
    try:
        from work_buddy.config import load_config

        return bool(load_config().get("dashboard", {}).get("read_only", False))
    except Exception:
        return False


def _default_allowed_roots() -> tuple[str | Path, ...]:
    try:
        from work_buddy.config import load_config

        dashboard = load_config().get("dashboard", {}) or {}
        roots = dashboard.get("cowork_allowed_roots") or []
        return tuple(roots) if isinstance(roots, list) else ()
    except Exception:
        return ()


def _error(exc: FolderLifecycleError):
    error: dict[str, Any] = {
        "code": exc.code,
        "message": str(exc),
        "retryable": exc.retryable,
    }
    if exc.details:
        error["details"] = exc.details
    return jsonify({"ok": False, "error": error}), exc.status


def _body() -> Mapping[str, Any]:
    value = request.get_json(silent=True)
    if not isinstance(value, Mapping):
        raise FolderLifecycleError(
            "invalid_request", "The request body must be a JSON object.", status=400
        )
    return value


def create_folder_blueprint(
    *,
    manager: ProjectStoreManager | None = None,
    registry_factory: Callable[[], TruthStoreRegistry] = TruthStoreRegistry,
    chooser: HostFolderChooser | None = None,
    access_policy: FolderAccessPolicy | None = None,
    read_only: Callable[[], bool] = _dashboard_read_only,
) -> Blueprint:
    """Build the Folder blueprint with injectable host and persistence seams."""

    manager = manager or ProjectStoreManager()
    access_policy = access_policy or FolderAccessPolicy(_default_allowed_roots())
    tokens = FolderTokenStore(
        manager.data_root / "runtime" / "cowork-folder-tokens"
    )
    blueprint = Blueprint(f"cowork_folders_{uuid.uuid4().hex}", __name__)

    @blueprint.get("/api/truth/cowork/folders")
    def folders_list():
        include_ineligible = request.args.get("include_ineligible", "0") == "1"
        registry = registry_factory()
        rows = registry.list_stores(refresh=False)
        summaries = [folder_summary(row, read_only=read_only()) for row in rows]
        diagnostics = [
            {
                "store_id": item["store_id"],
                "folder_name": item["folder_name"],
                "folder_path": item["folder_path"],
                "reason_code": item["ineligible_reason"],
            }
            for item in summaries
            if item["eligibility"] != "eligible"
        ]
        if not include_ineligible:
            summaries = [item for item in summaries if item["eligibility"] == "eligible"]
        return jsonify(
            {
                "ok": True,
                "read_only": read_only(),
                "chooser": {
                    "available": chooser is not None,
                    "kind": "host_native" if chooser is not None else "unavailable",
                },
                "folders": summaries,
                "diagnostics": diagnostics,
            }
        )

    @blueprint.post("/api/truth/cowork/folders/choose")
    def folders_choose():
        if chooser is None:
            return _error(
                FolderLifecycleError(
                    "folder_chooser_unavailable",
                    "Folder selection is unavailable here.",
                    status=501,
                )
            )
        try:
            selected = chooser()
            if selected is None:
                return jsonify({"ok": True, "cancelled": True})
            folder = access_policy.admit(selected)
            token = tokens.issue("selection", {"folder_path": str(folder)})
            return jsonify(
                {
                    "ok": True,
                    "cancelled": False,
                    "folder_name": folder.name,
                    "folder_path": str(folder),
                    "selection_token": token,
                }
            )
        except NativeFolderChooserError as exc:
            return _error(
                FolderLifecycleError(
                    "folder_chooser_failed",
                    str(exc),
                    status=409,
                    retryable=True,
                )
            )
        except FolderLifecycleError as exc:
            return _error(exc)

    @blueprint.post("/api/truth/cowork/folders/inspect")
    def folders_inspect():
        try:
            body = _body()
            internal_continuation: str | None = None
            supplied = sum(
                key in body
                for key in ("selection_token", "folder_path", "continuation_token")
            )
            if supplied != 1:
                raise FolderLifecycleError(
                    "invalid_request",
                    "Provide exactly one Folder selection, path, or continuation token.",
                    status=400,
                )
            if "selection_token" in body:
                data = tokens.resolve(body.get("selection_token"), kind="selection")
                folder = access_policy.admit(str(data["folder_path"]))
            elif "continuation_token" in body:
                data = tokens.resolve(body.get("continuation_token"), kind="continuation")
                folder = access_policy.admit(str(data["folder_path"]))
                internal_continuation = str(data["scan_token"])
            else:
                folder = access_policy.admit(str(body.get("folder_path") or ""))

            inspected = manager.inspect(
                folder,
                continuation_token=internal_continuation,
            )
            result = inspected.to_dict()
            if inspected.status == "initialized" and inspected.store_id:
                row = registry_factory().get_by_store_id(
                    inspected.store_id,
                    refresh=False,
                )
                if row is not None:
                    result["folder"] = folder_summary(row, read_only=read_only())
            if inspected.owner_store_id:
                owner_row = registry_factory().get_by_store_id(
                    inspected.owner_store_id,
                    refresh=False,
                )
                if owner_row is not None:
                    result["owner"] = folder_summary(
                        owner_row,
                        read_only=read_only(),
                    )
            if inspected.status == "inspection_pending":
                assert inspected.continuation_token is not None
                result["continuation_token"] = tokens.issue(
                    "continuation",
                    {
                        "folder_path": str(folder),
                        "scan_token": inspected.continuation_token,
                    },
                )
            elif inspected.status in {
                "initialized",
                "uninitialized",
            }:
                result["inspection_token"] = tokens.issue(
                    "inspection",
                    {
                        "folder_path": str(folder),
                        "status": inspected.status,
                        "fingerprint": inspected.fingerprint,
                    },
                )
            # Fingerprints are server preconditions carried only by the opaque
            # token, never a public filesystem contract.
            result.pop("fingerprint", None)
            return jsonify({"ok": True, **result})
        except FolderLifecycleError as exc:
            return _error(exc)

    @blueprint.post("/api/truth/cowork/folders/open")
    def folders_open():
        try:
            body = _body()
            data = tokens.resolve(body.get("inspection_token"), kind="inspection")
            if data.get("status") != "initialized":
                raise FolderLifecycleError(
                    "invalid_request",
                    "The Folder token does not authorize opening an initialized Folder.",
                    status=400,
                )
            folder = access_policy.admit(str(data["folder_path"]))
            row = manager.open_initialized(
                folder,
                registry=registry_factory(),
                inspection_fingerprint=str(data.get("fingerprint") or ""),
            )
            return jsonify(
                {
                    "ok": True,
                    "folder": folder_summary(row, read_only=read_only()),
                }
            )
        except FolderLifecycleError as exc:
            return _error(exc)

    def _initialize():
        if read_only():
            return _error(
                FolderLifecycleError(
                    "dashboard_read_only",
                    "The dashboard is in read-only mode.",
                    status=403,
                )
            )
        try:
            body = _body()
            data = tokens.resolve(body.get("inspection_token"), kind="inspection")
            if data.get("status") != "uninitialized":
                raise FolderLifecycleError(
                    "invalid_request", "The Folder token does not authorize this action.",
                    status=400,
                )
            key = body.get("idempotency_key")
            if not isinstance(key, str):
                raise FolderLifecycleError(
                    "invalid_request", "idempotency_key is required.", status=400
                )
            folder = access_policy.admit(str(data["folder_path"]))
            registry = registry_factory()
            store = manager.initialize(
                folder,
                registry=registry,
                inspection_fingerprint=str(data.get("fingerprint") or ""),
                idempotency_key=key,
            )
            row = registry.get_by_store_id(store.store_id, refresh=False)
            if row is None:
                raise FolderLifecycleError(
                    "recovery_in_progress",
                    "The Folder was set up, but Co-work is still finishing recovery.",
                    status=503,
                    retryable=True,
                )
            return jsonify({"ok": True, "folder": folder_summary(row, read_only=False)})
        except FolderLifecycleError as exc:
            return _error(exc)

    @blueprint.post("/api/truth/cowork/folders/initialize")
    def folders_initialize():
        return _initialize()

    return blueprint


cowork_folder_blueprint = create_folder_blueprint(
    chooser=default_host_folder_chooser()
)


__all__ = [
    "FolderAccessPolicy",
    "FolderTokenStore",
    "HostFolderChooser",
    "cowork_folder_blueprint",
    "create_folder_blueprint",
]

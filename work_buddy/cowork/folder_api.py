"""Standalone Flask blueprint for Co-work folder discovery and setup."""

from __future__ import annotations

import json
import logging
import os
import stat
import time
import uuid
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request

from work_buddy.cowork.paths import CoworkPathError, resolve_markdown_path
from work_buddy.cowork.project_store import (
    DEFAULT_TOKEN_TTL_SECONDS,
    FolderLifecycleError,
    ProjectStoreManager,
    folder_summary,
)
from work_buddy.cowork.native_folder_chooser import (
    NativeFolderChooserError,
    default_host_folder_chooser,
    default_host_location_chooser,
    default_host_markdown_chooser,
)
from work_buddy.truth.contracts import StorePaths
from work_buddy.truth.registry import TruthStoreRegistry


logger = logging.getLogger(__name__)

PICKER_INTENT_HEADER = "X-Work-Buddy-Intent"
PICKER_INTENT_VALUE = "cowork-folder-picker"
MARKDOWN_PICKER_INTENT_VALUE = "cowork-markdown-picker"
LOCATION_PICKER_INTENT_VALUE = "cowork-location-picker"
MAX_FOLDER_PATH_CHARS = 32_767
MAX_STORE_ID_CHARS = 200
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
_MANAGED_COMPONENT = ".wbuddy"


class HostFolderChooser(Protocol):
    """Select a directory on the machine hosting Work Buddy, or cancel."""

    def __call__(self) -> str | Path | None: ...


class HostScopedPathChooser(Protocol):
    """Select a path from a picker rooted at an active Co-work folder."""

    def __call__(self, start_directory: str | Path) -> str | Path | None: ...


class FolderAccessPolicy:
    """Admission policy for server-host paths supplied by trusted API callers."""

    def __init__(self, allowed_roots: tuple[str | Path, ...] = ()) -> None:
        self.allowed_roots = tuple(
            Path(item).expanduser().resolve() for item in allowed_roots
        )

    def admit(self, value: str | Path) -> Path:
        try:
            raw_path = os.fspath(value)
            if (
                not isinstance(raw_path, str)
                or "\x00" in raw_path
                or len(raw_path) > MAX_FOLDER_PATH_CHARS
            ):
                raise ValueError("invalid folder path")
            path = Path(raw_path).expanduser()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise FolderLifecycleError(
                "invalid_path", "The selected folder path is invalid.", status=400
            ) from exc
        if not path.is_absolute():
            raise FolderLifecycleError(
                "invalid_path", "The folder path must be an absolute host path.", status=400
            )
        try:
            resolved = path.resolve(strict=True)
        except ValueError as exc:
            raise FolderLifecycleError(
                "invalid_path", "The selected folder path is invalid.", status=400
            ) from exc
        except (OSError, RuntimeError) as exc:
            raise FolderLifecycleError(
                "folder_not_found", "The selected folder does not exist.", status=404
            ) from exc
        if not resolved.is_dir():
            raise FolderLifecycleError(
                "folder_not_found", "The selected path is not a folder.", status=404
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
                    "That host folder is outside the configured allowed roots.",
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
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.root.chmod(0o700)
        target = self.root / f"{token}.json"
        fd = os.open(
            str(target),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
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
            candidates = tuple(self.root.glob("*.json"))
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
                "The folder selection expired; open the folder again.",
                status=409,
                retryable=True,
            ) from exc
        if body.get("kind") != kind:
            raise FolderLifecycleError(
                "invalid_request", "The folder token is for a different action.", status=400
            )
        if float(body.get("expires_at") or 0) <= time.time():
            path.unlink(missing_ok=True)
            raise FolderLifecycleError(
                "selection_expired",
                "The folder selection expired; open the folder again.",
                status=409,
                retryable=True,
            )
        data = body.get("data")
        if not isinstance(data, Mapping):
            raise FolderLifecycleError(
                "invalid_request", "The folder token is malformed.", status=400
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


def _is_direct_loopback_request() -> bool:
    """Reject remote and reverse-proxied requests before opening host UI."""

    proxy_markers = (
        "forwarded",
        "via",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
    )
    proxied = any(request.headers.get(name) for name in proxy_markers) or any(
        name.lower().startswith("tailscale-") for name in request.headers.keys()
    )
    try:
        peer_is_loopback = ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        peer_is_loopback = False
    try:
        hostname = urlsplit(f"//{request.host}").hostname
        normalized_host = "" if hostname is None else hostname.rstrip(".").lower()
        host_is_loopback = normalized_host == "localhost" or ip_address(
            normalized_host
        ).is_loopback
    except ValueError:
        host_is_loopback = False
    return peer_is_loopback and host_is_loopback and not proxied


def _has_local_picker_intent(expected_value: str = PICKER_INTENT_VALUE) -> bool:
    """Require a same-origin browser action before opening host UI.

    The custom header makes an ordinary cross-origin request non-simple, so a
    hostile page cannot submit it without a successful CORS preflight.  The
    browser provenance checks fail closed as defense in depth if CORS policy is
    ever broadened elsewhere.
    """

    if not _is_direct_loopback_request():
        return False
    if request.headers.get(PICKER_INTENT_HEADER) != expected_value:
        return False
    fetch_site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return False
    origin = request.headers.get("Origin")
    if (
        origin
        and origin.rstrip("/").lower() != request.host_url.rstrip("/").lower()
    ):
        return False
    return True


def _active_store_root(
    body: Mapping[str, Any],
    *,
    registry_factory: Callable[[], TruthStoreRegistry],
) -> Path:
    store_id = body.get("store_id")
    if (
        not isinstance(store_id, str)
        or not store_id
        or store_id != store_id.strip()
        or len(store_id) > MAX_STORE_ID_CHARS
    ):
        raise FolderLifecycleError(
            "invalid_request",
            "Choose an active folder before opening this picker.",
            status=400,
        )
    try:
        row = registry_factory().get_by_store_id(store_id, refresh=True)
        if row is None or not row.reachable:
            raise LookupError("store is not reachable")
        if not row.document_surface_enabled:
            raise FolderLifecycleError(
                "document_surface_disabled",
                "Co-work documents are not enabled for this folder.",
                status=403,
            )
        root = StorePaths.from_sidecar(row.path).root.resolve(strict=True)
        if not root.is_dir():
            raise LookupError("The folder root is not a directory")
        return root
    except FolderLifecycleError:
        raise
    except Exception as exc:
        raise FolderLifecycleError(
            "folder_unreachable",
            "The selected folder is no longer available.",
            status=503,
            retryable=True,
        ) from exc


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def _contained_picker_selection(
    root: Path,
    selected: str | Path,
    *,
    outside_code: str,
    unavailable_code: str,
    invalid_code: str,
    managed_code: str,
    item_label: str,
) -> tuple[Path, str]:
    """Resolve an existing selection without following aliases out of root."""

    try:
        raw = os.fspath(selected)
    except TypeError as exc:
        raise FolderLifecycleError(
            invalid_code,
            f"That {item_label} can’t be used by Co-work.",
            status=422,
        ) from exc
    if (
        not isinstance(raw, str)
        or not raw
        or "\x00" in raw
        or len(raw) > MAX_FOLDER_PATH_CHARS
    ):
        raise FolderLifecycleError(
            invalid_code,
            f"That {item_label} can’t be used by Co-work.",
            status=422,
        )
    raw_path = Path(raw).expanduser()
    if not raw_path.is_absolute():
        raise FolderLifecycleError(
            outside_code,
            f"Choose {item_label} inside the active folder.",
            status=422,
        )
    lexical = Path(os.path.abspath(str(raw_path)))
    try:
        common = os.path.commonpath((str(root), str(lexical)))
    except ValueError as exc:
        raise FolderLifecycleError(
            outside_code,
            f"Choose {item_label} inside the active folder.",
            status=422,
        ) from exc
    if os.path.normcase(common) != os.path.normcase(str(root)):
        raise FolderLifecycleError(
            outside_code,
            f"Choose {item_label} inside the active folder.",
            status=422,
        )
    relative_text = os.path.relpath(str(lexical), str(root))
    parts = () if relative_text == "." else Path(relative_text).parts
    if any(part.casefold() == _MANAGED_COMPONENT for part in parts):
        raise FolderLifecycleError(
            managed_code,
            "Work Buddy support files can’t be selected here.",
            status=422,
        )

    cursor = root
    for part in parts:
        cursor /= part
        try:
            exists = cursor.exists() or cursor.is_symlink()
        except OSError as exc:
            raise FolderLifecycleError(
                unavailable_code,
                f"That {item_label} is no longer available.",
                status=409,
                retryable=True,
            ) from exc
        if not exists:
            raise FolderLifecycleError(
                unavailable_code,
                f"That {item_label} is no longer available.",
                status=409,
                retryable=True,
            )
        try:
            redirected = _is_reparse_or_symlink(cursor)
        except OSError as exc:
            raise FolderLifecycleError(
                unavailable_code,
                f"That {item_label} is no longer available.",
                status=409,
                retryable=True,
            ) from exc
        if redirected:
            raise FolderLifecycleError(
                invalid_code,
                f"That {item_label} can’t be used by Co-work.",
                status=422,
            )

    try:
        resolved = lexical.resolve(strict=True)
        resolved_common = os.path.commonpath((str(root), str(resolved)))
    except (OSError, RuntimeError, ValueError) as exc:
        raise FolderLifecycleError(
            unavailable_code,
            f"That {item_label} is no longer available.",
            status=409,
            retryable=True,
        ) from exc
    if os.path.normcase(resolved_common) != os.path.normcase(str(root)):
        raise FolderLifecycleError(
            outside_code,
            f"Choose {item_label} inside the active folder.",
            status=422,
        )
    canonical_relative_text = os.path.relpath(str(resolved), str(root))
    canonical_parts = (
        ()
        if canonical_relative_text == "."
        else Path(canonical_relative_text).parts
    )
    # Windows short-name aliases (for example ``WBUDDY~1``) can lexically
    # conceal the managed ``.wbuddy`` directory.  Derive the public relative
    # path from the resolved filesystem identity and enforce the namespace
    # boundary again before returning it.
    if any(part.casefold() == _MANAGED_COMPONENT for part in canonical_parts):
        raise FolderLifecycleError(
            managed_code,
            "Work Buddy support files can’t be selected here.",
            status=422,
        )
    return resolved, "/".join(canonical_parts)


def _admit_markdown_selection(root: Path, selected: str | Path) -> str:
    resolved, relative = _contained_picker_selection(
        root,
        selected,
        outside_code="markdown_outside_folder",
        unavailable_code="markdown_file_unavailable",
        invalid_code="invalid_markdown_file",
        managed_code="invalid_markdown_file",
        item_label="a Markdown file",
    )
    if not relative or resolved.suffix.casefold() not in _MARKDOWN_SUFFIXES:
        raise FolderLifecycleError(
            "invalid_markdown_file",
            "Choose a Markdown file ending in .md or .markdown.",
            status=422,
        )
    if not resolved.is_file():
        raise FolderLifecycleError(
            "markdown_file_unavailable",
            "That Markdown file is no longer available.",
            status=409,
            retryable=True,
        )
    try:
        admitted = resolve_markdown_path(root, relative)
    except CoworkPathError as exc:
        raise FolderLifecycleError(
            "invalid_markdown_file",
            "That Markdown file can’t be used by Co-work.",
            status=422,
        ) from exc
    if not admitted.path.is_file():
        raise FolderLifecycleError(
            "markdown_file_unavailable",
            "That Markdown file is no longer available.",
            status=409,
            retryable=True,
        )
    return admitted.normalized


def _admit_location_selection(root: Path, selected: str | Path) -> str:
    resolved, relative = _contained_picker_selection(
        root,
        selected,
        outside_code="location_outside_folder",
        unavailable_code="location_unavailable",
        invalid_code="location_unavailable",
        managed_code="managed_location",
        item_label="a location",
    )
    if not resolved.is_dir():
        raise FolderLifecycleError(
            "location_unavailable",
            "That location is no longer available.",
            status=409,
            retryable=True,
        )
    probe = (
        f"{relative}/work-buddy-location.md"
        if relative
        else "work-buddy-location.md"
    )
    try:
        resolve_markdown_path(root, probe, for_create=True)
    except CoworkPathError as exc:
        raise FolderLifecycleError(
            "location_unavailable",
            "That location can’t be used by Co-work.",
            status=422,
        ) from exc
    return relative


def create_folder_blueprint(
    *,
    manager: ProjectStoreManager | None = None,
    registry_factory: Callable[[], TruthStoreRegistry] = TruthStoreRegistry,
    chooser: HostFolderChooser | None = None,
    markdown_chooser: HostScopedPathChooser | None = None,
    location_chooser: HostScopedPathChooser | None = None,
    access_policy: FolderAccessPolicy | None = None,
    read_only: Callable[[], bool] = _dashboard_read_only,
) -> Blueprint:
    """Build the folder blueprint with injectable host and persistence seams."""

    manager = manager or ProjectStoreManager()
    access_policy = access_policy or FolderAccessPolicy(_default_allowed_roots())
    tokens = FolderTokenStore(
        manager.data_root / "runtime" / "cowork-folder-tokens"
    )
    blueprint = Blueprint(f"cowork_folders_{uuid.uuid4().hex}", __name__)

    def native_picker_error(exc: NativeFolderChooserError, *, label: str):
        details = None
        if exc.code == "folder_chooser_busy":
            logger.info("Co-work %s picker request ignored because it is busy", label)
        else:
            trace_id = uuid.uuid4().hex[:12]
            diagnostic = " ".join(str(exc.diagnostic).split())[:1000]
            logger.warning(
                "Co-work %s picker failed trace_id=%s code=%s diagnostic=%s",
                label,
                trace_id,
                exc.code,
                diagnostic,
            )
            details = {"trace_id": trace_id}
        return _error(
            FolderLifecycleError(
                exc.code,
                str(exc),
                status=exc.status,
                retryable=exc.retryable,
                details=details,
            )
        )

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
                    "markdown_available": markdown_chooser is not None,
                    "location_available": location_chooser is not None,
                },
                "folders": summaries,
                "diagnostics": diagnostics,
            }
        )

    @blueprint.post("/api/truth/cowork/folders/choose")
    def folders_choose():
        if not _has_local_picker_intent():
            return _error(
                FolderLifecycleError(
                    "folder_picker_intent_required",
                    "Choosing a folder must be started from Co-work.",
                    status=403,
                )
            )
        if chooser is None:
            return _error(
                FolderLifecycleError(
                    "folder_chooser_unavailable",
                    "Choosing a folder is unavailable here.",
                    status=503,
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
            return native_picker_error(exc, label="folder")
        except FolderLifecycleError as exc:
            return _error(exc)

    @blueprint.post("/api/truth/cowork/files/choose-markdown")
    def files_choose_markdown():
        if not _has_local_picker_intent(MARKDOWN_PICKER_INTENT_VALUE):
            return _error(
                FolderLifecycleError(
                    "folder_picker_intent_required",
                    "Markdown selection must be started from Co-work.",
                    status=403,
                )
            )
        if markdown_chooser is None:
            return _error(
                FolderLifecycleError(
                    "folder_chooser_unavailable",
                    "Markdown selection is unavailable here.",
                    status=503,
                )
            )
        try:
            root = _active_store_root(
                _body(),
                registry_factory=registry_factory,
            )
            selected = markdown_chooser(root)
            if selected is None:
                return jsonify({"ok": True, "cancelled": True})
            relative = _admit_markdown_selection(root, selected)
            return jsonify(
                {
                    "ok": True,
                    "cancelled": False,
                    "path": relative,
                }
            )
        except NativeFolderChooserError as exc:
            return native_picker_error(exc, label="Markdown")
        except FolderLifecycleError as exc:
            return _error(exc)

    @blueprint.post("/api/truth/cowork/folders/choose-location")
    def folders_choose_location():
        if not _has_local_picker_intent(LOCATION_PICKER_INTENT_VALUE):
            return _error(
                FolderLifecycleError(
                    "folder_picker_intent_required",
                    "Location selection must be started from Co-work.",
                    status=403,
                )
            )
        if location_chooser is None:
            return _error(
                FolderLifecycleError(
                    "folder_chooser_unavailable",
                    "Location selection is unavailable here.",
                    status=503,
                )
            )
        try:
            root = _active_store_root(
                _body(),
                registry_factory=registry_factory,
            )
            selected = location_chooser(root)
            if selected is None:
                return jsonify({"ok": True, "cancelled": True})
            relative = _admit_location_selection(root, selected)
            return jsonify(
                {
                    "ok": True,
                    "cancelled": False,
                    "path": relative,
                }
            )
        except NativeFolderChooserError as exc:
            return native_picker_error(exc, label="Location")
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
                    "Provide exactly one folder selection, path, or continuation token.",
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
                    "The folder token does not authorize opening an initialized folder.",
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
                    "invalid_request", "The folder token does not authorize this action.",
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
                    "The folder was set up, but Co-work is still finishing recovery.",
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
    chooser=default_host_folder_chooser(),
    markdown_chooser=default_host_markdown_chooser(),
    location_chooser=default_host_location_chooser(),
)


__all__ = [
    "FolderAccessPolicy",
    "FolderTokenStore",
    "HostFolderChooser",
    "HostScopedPathChooser",
    "LOCATION_PICKER_INTENT_VALUE",
    "MARKDOWN_PICKER_INTENT_VALUE",
    "PICKER_INTENT_HEADER",
    "PICKER_INTENT_VALUE",
    "cowork_folder_blueprint",
    "create_folder_blueprint",
]

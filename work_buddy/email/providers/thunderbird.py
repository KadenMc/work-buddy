"""Thunderbird-backed email provider.

Talks HTTP to the ``thunderbird-work-buddy`` extension. The bridge writes a
connection file at ``<tmpdir>/thunderbird-work-buddy/connection.json`` with
the per-startup port and bearer token; we discover it on demand.

Failure model: methods raise typed :class:`EmailError` subclasses. The
``ToolProbe`` registered for ``thunderbird`` short-circuits capability
dispatch when the bridge is closed, so ordinary callers only see
``EmailBridgeUnreachable`` on transient races (Thunderbird restarted between
probe and call).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from work_buddy.email.errors import (
    EmailBridgeUnauthorized,
    EmailBridgeUnreachable,
    EmailMessageNotFound,
    EmailProviderError,
)
from work_buddy.email.models import (
    EmailFolder,
    EmailMessage,
    EmailMessageHandle,
    EmailSummary,
    stable_key_for,
)

log = logging.getLogger(__name__)

CONNECTION_DIR_NAME = "thunderbird-work-buddy"
CONNECTION_FILE = "connection.json"
DEFAULT_TIMEOUT_SECONDS = 10
PROTOCOL_VERSION_MIN = "0.1.0"


def connection_file_path() -> Path:
    """Where the extension writes its connection file."""
    return Path(tempfile.gettempdir()) / CONNECTION_DIR_NAME / CONNECTION_FILE


def discover_connection() -> dict:
    """Read & validate the connection file. Raises typed errors on failure.

    Returns a dict with ``port``, ``token``, ``plugin``, ``version``,
    ``profile_dir`` so callers can do additional sanity checks (e.g.
    profile-dir match).
    """
    p = connection_file_path()
    if not p.exists():
        raise EmailBridgeUnreachable(
            f"connection file not found: {p}. Is Thunderbird running with the "
            "thunderbird-work-buddy extension installed?"
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EmailBridgeUnreachable(f"connection file unreadable: {exc}") from exc
    for k in ("plugin", "version", "port", "token"):
        if k not in data:
            raise EmailBridgeUnreachable(
                f"connection file missing required field {k!r}: {p}"
            )
    if data.get("plugin") != "thunderbird-work-buddy":
        raise EmailBridgeUnreachable(
            f"connection file plugin mismatch: {data.get('plugin')!r}"
        )
    return data


class ThunderbirdEmailProvider:
    """HTTP client for the thunderbird-work-buddy extension."""

    name = "thunderbird"

    def __init__(self, *, timeout_seconds: int | None = None) -> None:
        self._timeout = timeout_seconds or DEFAULT_TIMEOUT_SECONDS
        self._cached_conn: dict | None = None

    # --- Internal HTTP -----------------------------------------------------

    def _conn(self, refresh: bool = False) -> dict:
        if refresh or self._cached_conn is None:
            self._cached_conn = discover_connection()
        return self._cached_conn

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        retry_on_unauthorized: bool = True,
    ) -> Any:
        conn = self._conn()
        url = f"http://127.0.0.1:{conn['port']}{path}"
        headers = {
            "Authorization": f"Bearer {conn['token']}",
            "Accept": "application/json",
        }
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(url, data=data, method=method, headers=headers)

        try:
            with urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except HTTPError as exc:
            # Read body for diagnostics.
            try:
                err_body = exc.read().decode("utf-8")
            except Exception:
                err_body = ""
            if exc.code == 403 and retry_on_unauthorized:
                # Likely stale token — Thunderbird restarted. Refresh once.
                try:
                    self._conn(refresh=True)
                    return self._request(
                        method, path, body=body, retry_on_unauthorized=False,
                    )
                except EmailBridgeUnreachable:
                    raise EmailBridgeUnauthorized(
                        "bridge rejected token (403). Restart Thunderbird "
                        "to refresh credentials."
                    ) from exc
            if exc.code == 403:
                raise EmailBridgeUnauthorized(
                    f"bridge rejected token: {err_body}"
                ) from exc
            # Try to surface a structured {"error": "..."} body so callers can
            # `isinstance`-classify (e.g. message-not-found from a 400/404).
            err_msg: str | None = None
            try:
                parsed = json.loads(err_body) if err_body else None
                if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
                    err_msg = parsed["error"]
            except (UnicodeDecodeError, json.JSONDecodeError):
                err_msg = None
            if err_msg and "not found" in err_msg.lower():
                raise EmailMessageNotFound(err_msg) from exc
            if exc.code == 404:
                raise EmailProviderError(
                    f"bridge {method} {path}: HTTP 404 {err_msg or err_body}"
                ) from exc
            raise EmailProviderError(
                f"bridge {method} {path}: HTTP {exc.code} {err_msg or err_body}"
            ) from exc
        except (URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise EmailBridgeUnreachable(
                f"bridge {method} {path}: {type(exc).__name__}: {exc}"
            ) from exc

        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EmailProviderError(
                f"bridge {method} {path}: response was not valid JSON: {exc}"
            ) from exc

    # --- EmailProvider -----------------------------------------------------

    def health(self) -> dict:
        result = self._request("GET", "/health")
        if not isinstance(result, dict) or not result.get("ok"):
            raise EmailProviderError(f"unexpected /health response: {result!r}")
        return result

    def list_accounts(self) -> list[dict]:
        result = self._request("GET", "/accounts")
        return list(result.get("accounts", []))

    def list_folders(
        self, *, account_id: str | None = None, folder_path: str | None = None,
    ) -> list[EmailFolder]:
        body = {}
        if account_id:
            body["account_id"] = account_id
        if folder_path:
            body["folder_path"] = folder_path
        result = self._request("POST", "/folders", body=body)
        if isinstance(result, dict) and result.get("error"):
            raise EmailProviderError(result["error"])
        return [
            EmailFolder(
                path=f["path"], name=f["name"], type=f["type"],
                account_id=f.get("account_id", ""),
                total_messages=int(f.get("total_messages", 0) or 0),
                unread_messages=int(f.get("unread_messages", 0) or 0),
                depth=int(f.get("depth", 0) or 0),
            )
            for f in (result.get("folders") or [])
        ]

    def recent_messages(
        self,
        *,
        days_back: int = 2,
        max_results: int = 50,
        unread_only: bool = True,
        flagged_only: bool = False,
        folder_path: str | None = None,
        account_id: str | None = None,
        include_subfolders: bool = True,
    ) -> list[EmailSummary]:
        body = {
            "days_back": days_back,
            "max_results": max_results,
            "unread_only": unread_only,
            "flagged_only": flagged_only,
            "include_subfolders": include_subfolders,
        }
        if folder_path:
            body["folder_path"] = folder_path
        if account_id:
            body["account_id"] = account_id
        result = self._request("POST", "/messages/recent", body=body)
        return [self._summary_from_dict(m) for m in (result.get("messages") or [])]

    def search_messages(
        self,
        *,
        query: str,
        max_results: int = 50,
        unread_only: bool = False,
        flagged_only: bool = False,
        folder_path: str | None = None,
        account_id: str | None = None,
        include_subfolders: bool = True,
    ) -> list[EmailSummary]:
        body = {
            "query": query,
            "max_results": max_results,
            "unread_only": unread_only,
            "flagged_only": flagged_only,
            "include_subfolders": include_subfolders,
        }
        if folder_path:
            body["folder_path"] = folder_path
        if account_id:
            body["account_id"] = account_id
        result = self._request("POST", "/messages/search", body=body)
        return [self._summary_from_dict(m) for m in (result.get("messages") or [])]

    def get_message(
        self, handle: EmailMessageHandle, *, max_body_chars: int = 8000,
    ) -> EmailMessage:
        body = {
            "provider_message_id": handle.provider_message_id,
            "folder_path": handle.folder_path,
            "max_body_chars": max_body_chars,
        }
        result = self._request("POST", "/messages/get", body=body)
        if isinstance(result, dict) and result.get("error"):
            err = result["error"]
            if "not found" in err.lower():
                raise EmailMessageNotFound(err)
            raise EmailProviderError(err)
        summary = self._summary_from_dict(result)
        return EmailMessage(
            summary=summary,
            body=result.get("body", "") or "",
            body_format=result.get("body_format", "text") or "text",
            body_truncated=bool(result.get("body_truncated", False)),
            body_length=int(result.get("body_length", 0) or 0),
        )

    def display_message(
        self, handle: EmailMessageHandle, *, mode: str = "3pane",
    ) -> dict:
        body = {
            "provider_message_id": handle.provider_message_id,
            "folder_path": handle.folder_path,
            "mode": mode,
        }
        result = self._request("POST", "/messages/display", body=body)
        if isinstance(result, dict) and result.get("error"):
            err = result["error"]
            if "not found" in err.lower():
                raise EmailMessageNotFound(err)
            raise EmailProviderError(err)
        return result

    # --- Mapping -----------------------------------------------------------

    @staticmethod
    def _summary_from_dict(d: dict) -> EmailSummary:
        """Bridge → :class:`EmailSummary`. Computes the stable key."""
        rfc_id = d.get("provider_message_id") or ""  # bridge uses RFC ID directly
        sender = d.get("author") or d.get("sender") or ""
        date = d.get("date")
        subject = d.get("subject") or ""
        stable = stable_key_for(
            rfc_message_id=rfc_id, sender=sender, date=date, subject=subject,
        )
        return EmailSummary(
            stable_key=stable,
            handle=EmailMessageHandle(
                provider_message_id=rfc_id,
                folder_path=d.get("folder_path") or "",
            ),
            subject=subject,
            sender=sender,
            recipients=d.get("recipients") or "",
            cc=d.get("cc") or "",
            date=date,
            folder=d.get("folder") or "",
            account_id=d.get("account_id") or "",
            read=bool(d.get("read", False)),
            flagged=bool(d.get("flagged", False)),
            tags=list(d.get("tags") or []),
            preview=d.get("preview") or "",
            rfc_message_id=rfc_id,
            folder_type=d.get("folder_type") or "",
        )


# ---------------------------------------------------------------------------
# Probe — used by ToolProbe registration in work_buddy.tools
# ---------------------------------------------------------------------------


def probe_thunderbird_bridge() -> tuple[bool, str]:
    """Cheap reachability probe for the bridge.

    Returns ``(available, reason)`` so the tool-status dashboard can show
    actionable failure modes (no connection file, port closed, 403, etc.).
    """
    p = connection_file_path()
    if not p.exists():
        return False, f"connection file missing at {p} — is Thunderbird running with the extension?"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"connection file unparseable: {exc}"
    port = data.get("port")
    token = data.get("token")
    if not port or not token:
        return False, "connection file missing port/token"

    # Quick TCP probe before HTTP.
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            pass
    except OSError as exc:
        return False, f"port {port} not accepting connections: {exc}"

    # Authenticated /health round-trip.
    t0 = time.time()
    try:
        req = Request(
            f"http://127.0.0.1:{port}/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urlopen(req, timeout=2) as resp:
            body = resp.read()
            ok = resp.status == 200
    except HTTPError as exc:
        if exc.code == 403:
            return False, "bridge rejected auth token (stale connection file?)"
        return False, f"bridge HTTP {exc.code}"
    except Exception as exc:
        return False, f"bridge probe failed: {type(exc).__name__}: {exc}"
    elapsed_ms = (time.time() - t0) * 1000
    if not ok:
        return False, "bridge /health did not return 200"
    try:
        info = json.loads(body)
        accounts = info.get("accessible_accounts", 0)
    except Exception:
        accounts = 0
    return True, f"bridge reachable on port {port} ({elapsed_ms:.0f} ms, {accounts} accessible accounts)"

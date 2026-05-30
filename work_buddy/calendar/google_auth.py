"""Own-OAuth credentials for the ``google_native`` calendar adapter.

Runtime path — load a persisted OAuth2 token and refresh it silently — uses
``google-auth`` (``google.oauth2.credentials`` + ``google.auth.transport``),
which is already a dependency. The **interactive** consent flow
(``google-auth-oauthlib``'s ``InstalledAppFlow``) is a one-time *setup* operation
and is **lazy-imported** inside :func:`run_oauth_flow`, so this module loads even
when that optional dependency isn't installed yet.

Token storage follows repo convention: a JSON file at
``paths.resolve("credentials/google-oauth")`` (under the gitignored data root).
The OAuth **client secret** (your Google Cloud app credential) is referenced by
path via an env var (default ``GOOGLE_OAUTH_CLIENT_SECRET``) or config — it is
never stored in ``config.example.yaml`` and never logged.

Least-privilege scope: ``calendar.events`` (CRUD on existing calendars). The
consent screen should be published to "Production" so the refresh token does not
expire (Testing mode expires sensitive-scope refresh tokens after 7 days).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from work_buddy.calendar.errors import (
    CalendarProviderDisabled,
    CalendarProviderError,
)

DEFAULT_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
DEFAULT_CLIENT_SECRET_ENV = "GOOGLE_OAUTH_CLIENT_SECRET"


def _token_path() -> Path:
    from work_buddy import paths

    return Path(paths.resolve("credentials/google-oauth"))


def _scopes(cfg: dict[str, Any] | None) -> list[str]:
    return list((cfg or {}).get("scopes") or DEFAULT_SCOPES)


def _client_secret_path(cfg: dict[str, Any] | None) -> Path | None:
    cfg = cfg or {}
    env_name = cfg.get("client_secret_env", DEFAULT_CLIENT_SECRET_ENV)
    raw = os.environ.get(env_name) or cfg.get("client_secret_path")
    return Path(raw) if raw else None


def _persist(creds, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")


def load_credentials(cfg: dict[str, Any] | None = None):
    """Load persisted OAuth credentials, refreshing silently when expired.

    Raises :class:`CalendarProviderDisabled` when no token has been set up yet
    (so the factory degrades cleanly and the health/requirement layer can point
    the user at the OAuth setup), and :class:`CalendarProviderError` on a
    corrupt token file or a failed refresh.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = _token_path()
    if not token_path.exists():
        raise CalendarProviderDisabled(
            f"google_native: no OAuth token at {token_path} — run the Google "
            "Calendar OAuth setup first."
        )
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), _scopes(cfg))
    except (ValueError, KeyError, OSError) as exc:
        raise CalendarProviderError(
            f"google_native: could not read OAuth token at {token_path}: {exc}"
        ) from exc

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:  # google-auth raises a broad RefreshError
                raise CalendarProviderError(
                    f"google_native: OAuth token refresh failed ({exc}); re-run "
                    "the OAuth setup."
                ) from exc
            _persist(creds, token_path)
        else:
            raise CalendarProviderDisabled(
                "google_native: OAuth token is invalid and not refreshable — "
                "re-run the OAuth setup."
            )
    return creds


def token_status(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Cheap, no-network status for the requirement check (config-time layer)."""
    token_path = _token_path()
    secret = _client_secret_path(cfg)
    return {
        "token_present": token_path.exists(),
        "token_path": str(token_path),
        "client_secret_present": bool(secret and secret.exists()),
        "client_secret_path": str(secret) if secret else None,
    }


def run_oauth_flow(cfg: dict[str, Any] | None = None, *, scopes: list[str] | None = None) -> Path:
    """Run the one-time interactive OAuth consent flow and persist the token.

    Opens a browser, captures the redirect on a localhost loopback port, and
    writes the resulting token to :func:`_token_path`. This is a **setup-time**
    operation; it requires ``google-auth-oauthlib`` (lazy-imported) and a
    user-present desktop session.
    """
    cfg = cfg or {}
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise CalendarProviderError(
            "google_native: google-auth-oauthlib is required for the OAuth "
            "consent flow — install it (it is in pyproject) and sync the env."
        ) from exc

    secret = _client_secret_path(cfg)
    if not secret or not secret.exists():
        env_name = cfg.get("client_secret_env", DEFAULT_CLIENT_SECRET_ENV)
        raise CalendarProviderDisabled(
            f"google_native: OAuth client secret not found — set {env_name} to "
            "the path of your client_secret.json (Desktop-app OAuth client from "
            "Google Cloud Console)."
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(secret), scopes or _scopes(cfg)
    )
    creds = flow.run_local_server(port=0)
    token_path = _token_path()
    _persist(creds, token_path)
    return token_path

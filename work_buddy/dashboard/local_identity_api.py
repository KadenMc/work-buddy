"""Flask adapter for the authenticated local dashboard identity boundary.

There is intentionally no HTTP bootstrap-mint route.  A trusted host launch
path calls :func:`work_buddy.security.local_identity.LocalIdentityAuthority.mint_bootstrap`
in process and places the one-time grant in a browser URL fragment.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, Response, jsonify, request

from work_buddy.security.local_identity import (
    CSRF_HEADER_NAME,
    DEFAULT_AUDIENCE,
    GESTURE_HEADER_NAME,
    SESSION_COOKIE_NAME,
    BoundaryRequest,
    HumanAuthorityContext,
    LocalIdentityAuthority,
    LocalIdentityError,
    LocalPrincipal,
    SessionGrant,
    get_default_authority,
)


local_identity_blueprint = Blueprint("local_identity", __name__)

_SESSION_RECOVERY_ABSENCE_CODES = frozenset(
    {
        "session_expired",
        "session_unavailable",
    }
)


def _authority() -> LocalIdentityAuthority:
    """Test seam and the sole default-authority lookup for this adapter."""

    return get_default_authority()


def boundary_for_request() -> BoundaryRequest:
    """Construct transport facts from Flask/Werkzeug state, not JSON."""

    return BoundaryRequest.from_environ(
        remote_addr=request.remote_addr,
        scheme=request.scheme,
        host=request.host,
        origin=request.headers.get("Origin"),
        headers=request.headers,
    )


def _body() -> Mapping[str, Any]:
    value = request.get_json(silent=True)
    if not isinstance(value, Mapping):
        raise LocalIdentityError(
            "invalid_request", "The request body must be a JSON object.", status=400
        )
    return value


def _cookie_token() -> str:
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not token:
        raise LocalIdentityError(
            "session_unavailable", "The local session is unavailable.", status=401
        )
    return token


def _csrf_token() -> str:
    token = request.headers.get(CSRF_HEADER_NAME, "")
    if not token:
        raise LocalIdentityError(
            "csrf_required", "A CSRF token is required.", status=403
        )
    return token


def _gesture_token() -> str:
    token = request.headers.get(GESTURE_HEADER_NAME, "")
    if not token:
        raise LocalIdentityError(
            "gesture_required", "A human-authority gesture is required.", status=403
        )
    return token


def _error(exc: LocalIdentityError):
    response = jsonify(
        {
            "ok": False,
            "error": {
                "code": exc.code,
                "message": str(exc),
                "retryable": exc.code
                in {"session_rotation_required", "bootstrap_expired"},
            },
        }
    )
    response.status_code = exc.status
    response.headers["Cache-Control"] = "no-store"
    return response


def _secure_cookie_for_origin(origin: str) -> bool:
    return urlsplit(origin).scheme == "https"


def _set_session_cookie(response: Response, grant: SessionGrant) -> None:
    max_age = _authority().policy.session_idle_ttl_seconds
    response.set_cookie(
        SESSION_COOKIE_NAME,
        grant.cookie_token,
        max_age=max_age,
        httponly=True,
        secure=_secure_cookie_for_origin(grant.principal.origin),
        samesite="Strict",
        path="/",
    )


def _clear_session_cookie(response: Response, *, secure: bool = False) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        httponly=True,
        secure=secure,
        samesite="Strict",
        path="/",
    )


def _session_payload(
    principal: LocalPrincipal, *, csrf_token: str | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": True,
        "authenticated": True,
        "principal": principal.to_public_dict(),
    }
    if csrf_token is not None:
        result["csrf_token"] = csrf_token
    return result


def _unauthenticated_session_response() -> Response:
    """Return the normal result when no recoverable browser session exists."""

    response = jsonify(
        {
            "ok": True,
            "authenticated": False,
            "human_authority_available": False,
        }
    )
    _clear_session_cookie(response, secure=request.is_secure)
    return response


@local_identity_blueprint.after_request
def _never_cache_credentials(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@local_identity_blueprint.post("/api/local-identity/bootstrap/redeem")
def redeem_bootstrap():
    """Redeem one host-minted grant into an opaque server-side session."""

    try:
        body = _body()
        grant = _authority().redeem_bootstrap(
            token=str(body.get("token") or ""),
            audience=str(body.get("audience") or DEFAULT_AUDIENCE),
            boundary=boundary_for_request(),
        )
        response = jsonify(_session_payload(grant.principal, csrf_token=grant.csrf_token))
        _set_session_cookie(response, grant)
        return response
    except LocalIdentityError as exc:
        return _error(exc)


@local_identity_blueprint.get("/api/local-identity/session")
def session_status():
    """Return the enrolled principal for a valid cookie, without CSRF."""

    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not token:
        return jsonify(
            {"ok": True, "authenticated": False, "human_authority_available": False}
        )
    try:
        principal = _authority().authenticate_session(
            cookie_token=token,
            boundary=boundary_for_request(),
            allow_rotation_due=True,
        )
        return jsonify(_session_payload(principal))
    except LocalIdentityError as exc:
        response = _error(exc)
        _clear_session_cookie(response)
        return response


@local_identity_blueprint.post("/api/local-identity/session/csrf")
def refresh_session_csrf():
    """Recover an in-memory CSRF token after an exact-Origin page reload."""

    try:
        principal, csrf_token = _authority().refresh_csrf(
            cookie_token=_cookie_token(),
            boundary=boundary_for_request(),
        )
        return jsonify(_session_payload(principal, csrf_token=csrf_token))
    except LocalIdentityError as exc:
        if exc.code in _SESSION_RECOVERY_ABSENCE_CODES:
            return _unauthenticated_session_response()
        return _error(exc)


@local_identity_blueprint.post("/api/local-identity/session/rotate")
def rotate_session():
    try:
        grant = _authority().rotate_session(
            cookie_token=_cookie_token(),
            csrf_token=_csrf_token(),
            boundary=boundary_for_request(),
        )
        response = jsonify(_session_payload(grant.principal, csrf_token=grant.csrf_token))
        _set_session_cookie(response, grant)
        return response
    except LocalIdentityError as exc:
        return _error(exc)


@local_identity_blueprint.post("/api/local-identity/session/revoke")
def revoke_session():
    try:
        principal = _authority().revoke_session(
            cookie_token=_cookie_token(),
            csrf_token=_csrf_token(),
            boundary=boundary_for_request(),
        )
        response = jsonify(
            {
                "ok": True,
                "authenticated": False,
                "revoked_actor": principal.actor.to_dict(),
            }
        )
        _clear_session_cookie(
            response, secure=_secure_cookie_for_origin(principal.origin)
        )
        return response
    except LocalIdentityError as exc:
        return _error(exc)


@local_identity_blueprint.post("/api/local-identity/gestures")
def issue_gesture():
    """Mint a single-use challenge for one visible action and exact context."""

    try:
        body = _body()
        principal, gesture = _authority().issue_gesture(
            cookie_token=_cookie_token(),
            csrf_token=_csrf_token(),
            boundary=boundary_for_request(),
            action=str(body.get("action") or ""),
            subject=str(body.get("subject") or ""),
            context_sha256=str(body.get("context_sha256") or ""),
        )
        return jsonify(
            {
                "ok": True,
                "principal": principal.to_public_dict(),
                "gesture": {
                    "token": gesture.token,
                    "action": gesture.action,
                    "subject_sha256": gesture.subject_sha256,
                    "context_sha256": gesture.context_sha256,
                    "expires_at": gesture.expires_at,
                },
            }
        )
    except LocalIdentityError as exc:
        return _error(exc)


def authenticate_request_session(
    *,
    authority: LocalIdentityAuthority | None = None,
    require_csrf: bool = False,
    allow_rotation_due: bool = False,
) -> LocalPrincipal:
    """Migration seam for a route that needs the canonical local principal."""

    service = authority or _authority()
    return service.authenticate_session(
        cookie_token=_cookie_token(),
        csrf_token=_csrf_token() if require_csrf else None,
        require_csrf=require_csrf,
        boundary=boundary_for_request(),
        allow_rotation_due=allow_rotation_due,
    )


def require_human_authority_request(
    *,
    action: str,
    subject: str,
    context_sha256: str,
    authority: LocalIdentityAuthority | None = None,
) -> HumanAuthorityContext:
    """Hard gate for a human-authority domain mutation.

    The actor is always read from server enrollment/session state.  Request
    headers such as ``X-WB-User-Ref`` and actor-shaped JSON fields are ignored.
    """

    service = authority or _authority()
    return service.authorize_human_mutation(
        cookie_token=_cookie_token(),
        csrf_token=_csrf_token(),
        gesture_token=_gesture_token(),
        boundary=boundary_for_request(),
        action=action,
        subject=subject,
        context_sha256=context_sha256,
    )


def register_routes(app) -> None:
    app.register_blueprint(local_identity_blueprint)


__all__ = [
    "authenticate_request_session",
    "boundary_for_request",
    "local_identity_blueprint",
    "register_routes",
    "require_human_authority_request",
]

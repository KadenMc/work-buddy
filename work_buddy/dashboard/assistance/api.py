"""Protected same-origin HTTP binding for host-owned assisted drafts.

Uses the ordinary house conversation wire shape, so ConversationChat and its
HTTP provider retain ownership of transcript mapping, send retries, and polls.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import wraps
from typing import Any

from flask import Blueprint, jsonify, request

from work_buddy.dashboard import local_identity_api
from work_buddy.security.local_identity import LocalIdentityError

from .contracts import AssistanceError, digest, manifest
from .service import AssistanceBroker

AssistanceAuthorizer = Callable[[str, str, Mapping[str, Any]], str]


def _authorize(operation: str, subject: str, body: Mapping[str, Any]) -> str:
    if request.method == "GET":
        return local_identity_api.authenticate_request_session().actor.canonical_id
    authority = local_identity_api.require_human_authority_request(
        action=f"dashboard.assistance.{operation}", subject=subject,
        context_sha256=digest({"method": request.method, "path": request.path, "body": dict(body)}),
    )
    return authority.principal.actor.canonical_id


def create_assistance_blueprint(*, broker: AssistanceBroker | None = None, authorizer: AssistanceAuthorizer | None = None, dashboard_read_only: Callable[[], bool] | None = None) -> Blueprint:
    bp = Blueprint("dashboard_assistance", __name__, url_prefix="/api/assistance")
    service = broker or AssistanceBroker()
    if dashboard_read_only is not None:
        service.read_only = dashboard_read_only
    authorize = authorizer or _authorize

    def boundary(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                if request.method != "GET" and service.read_only():
                    raise AssistanceError("dashboard_read_only", "Form assistance is paused while the dashboard is read-only.", 403)
                if request.content_length is not None and request.content_length > 128 * 1024:
                    raise AssistanceError("assistance_request_too_large", status=413)
                response = function(*args, **kwargs)
                if isinstance(response, tuple):
                    return response
                response.headers["Cache-Control"] = "no-store"
                return response
            except AssistanceError as exc:
                return jsonify({"error": str(exc), "code": exc.code}), exc.status
            except LocalIdentityError as exc:
                return jsonify({"error": str(exc), "code": exc.code}), exc.status
        return wrapped

    def body() -> Mapping[str, Any]:
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise AssistanceError("invalid_assistance_request")
        return value

    @bp.get("/availability")
    @boundary
    def availability():
        # Public status contains provider labels only, never draft/transcript.
        return jsonify(service.availability())

    @bp.get("/schemas")
    @boundary
    def schemas():
        return jsonify(manifest())

    @bp.post("/sessions")
    @boundary
    def start():
        value = body()
        actor = authorize("start", f"assistance:new:{value.get('requestId', '')}", value)
        return jsonify(service.start(value, actor))

    @bp.get("/<session_id>")
    @boundary
    def session(session_id: str):
        actor = authorize("read", f"assistance:{session_id}", {})
        return jsonify(service.session(session_id, actor))

    @bp.post("/<session_id>/snapshots")
    @boundary
    def prepare(session_id: str):
        value = body()
        actor = authorize("prepare", f"assistance:{session_id}", value)
        return jsonify(service.prepare(session_id, actor, value))

    @bp.get("/<session_id>/conversations/<conversation_id>")
    @boundary
    def conversation(session_id: str, conversation_id: str):
        actor = authorize("read", f"assistance:{session_id}", {})
        return jsonify(service.conversation(session_id, conversation_id, actor))

    @bp.post("/<session_id>/conversations/<conversation_id>/respond")
    @boundary
    def respond(session_id: str, conversation_id: str):
        value = body()
        actor = authorize("respond", f"assistance:{session_id}", value)
        return jsonify(service.respond(session_id, conversation_id, actor, value))

    @bp.get("/<session_id>/patches")
    @boundary
    def patches(session_id: str):
        actor = authorize("read", f"assistance:{session_id}", {})
        return jsonify({"patches": service.patches(session_id, actor)})

    @bp.post("/<session_id>/receipts")
    @boundary
    def receipt(session_id: str):
        value = body()
        actor = authorize("acknowledge", f"assistance:{session_id}", value)
        return jsonify(service.acknowledge(session_id, actor, value))

    @bp.post("/<session_id>/stop")
    @boundary
    def stop(session_id: str):
        value = body()
        actor = authorize("stop", f"assistance:{session_id}", value)
        return jsonify(service.stop(session_id, actor))

    return bp

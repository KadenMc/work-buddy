"""Minimal process host for the real dashboard Flask app in Co-work live E2E.

This intentionally imports the production ``service.app`` rather than constructing a
test Flask application.  It skips unrelated sidecar pollers and pre-warm threads from
``service.main`` while serving the exact registered production routes.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


root = Path(_required("COWORK_LIVE_ROOT")).resolve()
marker = root / ".cowork-live-harness"
if not marker.is_file():
    raise RuntimeError("refusing to start outside a marked Co-work live temp root")

data_root = Path(_required("WORK_BUDDY_DATA_DIR")).resolve()
config_root = Path(_required("WORK_BUDDY_CONFIG_DIR")).resolve()
if root not in data_root.parents or root not in config_root.parents:
    raise RuntimeError("Co-work live data and config must be contained by the temp root")

port = int(_required("COWORK_LIVE_BACKEND_PORT"))
if port == 5127:
    raise RuntimeError("the live E2E backend must never use the normal dashboard port")

from flask import jsonify, request  # noqa: E402

from work_buddy.conversations.store import add_message, get_conversation  # noqa: E402
from work_buddy.cowork import api as cowork_api  # noqa: E402
from work_buddy.cowork import document_agent as document_agent  # noqa: E402
from work_buddy.cowork.conversations import CONVERSATION_SOURCE  # noqa: E402
from work_buddy.dashboard.service import app  # noqa: E402
from work_buddy.security.local_identity import (  # noqa: E402
    DEFAULT_AUDIENCE,
    LocalIdentityError,
    get_default_authority,
    normalize_loopback_origin,
)
from work_buddy.truth import documents, proposals, ydoc_store  # noqa: E402
from work_buddy.truth.anchors import CompositeSelector  # noqa: E402
from work_buddy.truth.contracts import Actor  # noqa: E402
from work_buddy.truth.registry import TruthStoreRegistry  # noqa: E402


_agent_lock = threading.Lock()
_agent_mode = "running"
_agent_spawn_calls = 0
_agent_conversation_ids: list[str] = []
_AGENT_MODES = frozenset({"running", "spawn_failed", "stopped"})


def _fake_document_agent_status(*, started: bool):
    with _agent_lock:
        mode = _agent_mode
    if mode == "spawn_failed":
        return document_agent.DocumentAgentStatus(
            status="spawn_failed",
            alive=False,
            started=False,
            error="Chat couldn’t start. Try again.",
        )
    if mode == "stopped":
        return document_agent.DocumentAgentStatus(
            status="stopped",
            alive=False,
            started=False,
            error=None,
        )
    return document_agent.DocumentAgentStatus(
        status="running",
        alive=True,
        started=started,
        error=None,
    )


def _ensure_fake_document_agent(**kwargs):
    """Keep the live harness deterministic without launching an external model."""

    global _agent_spawn_calls
    conversation_id = str(kwargs.get("conversation_id") or "")
    with _agent_lock:
        _agent_spawn_calls += 1
        _agent_conversation_ids.append(conversation_id)
    return _fake_document_agent_status(started=True)


def _inspect_fake_document_agent(conversation_id, **_kwargs):
    if conversation_id is None:
        return document_agent.DocumentAgentStatus(
            status="not_started",
            alive=None,
            started=False,
            error=None,
        )
    return _fake_document_agent_status(started=False)


# Route functions resolve these module globals when a request arrives. Patch both
# modules so the harness remains safe whether the production API imports the
# lifecycle functions at module scope or looks them up lazily.
document_agent.ensure_document_agent = _ensure_fake_document_agent
document_agent.inspect_document_agent = _inspect_fake_document_agent
if hasattr(cowork_api, "ensure_document_agent"):
    cowork_api.ensure_document_agent = _ensure_fake_document_agent
if hasattr(cowork_api, "inspect_document_agent"):
    cowork_api.inspect_document_agent = _inspect_fake_document_agent


def _harness_control_allowed() -> bool:
    return request.headers.get("X-WB-Cowork-Live-Control") == _required(
        "COWORK_LIVE_HARNESS_NONCE"
    )


@app.post("/api/_cowork-live/agent-control")
def _agent_control():
    """Set deterministic fake-agent behavior for the next product request."""

    if not _harness_control_allowed():
        return jsonify({"ok": False, "error": "harness control denied"}), 403
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "JSON object required"}), 400
    mode = payload.get("mode")
    if mode not in _AGENT_MODES:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "mode must be running, spawn_failed, or stopped",
                }
            ),
            400,
        )
    global _agent_mode, _agent_spawn_calls
    with _agent_lock:
        _agent_mode = str(mode)
        if payload.get("reset") is True:
            _agent_spawn_calls = 0
            _agent_conversation_ids.clear()
    return jsonify({"ok": True, "mode": mode})


@app.get("/api/_cowork-live/agent-state")
def _agent_state():
    """Expose fake spawn observations without touching production state."""

    if not _harness_control_allowed():
        return jsonify({"ok": False, "error": "harness control denied"}), 403
    with _agent_lock:
        return jsonify(
            {
                "ok": True,
                "spawn_calls": _agent_spawn_calls,
                "mode": _agent_mode,
                "conversation_ids": list(_agent_conversation_ids),
            }
        )


@app.post("/api/_cowork-live/identity-bootstrap")
def _identity_bootstrap():
    """Mint one isolated, exact-Origin browser bootstrap for live UI coverage.

    Production deliberately has no HTTP mint route. This nonce-gated route exists
    only in the throwaway live-harness process, and its authority database is under
    ``COWORK_LIVE_ROOT`` so teardown removes the token and resulting session.
    """

    if not _harness_control_allowed():
        return jsonify({"ok": False, "error": "harness control denied"}), 403
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "JSON object required"}), 400
    requested_origin = str(payload.get("origin") or "")
    observed_origin = str(request.headers.get("Origin") or "")
    try:
        normalized_requested = normalize_loopback_origin(requested_origin)
        normalized_observed = normalize_loopback_origin(observed_origin)
    except LocalIdentityError:
        return jsonify({"ok": False, "error": "exact loopback origin required"}), 400
    if normalized_requested != normalized_observed:
        return jsonify({"ok": False, "error": "browser origin mismatch"}), 403
    grant = get_default_authority().mint_bootstrap(
        origin=normalized_observed,
        audience=DEFAULT_AUDIENCE,
    )
    response = jsonify(
        {
            "ok": True,
            "token": grant.token,
            "origin": grant.origin,
            "expires_at": grant.expires_at,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.after_request
def _identify_harness(response):
    response.headers["X-WB-Cowork-Live-Harness"] = _required(
        "COWORK_LIVE_HARNESS_NONCE"
    )
    return response


@app.post("/api/_cowork-live/seed-proposal")
def _seed_proposal():
    """Author one real proposal in the isolated store for browser lifecycle coverage.

    This is deliberately a harness-only setup seam, not a product mock: the request is
    nonce-gated and the write runs through the production registry, proposal authoring,
    ledger, export, and review contracts against throwaway data.
    """

    if request.headers.get("X-WB-Cowork-Live-Control") != _required(
        "COWORK_LIVE_HARNESS_NONCE"
    ):
        return jsonify({"ok": False, "error": "harness control denied"}), 403
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "JSON object required"}), 400
    store_id = str(payload.get("store_id") or "").strip()
    document_id = str(payload.get("document_id") or "").strip()
    quote = str(payload.get("quote") or "")
    replacement = str(payload.get("replacement") or "")
    if not store_id or not document_id or not quote or not replacement:
        return jsonify({"ok": False, "error": "proposal fields required"}), 400

    store = TruthStoreRegistry().open_store(store_id)
    document = documents.get_document(store, document_id)
    if document.ydoc_snapshot_sha256 is None:
        return jsonify({"ok": False, "error": "document snapshot required"}), 409
    structured_head = ydoc_store.current_structured_head(
        store,
        document_id=document_id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    proposal = proposals.propose_edit(
        store,
        document_id=document_id,
        base_content_sha256=document.content_sha256,
        base_structured_head_sha256=structured_head,
        selector=CompositeSelector(exact=quote),
        quote_exact=quote,
        replacement=replacement,
        rationale="Exercise the real Co-work review and apply lifecycle.",
        tldr="Use the reviewed wording.",
        actor=Actor(
            "agent_run",
            "cowork-live-proposal-author",
            {
                "model": "cowork-live-fixture",
                "harness": "playwright-live",
                "surface": "cowork",
                "session_id": _required("WORK_BUDDY_SESSION_ID"),
                "call_id": "seed-proposal",
            },
        ),
    )
    return jsonify(
        {
            "ok": True,
            "proposal_id": proposal.id,
            "canonical_sha256": proposal.canonical_sha256,
        }
    )


@app.post("/api/_cowork-live/conversation-reply")
def _conversation_reply():
    """Append one agent turn through the production conversation store.

    The browser test uses this harness-only seam after exercising the real R9
    feedback route. It avoids launching an external model while still proving
    that the UI follows the opaque server-issued conversation id, observes a
    later agent turn, and restores the same transcript after reload.
    """

    if request.headers.get("X-WB-Cowork-Live-Control") != _required(
        "COWORK_LIVE_HARNESS_NONCE"
    ):
        return jsonify({"ok": False, "error": "harness control denied"}), 403
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "JSON object required"}), 400
    conversation_id = str(payload.get("conversation_id") or "").strip()
    message = str(payload.get("message") or "").strip()
    if not conversation_id or not message:
        return jsonify({"ok": False, "error": "conversation reply fields required"}), 400
    if len(message) > 20_000:
        return jsonify({"ok": False, "error": "conversation reply is too large"}), 400

    conversation = get_conversation(conversation_id)
    if conversation is None or conversation.source != CONVERSATION_SOURCE:
        return jsonify({"ok": False, "error": "Co-work conversation not found"}), 404
    posted = add_message(conversation_id, "agent", message)
    if posted is None:
        return jsonify({"ok": False, "error": "conversation is closed"}), 409
    return jsonify(
        {
            "ok": True,
            "conversation_id": conversation_id,
            "message_id": posted.message_id,
        }
    )


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=port,
        debug=False,
        threaded=True,
        use_reloader=False,
    )

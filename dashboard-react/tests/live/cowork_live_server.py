"""Minimal process host for the real dashboard Flask app in Co-work live E2E.

This intentionally imports the production ``service.app`` rather than constructing a
test Flask application.  It skips unrelated sidecar pollers and pre-warm threads from
``service.main`` while serving the exact registered production routes.
"""

from __future__ import annotations

import os
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

from work_buddy.dashboard.service import app  # noqa: E402
from work_buddy.truth import documents, proposals, ydoc_store  # noqa: E402
from work_buddy.truth.anchors import CompositeSelector  # noqa: E402
from work_buddy.truth.contracts import Actor  # noqa: E402
from work_buddy.truth.registry import TruthStoreRegistry  # noqa: E402


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


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=port,
        debug=False,
        threaded=True,
        use_reloader=False,
    )

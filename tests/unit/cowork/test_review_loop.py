"""End-to-end review loop over the Flask test client.

Drives the whole surface: register a document (R10), propose edits through the
cowork ops, pull the Yjs transport (R3), then submit one sitting (R5) carrying a
confirm, a reject_as_false, and a reject_as_preference with verbatim
preference_text. It asserts the gestures are minted hash-bound to a real
dashboard user, the proposal statuses are recorded, the FA-1 preference claim and
the reject_as_false negation are minted, and the materialize hash is verified onto
disk.
"""

from __future__ import annotations

import pytest

from work_buddy.truth import proposals
from work_buddy.truth.identity import sha256_text

from .conftest import gesture_actor_ref, gesture_count

DOC_REL = "docs/review-loop.md"
DOC_BODY = (
    "# Review loop fixture\n\n"
    "Alpha sentence to accept.\n\n"
    "Beta sentence to reject as false.\n\n"
    "Gamma sentence to reject as preference.\n"
)
RENDERED_AFTER_ACCEPT = (
    "# Review loop fixture\n\n"
    "Alpha sentence accepted.\n\n"
    "Beta sentence to reject as false.\n\n"
    "Gamma sentence to reject as preference.\n"
)
MODEL = "review-loop-model"
SESSION_ID = "session-review-loop"
USER_REF = "e2e-reviewer"


def _url(path: str, store_id: str) -> str:
    return f"{path}?store_id={store_id}"


@pytest.fixture
def loop(store_ctx, client, monkeypatch):
    """Wire the ops registry and session manifest to the client's live store.

    The ops are invoked as plain functions here (never through the gateway), so
    the registry does not need the ops registered, only its store resolution and
    the session manifest redirected to the client's live store.
    """
    import work_buddy.cowork.ops as cowork_ops
    import work_buddy.mcp_server.ops.truth_ops as truth_ops

    monkeypatch.setattr(cowork_ops, "_registry", lambda: store_ctx["registry"])
    monkeypatch.setattr(
        truth_ops,
        "_session_manifest",
        lambda session_id: {"session_id": session_id, "harness_id": "codex"},
    )
    return {**store_ctx, "client": client, "ops": cowork_ops}


def _propose(ops, store_id, doc_id, base_sha256, quote, replacement):
    result = ops.cowork_doc_propose_edit(
        store_id,
        doc_id,
        [{"quote_anchor": {"exact": quote}, "replacement": replacement}],
        f"Rationale for {quote}",
        f"Change {quote}",
        MODEL,
        agent_session_id=SESSION_ID,
        base_doc_sha256=base_sha256,
    )
    assert result["ok"] is True
    return result["proposals"][0]["id"]


def test_full_review_loop(loop):
    client = loop["client"]
    store = loop["store"]
    store_id = loop["store_id"]
    ops = loop["ops"]

    # Register the document through R10 with the file on disk.
    (loop["root"] / DOC_REL).parent.mkdir(parents=True, exist_ok=True)
    (loop["root"] / DOC_REL).write_bytes(DOC_BODY.encode("utf-8"))
    register = client.post(
        _url("/api/truth/doc/register", store_id),
        json={"path": DOC_REL, "title": "Review loop", "profile": "co_authored"},
    )
    assert register.status_code == 200
    registered = register.get_json()
    doc_id = registered["document_id"]
    base_sha256 = registered["current_file_sha256"]

    # Propose three edits through the ops surface.
    p_confirm = _propose(
        ops, store_id, doc_id, base_sha256,
        "Alpha sentence to accept.", "Alpha sentence accepted.",
    )
    p_false = _propose(
        ops, store_id, doc_id, base_sha256,
        "Beta sentence to reject as false.", "Beta sentence (proposed).",
    )
    p_pref = _propose(
        ops, store_id, doc_id, base_sha256,
        "Gamma sentence to reject as preference.", "Gamma sentence (proposed).",
    )
    canonical = {
        pid: proposals.get_proposal(store, pid).canonical_sha256
        for pid in (p_confirm, p_false, p_pref)
    }

    # Pull the Yjs transport (R3). A freshly registered doc has no snapshot yet,
    # so the pull is an empty body that still reports the current content hash.
    pull = client.get(_url(f"/api/truth/doc/{doc_id}/ydoc", store_id))
    assert pull.status_code == 200
    assert pull.headers["X-WB-Doc-Sha256"] == base_sha256

    # Submit one sitting: accept the first, reject the second as false with a
    # verbatim negation, reject the third as preference with verbatim text.
    post_apply_sha256 = sha256_text(RENDERED_AFTER_ACCEPT)
    negation_text = "Beta sentence is accurate as written."
    preference_text = "Keep the gamma sentence exactly as it stands."
    response = client.post(
        _url(f"/api/truth/doc/{doc_id}/marks", store_id),
        json={
            "base_doc_sha256": base_sha256,
            "items": [
                {
                    "proposal_id": p_confirm,
                    "verb": "confirm",
                    "canonical_sha256": canonical[p_confirm],
                },
                {
                    "proposal_id": p_false,
                    "verb": "reject_as_false",
                    "canonical_sha256": canonical[p_false],
                    "negation_text": negation_text,
                },
                {
                    "proposal_id": p_pref,
                    "verb": "reject_as_preference",
                    "canonical_sha256": canonical[p_pref],
                    "preference_text": preference_text,
                },
            ],
            "materialize": {
                "rendered_markdown": RENDERED_AFTER_ACCEPT,
                "post_apply_content_sha256": post_apply_sha256,
            },
        },
        headers={"X-WB-User-Ref": USER_REF},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["partial"] is True
    results = {item["proposal_id"]: item for item in payload["results"]}

    # confirm -> applied and materialized, its gesture minted.
    confirm_result = results[p_confirm]
    assert confirm_result["result"] == "applied"
    assert confirm_result["materialized"] is True
    assert confirm_result["gesture_id"]

    # reject_as_false -> closed, a confirmed negation minted from the verbatim text.
    false_result = results[p_false]
    assert false_result["result"] == "closed"
    negation_claim_id = false_result["negation_claim_id"]
    assert negation_claim_id

    # reject_as_preference -> closed, the FA-1 preference claim minted from text.
    pref_result = results[p_pref]
    assert pref_result["result"] == "closed"
    preference_claim_id = pref_result["preference_claim_id"]
    assert preference_claim_id

    # The materialize hash was verified and the file written byte-for-byte.
    assert payload["materialize"]["new_file_sha256"] == post_apply_sha256
    assert (loop["root"] / DOC_REL).read_text(encoding="utf-8") == RENDERED_AFTER_ACCEPT

    # Exactly three gestures minted, each bound to the real dashboard user.
    assert gesture_count(store) == 3
    for result in payload["results"]:
        ref = gesture_actor_ref(store, result["gesture_id"])
        assert ref == USER_REF
        assert ref != "work-buddy-user"

    # Statuses recorded on the ledger.
    assert proposals.latest_proposal_status(store, p_confirm).status == "applied"
    assert proposals.latest_proposal_status(store, p_false).status == "closed"
    assert proposals.latest_proposal_status(store, p_pref).status == "closed"

    # The minted claims are real rows: a fact negation, a human-authored preference.
    with store.connect() as conn:
        negation = conn.execute(
            "SELECT proposition, claim_kind FROM claims WHERE id = ?",
            (negation_claim_id,),
        ).fetchone()
        preference = conn.execute(
            "SELECT proposition, claim_kind, created_by_kind FROM claims WHERE id = ?",
            (preference_claim_id,),
        ).fetchone()
    assert negation["proposition"] == negation_text
    assert preference["proposition"] == preference_text
    assert preference["claim_kind"] == "preference"
    assert preference["created_by_kind"] == "human"

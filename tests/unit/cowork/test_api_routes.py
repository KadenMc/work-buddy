"""Flask test-client coverage for every /api/truth/doc/* route (R1-R10).

The client mounts only the co-work blueprint against a temporary registry, so no
live port is bound and the routes resolve stores exactly as in production.
"""

from __future__ import annotations

from work_buddy.cowork import api
from work_buddy.truth import proposals
from work_buddy.truth.identity import sha256_bytes, sha256_text

from .conftest import (
    DOC_QUOTE,
    DOC_REL,
    NOW,
    gesture_actor_ref,
    gesture_count,
    write_doc_file,
)


def _url(path: str, store_id: str) -> str:
    return f"{path}?store_id={store_id}"


# --- R10 register ----------------------------------------------------------


def test_register_imports_then_is_idempotent(client, store_ctx):
    write_doc_file(store_ctx["root"])
    body = {"path": DOC_REL, "title": "Throwaway fixture", "profile": "co_authored"}
    first = client.post(
        _url("/api/truth/doc/register", store_ctx["store_id"]), json=body
    )
    assert first.status_code == 200
    payload = first.get_json()
    assert payload["ok"] is True
    assert payload["imported"] is True
    assert payload["document_id"]
    second = client.post(
        _url("/api/truth/doc/register", store_ctx["store_id"]), json=body
    )
    assert second.get_json()["imported"] is False


def test_register_rejects_unknown_profile(client, store_ctx):
    write_doc_file(store_ctx["root"])
    resp = client.post(
        _url("/api/truth/doc/register", store_ctx["store_id"]),
        json={"path": DOC_REL, "title": "x", "profile": "bogus"},
    )
    assert resp.status_code == 400


# --- R1 list / R2 get ------------------------------------------------------


def test_list_returns_registered_docs(client, seeded):
    resp = client.get(_url("/api/truth/doc/list", seeded["store_id"]))
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["count"] == 1
    entry = payload["docs"][0]
    assert entry["document_id"] == seeded["document"].id
    assert entry["profile"] == "co_authored"
    assert entry["drift_state"] == "clean"
    assert entry["last_materialized_sha256"] == seeded["content_sha256"]


def test_get_returns_open_proposals_and_hashes(client, seeded, make_proposal):
    proposal = make_proposal()
    resp = client.get(
        _url(f"/api/truth/doc/{seeded['document'].id}", seeded["store_id"])
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["hashes"]["ydoc_snapshot_sha256"] == seeded["snapshot_sha256"]
    assert payload["drift"]["state"] == "clean"
    assert len(payload["open_proposals"]) == 1
    entry = payload["open_proposals"][0]
    assert entry["proposal_id"] == proposal.id
    assert entry["canonical_sha256"] == proposal.canonical_sha256
    assert entry["base_ok"] is True
    assert entry["quote_anchor"]["exact"] == DOC_QUOTE
    assert entry["kind"] == "edit"


def test_get_unknown_document_is_404(client, seeded):
    resp = client.get(_url("/api/truth/doc/" + "0" * 32, seeded["store_id"]))
    assert resp.status_code == 404


# --- R3 / R4 transport -----------------------------------------------------


def test_ydoc_pull_streams_octet_snapshot(client, seeded):
    resp = client.get(
        _url(f"/api/truth/doc/{seeded['document'].id}/ydoc", seeded["store_id"])
    )
    assert resp.status_code == 200
    assert resp.mimetype == "application/octet-stream"
    assert resp.headers["X-WB-Snapshot-Sha256"] == seeded["snapshot_sha256"]
    assert resp.headers["X-WB-Doc-Sha256"] == seeded["content_sha256"]
    # The framed body carries exactly the snapshot segment.
    assert seeded["snapshot_bytes"] in resp.data


def test_ydoc_push_appends_and_guards_stale_base(client, seeded):
    url = _url(f"/api/truth/doc/{seeded['document'].id}/ydoc", seeded["store_id"])
    ok = client.post(
        url,
        data=b"human-edit-batch",
        content_type="application/octet-stream",
        headers={"X-WB-Base-Sha256": seeded["content_sha256"]},
    )
    assert ok.status_code == 200
    assert ok.get_json()["applied"] is True
    stale = client.post(
        url,
        data=b"another-batch",
        content_type="application/octet-stream",
        headers={"X-WB-Base-Sha256": "0" * 64},
    )
    assert stale.status_code == 409
    assert stale.get_json()["error"] == "stale_base"


# --- R5 marks --------------------------------------------------------------


def test_marks_confirm_writes_file_and_threads_identity(client, seeded, make_proposal):
    proposal = make_proposal()
    new_body = "# Throwaway fixture\n\nConfirmed revision.\n"
    resp = client.post(
        _url(f"/api/truth/doc/{seeded['document'].id}/marks", seeded["store_id"]),
        json={
            "base_doc_sha256": seeded["content_sha256"],
            "items": [
                {
                    "proposal_id": proposal.id,
                    "verb": "confirm",
                    "canonical_sha256": proposal.canonical_sha256,
                }
            ],
            "materialize": {
                "rendered_markdown": new_body,
                "post_apply_content_sha256": sha256_text(new_body),
            },
        },
        headers={"X-WB-User-Ref": "alice-reviewer"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    result = payload["results"][0]
    assert result["result"] == "applied"
    assert result["materialized"] is True
    assert payload["materialize"]["new_file_sha256"] == sha256_text(new_body)
    assert (seeded["root"] / seeded["rel"]).read_text(encoding="utf-8") == new_body
    # The gesture carries the threaded dashboard user, never the MCP constant.
    ref = gesture_actor_ref(seeded["store"], result["gesture_id"])
    assert ref == "alice-reviewer"
    assert ref != "work-buddy-user"


def test_marks_partial_failure_is_flagged(client, seeded, make_proposal):
    good = make_proposal(quote="Original sentence for co-work tests.", replacement="Good.")
    stale = make_proposal(quote="Second target phrase.", replacement="Stale.")
    resp = client.post(
        _url(f"/api/truth/doc/{seeded['document'].id}/marks", seeded["store_id"]),
        json={
            "items": [
                {
                    "proposal_id": good.id,
                    "verb": "reject_plain",
                    "canonical_sha256": good.canonical_sha256,
                },
                {
                    "proposal_id": stale.id,
                    "verb": "reject_plain",
                    "canonical_sha256": "0" * 64,
                },
            ]
        },
    )
    payload = resp.get_json()
    assert payload["partial"] is True
    results = {item["proposal_id"]: item for item in payload["results"]}
    assert results[good.id]["result"] == "closed"
    assert results[stale.id]["result"] == "rejected_stale_view"
    assert gesture_count(seeded["store"]) == 1


def test_marks_materialize_hash_mismatch_is_409(client, seeded, make_proposal):
    proposal = make_proposal()
    resp = client.post(
        _url(f"/api/truth/doc/{seeded['document'].id}/marks", seeded["store_id"]),
        json={
            "items": [
                {
                    "proposal_id": proposal.id,
                    "verb": "confirm",
                    "canonical_sha256": proposal.canonical_sha256,
                }
            ],
            "materialize": {
                "rendered_markdown": "body",
                "post_apply_content_sha256": "0" * 64,
            },
        },
    )
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "hash_mismatch"
    # Nothing committed on the aborted sitting.
    assert proposals.latest_proposal_status(seeded["store"], proposal.id).status == "open"


def test_marks_read_only_is_rejected(client, seeded, make_proposal, monkeypatch):
    monkeypatch.setattr(api, "_is_read_only", lambda: True)
    proposal = make_proposal()
    resp = client.post(
        _url(f"/api/truth/doc/{seeded['document'].id}/marks", seeded["store_id"]),
        json={
            "items": [
                {
                    "proposal_id": proposal.id,
                    "verb": "reject_plain",
                    "canonical_sha256": proposal.canonical_sha256,
                }
            ]
        },
    )
    assert resp.status_code == 403


# --- R6 materialize --------------------------------------------------------


def test_materialize_verifies_snapshot_hash(client, seeded):
    url = _url(
        f"/api/truth/doc/{seeded['document'].id}/materialize", seeded["store_id"]
    )
    new_body = "# Throwaway fixture\n\nDirect materialize.\n"
    ok = client.post(
        url,
        json={
            "rendered_markdown": new_body,
            "structured_doc_sha256": seeded["snapshot_sha256"],
        },
    )
    assert ok.status_code == 200
    payload = ok.get_json()
    assert payload["new_file_sha256"] == sha256_text(new_body)
    assert (seeded["root"] / seeded["rel"]).read_text(encoding="utf-8") == new_body
    mismatch = client.post(
        url,
        json={"rendered_markdown": new_body, "structured_doc_sha256": "0" * 64},
    )
    assert mismatch.status_code == 409
    assert mismatch.get_json()["error"] == "hash_mismatch"


# --- R7 drift / R8 reimport ------------------------------------------------


def test_drift_reports_out_of_band_edit(client, seeded):
    url = _url(f"/api/truth/doc/{seeded['document'].id}/drift", seeded["store_id"])
    clean = client.get(url).get_json()
    assert clean["state"] == "clean"
    assert clean["can_reimport"] is False
    # Edit the file out of band.
    drifted_body = "# Throwaway fixture\n\nEdited outside the editor.\n"
    (seeded["root"] / seeded["rel"]).write_bytes(drifted_body.encode("utf-8"))
    drifted = client.get(url).get_json()
    assert drifted["state"] == "drifted"
    assert drifted["can_reimport"] is True
    assert drifted["current_file_sha256"] == sha256_bytes(drifted_body.encode("utf-8"))


def test_reimport_records_change_set(client, seeded):
    drifted_body = "# Throwaway fixture\n\nEdited outside the editor.\n"
    (seeded["root"] / seeded["rel"]).write_bytes(drifted_body.encode("utf-8"))
    file_sha256 = sha256_bytes(drifted_body.encode("utf-8"))
    resp = client.post(
        _url(f"/api/truth/doc/{seeded['document'].id}/reimport", seeded["store_id"]),
        json={"structured_doc": {"type": "doc", "content": []}, "file_sha256": file_sha256},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["change_set_id"]
    # The content pointer advanced, so the document reads clean again.
    drift = client.get(
        _url(f"/api/truth/doc/{seeded['document'].id}/drift", seeded["store_id"])
    ).get_json()
    assert drift["state"] == "clean"


# --- R9 feedback -----------------------------------------------------------


def test_feedback_captures_user_authored_utterance(client, seeded):
    resp = client.post(
        _url(f"/api/truth/doc/{seeded['document'].id}/feedback", seeded["store_id"]),
        json={
            "span": {"exact": DOC_QUOTE, "prefix": "", "suffix": ""},
            "text": "This sentence needs a citation.",
            "conversation_id": "conv-123",
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["conversation_id"] == "conv-123"
    assert payload["span_id"]
    with seeded["store"].connect() as conn:
        row = conn.execute(
            "SELECT kind, trust_class, content FROM evidence WHERE id = ?",
            (payload["evidence_id"],),
        ).fetchone()
    assert row["kind"] == "utterance"
    assert row["trust_class"] == "user_authored"
    assert row["content"] == "This sentence needs a citation."


def test_feedback_requires_document_surface_capture(client, store_ctx, tmp_path):
    # A second store in the same registry with feedback_capture turned off. The
    # profile is read from disk on each open, so the route sees it disabled.
    from work_buddy.truth import documents
    from work_buddy.truth.contracts import Actor
    from work_buddy.truth.store import TruthStore

    from .conftest import DOC_BODY, USER_REF, _profile

    profile = _profile()
    profile["document_surface"]["feedback_capture"] = False
    root = tmp_path / "scope-no-feedback"
    root.mkdir()
    store = TruthStore.create(root, profile)
    store_ctx["registry"].register(store)
    content_sha256 = write_doc_file(root)
    record = documents.register_document(
        store,
        path=DOC_REL,
        title="Throwaway fixture",
        document_class="co_authored",
        content_sha256=content_sha256,
        actor=Actor("human", USER_REF),
        at=NOW,
    )
    resp = client.post(
        _url(f"/api/truth/doc/{record.id}/feedback", store.store_id),
        json={"span": {"exact": DOC_QUOTE}, "text": "hi"},
    )
    assert resp.status_code == 403

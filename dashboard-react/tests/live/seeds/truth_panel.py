"""Claim-bearing document fixture confined to the marked live harness."""

from __future__ import annotations

import hashlib
import os

from work_buddy.cowork import truth_api, truth_surface
from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.security.local_identity import BoundaryRequest, get_default_authority
from work_buddy.truth import documents, expressions, queries, ydoc_store
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor
from work_buddy.truth.identity import sha256_bytes
from work_buddy.truth.lifecycle import TruthLifecycle


TITLE = "Throwaway Truth review"
PASSAGES = {
    "with_evidence": "The fixture study recorded twelve observations.",
    "without_evidence": "The fixture study reached every participant.",
    "confirmed": "The fixture report uses a consistent observation window.",
    "multiple_first": "The fixture observations were recorded on paper.",
    "needs_review": "The fixture review covered the whole observation period.",
    "multiple_second": "Paper records preserve the fixture observations.",
}


def _document_paragraphs() -> list[str]:
    paragraphs = []
    for name, passage in PASSAGES.items():
        paragraphs.append(passage)
        if name == "multiple_first":
            paragraphs.extend(
                f"Fixture context paragraph {index} keeps the observation notes "
                "available while reviewing passages in the document."
                for index in range(1, 19)
            )
    return paragraphs


def _snapshot(projection: bytes) -> bytes:
    with DocumentKernelClient(default_timeout=30) as kernel:
        outcome = kernel.request({
            "kind": "bootstrap_markdown",
            "sourceBase64": projection,
            "sourceSha256": sha256_bytes(projection),
            "newlineStyle": "lf",
            "utf8Bom": False,
            "trailingNewlineCount": 0,
        })
    if outcome.snapshot is None or outcome.projection != projection:
        raise RuntimeError("Truth seed requires a lossless canonical document snapshot")
    return outcome.snapshot


def seed_truth_panel(*, root, folder, store, existing=None):
    """Find or create the fixture, retaining all subsequent review and prose edits."""
    if not (root / ".wb-live-harness").is_file():
        raise RuntimeError("Truth seed requires a marked live root")
    if root not in folder.resolve().parents or root not in store.paths.sidecar.resolve().parents:
        raise RuntimeError("Truth seed must remain inside the marked live root")
    if existing is not None:
        if existing["store_id"] != store.store_id:
            raise RuntimeError("Truth seed belongs to another store")
        documents.get_document(store, existing["document_id"])
        return existing

    port = int(os.environ["WB_LIVE_BACKEND_PORT"])
    if port == 5127 or not 1024 <= port <= 65535:
        raise RuntimeError("Truth seed authority requires an isolated backend port")
    origin = f"http://127.0.0.1:{port}"
    boundary = BoundaryRequest("127.0.0.1", "http", f"127.0.0.1:{port}", origin)
    authority = get_default_authority()
    grant = authority.mint_bootstrap(origin=origin)
    session = authority.redeem_bootstrap(token=grant.token, boundary=boundary)
    human = Actor("human", session.principal.actor.canonical_id)
    agent = Actor("agent_run", "truth-panel-fixture", {
        "model": "fixture-model", "harness": "isolated-live-harness",
        "surface": "cowork", "session_id": "truth-panel-fixture", "call_id": "seed",
    })
    try:
        relative_path = "Throwaway Truth Review.md"
        document = next(
            (item for item in documents.list_documents(store) if item.path == relative_path),
            None,
        )
        if document is None:
            projection = (f"# {TITLE}\n\n" + "\n\n".join(_document_paragraphs())).encode("utf-8")
            target = folder / relative_path
            snapshot = _snapshot(projection)
            try:
                with target.open("xb") as output:
                    output.write(projection)
            except FileExistsError:
                if target.read_bytes() != projection:
                    raise RuntimeError("Truth seed will not overwrite an edited source")
            digest = ydoc_store.write_snapshot(store, snapshot=snapshot)
            document, _, _ = documents.register_ready_document(
                store, path=relative_path, title=TITLE, document_class="co_authored",
                projection_bytes=projection, ydoc_snapshot_sha256=digest,
                structured_head_sha256=ydoc_store.structured_head_from_segments(snapshot, ()),
                actor=human, mode="create",
            )

        claim_ids = {}
        for name in ("with_evidence", "without_evidence", "confirmed", "multiple_first", "needs_review"):
            key = "multiple" if name == "multiple_first" else name
            claim = store.propose_claim(
                proposition=PASSAGES[name], claim_kind="fact", actor=agent,
                meta={"fixture": "truth-panel", "case": key},
            ).claim
            claim_ids[key] = claim.id
            quotes = [PASSAGES[name]]
            if key == "multiple":
                quotes.append(PASSAGES["multiple_second"])
            connected = expressions.expressions_for_claim(store, claim.id)
            for quote in quotes:
                span = expressions.ensure_document_span(
                    store, document_id=document.id, selector=CompositeSelector(exact=quote),
                    quote_exact=quote, actor=agent,
                )
                if not any(item.document_span_id == span.id for item in connected):
                    expressions.mark_expression(
                        store, document_span_id=span.id, claim_ref=claim.id,
                        role="paraphrase", actor=agent,
                    )
            if key in {"with_evidence", "confirmed", "needs_review"}:
                evidence_id = hashlib.sha256(f"{store.store_id}:{key}:receipt".encode()).hexdigest()[:32]
                evidence = store.get_evidence(evidence_id)
                if evidence is None:
                    receipt_text = f"Throwaway review receipt: {PASSAGES[name]}"
                    receipt_path = root / "evidence" / "truth-panel" / f"{key}.txt"
                    receipt_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with receipt_path.open("x", encoding="utf-8") as output:
                            output.write(receipt_text)
                    except FileExistsError:
                        if receipt_path.read_text(encoding="utf-8") != receipt_text:
                            raise RuntimeError("Truth seed will not overwrite an edited receipt")
                    evidence = store.capture_evidence(
                        kind="document", source_locator=receipt_path.as_uri(),
                        actor=human, acquisition_method="paste", content=receipt_text,
                        record_id=evidence_id,
                    )
                    receipt = store.mark_span(
                        evidence_id=evidence.id,
                        selector=CompositeSelector(exact=receipt_text), actor=human,
                    )
                    store.add_link(
                        from_claim_id=claim.id, link_type="supports_span",
                        to_kind="evidence_span", to_ref=receipt.id, actor=human,
                    )
            if key in {"confirmed", "needs_review"}:
                state = next(item for item in queries.resolve_claim_states(store) if item.claim_id == claim.id)
                if state.base_status == "proposed":
                    binding = truth_surface.truth_claim_detail(store, document, claim.id)["decision_binding"]
                    payload = {
                        "claim_id": claim.id, "action": "confirm",
                        "expected_canonical_sha256": binding["payload_sha256"],
                        "expected_context_sha256": binding["context_sha256"],
                        "gesture_kind": None, "reason": None,
                    }
                    subject = truth_api.truth_mutation_subject(
                        operation="decision", store_id=store.store_id,
                        document_id=document.id, claim_id=claim.id,
                    )
                    context = truth_api.truth_mutation_context_sha256(
                        operation="decision", store_id=store.store_id,
                        document_id=document.id, payload=payload,
                    )
                    authorization = dict(
                        cookie_token=session.cookie_token, csrf_token=session.csrf_token,
                        boundary=boundary, action=truth_api.TRUTH_LIFECYCLE_ACTION,
                        subject=subject, context_sha256=context,
                    )
                    _, gesture = authority.issue_gesture(**authorization)
                    human_context = authority.authorize_human_mutation(
                        **authorization, gesture_token=gesture.token,
                    )
                    truth_surface.decide_claim(
                        store, document, claim.id, actor=human, authority_context=human_context,
                        action="confirm", expected_canonical_sha256=binding["payload_sha256"],
                        expected_context_sha256=binding["context_sha256"],
                    )
                if key == "needs_review" and not state.needs_review:
                    TruthLifecycle(store).mark_needs_review(
                        claim_id=claim.id, actor=Actor("system", "truth-panel-fixture"),
                        basis_kind="sweep", basis_ref="fixture-review",
                    )
        return {
            "store_id": store.store_id, "document_id": document.id, "title": TITLE,
            "path": relative_path, "claim_ids": claim_ids, "passages": PASSAGES,
            "app_path": f"/app/cowork?store_id={store.store_id}&document_id={document.id}",
        }
    finally:
        authority.revoke_session(
            cookie_token=session.cookie_token, csrf_token=session.csrf_token, boundary=boundary,
        )

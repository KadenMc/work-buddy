from __future__ import annotations

from pathlib import Path

from work_buddy.cowork import bootstrap, catalog_api
from work_buddy.cowork.file_importers import MARKDOWN_MAX_SOURCE_BYTES
from work_buddy.truth import documents
from work_buddy.truth.contracts import Actor
from work_buddy.truth.identity import sha256_bytes

from .conftest import DOC_BODY, DOC_REL


def _url(path: str, store_id: str) -> str:
    return f"{path}?store_id={store_id}"


def test_list_exposes_readiness_and_document_class_alias(client, seeded):
    response = client.get(_url("/api/truth/doc/list", seeded["store_id"]))
    assert response.status_code == 200
    payload = response.get_json()
    entry = payload["docs"][0]
    assert entry["document_class"] == "co_authored"
    assert entry["profile"] == "co_authored"
    assert entry["initialization_state"] == "ready"
    assert entry["structured_head_sha256"]
    assert entry["snapshot_sha256"] == seeded["snapshot_sha256"]
    assert entry["permissions"]["open"] is True
    assert payload["repairable_count"] == 0


def test_candidates_are_bounded_to_unregistered_markdown(client, store_ctx):
    root: Path = store_ctx["root"]
    (root / "notes").mkdir()
    (root / "notes" / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
    (root / "notes" / "beta.markdown").write_text("# Beta\n", encoding="utf-8")
    (root / "notes" / "ignore.txt").write_text("no", encoding="utf-8")
    (root / ".wbuddy").mkdir(exist_ok=True)
    (root / ".wbuddy" / "hidden.md").write_text("hidden", encoding="utf-8")
    beta = (root / "notes" / "beta.markdown").read_bytes()
    documents.register_document(
        store_ctx["store"],
        path="notes/beta.markdown",
        title="Beta",
        document_class="co_authored",
        content_sha256=sha256_bytes(beta),
        actor=Actor("human", "dashboard-user"),
    )

    response = client.get(
        _url("/api/truth/doc/candidates", store_ctx["store_id"])
        + "&query=alp&limit=10"
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert [entry["path"] for entry in payload["candidates"]] == ["notes/alpha.md"]
    assert payload["candidates"][0]["already_registered"] is False
    assert payload["next_cursor"] is None


def test_candidate_queries_reuse_one_bounded_folder_scan(
    client, store_ctx, monkeypatch
):
    root: Path = store_ctx["root"]
    (root / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
    catalog_api._SCAN_CACHE.clear()
    original = catalog_api._scan_candidate_paths
    calls = 0

    def counted(folder):
        nonlocal calls
        calls += 1
        return original(folder)

    monkeypatch.setattr(catalog_api, "_scan_candidate_paths", counted)
    first = client.get(
        _url("/api/truth/doc/candidates", store_ctx["store_id"]) + "&query=a"
    )
    second = client.get(
        _url("/api/truth/doc/candidates", store_ctx["store_id"]) + "&query=al"
    )

    assert first.status_code == second.status_code == 200
    assert calls == 1


def test_source_returns_exact_bom_crlf_bytes(client, store_ctx):
    data = b"\xef\xbb\xbf# Exact\r\n\r\nBody\r\n"
    target = store_ctx["root"] / "docs" / "exact.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(data)
    record = documents.register_document(
        store_ctx["store"],
        path="docs/exact.md",
        title="Exact",
        document_class="co_authored",
        content_sha256=sha256_bytes(data),
        actor=Actor("human", "dashboard-user"),
    )

    response = client.get(
        _url(f"/api/truth/doc/{record.id}/source", store_ctx["store_id"])
    )
    assert response.status_code == 200
    assert response.data == data
    assert response.headers["Content-Type"].startswith("application/octet-stream")
    assert response.headers["X-WB-Source-Sha256"] == sha256_bytes(data)
    assert response.headers["X-WB-Source-BOM"] == "utf-8"
    assert response.headers["X-WB-Source-Encoding"] == "utf-8"


def test_materialized_source_requires_and_verifies_retained_blob(client, seeded):
    url = _url(
        f"/api/truth/doc/{seeded['document'].id}/source", seeded["store_id"]
    ) + "&version=materialized"
    missing = client.get(url)
    assert missing.status_code == 404
    assert missing.get_json()["error"]["code"] == "baseline_unavailable"

    data = DOC_BODY.encode("utf-8")
    seeded["store"]._store_blob_bytes(seeded["content_sha256"], data)
    response = client.get(url)
    assert response.status_code == 200
    assert response.data == data


def test_source_rejects_invalid_version_with_typed_error(client, seeded):
    response = client.get(
        _url(f"/api/truth/doc/{seeded['document'].id}/source", seeded["store_id"])
        + "&version=future"
    )
    assert response.status_code == 400
    assert response.get_json() == {
        "ok": False,
        "error": {
            "code": "invalid_request",
            "message": "version must be current or materialized",
            "field": "version",
            "retryable": False,
        },
    }


def test_oversized_detached_source_keeps_catalog_usable_and_fails_current_read_typed(
    client, store_ctx
):
    source = b"# Initially bounded\n"
    rel = "imports/grown-after-import.md"
    target = store_ctx["root"] / rel
    target.parent.mkdir(parents=True)
    target.write_bytes(source)
    actor = Actor("human", "dashboard-user")
    intent, _ = bootstrap.prepare_bootstrap(
        store_ctx["store"],
        metadata={
            "mode": "import",
            "path": rel,
            "expected_file_sha256": sha256_bytes(source),
            "idempotency_key": "grown-source-bootstrap-0001",
        },
        source=None,
        actor=actor,
    )
    snapshot = b"YDOC:" + source
    ready = bootstrap.commit_bootstrap(
        store_ctx["store"],
        bootstrap_id=intent.id,
        snapshot=snapshot,
        source_sha256=sha256_bytes(source),
        snapshot_sha256=sha256_bytes(snapshot),
        ydoc_schema=bootstrap.YDOC_SCHEMA,
        actor=actor,
    )
    with target.open("wb") as stream:
        stream.truncate(MARKDOWN_MAX_SOURCE_BYTES + 1)

    listed = client.get(_url("/api/truth/doc/list", store_ctx["store_id"]))
    fetched = client.get(
        _url(f"/api/truth/doc/{ready['document_id']}", store_ctx["store_id"])
    )
    current = client.get(
        _url(
            f"/api/truth/doc/{ready['document_id']}/source",
            store_ctx["store_id"],
        )
    )

    assert listed.status_code == 200
    list_entry = listed.get_json()["docs"][0]
    assert list_entry["source_writeback"] == "never"
    assert list_entry["observed_source_file_sha256"] is None
    assert list_entry["current_file_sha256"] == sha256_bytes(source)
    assert fetched.status_code == 200
    assert fetched.get_json()["observed_source_file_sha256"] is None
    assert current.status_code == 413
    assert current.get_json()["error"] == {
        "code": "source_too_large",
        "message": "The current source file exceeds the size limit.",
        "retryable": False,
        "details": {
            "importer_id": "markdown/v1",
            "max_source_bytes": MARKDOWN_MAX_SOURCE_BYTES,
            "source_byte_length": MARKDOWN_MAX_SOURCE_BYTES + 1,
        },
    }

    target.unlink()
    target.mkdir()
    listed_again = client.get(_url("/api/truth/doc/list", store_ctx["store_id"]))
    non_regular = client.get(
        _url(
            f"/api/truth/doc/{ready['document_id']}/source",
            store_ctx["store_id"],
        )
    )
    assert listed_again.status_code == 200
    assert listed_again.get_json()["docs"][0]["observed_source_file_sha256"] is None
    assert non_regular.status_code == 409
    assert non_regular.get_json()["error"]["code"] == "source_unavailable"


def test_retire_is_idempotent_and_never_deletes_markdown(client, store_ctx):
    source = b"# Retirement route\n"
    rel = "docs/retirement-route.md"
    target = store_ctx["root"] / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source)
    actor = Actor("human", "dashboard-user")
    bootstrap_intent, _ = bootstrap.prepare_bootstrap(
        store_ctx["store"],
        metadata={
            "mode": "import",
            "path": rel,
            "idempotency_key": "retirement-route-bootstrap-0001",
            "expected_file_sha256": sha256_bytes(source),
        },
        source=None,
        actor=actor,
    )
    snapshot = b"YDOC:" + source
    ready = bootstrap.commit_bootstrap(
        store_ctx["store"],
        bootstrap_id=bootstrap_intent.id,
        snapshot=snapshot,
        source_sha256=sha256_bytes(source),
        snapshot_sha256=sha256_bytes(snapshot),
        ydoc_schema=bootstrap.YDOC_SCHEMA,
        actor=actor,
    )
    url = _url(
        f"/api/truth/doc/{ready['document_id']}/retire", store_ctx["store_id"]
    )
    prepared = client.post(
        url,
        json={"idempotency_key": "retirement-route-0001"},
    )
    assert prepared.status_code == 201
    intent_id = prepared.get_json()["intent_id"]
    first = client.post(url, json={"intent_id": intent_id})
    assert first.status_code == 200
    assert first.get_json()["file_retained"] is True
    assert target.read_bytes() == source

    repeated = client.post(url, json={"intent_id": intent_id})
    assert repeated.status_code == 200
    assert repeated.get_json() == first.get_json()

    catalog = client.get(_url("/api/truth/doc/list", store_ctx["store_id"]))
    assert catalog.get_json()["docs"] == []

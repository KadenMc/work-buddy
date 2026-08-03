"""HTTP contracts for bootstrap and dual-CAS materialization blueprints."""

from __future__ import annotations

import io
import json

from flask import Flask

from work_buddy.cowork import api as cowork_api
from work_buddy.cowork import bootstrap, bootstrap_api, materialization_api
from work_buddy.truth.identity import sha256_bytes


def _client(store_ctx, monkeypatch):
    monkeypatch.setattr(bootstrap_api, "_store", lambda: store_ctx["store"])
    monkeypatch.setattr(materialization_api, "_store", lambda: store_ctx["store"])
    app = Flask(__name__)
    app.config.update(TESTING=True)
    bootstrap_api.register_bootstrap_routes(app)
    materialization_api.register_materialization_routes(app)
    return app.test_client()


def test_lifecycle_blueprints_share_parent_registry(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(cowork_api, "_registry", lambda: sentinel)

    assert bootstrap_api._registry() is sentinel
    assert materialization_api._registry() is sentinel


def test_bootstrap_binary_http_round_trip(store_ctx, monkeypatch):
    client = _client(store_ctx, monkeypatch)
    source = b"\xef\xbb\xbf# Browser bootstrap\r\n"
    prepared = client.post(
        "/api/truth/doc/bootstrap?store_id=test",
        data={
            "metadata": json.dumps(
                {
                    "mode": "create",
                    "path": "docs/browser.md",
                    "title": "Browser",
                    "initial_source_sha256": sha256_bytes(source),
                    "idempotency_key": "browser-bootstrap-0001",
                }
            ),
            "source": (io.BytesIO(source), "source.bin"),
        },
        content_type="multipart/form-data",
    )
    assert prepared.status_code == 201
    intent = prepared.get_json()
    staged = client.get(intent["source_url"].replace("store_id=test", "store_id=test"))
    assert staged.status_code == 200
    assert staged.data == source
    assert staged.headers["X-WB-BOM"] == "utf-8"

    snapshot = b"opaque-browser-ydoc"
    committed = client.put(
        f"/api/truth/doc/bootstrap/{intent['bootstrap_id']}?store_id=test",
        data=snapshot,
        content_type="application/octet-stream",
        headers={
            "X-WB-Source-Sha256": sha256_bytes(source),
            "X-WB-Snapshot-Sha256": sha256_bytes(snapshot),
            "X-WB-Ydoc-Schema": "cowork-yjs/v1",
        },
    )
    assert committed.status_code == 200
    payload = committed.get_json()
    assert payload["initialization_state"] == "ready"
    assert payload["document_class"] == "co_authored"
    assert (store_ctx["root"] / "docs" / "browser.md").read_bytes() == source


def test_bootstrap_http_bounds_snapshot_and_projection_uploads(
    store_ctx,
    monkeypatch,
):
    client = _client(store_ctx, monkeypatch)
    source = b"# Bounded\n"
    prepared = client.post(
        "/api/truth/doc/bootstrap?store_id=test",
        data={
            "metadata": json.dumps(
                {
                    "mode": "create",
                    "path": "docs/bounded.md",
                    "idempotency_key": "bounded-bootstrap-0001",
                }
            ),
            "source": (io.BytesIO(source), "source.md"),
        },
        content_type="multipart/form-data",
    ).get_json()
    url = (
        f"/api/truth/doc/bootstrap/{prepared['bootstrap_id']}?store_id=test"
    )

    monkeypatch.setattr(bootstrap, "MAX_SNAPSHOT_BYTES", 4)
    large_snapshot = b"12345"
    snapshot_rejected = client.put(
        url,
        data={
            "metadata": json.dumps(
                {
                    "source_sha256": sha256_bytes(source),
                    "snapshot_sha256": sha256_bytes(large_snapshot),
                    "projection_sha256": sha256_bytes(source),
                    "ydoc_schema": "cowork-yjs/v1",
                }
            ),
            "snapshot": (io.BytesIO(large_snapshot), "snapshot.bin"),
            "projection": (io.BytesIO(source), "projection.md"),
        },
        content_type="multipart/form-data",
    )
    assert snapshot_rejected.status_code == 413
    assert (
        snapshot_rejected.get_json()["error"]["code"]
        == "snapshot_too_large"
    )

    snapshot = b"1234"
    monkeypatch.setattr(bootstrap, "MAX_CANONICAL_PROJECTION_BYTES", 4)
    projection_rejected = client.put(
        url,
        data={
            "metadata": json.dumps(
                {
                    "source_sha256": sha256_bytes(source),
                    "snapshot_sha256": sha256_bytes(snapshot),
                    "projection_sha256": sha256_bytes(source),
                    "ydoc_schema": "cowork-yjs/v1",
                }
            ),
            "snapshot": (io.BytesIO(snapshot), "snapshot.bin"),
            "projection": (io.BytesIO(source), "projection.md"),
        },
        content_type="multipart/form-data",
    )
    assert projection_rejected.status_code == 413
    assert (
        projection_rejected.get_json()["error"]["code"]
        == "projection_too_large"
    )
    assert not (store_ctx["root"] / "docs" / "bounded.md").exists()


def test_bootstrap_error_envelope_and_materialize(store_ctx, monkeypatch):
    client = _client(store_ctx, monkeypatch)
    emitted: list[dict] = []

    def capture_emit(event_type, store_id, data, *, event_id=None):
        emitted.append(
            {
                "event_type": event_type,
                "store_id": store_id,
                "data": data,
                "event_id": event_id,
            }
        )

    monkeypatch.setattr(cowork_api, "_emit", capture_emit)
    bad = client.post(
        "/api/truth/doc/bootstrap?store_id=test",
        data={
            "metadata": json.dumps(
                {
                    "mode": "create",
                    "path": "../escape.md",
                    "idempotency_key": "bad-bootstrap-0001",
                }
            )
        },
        content_type="multipart/form-data",
    )
    assert bad.status_code == 400
    assert bad.get_json()["error"]["code"] == "invalid_path"

    source = b"# Save me\n"
    prepared = client.post(
        "/api/truth/doc/bootstrap?store_id=test",
        data={
            "metadata": json.dumps(
                {
                    "mode": "create",
                    "path": "docs/save-api.md",
                    "idempotency_key": "save-api-bootstrap-0001",
                }
            ),
            "source": (io.BytesIO(source), "source.bin"),
        },
        content_type="multipart/form-data",
    ).get_json()
    snapshot = b"save-api-snapshot"
    ready = client.put(
        f"/api/truth/doc/bootstrap/{prepared['bootstrap_id']}?store_id=test",
        data=snapshot,
        content_type="application/octet-stream",
        headers={
            "X-WB-Source-Sha256": sha256_bytes(source),
            "X-WB-Snapshot-Sha256": sha256_bytes(snapshot),
            "X-WB-Ydoc-Schema": "cowork-yjs/v1",
        },
    ).get_json()
    rendered = "# Saved through API\n"
    save_payload = {
        "rendered_markdown": rendered,
        "rendered_sha256": sha256_bytes(rendered.encode()),
        "expected_file_sha256": ready["projection_sha256"],
        "expected_ydoc_head_sha256": ready["structured_head_sha256"],
        "snapshot_sha256": ready["snapshot_sha256"],
        "idempotency_key": "save-api-materialize-0001",
    }
    saved = client.post(
        f"/api/truth/doc/{ready['document_id']}/materialize?store_id=test",
        json=save_payload,
    )
    assert saved.status_code == 200
    assert saved.get_json()["drift_state"] == "clean"
    assert (store_ctx["root"] / "docs" / "save-api.md").read_text() == rendered

    replay = client.post(
        f"/api/truth/doc/{ready['document_id']}/materialize?store_id=test",
        json=save_payload,
    )
    assert replay.status_code == 200
    assert replay.get_json() == saved.get_json()
    assert len(emitted) == 2
    assert emitted[0]["event_id"] == emitted[1]["event_id"]
    assert emitted[0]["event_id"]

"""Tests for the externalized, content-hashed frontend assets.

The ~800 KB of app JS and the CSS are served as separate
``/assets/app.<hash>.js|css`` files with an immutable cache policy, instead of
being inlined in the ~1 MB no-store document. Every reload becomes a 304, and
a truncated asset transfer is a retryable fetch rather than a dead page.

Pins: the page references the hashed assets (and inlines neither JS nor CSS);
the /assets route serves the exact bytes with immutable Cache-Control; unknown
hashes 404; and the document itself stays no-store so it always points at the
current hashed names.
"""

from __future__ import annotations

import hashlib

import pytest

import work_buddy.dashboard.frontend as F
from work_buddy.dashboard.service import app


@pytest.fixture
def client():
    return app.test_client()


def test_page_references_hashed_assets_not_inlined():
    page = F.render_page()
    assert f"/assets/{F._JS_NAME}" in page
    assert f"/assets/{F._CSS_NAME}" in page
    # Neither the JS nor the CSS is inlined any more.
    assert "<script>" not in page
    assert "<style>" not in page


def test_asset_name_is_content_hash():
    F._build_assets()
    js = F.assembled_js().encode("utf-8")
    css = F.assembled_css().encode("utf-8")
    assert F._JS_NAME == "app.%s.js" % hashlib.sha256(js).hexdigest()[:12]
    assert F._CSS_NAME == "app.%s.css" % hashlib.sha256(css).hexdigest()[:12]


def test_assets_route_serves_immutable_js(client):
    F._ensure_assets()
    resp = client.get(f"/assets/{F._JS_NAME}")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["Content-Type"]
    cc = resp.headers.get("Cache-Control", "")
    assert "immutable" in cc and "max-age=31536000" in cc
    assert resp.get_data() == F.assembled_js().encode("utf-8")


def test_assets_route_serves_immutable_css(client):
    F._ensure_assets()
    resp = client.get(f"/assets/{F._CSS_NAME}")
    assert resp.status_code == 200
    assert "css" in resp.headers["Content-Type"]
    assert "immutable" in resp.headers.get("Cache-Control", "")
    assert resp.get_data() == F.assembled_css().encode("utf-8")


def test_unknown_asset_404s(client):
    assert client.get("/assets/app.deadbeef0000.js").status_code == 404


def test_index_document_is_no_store_and_points_at_asset(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "no-store" in resp.headers.get("Cache-Control", "")
    F._ensure_assets()
    assert f"/assets/{F._JS_NAME}".encode() in resp.get_data()

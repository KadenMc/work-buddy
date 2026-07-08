"""Tests for the React dashboard shell routes (``/app`` and ``/app/assets/``).

The React app sources live in ``dashboard-react/`` at the repo root; the
build output (``dashboard-react/dist``) is gitignored and produced on demand
with ``npm install && npm run build``. Content assertions skip with a clear
message when the build output is absent, so CI without Node still passes.

Pins: ``GET /app`` serves the built shell no-store and titled
"work-buddy dashboard"; the hashed Vite assets serve from ``/app/assets/``
with an immutable cache policy and a JavaScript MIME type; traversal is
rejected; and the not-built state is a helpful 404, not a stack trace.
"""

from __future__ import annotations

import re

import pytest

from work_buddy.dashboard.service import _react_dist_dir, app


@pytest.fixture
def client():
    return app.test_client()


def _dist_built() -> bool:
    return (_react_dist_dir() / "index.html").is_file()


requires_dist = pytest.mark.skipif(
    not _dist_built(),
    reason=(
        "React dist not built; run 'npm install && npm run build' in "
        "dashboard-react/ (skipping is expected on CI without Node)"
    ),
)


@requires_dist
def test_app_route_serves_shell_no_store(client):
    resp = client.get("/app")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("Content-Type", "")
    body = resp.get_data(as_text=True)
    assert "work-buddy dashboard" in body
    cc = resp.headers.get("Cache-Control", "")
    assert "no-store" in cc


@requires_dist
def test_app_trailing_slash_also_serves(client):
    assert client.get("/app/").status_code == 200


@requires_dist
def test_app_assets_serve_hashed_js_immutable(client):
    # The built index.html references at least one hashed JS asset under
    # /app/assets/ (Vite base is /app/); serve it and pin the cache policy.
    body = client.get("/app").get_data(as_text=True)
    m = re.search(r"/app/assets/([^\"']+\.js)", body)
    assert m, "built index.html should reference a hashed /app/assets/ JS file"
    resp = client.get(f"/app/assets/{m.group(1)}")
    assert resp.status_code == 200
    cc = resp.headers.get("Cache-Control", "")
    assert "immutable" in cc and "max-age=31536000" in cc
    assert "javascript" in resp.headers.get("Content-Type", "")


def test_app_assets_reject_traversal(client):
    # Rejected (or unroutable) regardless of whether the dist exists.
    assert client.get("/app/assets/../index.html").status_code == 404


def test_app_assets_unknown_file_404s(client):
    assert client.get("/app/assets/nope-0000000000.js").status_code == 404


def test_app_route_absent_dist_is_helpful_404(client):
    if _dist_built():
        pytest.skip("dist present; the not-built branch is unreachable here")
    resp = client.get("/app")
    assert resp.status_code == 404
    assert "npm run build" in resp.get_data(as_text=True)

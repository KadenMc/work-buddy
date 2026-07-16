"""Tests for the React dashboard shell routes (``/app`` and ``/app/assets/``).

The React app sources live in ``dashboard-react/`` at the repo root; the
build output (``dashboard-react/dist``) is gitignored and produced on demand
with ``npm install && npm run build``. Content assertions skip with a clear
message when the build output is absent, so CI without Node still passes.

Pins: ``GET /app`` and safe single-segment history routes serve the built
shell no-store and titled "work-buddy dashboard"; the hashed Vite assets
serve from ``/app/assets/`` with an immutable cache policy and a JavaScript
MIME type; traversal is rejected; and the not-built state is a helpful 404,
not a stack trace.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from work_buddy.dashboard.service import _react_dist_dir, app


@pytest.fixture
def client():
    return app.test_client()


def _dist_built() -> bool:
    return (_react_dist_dir() / "index.html").is_file()


def test_dashboard_context_uses_configured_timezone(client, monkeypatch):
    """Shared React chrome gets an explicit Work Buddy zone, never a browser guess."""
    from work_buddy import config as wb_config

    configured = ZoneInfo("Pacific/Kiritimati")
    monkeypatch.setattr(wb_config, "_USER_TZ_CACHE", configured)

    response = client.get("/api/dashboard/context")
    body = response.get_json()
    instant = datetime.fromisoformat(body["now"])

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert body == {
        "schema_version": 1,
        "revision": "timezone:Pacific/Kiritimati",
        "timezone": "Pacific/Kiritimati",
        "now": body["now"],
    }
    assert instant.tzinfo is not None


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
def test_app_journal_history_route_serves_shell_no_store(client):
    resp = client.get("/app/journal")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("Content-Type", "")
    assert "work-buddy dashboard" in resp.get_data(as_text=True)
    assert "no-store" in resp.headers.get("Cache-Control", "")


@requires_dist
def test_app_unknown_safe_view_reaches_client_shell(client):
    resp = client.get("/app/not-registered-yet")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("Content-Type", "")


@requires_dist
def test_app_settings_section_history_route_serves_shell_no_store(client):
    resp = client.get("/app/settings/accessibility")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("Content-Type", "")
    assert "work-buddy dashboard" in resp.get_data(as_text=True)
    assert "no-store" in resp.headers.get("Cache-Control", "")


@requires_dist
@pytest.mark.parametrize(
    "path",
    (
        "/app/settings/system/accessibility",
        "/app/settings/apps/journal",
        "/app/settings/views/journal",
        "/app/settings/setting/wb.journal.day-boundary",
    ),
)
def test_app_nested_settings_history_routes_serve_shell_no_store(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("Content-Type", "")
    assert "work-buddy dashboard" in resp.get_data(as_text=True)
    assert "no-store" in resp.headers.get("Cache-Control", "")


def test_app_history_fallback_rejects_asset_and_traversal_shapes(client):
    assert client.get("/app/index.html").status_code == 404
    assert client.get("/app/not.a-view").status_code == 404
    assert client.get("/app/%5Cindex").status_code == 404
    assert client.get("/app/settings/not.a-section").status_code == 404
    assert client.get("/app/settings/sections/accessibility").status_code == 404
    assert client.get("/app/settings/views/not.a-page").status_code == 404
    assert client.get("/app/settings/not.a-group/journal").status_code == 404


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


@requires_dist
def test_app_manifest_is_installable_and_targets_react_app(client):
    resp = client.get("/app/manifest.webmanifest")
    assert resp.status_code == 200
    assert "application/manifest+json" in resp.headers.get("Content-Type", "")
    manifest = resp.get_json()
    assert manifest["start_url"] == "/app/"
    assert manifest["scope"] == "/app/"
    assert manifest["display"] == "standalone"
    assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}


@requires_dist
def test_app_manifest_icons_are_served(client):
    for name in ("app-192.png", "app-512.png", "app-maskable-512.png"):
        resp = client.get(f"/app/icons/{name}")
        assert resp.status_code == 200
        assert "image/png" in resp.headers.get("Content-Type", "")


def test_app_route_absent_dist_is_helpful_404(client):
    if _dist_built():
        pytest.skip("dist present; the not-built branch is unreachable here")
    resp = client.get("/app")
    assert resp.status_code == 404
    assert "npm run build" in resp.get_data(as_text=True)
    history_resp = client.get("/app/journal")
    assert history_resp.status_code == 404
    assert "npm run build" in history_resp.get_data(as_text=True)

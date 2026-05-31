"""The google_calendar_native health component: registration + check/fixer fns."""

from __future__ import annotations

import pytest


# --- registration -----------------------------------------------------------


def test_component_and_requirements_registered():
    from work_buddy.health.components import COMPONENT_CATALOG
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY

    comp = COMPONENT_CATALOG["google_calendar_native"]
    assert comp.category == "integration"
    assert comp.health_source == "custom"
    # Component's declared requirements all exist in the registry.
    for req_id in comp.requirements:
        assert req_id in REQUIREMENT_REGISTRY
    # And they point back at the component.
    for req_id in comp.requirements:
        assert REQUIREMENT_REGISTRY[req_id].component == "google_calendar_native"


# --- requirement checks (config-time, no HTTP) ------------------------------


def test_check_client_secret(monkeypatch):
    from work_buddy.calendar import google_auth
    from work_buddy.health import requirement_checks as rc

    monkeypatch.setattr(google_auth, "token_status",
                        lambda cfg=None: {"client_secret_present": True,
                                          "client_secret_path": "/x/cs.json",
                                          "token_present": False, "token_path": "/x/t.json"})
    assert rc.check_google_oauth_client_secret()["ok"] is True

    monkeypatch.setattr(google_auth, "token_status",
                        lambda cfg=None: {"client_secret_present": False,
                                          "client_secret_path": None,
                                          "token_present": False, "token_path": "/x/t.json"})
    res = rc.check_google_oauth_client_secret()
    assert res["ok"] is False and "google_client_secret.json" in res["detail"]


def test_check_token(monkeypatch):
    from work_buddy.calendar import google_auth
    from work_buddy.health import requirement_checks as rc

    monkeypatch.setattr(google_auth, "token_status",
                        lambda cfg=None: {"token_present": True, "token_path": "/x/t.json",
                                          "client_secret_present": True, "client_secret_path": "/x/cs.json"})
    assert rc.check_google_oauth_token()["ok"] is True

    monkeypatch.setattr(google_auth, "token_status",
                        lambda cfg=None: {"token_present": False, "token_path": "/x/t.json",
                                          "client_secret_present": True, "client_secret_path": "/x/cs.json"})
    assert rc.check_google_oauth_token()["ok"] is False


# --- runtime check ----------------------------------------------------------


def test_runtime_check_uses_provider_health(monkeypatch):
    from work_buddy.calendar.providers import google_native as gn
    from work_buddy.health import checks

    monkeypatch.setattr(gn.GoogleNativeCalendarProvider, "health",
                        lambda self: {"ready": True, "calendar_count": 7})
    res = checks.check_google_calendar_native_api()
    assert res["ok"] is True and "7 calendars" in res["detail"]

    monkeypatch.setattr(gn.GoogleNativeCalendarProvider, "health",
                        lambda self: {"ready": False, "reason": "no token"})
    res = checks.check_google_calendar_native_api()
    assert res["ok"] is False and res["detail"] == "no token"


# --- fixers -----------------------------------------------------------------


def test_fix_client_secret(monkeypatch, tmp_path):
    from work_buddy import paths
    from work_buddy.health import fixers

    # Redirect the convention destination into tmp so we don't touch .data/.
    dest = tmp_path / "dest" / "google_client_secret.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    orig_resolve = paths.resolve
    monkeypatch.setattr(
        paths, "resolve",
        lambda rid: dest if rid == "credentials/google-client-secret" else orig_resolve(rid),
    )

    # Missing / nonexistent path → not ok.
    assert fixers.fix_google_oauth_client_secret(client_secret_path="")["ok"] is False
    assert fixers.fix_google_oauth_client_secret(client_secret_path=str(tmp_path / "nope.json"))["ok"] is False
    # Wrong shape (no 'installed' object) → rejected.
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert fixers.fix_google_oauth_client_secret(client_secret_path=str(bad))["ok"] is False
    # Valid Desktop-app secret → copied to the convention destination.
    good = tmp_path / "client_secret.json"
    good.write_text('{"installed": {"client_id": "x", "client_secret": "y"}}', encoding="utf-8")
    res = fixers.fix_google_oauth_client_secret(client_secret_path=str(good))
    assert res["ok"] is True and dest.exists()


def test_fix_token_reports_flow_failure(monkeypatch):
    from work_buddy.calendar import google_auth
    from work_buddy.health import fixers

    def _boom(cfg=None, **kw):
        raise RuntimeError("no browser")

    monkeypatch.setattr(google_auth, "run_oauth_flow", _boom)
    res = fixers.fix_google_oauth_token()
    assert res["ok"] is False and "OAuth flow failed" in res["detail"]

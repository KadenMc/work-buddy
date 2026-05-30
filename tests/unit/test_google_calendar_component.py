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
    assert res["ok"] is False and "GOOGLE_OAUTH_CLIENT_SECRET" in res["detail"]


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
    from work_buddy.health import fixers

    # Don't touch the real .env.
    monkeypatch.setattr(fixers, "_set_env_var",
                        lambda name, value: (True, f"set {name}", [f"wrote {name}"]))
    # Missing path → not ok.
    assert fixers.fix_google_oauth_client_secret(client_secret_path="")["ok"] is False
    assert fixers.fix_google_oauth_client_secret(client_secret_path=str(tmp_path / "nope.json"))["ok"] is False
    # Real file → ok.
    cs = tmp_path / "client_secret.json"
    cs.write_text("{}", encoding="utf-8")
    res = fixers.fix_google_oauth_client_secret(client_secret_path=str(cs))
    assert res["ok"] is True


def test_fix_token_reports_flow_failure(monkeypatch):
    from work_buddy.calendar import google_auth
    from work_buddy.health import fixers

    def _boom(cfg=None, **kw):
        raise RuntimeError("no browser")

    monkeypatch.setattr(google_auth, "run_oauth_flow", _boom)
    res = fixers.fix_google_oauth_token()
    assert res["ok"] is False and "OAuth flow failed" in res["detail"]

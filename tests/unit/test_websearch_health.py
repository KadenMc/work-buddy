"""Unit tests — websearch health/Settings wiring: component + requirement
registration, the env-or-.env key check, the fixer, and the backend probe.
"""

from __future__ import annotations

import work_buddy.health.fixers as fixers
import work_buddy.health.requirement_checks as rc
import work_buddy.secret_env as secret_env
import work_buddy.websearch.health as wshealth


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_component_registered():
    from work_buddy.health.components import COMPONENT_CATALOG
    comp = COMPONENT_CATALOG.get("websearch")
    assert comp is not None
    assert comp.is_core is False
    assert comp.health_source == "custom"
    assert comp.category == "integration"
    assert "integrations/websearch/jina-api-key" in comp.requirements
    assert comp.check_sequence[0].check_fn == "work_buddy.websearch.health.check_websearch"


def test_requirement_registered():
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY
    req = REQUIREMENT_REGISTRY.get("integrations/websearch/jina-api-key")
    assert req is not None
    assert req.component == "websearch"
    assert req.severity == "recommended"
    assert req.fix_kind == "input_required"
    assert req.fix_fn == "work_buddy.health.fixers.fix_jina_api_key"
    assert req.fix_params["api_key"]["secret"] is True
    assert req.check_fn == "work_buddy.health.requirement_checks.check_jina_api_key"


# ---------------------------------------------------------------------------
# check_jina_api_key
# ---------------------------------------------------------------------------


def test_check_jina_key_present(monkeypatch):
    monkeypatch.setattr(rc, "_cfg", lambda: {})
    monkeypatch.setattr(secret_env, "read_secret_env", lambda name: "a-key")
    out = rc.check_jina_api_key()
    assert out["ok"] is True


def test_check_jina_key_absent(monkeypatch):
    monkeypatch.setattr(rc, "_cfg", lambda: {})
    monkeypatch.setattr(secret_env, "read_secret_env", lambda name: None)
    out = rc.check_jina_api_key()
    assert out["ok"] is False and "ddgs" in out["detail"]


def test_check_jina_respects_configured_env_name(monkeypatch):
    monkeypatch.setattr(rc, "_cfg", lambda: {"websearch": {"jina": {"api_key_env": "MY_JINA"}}})
    captured = {}
    def fake_read(name):
        captured["name"] = name
        return "k"
    monkeypatch.setattr(secret_env, "read_secret_env", fake_read)
    assert rc.check_jina_api_key()["ok"] is True
    assert captured["name"] == "MY_JINA"


# ---------------------------------------------------------------------------
# fix_jina_api_key
# ---------------------------------------------------------------------------


def test_fix_jina_empty_rejected():
    assert fixers.fix_jina_api_key(api_key="")["ok"] is False


def test_fix_jina_writes_env(monkeypatch):
    seen = {}
    def fake_set(name, value):
        seen["name"] = name
        seen["value"] = value
        return True, "set", [f"wrote {name}"]
    monkeypatch.setattr(fixers, "_set_env_var", fake_set)
    out = fixers.fix_jina_api_key(api_key="  jina_abc  ")
    assert out["ok"] is True
    assert seen["name"] == "JINA_API_KEY" and seen["value"] == "jina_abc"  # trimmed


# ---------------------------------------------------------------------------
# check_websearch probe
# ---------------------------------------------------------------------------


def test_check_websearch_ok(monkeypatch):
    import work_buddy.websearch.router as router
    import work_buddy.websearch.provider as provider_mod

    class _P:
        def health(self):
            return {"ok": True}

    monkeypatch.setattr(router, "active_backend", lambda routing=None: "ddgs")
    monkeypatch.setattr(provider_mod, "get_search_provider", lambda name: _P())
    out = wshealth.check_websearch()
    assert out["ok"] is True and "ddgs" in out["detail"]


def test_check_websearch_no_backend(monkeypatch):
    import work_buddy.websearch.router as router
    monkeypatch.setattr(router, "active_backend", lambda routing=None: None)
    out = wshealth.check_websearch()
    assert out["ok"] is False

"""Unit tests for the SetupWizard orchestrator (health/wizard.py)."""

import json
import pytest

from work_buddy.health.wizard import SetupWizard


@pytest.fixture(autouse=True)
def _reset_control_graph_cache():
    """Phase G migration: guided() + preferences() now consult the control
    graph, which has a module-level TTL cache. Invalidate between tests
    so mocked-preference changes are picked up on the next graph build.
    """
    from work_buddy.control.graph import invalidate_graph
    invalidate_graph()
    yield
    invalidate_graph()


@pytest.fixture
def mock_preferences(monkeypatch):
    """Provide controllable preferences without touching config.local.yaml.

    Patches the source module (work_buddy.health.preferences) so that all
    consumers of ``from ... import is_wanted`` / ``load_preferences`` get
    the mock via the module-level reference.
    """
    _prefs = {}

    import work_buddy.health.preferences as pmod

    def _load():
        from work_buddy.health.preferences import FeaturePreference
        return {
            k: FeaturePreference(k, **v) if isinstance(v, dict) else FeaturePreference(k, wanted=v)
            for k, v in _prefs.items()
        }

    def _get(cid):
        from work_buddy.health.preferences import FeaturePreference
        p = _prefs.get(cid)
        if p is None:
            return FeaturePreference(cid)
        if isinstance(p, dict):
            return FeaturePreference(cid, **p)
        return FeaturePreference(cid, wanted=p)

    def _is_wanted(cid):
        p = _prefs.get(cid)
        if p is None:
            return None
        return p.get("wanted") if isinstance(p, dict) else p

    # Patch at the source module — all importers see the same mock
    monkeypatch.setattr(pmod, "load_preferences", _load)
    monkeypatch.setattr(pmod, "get_preference", _get)
    monkeypatch.setattr(pmod, "is_wanted", _is_wanted)

    return _prefs


class TestWizardStatus:
    def test_status_returns_three_sections(self, mock_preferences):
        wizard = SetupWizard()
        result = wizard.status()
        assert result["mode"] == "status"
        assert "bootstrap" in result
        assert "health" in result
        assert "requirements" in result

    def test_status_bootstrap_has_summary(self, mock_preferences):
        wizard = SetupWizard()
        result = wizard.status()
        bs = result["bootstrap"]
        assert "summary" in bs
        assert "results" in bs
        assert bs["summary"]["total"] > 0

    def test_status_health_has_summary(self, mock_preferences):
        wizard = SetupWizard()
        result = wizard.status()
        health = result["health"]
        assert "summary" in health
        assert "components" in health

    def test_status_requirements_has_summary(self, mock_preferences):
        wizard = SetupWizard()
        result = wizard.status()
        reqs = result["requirements"]
        assert "summary" in reqs
        assert "results" in reqs


class TestWizardGuided:
    def test_guided_returns_four_steps(self, mock_preferences):
        wizard = SetupWizard()
        result = wizard.guided()
        assert result["mode"] == "guided"
        assert len(result["steps"]) == 4
        step_names = [s["name"] for s in result["steps"]]
        assert step_names == ["bootstrap", "features", "requirements", "health"]

    def test_guided_features_step_groups_by_category(self, mock_preferences):
        """Legacy category view is still exposed on features.components for
        backward compat with any caller that hasn't migrated yet."""
        wizard = SetupWizard()
        result = wizard.guided()
        features_step = result["steps"][1]
        components = features_step["components"]
        # Should have at least some categories
        assert len(components) > 0
        # Each category should be a list
        for cat, items in components.items():
            assert isinstance(items, list)
            for item in items:
                assert "id" in item
                assert "display_name" in item
                assert "wanted" in item

    def test_guided_features_step_exposes_domains(self, mock_preferences):
        """Phase G: the features step now carries a user-facing `domains`
        list populated from the control graph."""
        wizard = SetupWizard()
        result = wizard.guided()
        features_step = result["steps"][1]
        assert "domains" in features_step
        domains = features_step["domains"]
        assert isinstance(domains, list) and len(domains) > 0
        domain_ids = [d["id"] for d in domains]
        # Canonical domain order is preserved
        assert "domain:journal" in domain_ids
        assert "domain:notifications" in domain_ids
        assert "domain:knowledge" in domain_ids

        for d in domains:
            assert set(d.keys()) >= {
                "id", "label", "description", "effective_state",
                "subsystems", "direct_components",
            }
            # Subsystems and direct_components are always lists
            assert isinstance(d["subsystems"], list)
            assert isinstance(d["direct_components"], list)

    def test_guided_journal_domain_contains_daily_notes_subsystem(self, mock_preferences):
        """Proof that the control-graph-driven view correctly threads
        subsystem:daily-notes under domain:journal."""
        wizard = SetupWizard()
        result = wizard.guided()
        domains = result["steps"][1]["domains"]
        journal = next(d for d in domains if d["id"] == "domain:journal")
        sub_ids = [s["id"] for s in journal["subsystems"]]
        assert "subsystem:daily-notes" in sub_ids

    def test_guided_includes_instructions(self, mock_preferences):
        wizard = SetupWizard()
        result = wizard.guided()
        assert "instructions" in result
        assert "Walk the user" in result["instructions"]
        # Phase G updated the prose — should mention walking by DOMAIN
        assert "DOMAIN" in result["instructions"] or "domain" in result["instructions"].lower()


class TestWizardDiagnose:
    def test_diagnose_known_component(self, mock_preferences):
        wizard = SetupWizard()
        result = wizard.diagnose("obsidian")
        assert result["mode"] == "diagnose"
        assert result["component"] == "obsidian"
        assert "preference" in result
        assert "requirements" in result
        assert "health" in result
        assert "diagnostics" in result

    def test_diagnose_unknown_component(self, mock_preferences):
        wizard = SetupWizard()
        result = wizard.diagnose("nonexistent")
        assert result["mode"] == "diagnose"
        assert "error" in result
        assert "available_components" in result

    def test_diagnose_shows_preference(self, mock_preferences):
        mock_preferences["hindsight"] = {"wanted": False, "reason": "No DB"}
        wizard = SetupWizard()
        result = wizard.diagnose("hindsight")
        assert result["preference"]["wanted"] is False
        assert result["preference"]["reason"] == "No DB"


class TestWizardPreferences:
    def test_preferences_view(self, mock_preferences):
        wizard = SetupWizard()
        result = wizard.preferences()
        assert result["mode"] == "preferences"
        assert "components" in result
        assert result["updated"] is False

    def test_preferences_lists_all_components(self, mock_preferences):
        from work_buddy.health.components import COMPONENT_CATALOG
        wizard = SetupWizard()
        result = wizard.preferences()
        ids = {c["id"] for c in result["components"]}
        for comp_id in COMPONENT_CATALOG:
            assert comp_id in ids

    def test_preferences_components_include_domains(self, mock_preferences):
        """Phase G: every component entry carries a ``domains`` list
        sourced from the control graph. Components that aren't in any
        domain's subtree get an empty list; that's fine."""
        wizard = SetupWizard()
        result = wizard.preferences()
        by_id = {c["id"]: c for c in result["components"]}

        # obsidian lives under domain:notifications and also as a dep
        # of subsystems in domain:journal — control-graph-wise its
        # grouping_parents includes domain:notifications only (subsystems
        # are not domains).
        obsidian = by_id.get("obsidian")
        assert obsidian is not None
        assert "domains" in obsidian
        assert isinstance(obsidian["domains"], list)
        # obsidian is listed as a direct child of domain:notifications
        # (per graph_static.py:children_components). So that domain must
        # appear here.
        assert "domain:notifications" in obsidian["domains"]

        # sidecar is the sole entry in domain:system
        sidecar = by_id.get("sidecar")
        assert sidecar is not None
        assert "domain:system" in sidecar["domains"]

        # category is still present for backward compat
        assert "category" in obsidian

    def test_preferences_update_flag(self, mock_preferences, monkeypatch):
        # Mock set_preference at the source module to avoid writing config
        import work_buddy.health.preferences as pmod
        from work_buddy.consent import grant_consent
        called = []
        monkeypatch.setattr(pmod, "set_preference",
                            lambda *a, **kw: called.append((a, kw)))
        # apply_preference_updates is consent-gated — grant it for the test
        grant_consent("setup.write_preferences", mode="once")
        wizard = SetupWizard()
        result = wizard.preferences(updates={"hindsight": {"wanted": False}})
        assert result["updated"] is True

    def test_preferences_ignores_unknown_components(self, mock_preferences, monkeypatch):
        import work_buddy.health.preferences as pmod
        from work_buddy.consent import grant_consent
        called = []
        monkeypatch.setattr(pmod, "set_preference",
                            lambda *a, **kw: called.append((a, kw)))
        grant_consent("setup.write_preferences", mode="once")
        wizard = SetupWizard()
        result = wizard.preferences(updates={"totally_fake": {"wanted": False}})
        # Should not have called set_preference for unknown component
        assert all("totally_fake" not in str(c) for c in called)

    def test_preferences_refuses_to_opt_out_core_component(
        self, mock_preferences, monkeypatch
    ):
        """Core components (sidecar/messaging/embedding/dashboard) cannot
        be opted out via apply_preference_updates. A malicious or stale
        POST that tries to set features.sidecar.wanted=false should be
        silently dropped — is_wanted() ignores such settings at read
        time anyway, so admitting them to the config would just be
        dead bytes."""
        import work_buddy.health.preferences as pmod
        from work_buddy.consent import grant_consent
        called = []
        monkeypatch.setattr(pmod, "set_preference",
                            lambda *a, **kw: called.append((a, kw)))
        grant_consent("setup.write_preferences", mode="once")

        wizard = SetupWizard()
        # Try to opt out sidecar (core) + obsidian (non-core) in one call
        wizard.preferences(updates={
            "sidecar": {"wanted": False},
            "obsidian": {"wanted": False},
        })
        # sidecar write must NOT have happened; obsidian write must have.
        any_sidecar = any("sidecar" in str(c) for c in called)
        any_obsidian = any("obsidian" in str(c) for c in called)
        assert not any_sidecar, f"sidecar write leaked: {called}"
        assert any_obsidian, f"obsidian write missing: {called}"


class TestWizardPreferencePropagation:
    """Verify that setting wanted=false affects health and requirements."""

    def test_opted_out_component_disabled_in_health(self, mock_preferences):
        mock_preferences["telegram"] = {"wanted": False}
        wizard = SetupWizard()
        result = wizard.status()
        tg = next(
            c for c in result["health"]["components"]
            if c["id"] == "telegram"
        )
        assert tg["status"] == "disabled"
        assert tg["wanted"] is False

    def test_opted_out_requirements_excluded(self, mock_preferences):
        mock_preferences["telegram"] = {"wanted": False}
        wizard = SetupWizard()
        result = wizard.status()
        tg_reqs = [
            r for r in result["requirements"]["results"]
            if r["component"] == "telegram"
        ]
        assert tg_reqs == []

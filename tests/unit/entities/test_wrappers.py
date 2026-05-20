"""Integration tests for the entity MCP wrappers.

Exercises :mod:`work_buddy.mcp_server.context_wrappers` entity_* functions
against a tmp entity store + tmp project store. Covers federated
resolution across both providers, side-effect reference recording on
resolve, consent gating, and the JSON wire shape callers see.
"""

from __future__ import annotations

import json

import pytest


# ─── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def wrappers_env(tmp_path, monkeypatch):
    """Tmp entity DB + tmp project DB + isolated consent cache.

    Both stores get fresh DBs and their `load_config` calls patched
    to return the matching db_path. The consent cache is reset so
    grants from previous tests don't leak in.
    """
    entity_db = tmp_path / "entities.db"
    project_db = tmp_path / "projects.db"
    fake_cfg = {
        "entities": {"db_path": str(entity_db)},
        "projects": {"db_path": str(project_db)},
        "vault_root": str(tmp_path / "vault"),
    }

    from work_buddy.entities import store as entity_store
    from work_buddy.projects import store as project_store

    monkeypatch.setattr(
        entity_store, "load_config", lambda *a, **k: fake_cfg,
    )
    monkeypatch.setattr(
        project_store, "load_config", lambda *a, **k: fake_cfg,
    )
    monkeypatch.setattr(
        entity_store, "_publish_entity_event", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        project_store, "_publish_project_event", lambda *a, **k: None,
    )

    # Auto-grant consent so wrappers don't block in tests. The
    # consent cache is per-session and SQLite-backed, so we
    # monkey-patch ``is_granted`` directly rather than touching the
    # underlying DB. Tests that exercise the consent gate flip the
    # auto-grant off via ``env.set_consent_granted(...)``.
    from work_buddy.consent import _cache as consent_cache

    _allowlist: set[str] = {
        "entity_create", "entity_delete", "project_create",
    }
    monkeypatch.setattr(
        consent_cache, "is_granted",
        lambda op: op in _allowlist,
    )

    from work_buddy.mcp_server import context_wrappers as cw

    class Env:
        pass

    env = Env()
    env.entities = entity_store
    env.projects = project_store
    env.cw = cw
    env.consent = consent_cache
    env.vault = tmp_path / "vault"

    def set_consent_granted(op: str, granted: bool) -> None:
        if granted:
            _allowlist.add(op)
        else:
            _allowlist.discard(op)
    env.set_consent_granted = set_consent_granted
    return env


# ─── entity_list / entity_get ───────────────────────────────────────


def test_entity_list_empty(wrappers_env):
    out = json.loads(wrappers_env.cw.entity_list())
    assert out == []


def test_entity_list_returns_created_rows(wrappers_env):
    wrappers_env.entities.create_entity("Max", tags=["person/family"])
    wrappers_env.entities.create_entity("Toronto", tags=["place"])
    out = json.loads(wrappers_env.cw.entity_list())
    names = {e["canonical_name"] for e in out}
    assert names == {"Max", "Toronto"}


def test_entity_list_tag_filter_hierarchical(wrappers_env):
    wrappers_env.entities.create_entity("Max", tags=["person/family"])
    wrappers_env.entities.create_entity("Toronto", tags=["place"])
    out = json.loads(wrappers_env.cw.entity_list(tag="person"))
    names = {e["canonical_name"] for e in out}
    assert names == {"Max"}


def test_entity_get_by_name(wrappers_env):
    wrappers_env.entities.create_entity("Max McKeen", description="brother")
    out = json.loads(wrappers_env.cw.entity_get(name_or_id="Max McKeen"))
    assert out["canonical_name"] == "Max McKeen"
    assert out["description"] == "brother"
    assert "recent_references" in out and out["recent_references"] == []
    assert out["reference_count"] == 0


def test_entity_get_by_int_id_as_string(wrappers_env):
    e = wrappers_env.entities.create_entity("Max")
    out = json.loads(wrappers_env.cw.entity_get(name_or_id=str(e["id"])))
    assert out["canonical_name"] == "Max"


def test_entity_get_missing_returns_error(wrappers_env):
    out = json.loads(wrappers_env.cw.entity_get(name_or_id="Nobody"))
    assert "error" in out


def test_entity_get_numeric_string_falls_back_to_id(wrappers_env):
    """A numeric string with no name match resolves as an integer id."""
    e = wrappers_env.entities.create_entity("Max")
    out = json.loads(wrappers_env.cw.entity_get(name_or_id=str(e["id"])))
    assert out["canonical_name"] == "Max"


def test_entity_get_numeric_name_resolves_by_name_first(wrappers_env):
    """An entity literally named a number is reachable by that name —
    name resolution is tried before the integer-id fallback."""
    numeric = wrappers_env.entities.create_entity("2024")
    # Create filler rows so the numeric name does not coincide with
    # its own id by accident.
    for i in range(3):
        wrappers_env.entities.create_entity(f"filler-{i}")
    out = json.loads(wrappers_env.cw.entity_get(name_or_id="2024"))
    assert out["id"] == numeric["id"]
    assert out["canonical_name"] == "2024"


# ─── entity_create ──────────────────────────────────────────────────


def test_entity_create_basic(wrappers_env):
    out = json.loads(wrappers_env.cw.entity_create(
        canonical_name="Max McKeen",
        description="Kaden's younger brother.",
        tags=["person/family"],
        aliases=["Max"],
    ))
    assert out["canonical_name"] == "Max McKeen"
    assert len(out["tags"]) == 1
    assert len(out["aliases"]) == 1


def test_entity_create_duplicate_returns_error(wrappers_env):
    wrappers_env.cw.entity_create(canonical_name="Max")
    out = json.loads(wrappers_env.cw.entity_create(canonical_name="MAX"))
    assert "error" in out and "already exists" in out["error"]


def test_entity_create_agent_author_requires_consent(wrappers_env):
    from work_buddy.consent import ConsentRequired
    # Revoke the auto-grant for this one test.
    wrappers_env.set_consent_granted("entity_create", False)
    with pytest.raises(ConsentRequired):
        wrappers_env.cw.entity_create(
            canonical_name="Max", author="agent",
        )


def test_entity_create_user_author_skips_consent(wrappers_env):
    # Revoke the grant; user-author should still succeed.
    wrappers_env.set_consent_granted("entity_create", False)
    out = json.loads(wrappers_env.cw.entity_create(
        canonical_name="Max", author="user",
    ))
    assert out["canonical_name"] == "Max"


def test_entity_create_with_source_anchors_reference(wrappers_env):
    out = json.loads(wrappers_env.cw.entity_create(
        canonical_name="Max",
        source_path="vault://daily/2026-05-19.md",
        source_kind="document",
    ))
    refs = wrappers_env.entities.list_references(out["id"])
    assert len(refs) == 1
    assert refs[0]["source_path"] == "vault://daily/2026-05-19.md"


def test_entity_create_with_invalid_source_kind_still_creates(wrappers_env):
    """The entity is created even when the reference-anchor fails;
    the failure surfaces as a field on the response."""
    out = json.loads(wrappers_env.cw.entity_create(
        canonical_name="Max",
        source_path="vault://x.md",
        source_kind="bogus_kind",
    ))
    assert out["canonical_name"] == "Max"
    assert "reference_record_error" in out


# ─── entity_update ──────────────────────────────────────────────────


def test_entity_update_rename(wrappers_env):
    e = wrappers_env.entities.create_entity("Max")
    out = json.loads(wrappers_env.cw.entity_update(
        entity_id=e["id"], canonical_name="Maxwell",
    ))
    assert out["canonical_name"] == "Maxwell"


def test_entity_update_clear_description_via_empty_string(wrappers_env):
    e = wrappers_env.entities.create_entity("Max", description="initial")
    out = json.loads(wrappers_env.cw.entity_update(
        entity_id=e["id"], description="",
    ))
    assert out["description"] is None


def test_entity_update_collision_returns_error(wrappers_env):
    a = wrappers_env.entities.create_entity("Max")
    wrappers_env.entities.create_entity("Maxwell")
    out = json.loads(wrappers_env.cw.entity_update(
        entity_id=a["id"], canonical_name="Maxwell",
    ))
    assert "error" in out


def test_entity_update_missing_returns_error(wrappers_env):
    out = json.loads(wrappers_env.cw.entity_update(
        entity_id=9999, canonical_name="X",
    ))
    assert "error" in out


def test_entity_update_with_source_records_reference(wrappers_env):
    e = wrappers_env.entities.create_entity("Max")
    wrappers_env.cw.entity_update(
        entity_id=e["id"], description="updated",
        source_path="chat://session-x", source_kind="chat",
    )
    refs = wrappers_env.entities.list_references(e["id"])
    assert len(refs) == 1


# ─── entity_delete ──────────────────────────────────────────────────


def test_entity_delete_requires_consent(wrappers_env):
    from work_buddy.consent import ConsentRequired
    e = wrappers_env.entities.create_entity("Max")
    wrappers_env.set_consent_granted("entity_delete", False)
    with pytest.raises(ConsentRequired):
        wrappers_env.cw.entity_delete(entity_id=e["id"])


def test_entity_delete_succeeds_with_consent(wrappers_env):
    e = wrappers_env.entities.create_entity("Max")
    out = json.loads(wrappers_env.cw.entity_delete(entity_id=e["id"]))
    assert out.get("deleted") is True


def test_entity_delete_missing_returns_error(wrappers_env):
    out = json.loads(wrappers_env.cw.entity_delete(entity_id=9999))
    assert "error" in out


# ─── tags + aliases ─────────────────────────────────────────────────


def test_entity_set_tags(wrappers_env):
    e = wrappers_env.entities.create_entity("Max")
    out = json.loads(wrappers_env.cw.entity_set_tags(
        entity_id=e["id"], tags=["person/family", "place/work"],
    ))
    assert {t["tag_norm"] for t in out["tags"]} == {
        "person/family", "place/work",
    }


def test_entity_set_tags_collapses_ancestor(wrappers_env):
    """Ancestor collapse applies through the capability wrapper —
    person is dropped when person/family is in the same set."""
    e = wrappers_env.entities.create_entity("Max")
    out = json.loads(wrappers_env.cw.entity_set_tags(
        entity_id=e["id"], tags=["person", "person/family"],
    ))
    assert {t["tag_norm"] for t in out["tags"]} == {"person/family"}


def test_entity_add_alias_and_remove(wrappers_env):
    e = wrappers_env.entities.create_entity("Max McKeen")
    out = json.loads(wrappers_env.cw.entity_add_alias(
        entity_id=e["id"], alias="Max",
    ))
    assert {a["alias_norm"] for a in out["aliases"]} == {"max"}
    out2 = json.loads(wrappers_env.cw.entity_remove_alias(
        entity_id=e["id"], alias="Max",
    ))
    assert out2["aliases"] == []


def test_entity_add_alias_collision_returns_error(wrappers_env):
    a = wrappers_env.entities.create_entity("Maxwell")
    b = wrappers_env.entities.create_entity("Max McKeen")
    out = json.loads(wrappers_env.cw.entity_add_alias(
        entity_id=b["id"], alias="Maxwell",
    ))
    assert "error" in out
    assert "collides" in out["error"] or "belongs" in out["error"]
    # Suppress unused-warning noise.
    del a


# ─── References (explicit + list) ───────────────────────────────────


def test_entity_add_reference_explicit(wrappers_env):
    e = wrappers_env.entities.create_entity("Max")
    out = json.loads(wrappers_env.cw.entity_add_reference(
        entity_id=e["id"],
        source_path="vault://note.md",
        source_kind="document",
        snippet="met with Max",
    ))
    assert "reference_id" in out


def test_entity_add_reference_invalid_kind_returns_error(wrappers_env):
    e = wrappers_env.entities.create_entity("Max")
    out = json.loads(wrappers_env.cw.entity_add_reference(
        entity_id=e["id"], source_path="vault://x", source_kind="bogus",
    ))
    assert "error" in out


def test_entity_list_references(wrappers_env):
    e = wrappers_env.entities.create_entity("Max")
    wrappers_env.cw.entity_add_reference(
        entity_id=e["id"], source_path="vault://x", source_kind="document",
    )
    out = json.loads(wrappers_env.cw.entity_list_references(
        entity_id=e["id"],
    ))
    assert out["count"] == 1
    assert out["entity_id"] == e["id"]


# ─── entity_resolve — federation ────────────────────────────────────


def test_entity_resolve_returns_empty_when_no_match(wrappers_env):
    out = json.loads(wrappers_env.cw.entity_resolve(query="Nobody"))
    assert out["matches"] == []
    assert out["ambiguous"] is False


def test_entity_resolve_finds_entity(wrappers_env):
    wrappers_env.entities.create_entity(
        "Max McKeen",
        description="Kaden's brother.",
        tags=["person/family"],
        aliases=["Max"],
    )
    out = json.loads(wrappers_env.cw.entity_resolve(query="Max"))
    assert len(out["matches"]) == 1
    m = out["matches"][0]
    assert m["provider"] == "entities"
    assert m["name"] == "Max McKeen"
    assert m["kind"] == "person"
    assert "Max" in m["aliases"]


def test_entity_resolve_finds_project(wrappers_env):
    # Create a project directly via the store (skipping the consent
    # wrapper, since this test isn't about consent).
    wrappers_env.projects.upsert_project(
        "ecg-inquiry", "ECG Inquiry",
        status="active", description="research project",
    )
    out = json.loads(wrappers_env.cw.entity_resolve(query="ecg-inquiry"))
    assert len(out["matches"]) == 1
    m = out["matches"][0]
    assert m["provider"] == "projects"
    assert m["kind"] == "project"
    assert m["id"] == "ecg-inquiry"


def test_entity_resolve_federation_returns_both_providers(wrappers_env):
    """A name that exists in both providers surfaces twice, with the
    ambiguous flag set."""
    wrappers_env.entities.create_entity(
        "ecg-inquiry", description="entity stub",
    )
    wrappers_env.projects.upsert_project(
        "ecg-inquiry", "ECG Inquiry",
        status="active", description="project",
    )
    out = json.loads(wrappers_env.cw.entity_resolve(query="ecg-inquiry"))
    providers = {m["provider"] for m in out["matches"]}
    assert providers == {"entities", "projects"}
    assert out["ambiguous"] is True


def test_entity_resolve_records_reference_when_source_supplied(wrappers_env):
    e = wrappers_env.entities.create_entity("Max")
    wrappers_env.cw.entity_resolve(
        query="Max",
        source_path="chat://session-1",
        source_kind="chat",
    )
    refs = wrappers_env.entities.list_references(e["id"])
    assert len(refs) == 1
    assert refs[0]["source_path"] == "chat://session-1"


def test_entity_resolve_dedup_within_window(wrappers_env):
    """Calling resolve twice in the same source within the de-dup
    window should not produce two references."""
    e = wrappers_env.entities.create_entity("Max")
    wrappers_env.cw.entity_resolve(
        query="Max", source_path="chat://x", source_kind="chat",
    )
    wrappers_env.cw.entity_resolve(
        query="Max", source_path="chat://x", source_kind="chat",
    )
    assert wrappers_env.entities.count_references(e["id"]) == 1


def test_entity_resolve_no_reference_when_only_project_matches(wrappers_env):
    """References belong to the entity store, not to the project
    registry — a project-only match should NOT record an entity
    reference even when source_path is supplied."""
    wrappers_env.projects.upsert_project(
        "ecg-inquiry", "ECG Inquiry",
        status="active",
    )
    out = json.loads(wrappers_env.cw.entity_resolve(
        query="ecg-inquiry",
        source_path="vault://x", source_kind="document",
    ))
    assert len(out["matches"]) == 1
    assert out["matches"][0]["provider"] == "projects"
    # No entity rows exist, so list_entities is empty — there's
    # nowhere a reference could have been recorded.
    assert wrappers_env.entities.list_entities() == []


def test_entity_resolve_omits_side_effect_without_source(wrappers_env):
    e = wrappers_env.entities.create_entity("Max")
    wrappers_env.cw.entity_resolve(query="Max")
    assert wrappers_env.entities.count_references(e["id"]) == 0


def test_entity_resolve_continues_when_a_provider_raises(
    wrappers_env, monkeypatch,
):
    """A misbehaving provider must not break the federation. Patch
    the projects provider to raise; the entities-provider match
    should still come through."""
    wrappers_env.entities.create_entity("Max")

    def _broken(_q):
        raise RuntimeError("synthetic provider failure")

    monkeypatch.setattr(
        wrappers_env.cw, "_entity_provider_projects", _broken,
    )
    # Re-register in the providers list (it's a list of fn refs).
    monkeypatch.setattr(
        wrappers_env.cw, "_RESOLUTION_PROVIDERS",
        [wrappers_env.cw._entity_provider_entities, _broken],
    )
    out = json.loads(wrappers_env.cw.entity_resolve(query="Max"))
    assert len(out["matches"]) == 1
    assert out["matches"][0]["provider"] == "entities"

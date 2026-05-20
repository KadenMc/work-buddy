"""Unit tests for :mod:`work_buddy.entities.store`.

Covers CRUD, normalization, alias collision detection, hierarchical
tag filtering, the append-only reference index, and the de-dup window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ─── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def entity_env(tmp_path, monkeypatch):
    """Temp entity DB with config redirected.

    Returns the live :mod:`work_buddy.entities.store` module rebound to
    a fresh tmp DB. The fixture also nukes any pre-existing SQLite WAL
    sidecars on cleanup (pytest gives us a fresh tmp_path per test, so
    no extra work needed for the DB itself).
    """
    db_path = tmp_path / "entities.db"
    fake_cfg = {"entities": {"db_path": str(db_path)}}

    from work_buddy.entities import store as entity_store
    monkeypatch.setattr(
        entity_store, "load_config", lambda *a, **k: fake_cfg,
    )

    # Suppress the dashboard event-bus side effect so tests don't
    # depend on a running messaging service.
    monkeypatch.setattr(
        entity_store, "_publish_entity_event", lambda *a, **k: None,
    )

    class Env:
        pass

    env = Env()
    env.store = entity_store
    env.db_path = db_path
    return env


# ─── Normalization ──────────────────────────────────────────────────


def test_normalize_name_lowercases_and_collapses_whitespace(entity_env):
    _normalize_name = entity_env.store._normalize_name
    assert _normalize_name("Max McKeen") == "max mckeen"
    assert _normalize_name("  Max   McKeen  ") == "max mckeen"
    assert _normalize_name("SickKids") == "sickkids"
    assert _normalize_name("") == ""


def test_normalize_tag_collapses_slashes_and_lowercases(entity_env):
    _normalize_tag = entity_env.store._normalize_tag
    assert _normalize_tag("person") == "person"
    assert _normalize_tag("Person/Family") == "person/family"
    assert _normalize_tag("Person//Family/") == "person/family"
    assert _normalize_tag("/place/work/") == "place/work"
    assert _normalize_tag("   ") == ""


# ─── Create + get ───────────────────────────────────────────────────


def test_create_entity_basic(entity_env):
    store = entity_env.store
    e = store.create_entity("Max McKeen")
    assert e["canonical_name"] == "Max McKeen"
    assert e["canonical_norm"] == "max mckeen"
    assert e["description"] is None
    assert e["author"] == "user"
    assert e["tags"] == []
    assert e["aliases"] == []
    assert "created_at" in e and "updated_at" in e


def test_create_entity_with_tags_and_aliases_and_description(entity_env):
    store = entity_env.store
    e = store.create_entity(
        "Max McKeen",
        description="Kaden's younger brother.",
        tags=["person", "Person/Family"],
        aliases=["Max"],
        author="agent",
    )
    assert e["description"] == "Kaden's younger brother."
    assert e["author"] == "agent"
    tag_norms = {t["tag_norm"] for t in e["tags"]}
    assert tag_norms == {"person", "person/family"}
    alias_norms = {a["alias_norm"] for a in e["aliases"]}
    assert alias_norms == {"max"}


def test_create_entity_duplicate_canonical_norm_raises(entity_env):
    store = entity_env.store
    store.create_entity("Max McKeen")
    with pytest.raises(ValueError, match="already exists"):
        store.create_entity("  max   mckeen  ")  # same norm


def test_create_entity_empty_name_raises(entity_env):
    store = entity_env.store
    with pytest.raises(ValueError, match="empty"):
        store.create_entity("   ")


def test_create_entity_invalid_author_raises(entity_env):
    store = entity_env.store
    with pytest.raises(ValueError, match="author"):
        store.create_entity("X", author="bot")


def test_get_entity_by_id(entity_env):
    store = entity_env.store
    e = store.create_entity("Max McKeen")
    fetched = store.get_entity(e["id"])
    assert fetched is not None
    assert fetched["id"] == e["id"]
    assert fetched["canonical_name"] == "Max McKeen"


def test_get_entity_by_canonical_name(entity_env):
    store = entity_env.store
    store.create_entity("Max McKeen")
    e = store.get_entity("Max McKeen")
    assert e is not None and e["canonical_name"] == "Max McKeen"


def test_get_entity_case_insensitive(entity_env):
    store = entity_env.store
    store.create_entity("Max McKeen")
    e = store.get_entity("max mckeen")
    assert e is not None


def test_get_entity_by_alias(entity_env):
    store = entity_env.store
    store.create_entity("Max McKeen", aliases=["Max"])
    e = store.get_entity("Max")
    assert e is not None and e["canonical_name"] == "Max McKeen"


def test_get_entity_missing_returns_none(entity_env):
    store = entity_env.store
    assert store.get_entity("Nobody") is None
    assert store.get_entity(9999) is None


# ─── Update ─────────────────────────────────────────────────────────


def test_update_entity_rename(entity_env):
    store = entity_env.store
    e = store.create_entity("Max McKeen")
    updated = store.update_entity(e["id"], canonical_name="Maxwell McKeen")
    assert updated is not None
    assert updated["canonical_name"] == "Maxwell McKeen"
    assert updated["canonical_norm"] == "maxwell mckeen"
    # Old name no longer resolves
    assert store.resolve_name("Max McKeen") is None


def test_update_entity_rename_collision_raises(entity_env):
    store = entity_env.store
    a = store.create_entity("Max McKeen")
    store.create_entity("Maxwell McKeen")
    with pytest.raises(ValueError, match="collides"):
        store.update_entity(a["id"], canonical_name="Maxwell McKeen")


def test_update_entity_description_only(entity_env):
    store = entity_env.store
    e = store.create_entity("Max McKeen", description="initial")
    updated = store.update_entity(e["id"], description="changed")
    assert updated["description"] == "changed"
    assert updated["canonical_name"] == "Max McKeen"


def test_update_entity_clear_description_with_explicit_none(entity_env):
    store = entity_env.store
    e = store.create_entity("Max McKeen", description="initial")
    updated = store.update_entity(e["id"], description=None)
    assert updated["description"] is None


def test_update_entity_missing_returns_none(entity_env):
    store = entity_env.store
    assert store.update_entity(9999, description="x") is None


# ─── Delete ─────────────────────────────────────────────────────────


def test_delete_entity_no_refs(entity_env):
    store = entity_env.store
    e = store.create_entity("Max McKeen", tags=["person"], aliases=["Max"])
    assert store.delete_entity(e["id"]) is True
    assert store.get_entity(e["id"]) is None


def test_delete_entity_cascades_references(entity_env):
    store = entity_env.store
    e = store.create_entity("Max McKeen")
    store.record_reference(
        e["id"], "chat://session-x", "chat", dedup_window_seconds=0,
    )
    assert store.count_references(e["id"]) == 1
    assert store.delete_entity(e["id"]) is True
    # Cascade removes the reference row too.
    conn = store.get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM entity_references "
            "WHERE entity_id = ?",
            (e["id"],),
        ).fetchone()
        assert row["n"] == 0
    finally:
        conn.close()


def test_delete_entity_missing_returns_false(entity_env):
    store = entity_env.store
    assert store.delete_entity(9999) is False


# ─── Tags ───────────────────────────────────────────────────────────


def test_set_tags_replaces_full_set(entity_env):
    store = entity_env.store
    e = store.create_entity("Max", tags=["person", "person/family"])
    updated = store.set_tags(e["id"], ["person/colleague"])
    norms = {t["tag_norm"] for t in updated["tags"]}
    assert norms == {"person/colleague"}


def test_set_tags_dedup_within_input(entity_env):
    store = entity_env.store
    e = store.create_entity("X")
    updated = store.set_tags(
        e["id"], ["person", "Person", "  person/family  "],
    )
    norms = {t["tag_norm"] for t in updated["tags"]}
    assert norms == {"person", "person/family"}


def test_set_tags_missing_entity_returns_none(entity_env):
    store = entity_env.store
    assert store.set_tags(9999, ["person"]) is None


# ─── Aliases ────────────────────────────────────────────────────────


def test_add_alias_then_resolve(entity_env):
    store = entity_env.store
    e = store.create_entity("Max McKeen")
    store.add_alias(e["id"], "Max")
    assert store.resolve_name("max") == e["id"]


def test_add_alias_collision_with_canonical_raises(entity_env):
    store = entity_env.store
    store.create_entity("Maxwell")
    e = store.create_entity("Max McKeen")
    with pytest.raises(ValueError, match="collides with canonical"):
        store.add_alias(e["id"], "Maxwell")


def test_add_alias_collision_with_other_entity_alias_raises(entity_env):
    store = entity_env.store
    a = store.create_entity("Max McKeen")
    b = store.create_entity("Maxine Eldritch")
    store.add_alias(a["id"], "M")
    with pytest.raises(ValueError, match="already belongs"):
        store.add_alias(b["id"], "M")


def test_add_alias_same_entity_idempotent(entity_env):
    store = entity_env.store
    e = store.create_entity("Max McKeen")
    store.add_alias(e["id"], "Max")
    # No raise; alias set unchanged.
    fetched = store.add_alias(e["id"], "Max")
    norms = {a["alias_norm"] for a in fetched["aliases"]}
    assert norms == {"max"}


def test_remove_alias(entity_env):
    store = entity_env.store
    e = store.create_entity("Max McKeen", aliases=["Max"])
    updated = store.remove_alias(e["id"], "Max")
    assert updated["aliases"] == []


# ─── Resolution ─────────────────────────────────────────────────────


def test_resolve_canonical_wins_over_alias(entity_env):
    """If a name is canonical for one entity and an alias on another,
    the canonical owner wins resolution."""
    store = entity_env.store
    a = store.create_entity("Max")  # canonical: "Max"
    b = store.create_entity("Maxwell McKeen")
    # "Max" cannot become an alias on b — it collides with a's canonical.
    with pytest.raises(ValueError):
        store.add_alias(b["id"], "Max")
    assert store.resolve_name("Max") == a["id"]


# ─── List + tag filter ──────────────────────────────────────────────


def test_list_entities_empty(entity_env):
    store = entity_env.store
    assert store.list_entities() == []


def test_list_entities_sorted_by_updated_at_desc(entity_env):
    store = entity_env.store
    a = store.create_entity("Alice")
    b = store.create_entity("Bob")
    # Touch a so it becomes the most-recently-updated.
    store.update_entity(a["id"], description="hi")
    listed = store.list_entities()
    names = [e["canonical_name"] for e in listed]
    assert names[0] == "Alice"
    assert "Bob" in names


def test_list_entities_hierarchical_tag_filter(entity_env):
    store = entity_env.store
    p = store.create_entity("Max", tags=["person/family"])
    q = store.create_entity("Erica", tags=["person/colleague"])
    r = store.create_entity("Toronto", tags=["place"])
    # ``person`` matches both ``person/family`` and ``person/colleague``.
    persons = {e["canonical_name"] for e in store.list_entities(tag="person")}
    assert persons == {"Max", "Erica"}
    # Exact ``person/family`` only matches Max.
    family = {
        e["canonical_name"]
        for e in store.list_entities(tag="person/family")
    }
    assert family == {"Max"}
    # ``place`` matches only Toronto.
    places = {e["canonical_name"] for e in store.list_entities(tag="place")}
    assert places == {"Toronto"}
    # Unrelated tag is empty.
    assert store.list_entities(tag="vehicle") == []
    # Suppress unused-warning noise.
    del p, q, r


def test_list_entities_limit(entity_env):
    store = entity_env.store
    for i in range(5):
        store.create_entity(f"E{i}")
    assert len(store.list_entities(limit=3)) == 3


# ─── References ─────────────────────────────────────────────────────


def test_record_reference_appends(entity_env):
    store = entity_env.store
    e = store.create_entity("Max McKeen")
    rid = store.record_reference(
        e["id"], "vault://daily/2026-05-19.md", "document",
        snippet="met with Max", dedup_window_seconds=0,
    )
    assert rid is not None
    refs = store.list_references(e["id"])
    assert len(refs) == 1
    assert refs[0]["source_path"] == "vault://daily/2026-05-19.md"
    assert refs[0]["snippet"] == "met with Max"


def test_record_reference_invalid_kind_raises(entity_env):
    store = entity_env.store
    e = store.create_entity("X")
    with pytest.raises(ValueError, match="source_kind"):
        store.record_reference(e["id"], "anything", "bogus")


def test_record_reference_missing_entity_returns_none(entity_env):
    store = entity_env.store
    assert store.record_reference(9999, "x", "document") is None


def test_record_reference_dedup_window_skips_duplicate(entity_env):
    store = entity_env.store
    e = store.create_entity("Max")
    a = store.record_reference(
        e["id"], "chat://session-1", "chat", dedup_window_seconds=3600,
    )
    b = store.record_reference(
        e["id"], "chat://session-1", "chat", dedup_window_seconds=3600,
    )
    assert a == b  # Returns the existing row's id.
    assert store.count_references(e["id"]) == 1


def test_record_reference_dedup_zero_window_always_inserts(entity_env):
    store = entity_env.store
    e = store.create_entity("Max")
    store.record_reference(
        e["id"], "chat://x", "chat", dedup_window_seconds=0,
    )
    store.record_reference(
        e["id"], "chat://x", "chat", dedup_window_seconds=0,
    )
    assert store.count_references(e["id"]) == 2


def test_record_reference_outside_window_inserts(entity_env):
    """A reference older than the window should not dedup against new
    observations — we pass an explicit ``occurred_at`` to simulate
    time passing without sleeping."""
    store = entity_env.store
    e = store.create_entity("Max")
    old_ts = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat(timespec="milliseconds")
    store.record_reference(
        e["id"], "vault://note.md", "document",
        occurred_at=old_ts, dedup_window_seconds=3600,
    )
    # New observation now should NOT dedup against the 2-hour-old row.
    store.record_reference(
        e["id"], "vault://note.md", "document",
        dedup_window_seconds=3600,
    )
    assert store.count_references(e["id"]) == 2


def test_record_reference_different_source_path_does_not_dedup(entity_env):
    store = entity_env.store
    e = store.create_entity("Max")
    store.record_reference(
        e["id"], "chat://a", "chat", dedup_window_seconds=3600,
    )
    store.record_reference(
        e["id"], "chat://b", "chat", dedup_window_seconds=3600,
    )
    assert store.count_references(e["id"]) == 2


def test_record_reference_touches_entity_updated_at(entity_env):
    store = entity_env.store
    e = store.create_entity("Max")
    initial_updated = e["updated_at"]
    # Use a future occurred_at so the touched updated_at strictly differs.
    future_ts = (
        datetime.now(timezone.utc) + timedelta(seconds=1)
    ).isoformat(timespec="milliseconds")
    store.record_reference(
        e["id"], "vault://x.md", "document",
        occurred_at=future_ts, dedup_window_seconds=0,
    )
    fetched = store.get_entity(e["id"])
    assert fetched["updated_at"] >= initial_updated


def test_list_references_default_limit(entity_env):
    store = entity_env.store
    e = store.create_entity("Max")
    for i in range(75):
        ts = (
            datetime.now(timezone.utc) + timedelta(seconds=i)
        ).isoformat(timespec="milliseconds")
        store.record_reference(
            e["id"], f"vault://note-{i}.md", "document",
            occurred_at=ts, dedup_window_seconds=0,
        )
    default_limited = store.list_references(e["id"])
    assert len(default_limited) == 50
    assert store.count_references(e["id"]) == 75
    assert len(store.list_references(e["id"], limit=None)) == 75


# ─── Migration framework integration ────────────────────────────────


def test_migrations_run_on_first_connection(entity_env):
    """A fresh DB is migrated to the latest version on first
    ``get_connection()`` call. The runner is cheap on subsequent calls."""
    store = entity_env.store
    conn = store.get_connection()
    try:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 1  # current ENTITY_MIGRATIONS target
    finally:
        conn.close()


def test_foreign_keys_enabled(entity_env):
    store = entity_env.store
    conn = store.get_connection()
    try:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
    finally:
        conn.close()

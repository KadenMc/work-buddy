"""Tests for the entity registry HTTP API in ``work_buddy.dashboard.service``.

Exercises the Flask routes end-to-end against a tmp entity DB, covering
the round trip the Memory tab's JS makes.
"""

from __future__ import annotations

import pytest


# ─── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """Flask test client with a tmp entity store wired in."""
    entity_db = tmp_path / "entities.db"
    fake_cfg = {"entities": {"db_path": str(entity_db)}}

    from work_buddy.entities import store as entity_store
    monkeypatch.setattr(
        entity_store, "load_config", lambda *a, **k: fake_cfg,
    )
    monkeypatch.setattr(
        entity_store, "_publish_entity_event", lambda *a, **k: None,
    )

    from work_buddy.dashboard.service import app
    app.config["TESTING"] = True
    client = app.test_client()

    class Env:
        pass

    env = Env()
    env.client = client
    env.store = entity_store
    return env


# ─── Schema ─────────────────────────────────────────────────────────


def test_schema_endpoint(api_client):
    resp = api_client.client.get("/api/entities/_schema")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "authors" in body and "source_kinds" in body
    assert "user" in body["authors"]
    assert "document" in body["source_kinds"]


# ─── Tag autocomplete endpoint ──────────────────────────────────────


def test_tags_endpoint_empty(api_client):
    resp = api_client.client.get("/api/entities/tags")
    assert resp.status_code == 200
    assert resp.get_json()["tags"] == []


def test_tags_endpoint_returns_aggregated_nodes(api_client):
    api_client.store.create_entity("A", tags=["person/family"])
    api_client.store.create_entity("B", tags=["person/family"])
    api_client.store.create_entity("C", tags=["person/colleague"])
    resp = api_client.client.get("/api/entities/tags")
    assert resp.status_code == 200
    nodes = {n["path"]: n for n in resp.get_json()["tags"]}
    # Intermediate `person` node aggregates its subtree.
    assert nodes["person"]["count"] == 3
    assert nodes["person"]["is_literal"] is False
    assert nodes["person/family"]["count"] == 2
    assert nodes["person/family"]["is_literal"] is True


def test_tags_endpoint_not_shadowed_by_int_route(api_client):
    """``/api/entities/tags`` must resolve to the tag endpoint, not be
    swallowed by ``/api/entities/<int:entity_id>`` (it is not an int,
    so the converter rejects it — this pins that)."""
    resp = api_client.client.get("/api/entities/tags")
    assert resp.status_code == 200
    assert "tags" in resp.get_json()


# ─── List + filter ──────────────────────────────────────────────────


def test_list_empty(api_client):
    resp = api_client.client.get("/api/entities")
    assert resp.status_code == 200
    assert resp.get_json()["entities"] == []


def test_list_returns_created(api_client):
    api_client.store.create_entity("Max", tags=["person/family"])
    api_client.store.create_entity("Toronto", tags=["place"])
    body = api_client.client.get("/api/entities").get_json()
    names = {e["canonical_name"] for e in body["entities"]}
    assert names == {"Max", "Toronto"}


def test_list_tag_filter_hierarchical(api_client):
    api_client.store.create_entity("Max", tags=["person/family"])
    api_client.store.create_entity("Toronto", tags=["place"])
    body = api_client.client.get("/api/entities?tag=person").get_json()
    names = {e["canonical_name"] for e in body["entities"]}
    assert names == {"Max"}


# ─── Detail ─────────────────────────────────────────────────────────


def test_detail_missing_404(api_client):
    resp = api_client.client.get("/api/entities/9999")
    assert resp.status_code == 404


def test_detail_returns_full_payload(api_client):
    e = api_client.store.create_entity(
        "Max", description="brother", tags=["person/family"],
        aliases=["M"],
    )
    api_client.store.record_reference(
        e["id"], "vault://note.md", "document",
        dedup_window_seconds=0,
    )
    body = api_client.client.get(f"/api/entities/{e['id']}").get_json()
    assert body["canonical_name"] == "Max"
    assert body["description"] == "brother"
    assert len(body["tags"]) == 1
    assert len(body["aliases"]) == 1
    assert body["reference_count"] == 1
    assert len(body["recent_references"]) == 1


# ─── Create ─────────────────────────────────────────────────────────


def test_create_minimal(api_client):
    resp = api_client.client.post("/api/entities", json={
        "canonical_name": "Max McKeen",
    })
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["canonical_name"] == "Max McKeen"
    assert body["author"] == "user"


def test_create_with_tags_and_aliases(api_client):
    resp = api_client.client.post("/api/entities", json={
        "canonical_name": "Max McKeen",
        "description": "brother",
        "tags": ["person/family"],
        "aliases": ["Max"],
    })
    assert resp.status_code == 201
    body = resp.get_json()
    assert len(body["tags"]) == 1
    assert len(body["aliases"]) == 1


def test_create_empty_name_400(api_client):
    resp = api_client.client.post("/api/entities", json={
        "canonical_name": "  ",
    })
    assert resp.status_code == 400


def test_create_duplicate_400(api_client):
    api_client.client.post("/api/entities", json={"canonical_name": "Max"})
    resp = api_client.client.post("/api/entities", json={"canonical_name": "MAX"})
    assert resp.status_code == 400
    assert "already exists" in resp.get_json()["error"]


# ─── Update ─────────────────────────────────────────────────────────


def test_update_rename(api_client):
    e = api_client.store.create_entity("Max")
    resp = api_client.client.patch(f"/api/entities/{e['id']}", json={
        "canonical_name": "Maxwell",
    })
    assert resp.status_code == 200
    assert resp.get_json()["canonical_name"] == "Maxwell"


def test_update_description_clear_via_empty_string(api_client):
    e = api_client.store.create_entity("Max", description="initial")
    resp = api_client.client.patch(f"/api/entities/{e['id']}", json={
        "description": "",
    })
    assert resp.status_code == 200
    assert resp.get_json()["description"] is None


def test_update_missing_404(api_client):
    resp = api_client.client.patch("/api/entities/9999", json={"description": "x"})
    assert resp.status_code == 404


def test_update_collision_400(api_client):
    a = api_client.store.create_entity("Max")
    api_client.store.create_entity("Maxwell")
    resp = api_client.client.patch(f"/api/entities/{a['id']}", json={
        "canonical_name": "Maxwell",
    })
    assert resp.status_code == 400


# ─── Delete ─────────────────────────────────────────────────────────


def test_delete(api_client):
    e = api_client.store.create_entity("Max")
    resp = api_client.client.delete(f"/api/entities/{e['id']}")
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] is True
    # Gone.
    assert api_client.client.get(f"/api/entities/{e['id']}").status_code == 404


def test_delete_missing_404(api_client):
    resp = api_client.client.delete("/api/entities/9999")
    assert resp.status_code == 404


# ─── Tags ───────────────────────────────────────────────────────────


def test_set_tags_replaces_set(api_client):
    e = api_client.store.create_entity("Max", tags=["person/family"])
    resp = api_client.client.post(f"/api/entities/{e['id']}/tags", json={
        "tags": ["person/colleague", "institution"],
    })
    assert resp.status_code == 200
    norms = {t["tag_norm"] for t in resp.get_json()["tags"]}
    assert norms == {"person/colleague", "institution"}


def test_set_tags_collapses_ancestor(api_client):
    """The store's ancestor collapse applies through the HTTP route —
    POSTing person + person/family stores only person/family."""
    e = api_client.store.create_entity("Max")
    resp = api_client.client.post(f"/api/entities/{e['id']}/tags", json={
        "tags": ["person", "person/family"],
    })
    assert resp.status_code == 200
    norms = {t["tag_norm"] for t in resp.get_json()["tags"]}
    assert norms == {"person/family"}


def test_set_tags_clear(api_client):
    e = api_client.store.create_entity("Max", tags=["person/family"])
    resp = api_client.client.post(f"/api/entities/{e['id']}/tags", json={
        "tags": [],
    })
    assert resp.status_code == 200
    assert resp.get_json()["tags"] == []


def test_set_tags_not_a_list_400(api_client):
    e = api_client.store.create_entity("Max")
    resp = api_client.client.post(f"/api/entities/{e['id']}/tags", json={
        "tags": "person",
    })
    assert resp.status_code == 400


# ─── Aliases ────────────────────────────────────────────────────────


def test_add_alias_then_remove(api_client):
    e = api_client.store.create_entity("Max McKeen")
    add_resp = api_client.client.post(f"/api/entities/{e['id']}/aliases", json={
        "alias": "Max",
    })
    assert add_resp.status_code == 200
    assert {a["alias_norm"] for a in add_resp.get_json()["aliases"]} == {"max"}
    rm_resp = api_client.client.delete(
        f"/api/entities/{e['id']}/aliases", json={"alias": "Max"},
    )
    assert rm_resp.status_code == 200
    assert rm_resp.get_json()["aliases"] == []


def test_add_alias_missing_alias_400(api_client):
    e = api_client.store.create_entity("Max")
    resp = api_client.client.post(f"/api/entities/{e['id']}/aliases", json={
        "alias": "  ",
    })
    assert resp.status_code == 400


def test_add_alias_collision_400(api_client):
    api_client.store.create_entity("Maxwell")
    b = api_client.store.create_entity("Max McKeen")
    resp = api_client.client.post(f"/api/entities/{b['id']}/aliases", json={
        "alias": "Maxwell",
    })
    assert resp.status_code == 400


# ─── References ─────────────────────────────────────────────────────


def test_add_reference_explicit(api_client):
    e = api_client.store.create_entity("Max")
    resp = api_client.client.post(f"/api/entities/{e['id']}/references", json={
        "source_path": "vault://x.md",
        "source_kind": "document",
        "snippet": "met with Max",
    })
    assert resp.status_code == 201
    assert "reference_id" in resp.get_json()


def test_add_reference_missing_required_400(api_client):
    e = api_client.store.create_entity("Max")
    resp = api_client.client.post(f"/api/entities/{e['id']}/references", json={
        "source_path": "vault://x.md",
    })
    assert resp.status_code == 400


def test_add_reference_invalid_kind_400(api_client):
    e = api_client.store.create_entity("Max")
    resp = api_client.client.post(f"/api/entities/{e['id']}/references", json={
        "source_path": "vault://x.md",
        "source_kind": "bogus",
    })
    assert resp.status_code == 400


def test_list_references(api_client):
    e = api_client.store.create_entity("Max")
    api_client.client.post(f"/api/entities/{e['id']}/references", json={
        "source_path": "vault://x.md",
        "source_kind": "document",
    })
    resp = api_client.client.get(f"/api/entities/{e['id']}/references")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 1
    assert body["total"] == 1


def test_list_references_limit_param(api_client):
    e = api_client.store.create_entity("Max")
    for i in range(5):
        api_client.client.post(f"/api/entities/{e['id']}/references", json={
            "source_path": f"vault://note-{i}.md",
            "source_kind": "document",
        })
    resp = api_client.client.get(f"/api/entities/{e['id']}/references?limit=2")
    body = resp.get_json()
    assert body["count"] == 2
    assert body["total"] == 5

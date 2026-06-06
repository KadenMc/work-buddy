"""The vault_search / vault_index gateway dispatchers."""
from __future__ import annotations

import json

from work_buddy.mcp_server.ops import vault_ops


def _hit(**over):
    base = {
        "doc_id": "d1", "score": 0.5, "bm25_score": 0.4, "dense_score": 0.6,
        "source": "vault_index", "display_text": "some ecg text",
        "metadata": {"source_path": "v/a.md", "heading_path": ["Methods"], "vault_id": "v"},
    }
    base.update(over)
    return base


def test_search_formats_markdown(monkeypatch):
    from work_buddy.embedding import client
    monkeypatch.setattr(client, "vault_search", lambda *a, **k: [_hit()])
    out = vault_ops._vault_search_dispatch("ecg")
    assert "[vault] v/a.md" in out and "Methods" in out
    assert "bm25=0.400" in out and "dense=0.600" in out


def test_search_empty_query():
    assert vault_ops._vault_search_dispatch("   ") == "No query provided."


def test_search_degrades_to_lexical_when_service_down(monkeypatch):
    from work_buddy.embedding import client
    import work_buddy.vault_index.search as vsearch

    monkeypatch.setattr(client, "vault_search", lambda *a, **k: None)  # service down
    monkeypatch.setattr(
        vsearch, "search",
        lambda q, **k: [_hit(dense_score=0.0)] if k.get("method") == "lexical" else [],
    )
    out = vault_ops._vault_search_dispatch("ecg")
    assert "degraded" in out and "[vault] v/a.md" in out


def test_index_status_action(monkeypatch):
    import work_buddy.vault_index.status as vstatus
    monkeypatch.setattr(vstatus, "index_status", lambda cfg=None: {"status": "ok", "total_chunks": 5})
    out = json.loads(vault_ops._vault_index_dispatch(action="status"))
    assert out["total_chunks"] == 5


def test_index_build_skips_when_locked(monkeypatch):
    from work_buddy.utils import index_lock
    monkeypatch.setattr(index_lock, "is_locked", lambda target: True)
    out = json.loads(vault_ops._vault_index_dispatch(action="build"))
    assert out["skipped"] is True and out["reason"] == "build_in_progress"


def test_index_build_posts_to_service_when_unlocked(monkeypatch):
    from work_buddy.embedding import client
    from work_buddy.utils import index_lock

    monkeypatch.setattr(index_lock, "is_locked", lambda target: False)
    called = {}

    def fake_vault_index(action="build", force=False):
        called["action"] = action
        called["force"] = force
        return {"files_new": 0, "vectors": {"vectors_new": 0}}

    monkeypatch.setattr(client, "vault_index", fake_vault_index)
    out = json.loads(vault_ops._vault_index_dispatch(action="build", force=True))
    assert called == {"action": "build", "force": True}  # POSTed in-service, not run locally
    assert "files_new" in out


def test_index_build_surfaces_unreachable_service(monkeypatch):
    from work_buddy.embedding import client
    from work_buddy.utils import index_lock

    monkeypatch.setattr(index_lock, "is_locked", lambda target: False)
    monkeypatch.setattr(client, "vault_index", lambda **k: None)  # service unreachable
    out = json.loads(vault_ops._vault_index_dispatch(action="build"))
    assert "error" in out and "unreachable" in out["error"].lower()

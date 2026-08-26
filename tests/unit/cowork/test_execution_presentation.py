"""Co-work uses catalog labels without mutating default or pinned authority."""

from __future__ import annotations

from work_buddy.agent_execution import registry
from work_buddy.agent_execution.models import AgentExecutionSelection
from work_buddy.conversations import execution, store
from work_buddy.cowork import conversations


def test_raw_default_and_saved_pin_display_catalog_labels_without_writes(
    client,
    seeded,
    fake_document_agent,
    monkeypatch,
):
    raw = AgentExecutionSelection("codex", "gpt-5.6-sol", "codex", "gpt-5.6-sol")
    monkeypatch.setattr(registry, "default_selection", lambda: raw)
    url = (
        f"/api/truth/doc/{seeded['document'].id}/conversation"
        f"?store_id={seeded['store_id']}"
    )
    unbound = client.get(url)
    assert unbound.status_code == 200, unbound.get_json()
    payload = unbound.get_json()
    assert payload["conversation_id"] is None
    assert payload["execution"]["selection"] == {
        "schema_version": 1,
        "provider_id": "codex",
        "model_id": "gpt-5.6-sol",
        "provider_label": "Codex",
        "model_label": "GPT-5.6 Sol",
        "revision": "",
        "persisted": False,
    }
    assert raw.provider_label == "codex" and raw.model_label == "gpt-5.6-sol"

    binding = conversations.ensure_document_conversation(
        document_id=seeded["document"].id,
        store_id=seeded["store_id"],
    )
    pinned = execution.set_execution(
        binding.conversation_id,
        raw.to_dict(),
        expected_revision=None,
    )
    with store.get_connection() as conn:
        before = conn.execute(
            "SELECT metadata FROM conversations WHERE conversation_id=?",
            (binding.conversation_id,),
        ).fetchone()["metadata"]
    bound = client.get(url)
    assert bound.status_code == 200, bound.get_json()
    selected = bound.get_json()["execution"]["selection"]
    assert selected == {
        **pinned.to_dict(),
        "provider_label": "Codex",
        "model_label": "GPT-5.6 Sol",
    }
    with store.get_connection() as conn:
        after = conn.execute(
            "SELECT metadata FROM conversations WHERE conversation_id=?",
            (binding.conversation_id,),
        ).fetchone()["metadata"]
        assert (
            conn.execute("SELECT COUNT(*) FROM conversation_agent_leases").fetchone()[0]
            == 0
        )
    assert after == before
    assert fake_document_agent == []

"""find → consolidated-index re-point (drill=False), reusing the shared IR-consumer helper
with consumer="find". Verifies the per-consumer gate, find_op routing + IR fallback, and
that drill=True never routes to the consolidated index."""
from __future__ import annotations

import importlib

from work_buddy.index.config import IndexConfig
from work_buddy.mcp_server.ops import context_consolidated as cc
from work_buddy.mcp_server.ops.search_ops import find_op

# ``work_buddy.ir.search`` is re-exported as a function in the package namespace, so a
# string monkeypatch path resolves to the function, not the module. Grab the real module.
_ir_mod = importlib.import_module("work_buddy.ir.search")


def _cfg(monkeypatch, **consumers):
    monkeypatch.setattr(
        "work_buddy.index.config.load_index_config",
        lambda *a, **k: IndexConfig(enabled=True, consumers=consumers),
    )


_HIT = {
    "doc_id": "conversation:s1#2", "score": 0.8,
    "signals": {"fused": 0.8, "lexical": 0.3, "default": 0.6},
    "display_text": "span text", "metadata": {"session_id": "s1"},
}


class TestHelperConsumerParam:
    def test_find_flag_gates_independently(self, monkeypatch):
        # context_search ON but find OFF → the consumer="find" gate is closed.
        _cfg(monkeypatch, context_search=True)
        assert cc.search_context_via_consolidated(
            "q", source="conversation", consumer="find") is None

    def test_find_flag_on_passes_gate(self, monkeypatch):
        _cfg(monkeypatch, find=True)
        monkeypatch.setattr("work_buddy.embedding.client.index_search", lambda q, **k: [_HIT])
        out = cc.search_context_via_consolidated("q", source="conversation", consumer="find")
        assert out and out[0]["source"] == "conversation"
        assert out[0]["doc_id"] == "conversation:s1#2"


class TestFindRouting:
    def test_drill_false_routes_when_find_on(self, monkeypatch):
        _cfg(monkeypatch, find=True)
        monkeypatch.setattr("work_buddy.embedding.client.index_search", lambda q, **k: [_HIT])

        def boom(*a, **k):
            raise AssertionError("should not hit the IR engine when routed")
        monkeypatch.setattr(_ir_mod, "search", boom)
        out = find_op("q", source="conversation", drill=False)
        assert isinstance(out, list) and out[0]["source"] == "conversation"

    def test_drill_false_falls_back_when_find_off(self, monkeypatch):
        _cfg(monkeypatch)  # find off
        sentinel = [{"doc_id": "x", "score": 1.0, "source": "conversation",
                     "display_text": "", "metadata": {}}]
        called = {}

        def fake_ir(q, **k):
            called["ir"] = True
            return sentinel
        monkeypatch.setattr(_ir_mod, "search", fake_ir)
        out = find_op("q", source="conversation", drill=False)
        assert called.get("ir") and out is sentinel

    def test_drill_false_unsupported_subset_falls_back(self, monkeypatch):
        _cfg(monkeypatch, find=True)
        called = {}

        def fake_ir(q, **k):
            called["ir"] = True
            return []
        monkeypatch.setattr(_ir_mod, "search", fake_ir)
        find_op("q", source=None, drill=False)  # all-source is out of the subset → IR
        assert called.get("ir")

    def test_drill_true_never_routes_to_consolidated(self, monkeypatch):
        _cfg(monkeypatch, find=True)

        def boom(*a, **k):
            raise AssertionError("drill=True must not route to the consolidated index")
        monkeypatch.setattr(
            "work_buddy.mcp_server.ops.context_consolidated.search_context_via_consolidated",
            boom,
        )
        monkeypatch.setattr(_ir_mod, "search", lambda q, **k: [])
        monkeypatch.setattr(
            "work_buddy.summarization.drill_registry.get_drill_handler", lambda s: None)
        out = find_op("q", source="conversation", drill=True)
        assert isinstance(out, dict) and "stage1_hits" in out  # funnel shape, helper untouched

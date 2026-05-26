"""Tests for the `walk` op — canonical short name for `drill_tree`.

Both ops share the same callable (`drill_tree_op`); these tests verify
the binding via the op registry and that the capability declaration
loads cleanly.
"""

from __future__ import annotations

import time

import pytest

# Trigger the disclosure_ops registration side effect — these tests verify
# the side effect produced the expected `op.wb.walk` and `op.wb.drill_tree`
# entries in the op registry.
import work_buddy.mcp_server.ops.disclosure_ops  # noqa: F401
from work_buddy.summarization import Provenance, SummaryNode
from work_buddy.summarization.stores import DurableSummaryStore


@pytest.fixture
def tmp_summarization_db(monkeypatch, tmp_path):
    from work_buddy.summarization import db as db_mod

    db_file = tmp_path / "summarization.db"
    monkeypatch.setattr(db_mod, "_default_db_path", lambda: db_file)
    monkeypatch.setattr(db_mod, "db_path", lambda cfg=None: db_file)
    return db_file


def _prov():
    return Provenance(
        model="m", backend="b", profile=None,
        generated_at=Provenance.now_iso(),
        prompt_version=1, summary_schema_version=1,
        selection_version=1, cache_version=1,
    )


def test_walk_op_registered():
    from work_buddy.mcp_server.op_registry import get_op

    assert get_op("op.wb.walk") is not None


def test_walk_and_drill_tree_share_callable():
    from work_buddy.mcp_server.op_registry import get_op

    drill = get_op("op.wb.drill_tree")
    walk = get_op("op.wb.walk")
    assert drill is walk  # exact same callable object


def test_walk_callable_returns_view(tmp_summarization_db):
    """End-to-end smoke: invoking the walk callable returns a view dict
    equivalent to drill_tree."""
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    children = [SummaryNode(
        summary="topic body", source_ref=None,
        extra={"title": "Topic"},
    )]
    root = SummaryNode(summary="tldr", source_ref=None, children=children)
    store.save("sess-walk-1", root, _prov(), str(time.time()))

    from work_buddy.mcp_server.op_registry import get_op

    walk = get_op("op.wb.walk")
    drill = get_op("op.wb.drill_tree")

    out_walk = walk("summary", "conversation_session:sess-walk-1", "summary")
    out_drill = drill(
        "summary", "conversation_session:sess-walk-1", "summary",
    )
    assert out_walk == out_drill
    assert out_walk["domain"] == "summary"
    assert out_walk["summary_text"] == "tldr"
    assert len(out_walk["children"]) == 1

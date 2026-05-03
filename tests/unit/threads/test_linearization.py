"""v5 Stage 4.7 — write-time sub-thread linearization.

Pins:
- Linearization writes order_index on every sibling at spawn time.
- Render time NEVER recomputes — list query reads order_index.
- Embedding-service unavailable → falls back to creation-order.
- decompose calls linearize_after_spawn.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.threads import decompose, linearization, store
from work_buddy.threads.models import ContextItem, Thread


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


def _items(*labels):
    return [
        ContextItem(id=f"item-{i}", source="test", type="x", label=l)
        for i, l in enumerate(labels)
    ]


# ---------------------------------------------------------------------------
# linearize_siblings
# ---------------------------------------------------------------------------


class TestLinearizeSiblings:
    def test_no_op_for_zero_or_one_sibling(self, fresh_db):
        out = linearization.linearize_siblings("nonexistent")
        assert out == []

        # One child
        p = Thread()
        store.insert_thread(p)
        c = Thread(parent_id=p.thread_id)
        store.insert_thread(c)
        out = linearization.linearize_siblings(p.thread_id)
        assert out == [c.thread_id]
        # order_index set
        fetched = store.get_thread(c.thread_id)
        assert fetched.order_index == 0

    def test_three_siblings_with_embeddings_assigned_indices(self, fresh_db):
        p = Thread()
        store.insert_thread(p)
        children = []
        for i, label in enumerate(("alpha", "beta", "gamma")):
            c = Thread(
                parent_id=p.thread_id,
                context_items=(
                    ContextItem(id=f"ci-{i}", source="x", type="y",
                                label=label, payload={}),
                ),
            )
            store.insert_thread(c)
            children.append(c)

        # Stub embeddings — three orthogonal vectors so seriation
        # has a deterministic input.
        embeddings = [
            [1.0, 0.0, 0.0],
            [0.5, 0.5, 0.0],
            [0.0, 1.0, 0.0],
        ]
        with patch.object(linearization, "_embed_texts",
                          return_value=embeddings):
            order = linearization.linearize_siblings(p.thread_id)
        assert len(order) == 3
        # All three children should be present in the order
        assert set(order) == {c.thread_id for c in children}
        # Each child's order_index matches its position in the order
        for idx, tid in enumerate(order):
            assert store.get_thread(tid).order_index == idx

    def test_embedding_failure_falls_back_to_creation_order(self, fresh_db):
        p = Thread()
        store.insert_thread(p)
        children = []
        for i, label in enumerate(("first", "second", "third")):
            c = Thread(
                parent_id=p.thread_id,
                context_items=(
                    ContextItem(id=f"ci-{i}", source="x", type="y",
                                label=label, payload={}),
                ),
            )
            store.insert_thread(c)
            children.append(c)

        with patch.object(linearization, "_embed_texts", return_value=None):
            order = linearization.linearize_siblings(p.thread_id)

        # All three present; order matches creation order
        assert order == [c.thread_id for c in children]
        for idx, tid in enumerate(order):
            assert store.get_thread(tid).order_index == idx


# ---------------------------------------------------------------------------
# decompose integration
# ---------------------------------------------------------------------------


class TestDecomposeIntegration:
    def test_decompose_writes_order_index(self, fresh_db):
        p = Thread()
        store.insert_thread(p)
        # Force fallback (no embedding) so we don't depend on
        # the embedding service in tests.
        with patch.object(linearization, "_embed_texts", return_value=None):
            ids = decompose.decompose_thread(
                p.thread_id, _items("a", "b", "c"),
            )
        # Three children, three contiguous order_indexes 0/1/2
        children = store.list_threads(parent_id=p.thread_id)
        idxs = sorted(c.order_index for c in children)
        assert idxs == [0, 1, 2]

    def test_decompose_failure_in_linearization_does_not_break_spawn(
        self, fresh_db
    ):
        p = Thread()
        store.insert_thread(p)
        # Make linearize_after_spawn raise
        with patch.object(linearization, "linearize_after_spawn",
                          side_effect=RuntimeError("oh no")):
            ids = decompose.decompose_thread(p.thread_id, _items("x", "y"))
        # Children still exist
        assert len(store.list_threads(parent_id=p.thread_id)) == 2


# ---------------------------------------------------------------------------
# list_threads renders by order_index
# ---------------------------------------------------------------------------


class TestListByOrderIndex:
    def test_sub_threads_listed_by_order_index(self, fresh_db):
        p = Thread()
        store.insert_thread(p)
        # Insert in non-order_index order
        for i, oi in enumerate([2, 0, 1]):
            c = Thread(parent_id=p.thread_id, order_index=oi)
            store.insert_thread(c)
        children = store.list_threads(parent_id=p.thread_id)
        assert [c.order_index for c in children] == [0, 1, 2]

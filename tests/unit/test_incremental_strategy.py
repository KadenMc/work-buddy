"""Unit tests for the v2 incremental refresh path.

Covers:
- `IncrementalLayeredStrategy.parse` — output shape
- `IncrementalLayeredStrategy.is_finalized` — distance-based heuristic
- `DurableSummaryStore.apply_incremental` — merge semantics
- `refresh_one_incremental` — end-to-end with stub LLM
- `build_incremental_prompt` — prompt assembly
"""

from __future__ import annotations

import json

import pytest

from work_buddy.summarization import (
    Provenance,
    SummaryNode,
)
from work_buddy.summarization.protocol import SummaryCapability
from work_buddy.summarization.strategies import IncrementalLayeredStrategy
from work_buddy.summarization.stores import DurableSummaryStore
from work_buddy.summarization.incremental import (
    _compute_finalized_count,
    build_incremental_prompt,
    refresh_one_incremental,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    from work_buddy.summarization import db as db_mod

    db_file = tmp_path / "incremental-test.db"
    monkeypatch.setattr(db_mod, "_default_db_path", lambda: db_file)
    monkeypatch.setattr(db_mod, "db_path", lambda cfg=None: db_file)
    return db_file


def _prov(model: str = "stub") -> Provenance:
    return Provenance(
        model=model, backend="stub", profile="p",
        generated_at=Provenance.now_iso(),
        prompt_version=1, summary_schema_version=1,
        selection_version=1, cache_version=1,
    )


# ---------------------------------------------------------------------------
# Strategy: parse output shape
# ---------------------------------------------------------------------------


def test_strategy_parse_initial_full_session():
    """No prior topics → model emits N topics covering the whole session."""
    strat = IncrementalLayeredStrategy()
    output = {
        "tldr": "Wrote a PRD for conversation summarization v2.",
        "activity_kind": "planning",
        "trailing_and_new_topics": [
            {
                "title": "Problem framing",
                "summary": "Discussed shortcomings of v1.",
                "span_range": [0, 12],
                "keywords": ["v1 shortcomings", "topic cap", "truncation"],
            },
            {
                "title": "Design discussion",
                "summary": "Settled on incremental refresh.",
                "span_range": [13, 30],
                "keywords": ["incremental", "cooldown", "model chain"],
            },
        ],
    }
    node = strat.parse(output, "raw content")

    assert node.summary == "Wrote a PRD for conversation summarization v2."
    assert node.extra["activity_kind"] == "planning"
    assert node.extra["_emitted_count"] == 2
    assert len(node.children) == 2
    assert node.children[0].extra["title"] == "Problem framing"
    assert node.children[0].extra["span_start"] == 0
    assert node.children[0].extra["span_end"] == 12
    assert node.children[0].source_ref == {"span_start": 0, "span_end": 12}
    assert node.children[1].extra["title"] == "Design discussion"


def test_strategy_parse_missing_activity_kind_defaults_unknown():
    strat = IncrementalLayeredStrategy()
    output = {
        "tldr": "X",
        "trailing_and_new_topics": [],
    }
    node = strat.parse(output, "")
    assert node.extra["activity_kind"] == "unknown"
    assert node.extra["_emitted_count"] == 0
    assert node.children == []


def test_strategy_parse_rejects_non_dict():
    strat = IncrementalLayeredStrategy()
    from work_buddy.summarization.protocol import SummarizationError
    with pytest.raises(SummarizationError):
        strat.parse(None, "")
    with pytest.raises(SummarizationError):
        strat.parse([], "")


def test_strategy_capabilities_declared():
    strat = IncrementalLayeredStrategy()
    assert SummaryCapability.LAYERED in strat.capabilities
    assert SummaryCapability.INCREMENTAL in strat.capabilities


# ---------------------------------------------------------------------------
# Strategy: finalization heuristic
# ---------------------------------------------------------------------------


def test_is_finalized_distance_heuristic():
    """Default: 10 turns past `span_end` finalizes the topic."""
    strat = IncrementalLayeredStrategy()
    # Topic ending at turn 20, total turns = 30 → 30 - 1 - 20 = 9 turns past → NOT finalized
    assert not strat.is_finalized(20, 30)
    # Total turns = 31 → 31 - 1 - 20 = 10 → FINALIZED (>= 10)
    assert strat.is_finalized(20, 31)
    # Total turns = 100 → way past → finalized
    assert strat.is_finalized(20, 100)


def test_is_finalized_configurable():
    strat = IncrementalLayeredStrategy()
    strat.finalization_distance_turns = 5
    assert not strat.is_finalized(10, 14)  # 14-1-10 = 3 turns past
    assert strat.is_finalized(10, 16)  # 16-1-10 = 5 turns past


def test_compute_finalized_count():
    strat = IncrementalLayeredStrategy()
    # Three topics ending at 10, 20, 30. Total turns = 50.
    # Topic 0 (end=10): 50-1-10 = 39 turns past → finalized
    # Topic 1 (end=20): 29 past → finalized
    # Topic 2 (end=30): 19 past → finalized
    topics = [
        SummaryNode(summary="t1", extra={"span_end": 10}),
        SummaryNode(summary="t2", extra={"span_end": 20}),
        SummaryNode(summary="t3", extra={"span_end": 30}),
    ]
    assert _compute_finalized_count(topics, 50, strat) == 3

    # Total = 35 → topic 2 is only 4 past → NOT finalized.
    # Topic 0 = 24 past, finalized. Topic 1 = 14 past, finalized. Topic 2 = 4 past, not.
    assert _compute_finalized_count(topics, 35, strat) == 2

    # Total = 12 → only topic 0 is finalized? 12-1-10 = 1, not finalized.
    # All trailing.
    assert _compute_finalized_count(topics, 12, strat) == 0


def test_compute_finalized_count_missing_span_end():
    """If a topic is missing span_end, stop counting (safer default)."""
    strat = IncrementalLayeredStrategy()
    topics = [
        SummaryNode(summary="t1", extra={"span_end": 10}),
        SummaryNode(summary="t2", extra={}),  # missing
        SummaryNode(summary="t3", extra={"span_end": 30}),
    ]
    # Topic 0 finalizes (50-1-10=39). Topic 1 missing span_end → stop.
    assert _compute_finalized_count(topics, 50, strat) == 1


# ---------------------------------------------------------------------------
# Store: apply_incremental merge semantics
# ---------------------------------------------------------------------------


def test_apply_incremental_no_prior(tmp_db):
    """First refresh: prior is empty → new_root saved whole."""
    store = DurableSummaryStore("ns_a")
    store.set_strategy_versions(1, 1)
    new_root = SummaryNode(
        summary="initial tldr",
        children=[
            SummaryNode(
                summary="topic 1", source_ref={"span_start": 0, "span_end": 5},
                extra={"title": "T1", "span_start": 0, "span_end": 5, "topic_index": 0},
            ),
        ],
        extra={"activity_kind": "planning"},
    )
    store.apply_incremental("item-1", new_root, 0, _prov(), "tok-1")

    loaded = store.load("item-1")
    assert loaded.summary == "initial tldr"
    assert len(loaded.children) == 1
    assert loaded.children[0].extra["title"] == "T1"
    assert loaded.children[0].extra["topic_index"] == 0


def test_apply_incremental_merge_finalized_with_new(tmp_db):
    """Existing 3 topics, 2 finalized → merge keeps first 2 + replaces with new."""
    store = DurableSummaryStore("ns_a")
    store.set_strategy_versions(1, 1)

    # Save initial 3-topic tree.
    existing = SummaryNode(
        summary="tldr v1",
        children=[
            SummaryNode(summary="topic A", extra={"title": "A", "topic_index": 0, "span_start": 0, "span_end": 5}),
            SummaryNode(summary="topic B", extra={"title": "B", "topic_index": 1, "span_start": 6, "span_end": 12}),
            SummaryNode(summary="topic C (trailing)", extra={"title": "C", "topic_index": 2, "span_start": 13, "span_end": 20}),
        ],
        extra={},
    )
    store.save("item-1", existing, _prov(), "tok-1")

    # Now apply incremental: finalized=2 (A, B); new emitted: C-updated + D.
    new = SummaryNode(
        summary="tldr v2",
        children=[
            SummaryNode(summary="topic C updated", extra={"title": "C", "topic_index": 0, "span_start": 13, "span_end": 25}),  # extended
            SummaryNode(summary="topic D", extra={"title": "D", "topic_index": 1, "span_start": 26, "span_end": 35}),
        ],
        extra={"activity_kind": "implementation"},
    )
    store.apply_incremental("item-1", new, 2, _prov(), "tok-2")

    loaded = store.load("item-1")
    assert loaded.summary == "tldr v2"
    assert len(loaded.children) == 4  # A + B + C-updated + D
    assert [c.extra["title"] for c in loaded.children] == ["A", "B", "C", "D"]
    # topic_index re-stamped 0..N-1
    assert [c.extra["topic_index"] for c in loaded.children] == [0, 1, 2, 3]
    # The C update is in place (extended span)
    assert loaded.children[2].extra["span_end"] == 25
    assert loaded.children[2].summary == "topic C updated"


def test_apply_incremental_v2_meta_persisted(tmp_db):
    """`apply_incremental` writes the v2 metadata columns on summary_items."""
    store = DurableSummaryStore("ns_a")
    store.set_strategy_versions(1, 1)
    new = SummaryNode(
        summary="tldr",
        children=[SummaryNode(summary="t1", extra={"title": "T1", "topic_index": 0})],
    )
    v2_meta = {
        "total_turns": 50,
        "last_finalized_boundary": 20,
        "truncated": 0,
        "activity_kind": "debugging",
        "pathway": "single-call",
        "chunks_used": 1,
        "model_chain": ["claude-haiku-4-5"],
        "models_actually_used": ["claude-haiku-4-5"],
        "escalation_triggered": 0,
        "escalation_reason": None,
    }
    store.apply_incremental("item-1", new, 0, _prov(), "tok-1", v2_meta=v2_meta)

    meta = store.load_item_meta("item-1")
    assert meta["total_turns"] == 50
    assert meta["last_finalized_boundary"] == 20
    assert meta["activity_kind"] == "debugging"
    assert meta["pathway"] == "single-call"
    assert json.loads(meta["model_chain"]) == ["claude-haiku-4-5"]


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def test_build_incremental_prompt_no_prior():
    text = build_incremental_prompt(
        finalized=[],
        trailing=None,
        fresh_text="[turn 0 | user]\nhello",
        fresh_from_turn=0,
        total_turns=1,
    )
    assert "(none yet" in text
    assert "(none — emit topics covering the new turns from scratch)" in text
    assert "[turn 0 | user]" in text
    assert "from turn 0" in text


def test_build_incremental_prompt_with_finalized_and_trailing():
    finalized = [
        SummaryNode(
            summary="discussed PRD",
            extra={"title": "PRD discussion", "span_start": 0, "span_end": 10, "keywords": ["prd", "design"]},
        ),
    ]
    trailing = SummaryNode(
        summary="started implementation",
        extra={"title": "Implementation", "span_start": 11, "span_end": 25, "keywords": ["code", "tests"]},
    )
    text = build_incremental_prompt(
        finalized=finalized,
        trailing=trailing,
        fresh_text="[turn 26 | assistant]\nrun tests",
        fresh_from_turn=26,
        total_turns=27,
    )
    assert "PRD discussion" in text
    assert "(IMMUTABLE context only" in text
    assert "Implementation" in text
    assert "MUTABLE" in text
    assert "[turn 26 | assistant]" in text


# ---------------------------------------------------------------------------
# End-to-end: refresh_one_incremental with stub source + stub LLM
# ---------------------------------------------------------------------------


class _StubSource:
    """Minimal source for testing the incremental orchestrator path."""
    name = "stub_source"
    capabilities = frozenset()

    def __init__(self, total: int, fresh_text: str = "fresh turns text"):
        self._total = total
        self._fresh_text = fresh_text

    def discover(self, window):
        return [("item-1", "tok-1")]

    def render(self, item_id):
        return self._fresh_text  # ignored by incremental path

    def render_batch(self, item_ids):
        return [self._fresh_text for _ in item_ids]

    def total_turns(self, item_id):
        return self._total

    def render_from(self, item_id, from_turn):
        return self._fresh_text


def _stub_llm_returning(output: dict):
    """Build an LLMCaller that always returns the given structured_output."""
    from work_buddy.summarization.protocol import LLMCallResult

    class _Stub:
        def call(self, *, system, user, output_schema=None, profile=None,
                 max_tokens=None, trace_id=None):
            return LLMCallResult(
                structured_output=output,
                content=json.dumps(output),
                model="stub-model",
                backend="stub",
            )
    return _Stub()


def test_refresh_one_incremental_initial(tmp_db):
    """First refresh: no prior topics. Whole session goes through; one LLM call."""
    from work_buddy.summarization.summarizer import Summarizer

    source = _StubSource(total=15)
    strategy = IncrementalLayeredStrategy()
    store = DurableSummaryStore("ns_test")
    s = Summarizer(name="t", source=source, strategy=strategy, store=store)

    llm = _stub_llm_returning({
        "tldr": "Discussed a feature in 15 turns.",
        "activity_kind": "planning",
        "trailing_and_new_topics": [
            {
                "title": "Initial discussion",
                "summary": "Discussed scope.",
                "span_range": [0, 14],
                "keywords": ["scope", "v1"],
            },
        ],
    })

    node = refresh_one_incremental(
        s, "item-1",
        freshness_token="tok-1",
        llm_caller=llm,
    )

    assert node is not None
    assert node.summary == "Discussed a feature in 15 turns."
    assert len(node.children) == 1
    assert node.children[0].extra["span_end"] == 14

    # Verify persistence
    loaded = store.load("item-1")
    assert loaded.summary == "Discussed a feature in 15 turns."

    # Verify v2 meta
    meta = store.load_item_meta("item-1")
    assert meta["total_turns"] == 15
    assert meta["pathway"] == "single-call"
    assert meta["activity_kind"] == "planning"


def test_refresh_one_incremental_with_prior_finalized(tmp_db):
    """Prior 3 topics, 2 are finalized (distance 10). Refresh emits trailing+new."""
    from work_buddy.summarization.summarizer import Summarizer

    # Total turns = 50; topics ending at 10, 20, 30 → all finalized.
    # But we want 2 finalized and 1 trailing — adjust distance.
    source = _StubSource(total=35)
    strategy = IncrementalLayeredStrategy()
    strategy.finalization_distance_turns = 10
    # With total=35: topic[end=10]: 35-1-10=24 ≥ 10 finalized. end=20: 14 ≥ 10 finalized.
    # end=30: 4 < 10 NOT finalized. So 2 finalized, 1 trailing. Good.
    store = DurableSummaryStore("ns_test")
    s = Summarizer(name="t", source=source, strategy=strategy, store=store)

    # Save prior state
    existing = SummaryNode(
        summary="tldr v1",
        children=[
            SummaryNode(
                summary="A", source_ref={"span_start": 0, "span_end": 10},
                extra={"title": "A", "topic_index": 0, "span_start": 0, "span_end": 10},
            ),
            SummaryNode(
                summary="B", source_ref={"span_start": 11, "span_end": 20},
                extra={"title": "B", "topic_index": 1, "span_start": 11, "span_end": 20},
            ),
            SummaryNode(
                summary="C", source_ref={"span_start": 21, "span_end": 30},
                extra={"title": "C", "topic_index": 2, "span_start": 21, "span_end": 30},
            ),
        ],
    )
    store.apply_incremental("item-1", existing, 0, _prov(), "tok-old")

    # Now an incremental refresh.
    llm = _stub_llm_returning({
        "tldr": "Three topics covered, now extended.",
        "activity_kind": "implementation",
        "trailing_and_new_topics": [
            {
                "title": "C extended",
                "summary": "C now spans more.",
                "span_range": [21, 32],
                "keywords": ["c"],
            },
            {
                "title": "D new",
                "summary": "Started D.",
                "span_range": [33, 34],
                "keywords": ["d"],
            },
        ],
    })

    node = refresh_one_incremental(
        s, "item-1",
        freshness_token="tok-new",
        llm_caller=llm,
    )

    assert node is not None
    loaded = store.load("item-1")
    # 4 children: A, B (preserved), C-extended (replaced), D (new)
    assert len(loaded.children) == 4
    titles = [c.extra["title"] for c in loaded.children]
    assert titles == ["A", "B", "C extended", "D new"]
    # A, B preserved
    assert loaded.children[0].summary == "A"
    assert loaded.children[1].summary == "B"
    # C was replaced with the extended version
    assert loaded.children[2].extra["span_end"] == 32

    # Verify v2 meta
    meta = store.load_item_meta("item-1")
    assert meta["total_turns"] == 35
    assert meta["last_finalized_boundary"] == 20  # max span_end of finalized


def test_refresh_one_incremental_nothing_fresh(tmp_db):
    """If `fresh_from_turn >= total_turns`, skip the LLM call entirely."""
    from work_buddy.summarization.summarizer import Summarizer

    source = _StubSource(total=20)
    strategy = IncrementalLayeredStrategy()
    strategy.finalization_distance_turns = 1
    store = DurableSummaryStore("ns_test")
    s = Summarizer(name="t", source=source, strategy=strategy, store=store)

    # All topics finalized and cover up to turn 19 (total=20, fresh_from would be 20).
    existing = SummaryNode(
        summary="all done",
        children=[
            SummaryNode(
                summary="A", extra={"title": "A", "topic_index": 0, "span_start": 0, "span_end": 19},
            ),
        ],
    )
    store.apply_incremental("item-1", existing, 0, _prov(), "tok-old")

    # An LLM caller that would CRASH if called — proves no call happened.
    class _CrashStub:
        def call(self, **kw):
            raise AssertionError("LLM should not be called when nothing fresh")

    node = refresh_one_incremental(
        s, "item-1",
        freshness_token="tok-new",
        llm_caller=_CrashStub(),
    )
    # Returns the existing (unchanged) tree.
    assert node is not None
    assert node.summary == "all done"


def test_coherence_check_incremental_requires_render_from():
    """Constructing a Summarizer with INCREMENTAL strategy but no render_from
    on the source should raise IncoherentComposition."""
    from work_buddy.summarization.summarizer import Summarizer
    from work_buddy.summarization.protocol import IncoherentComposition

    class _BadSource:
        name = "bad"
        capabilities = frozenset()
        def discover(self, window): return []
        def render(self, item_id): return None
        def render_batch(self, item_ids): return []
        # Missing: total_turns, render_from

    strategy = IncrementalLayeredStrategy()
    store = DurableSummaryStore("ns_test")
    with pytest.raises(IncoherentComposition, match="render_from"):
        Summarizer(name="t", source=_BadSource(), strategy=strategy, store=store)

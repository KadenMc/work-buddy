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


def test_strategy_parse_populates_turn_start_end_in_extra():
    """v2 regression: parse() must populate `turn_start`/`turn_end` in extra,
    not just `span_start`/`span_end`. v2's semantic treats span_start/end as
    absolute turn indices, but the adapter (session_summary_row) falls back
    to span_to_turn re-derivation when turn_* fields are missing — which
    produces (0, 0) garbage for v2 input. Caught via live-test 2026-05-28
    on dashboard /api/chats/<sid>/topics."""
    strat = IncrementalLayeredStrategy()
    output = {
        "tldr": "x",
        "activity_kind": "implementation",
        "trailing_and_new_topics": [
            {"title": "T1", "summary": "s1", "span_range": [0, 12], "keywords": []},
            {"title": "T2", "summary": "s2", "span_range": [13, 30], "keywords": []},
        ],
    }
    node = strat.parse(output, "")
    assert len(node.children) == 2
    # Critical assertion: turn_start/turn_end must mirror span_start/span_end.
    assert node.children[0].extra["turn_start"] == 0
    assert node.children[0].extra["turn_end"] == 12
    assert node.children[1].extra["turn_start"] == 13
    assert node.children[1].extra["turn_end"] == 30


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


def test_incremental_llm_failure_is_recorded_and_raised(tmp_db):
    from work_buddy.llm.response import ErrorKind
    from work_buddy.summarization.protocol import LLMCallResult, SummarizationError
    from work_buddy.summarization.summarizer import Summarizer

    source = _StubSource(total=15)
    store = DurableSummaryStore("ns_test")
    summarizer = Summarizer(
        name="t", source=source,
        strategy=IncrementalLayeredStrategy(), store=store,
    )

    class _FailingCaller:
        def call(self, **_kwargs):
            return LLMCallResult(
                error="backend timed out", error_kind=ErrorKind.TIMEOUT,
            )

    with pytest.raises(SummarizationError) as raised:
        refresh_one_incremental(
            summarizer, "item-1", freshness_token="tok-1",
            llm_caller=_FailingCaller(),
        )

    assert raised.value.error_kind is ErrorKind.TIMEOUT
    assert raised.value.recorded is True
    assert store.load_item_meta("item-1")["status"] == "error"


def test_incremental_parse_failure_is_intrinsic(tmp_db):
    from work_buddy.llm.response import ErrorKind
    from work_buddy.summarization.protocol import SummarizationError
    from work_buddy.summarization.summarizer import Summarizer

    source = _StubSource(total=15)
    store = DurableSummaryStore("ns_test")
    summarizer = Summarizer(
        name="t", source=source,
        strategy=IncrementalLayeredStrategy(), store=store,
    )

    with pytest.raises(SummarizationError) as raised:
        refresh_one_incremental(
            summarizer, "item-1", freshness_token="tok-1",
            llm_caller=_stub_llm_returning({"not": "the schema"}),
        )

    assert raised.value.error_kind is ErrorKind.MALFORMED_RESPONSE
    assert raised.value.recorded is True


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


# ---------------------------------------------------------------------------
# P3 — pathway selection + chunked pathway
# ---------------------------------------------------------------------------


def test_estimate_tokens_helper():
    from work_buddy.summarization.incremental import _estimate_tokens
    assert _estimate_tokens("") == 1  # min-1 floor
    assert _estimate_tokens("a" * 4) == 1
    assert _estimate_tokens("a" * 40) == 10
    assert _estimate_tokens("a" * 400) == 100


def test_topic_context_token_estimate():
    """Each finalized topic ≈ 30 tokens; trailing ≈ 60 tokens; +200 overhead."""
    from work_buddy.summarization.incremental import (
        _estimate_topic_context_tokens,
    )
    assert _estimate_topic_context_tokens([], None) == 200
    finalized = [SummaryNode(summary="a"), SummaryNode(summary="b")]
    assert _estimate_topic_context_tokens(finalized, None) == 260  # 200 + 60
    trailing = SummaryNode(summary="t")
    assert (
        _estimate_topic_context_tokens(finalized, trailing) == 320  # 200 + 60 + 60
    )


class _LongSource:
    """Source for chunked-pathway tests. Emits fake turn-block text per range."""
    name = "long_source"
    capabilities = frozenset()

    def __init__(self, total_turns: int, chars_per_turn: int):
        self._total = total_turns
        self._cpt = chars_per_turn

    def discover(self, window):
        return [("item-1", "tok-1")]

    def render(self, item_id):
        return self._render_block(0, self._total)

    def render_batch(self, item_ids):
        return [self.render(i) for i in item_ids]

    def total_turns(self, item_id):
        return self._total

    def render_from(self, item_id, from_turn):
        # Mirror the production cap so the probe matches reality.
        text = self._render_block(from_turn, self._total)
        # Apply the 40k char cap like the real renderer.
        if len(text) > 40_000:
            text = text[:40_000] + "\n[…truncated…]"
        return text

    def render_range(self, item_id, from_turn, to_turn):
        return self._render_block(from_turn, to_turn)

    def _render_block(self, fr, to):
        end = min(to, self._total)
        parts = []
        for i in range(fr, end):
            body = "x" * self._cpt
            parts.append(f"[turn {i} | user]\n{body}")
        return "\n\n".join(parts)


def test_pathway_selection_single_call_short_session(tmp_db):
    """Short fresh tail → single-call pathway → 1 chunks_used."""
    from work_buddy.summarization.summarizer import Summarizer

    # 5 turns × 200 chars each = ~250 tokens fresh — well under 32k budget.
    source = _LongSource(total_turns=5, chars_per_turn=200)
    strategy = IncrementalLayeredStrategy()
    store = DurableSummaryStore("ns_p3")
    s = Summarizer(name="p3", source=source, strategy=strategy, store=store)

    llm = _stub_llm_returning({
        "tldr": "short",
        "activity_kind": "planning",
        "trailing_and_new_topics": [
            {"title": "T1", "summary": "s1", "span_range": [0, 4], "keywords": []},
        ],
    })

    node = refresh_one_incremental(s, "item-1", freshness_token="tok-1", llm_caller=llm)
    assert node is not None
    meta = store.load_item_meta("item-1")
    assert meta["pathway"] == "single-call"
    assert meta["chunks_used"] == 1


def test_pathway_selection_chunked_long_session(tmp_db, monkeypatch):
    """Long fresh tail exceeding budget → chunked pathway → multiple chunks_used.

    We force the chunked path by setting a tiny budget via monkeypatching the
    default budget constant.
    """
    from work_buddy.summarization.summarizer import Summarizer
    from work_buddy.summarization import incremental as incr_mod

    # 100 turns × 200 chars = ~5000 chars = ~1250 tokens total.
    # We'll set a tiny budget so a chunk fits ~10 turns.
    source = _LongSource(total_turns=100, chars_per_turn=200)
    strategy = IncrementalLayeredStrategy()
    store = DurableSummaryStore("ns_p3_chunked")
    s = Summarizer(name="p3c", source=source, strategy=strategy, store=store)

    # Tiny budget: 500 tokens. fresh_budget = 0.85 * 500 - 200 = 225.
    # Each turn ≈ 50 tokens; turns_per_chunk = floor(225 / 50) = 4. (min 5)
    # So chunks of 5 turns; 100 turns = 20 chunks. We'll truncate the
    # test to a more reasonable number by reducing total_turns.
    source._total = 20  # 20 turns total

    # Override the default budget constant in the module to force chunking.
    original_budget = incr_mod._DEFAULT_PER_CALL_BUDGET_TOKENS
    incr_mod._DEFAULT_PER_CALL_BUDGET_TOKENS = 500
    monkeypatch.setattr(incr_mod, "_resolve_per_call_budget", lambda: 500)
    try:
        # Stub LLM returns one new topic per chunk so accumulator advances.
        call_count = {"n": 0}

        class _AdvancingStub:
            def call(self, *, system, user, output_schema=None, profile=None,
                     max_tokens=None, trace_id=None):
                from work_buddy.summarization.protocol import LLMCallResult
                call_count["n"] += 1
                n = call_count["n"]
                out = {
                    "tldr": f"after chunk {n}",
                    "activity_kind": "implementation",
                    "trailing_and_new_topics": [
                        {
                            "title": f"chunk-{n} topic",
                            "summary": f"summary {n}",
                            "span_range": [(n - 1) * 5, n * 5 - 1],
                            "keywords": [],
                        },
                    ],
                }
                return LLMCallResult(
                    structured_output=out, content=json.dumps(out),
                    model="stub", backend="stub",
                )

        node = refresh_one_incremental(
            s, "item-1", freshness_token="tok-1",
            llm_caller=_AdvancingStub(),
        )
        assert node is not None
        meta = store.load_item_meta("item-1")
        assert meta["pathway"] == "chunked"
        assert meta["chunks_used"] >= 2  # at least 2 chunks for 20 turns × 5 per chunk
        assert call_count["n"] == meta["chunks_used"]
    finally:
        incr_mod._DEFAULT_PER_CALL_BUDGET_TOKENS = original_budget


def test_chunked_pathway_records_model_used(tmp_db, monkeypatch):
    """Chunked pathway records the model that ran each chunk."""
    from work_buddy.summarization.summarizer import Summarizer
    from work_buddy.summarization import incremental as incr_mod

    source = _LongSource(total_turns=20, chars_per_turn=200)
    strategy = IncrementalLayeredStrategy()
    store = DurableSummaryStore("ns_p3_models")
    s = Summarizer(name="p3m", source=source, strategy=strategy, store=store)

    original = incr_mod._DEFAULT_PER_CALL_BUDGET_TOKENS
    incr_mod._DEFAULT_PER_CALL_BUDGET_TOKENS = 500
    monkeypatch.setattr(incr_mod, "_resolve_per_call_budget", lambda: 500)
    try:
        out = {
            "tldr": "x",
            "activity_kind": "unknown",
            "trailing_and_new_topics": [
                {"title": "t", "summary": "s", "span_range": [0, 4], "keywords": []},
            ],
        }

        class _ModelLabelingStub:
            n = 0

            def call(self_inner, **kw):
                from work_buddy.summarization.protocol import LLMCallResult
                self_inner.n += 1
                return LLMCallResult(
                    structured_output=out,
                    content=json.dumps(out),
                    model=f"stub-model-{self_inner.n}",
                    backend="stub",
                )

        node = refresh_one_incremental(
            s, "item-1", freshness_token="tok-1",
            llm_caller=_ModelLabelingStub(),
        )
        assert node is not None
        meta = store.load_item_meta("item-1")
        models = json.loads(meta["models_actually_used"])
        # At least 2 distinct stub names recorded
        assert len(models) >= 2
        assert all(m.startswith("stub-model-") for m in models)
    finally:
        incr_mod._DEFAULT_PER_CALL_BUDGET_TOKENS = original

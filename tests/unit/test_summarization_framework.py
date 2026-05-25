"""Unit tests for the summarization framework core.

Covers: the orchestrator (per-item + batch refresh paths), the `Summarizer`
composer's coherence checks, error isolation, and the `as_caller` adapter
that normalizes legacy bare-callable LLM stubs.

Uses lightweight fake `Source` / `SummaryStrategy` / `Store` implementations
in-file. Real conv_obs / Chrome composition behavior is tested in
`test_conversation_observability_summaries.py` and Chrome's own test file.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from work_buddy.summarization import (
    DiscoveryWindow,
    IncoherentComposition,
    LLMCallResult,
    Provenance,
    RefreshReport,
    SummarizationError,
    Summarizer,
    SummaryCapability,
    SummaryNode,
    as_caller,
    run_refresh,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSource:
    """Per-item fake source. Pass a `discover_items` list of `(id, token)`
    and a `render_map: dict[id, str | None]` to control behaviour."""

    def __init__(
        self,
        discover_items: list[tuple[str, Any]],
        render_map: dict[str, str | None] | None = None,
        capabilities: frozenset[SummaryCapability] = frozenset(),
    ) -> None:
        self.name = "fake_source"
        self.capabilities = capabilities
        self._discover = discover_items
        self._render = render_map or {}
        self.discover_calls = 0
        self.render_calls: list[str] = []
        self.render_batch_calls: list[list[str]] = []

    def discover(self, window: DiscoveryWindow) -> list[tuple[str, Any]]:
        self.discover_calls += 1
        return list(self._discover)

    def render(self, item_id: str) -> str | None:
        self.render_calls.append(item_id)
        if item_id in self._render:
            return self._render[item_id]
        return f"body for {item_id}"

    def render_batch(self, item_ids: list[str]) -> list[str | None]:
        self.render_batch_calls.append(list(item_ids))
        return [self.render(iid) for iid in item_ids]


class FakeStrategy:
    """Flat strategy that parses `{summary: str}` into a single-node tree."""

    def __init__(
        self,
        capabilities: frozenset[SummaryCapability] = frozenset({
            SummaryCapability.FLAT
        }),
        prompt_version: int = 1,
        schema_version: int = 1,
        raise_on_parse: bool = False,
    ) -> None:
        self.name = "fake_strategy"
        self.capabilities = capabilities
        self.prompt_version = prompt_version
        self.schema_version = schema_version
        self.system_prompt = "fake system"
        self.output_schema = {"type": "object"}
        self.batch_output_schema = {"type": "object"}
        self._raise = raise_on_parse

    def parse(
        self, structured_output: dict[str, Any] | None, raw_content: str,
    ) -> SummaryNode:
        if self._raise:
            raise SummarizationError("fake parse failure")
        if not isinstance(structured_output, dict):
            raise SummarizationError("not dict")
        return SummaryNode(
            summary=str(structured_output.get("summary", "")),
            extra=dict(structured_output),
        )

    def parse_batch(
        self,
        structured_output: dict[str, Any] | None,
        raw_content: str,
        item_ids: list[str],
    ) -> list[SummaryNode | None]:
        if not isinstance(structured_output, dict):
            return [None] * len(item_ids)
        raw = structured_output.get("summaries") or []
        by_idx = {
            e["item_index"]: e for e in raw
            if isinstance(e, dict) and "item_index" in e
        }
        return [
            self.parse(by_idx[i], "") if i in by_idx else None
            for i, _ in enumerate(item_ids)
        ]


class FakeStore:
    """In-memory store. Treats `freshness_token` as the exact-match key."""

    def __init__(
        self,
        capabilities: frozenset[SummaryCapability] = frozenset({
            SummaryCapability.PERSISTS_TREE,
            SummaryCapability.VERSION_STAMPED,
        }),
        prior: dict[str, tuple[SummaryNode, Any]] | None = None,
    ) -> None:
        self.name = "fake_store"
        self.capabilities = capabilities
        self.selection_version = 1
        self.cache_version = 1
        self._records: dict[str, tuple[SummaryNode, Any, str, str | None]] = {}
        if prior:
            for iid, (node, token) in prior.items():
                self._records[iid] = (node, token, "ok", None)
        self.save_calls: list[tuple[str, SummaryNode, Provenance, Any]] = []
        self.error_calls: list[tuple[str, str]] = []

    def is_fresh(self, item_id: str, freshness_token: Any) -> bool:
        rec = self._records.get(item_id)
        if rec is None:
            return False
        if rec[2] != "ok":
            return False
        return str(rec[1]) == str(freshness_token)

    def select_stale(
        self, candidates: list[tuple[str, Any]],
    ) -> list[tuple[str, Any]]:
        return [
            (iid, tok) for iid, tok in candidates
            if not self.is_fresh(iid, tok)
        ]

    def save(
        self,
        item_id: str,
        result: SummaryNode,
        provenance: Provenance,
        freshness_token: Any,
    ) -> None:
        self.save_calls.append((item_id, result, provenance, freshness_token))
        self._records[item_id] = (result, freshness_token, "ok", None)

    def load(self, item_id: str) -> SummaryNode | None:
        rec = self._records.get(item_id)
        return rec[0] if rec else None

    def record_error(
        self, item_id: str, error: str, provenance: Provenance,
    ) -> None:
        self.error_calls.append((item_id, error))
        prev = self._records.get(item_id)
        if prev is None:
            self._records[item_id] = (
                SummaryNode(summary=""), "", "error", error,
            )
        else:
            # Preserve prior node; flip status.
            self._records[item_id] = (prev[0], prev[1], "error", error)


def _stub_llm_dict_returner(payload: dict[str, Any]):
    """Build a legacy-shape llm_call stub that returns `payload` as a dict."""
    calls: list[dict[str, Any]] = []

    def _fn(*, system: str, user: str, output_schema=None, profile=None):
        calls.append({
            "system": system, "user": user,
            "output_schema": output_schema, "profile": profile,
        })
        return payload

    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


# ---------------------------------------------------------------------------
# 1. Framework core — per-item refresh
# ---------------------------------------------------------------------------


def test_run_refresh_summarizes_stale_items():
    source = FakeSource(
        [("a", "tok-a"), ("b", "tok-b"), ("c", "tok-c")],
    )
    store = FakeStore()
    summ = Summarizer("test", source, FakeStrategy(), store)

    stub = _stub_llm_dict_returner({"summary": "S"})
    report = summ.refresh(
        days=7, max_items=10, force=False,
        llm_caller=as_caller(stub),
    )

    assert report.summarized == 3
    assert report.errored == 0
    assert report.skipped_fresh == 0
    assert report.total_candidates == 3
    assert len(store.save_calls) == 3
    assert store.load("a") is not None
    # Stub was called for each item.
    assert len(stub.calls) == 3


def test_max_items_caps_work():
    source = FakeSource([("a", "1"), ("b", "1"), ("c", "1"), ("d", "1")])
    store = FakeStore()
    summ = Summarizer("test", source, FakeStrategy(), store)

    stub = _stub_llm_dict_returner({"summary": "S"})
    report = summ.refresh(
        days=7, max_items=2, force=False,
        llm_caller=as_caller(stub),
    )

    assert report.summarized == 2
    assert len(stub.calls) == 2
    # The two that didn't make it are still "stale" but not counted as
    # skipped_fresh (which means "non-stale" by `select_stale`).
    assert report.skipped_fresh == 0
    assert report.total_candidates == 4


def test_skips_fresh_items():
    source = FakeSource([("a", "1"), ("b", "1"), ("c", "1")])
    pre = SummaryNode(summary="prior")
    store = FakeStore(prior={"a": (pre, "1"), "b": (pre, "1")})
    summ = Summarizer("test", source, FakeStrategy(), store)

    stub = _stub_llm_dict_returner({"summary": "S"})
    report = summ.refresh(
        days=7, max_items=10, force=False,
        llm_caller=as_caller(stub),
    )

    assert report.summarized == 1
    assert report.skipped_fresh == 2
    assert report.total_candidates == 3
    assert len(stub.calls) == 1


def test_render_none_skips_item_cleanly():
    source = FakeSource(
        [("a", "1"), ("b", "1")],
        render_map={"b": None},
    )
    store = FakeStore()
    summ = Summarizer("test", source, FakeStrategy(), store)

    stub = _stub_llm_dict_returner({"summary": "S"})
    report = summ.refresh(
        days=7, max_items=10, force=False,
        llm_caller=as_caller(stub),
    )

    assert report.summarized == 1
    assert report.errored == 0
    assert len(stub.calls) == 1
    assert store.load("a") is not None
    assert store.load("b") is None


def test_force_bypasses_select_stale():
    source = FakeSource([("a", "1"), ("b", "1")])
    pre = SummaryNode(summary="prior")
    store = FakeStore(prior={"a": (pre, "1"), "b": (pre, "1")})
    summ = Summarizer("test", source, FakeStrategy(), store)

    stub = _stub_llm_dict_returner({"summary": "S"})
    report = summ.refresh(
        days=7, max_items=10, force=True,
        llm_caller=as_caller(stub),
    )

    assert report.summarized == 2
    assert len(stub.calls) == 2


# ---------------------------------------------------------------------------
# 2. Coherence checks
# ---------------------------------------------------------------------------


def test_layered_requires_persists_tree():
    layered_strat = FakeStrategy(
        capabilities=frozenset({SummaryCapability.LAYERED}),
    )
    flat_only_store = FakeStore(
        capabilities=frozenset({SummaryCapability.PERSISTS_FLAT}),
    )
    src = FakeSource([])
    with pytest.raises(IncoherentComposition, match="PERSISTS_TREE"):
        Summarizer("bad", src, layered_strat, flat_only_store)


def test_flat_strategy_ok_with_flat_store():
    flat_strat = FakeStrategy(
        capabilities=frozenset({SummaryCapability.FLAT}),
    )
    flat_store = FakeStore(
        capabilities=frozenset({SummaryCapability.PERSISTS_FLAT}),
    )
    src = FakeSource([])
    # Should not raise.
    Summarizer("ok_flat", src, flat_strat, flat_store)


def test_flat_strategy_ok_with_tree_store():
    flat_strat = FakeStrategy(
        capabilities=frozenset({SummaryCapability.FLAT}),
    )
    tree_store = FakeStore(
        capabilities=frozenset({SummaryCapability.PERSISTS_TREE}),
    )
    src = FakeSource([])
    Summarizer("ok_flat_in_tree", src, flat_strat, tree_store)


def test_batched_mismatch_strategy_only_rejected():
    batched_strat = FakeStrategy(
        capabilities=frozenset({
            SummaryCapability.FLAT, SummaryCapability.BATCHED,
        }),
    )
    non_batched_src = FakeSource([], capabilities=frozenset())
    store = FakeStore()
    with pytest.raises(IncoherentComposition, match="BATCHED"):
        Summarizer("bad", non_batched_src, batched_strat, store)


def test_batched_mismatch_source_only_rejected():
    flat_strat = FakeStrategy(
        capabilities=frozenset({SummaryCapability.FLAT}),
    )
    batched_src = FakeSource(
        [], capabilities=frozenset({SummaryCapability.BATCHED}),
    )
    store = FakeStore()
    with pytest.raises(IncoherentComposition, match="BATCHED"):
        Summarizer("bad", batched_src, flat_strat, store)


# ---------------------------------------------------------------------------
# 3. Error isolation
# ---------------------------------------------------------------------------


def test_llm_error_isolated_and_recorded():
    source = FakeSource([("a", "1"), ("b", "1"), ("c", "1")])
    store = FakeStore()
    summ = Summarizer("test", source, FakeStrategy(), store)

    call_count = {"n": 0}

    def stub(*, system, user, output_schema=None, profile=None):
        call_count["n"] += 1
        if user == "body for b":
            return None  # legacy: None response → error
        return {"summary": "ok"}

    report = summ.refresh(
        days=7, max_items=10, force=False,
        llm_caller=as_caller(stub),
    )

    assert report.summarized == 2
    assert report.errored == 1
    assert any(iid == "b" for iid, _ in store.error_calls)
    # Items a and c saved despite b's error.
    assert store.load("a") is not None
    assert store.load("c") is not None


def test_parse_failure_isolated():
    source = FakeSource([("a", "1"), ("b", "1")])
    store = FakeStore()

    class _StrategyOneFails(FakeStrategy):
        def parse(self, structured_output, raw_content):
            if structured_output and structured_output.get("summary") == "BOOM":
                raise SummarizationError("planned parse failure")
            return super().parse(structured_output, raw_content)

    summ = Summarizer("test", source, _StrategyOneFails(), store)

    def stub(*, system, user, output_schema=None, profile=None):
        if user == "body for a":
            return {"summary": "BOOM"}
        return {"summary": "ok"}

    report = summ.refresh(
        days=7, max_items=10, force=False,
        llm_caller=as_caller(stub),
    )

    assert report.errored == 1
    assert report.summarized == 1
    assert store.load("b") is not None


def test_record_error_preserves_prior_good_result():
    pre = SummaryNode(summary="prior good")
    source = FakeSource([("a", "tok-a")])  # different token → stale
    store = FakeStore(prior={"a": (pre, "tok-old")})
    summ = Summarizer("test", source, FakeStrategy(), store)

    def stub(*, system, user, output_schema=None, profile=None):
        return None  # forces an error

    report = summ.refresh(
        days=7, max_items=10, force=True,
        llm_caller=as_caller(stub),
    )

    assert report.errored == 1
    # Prior good summary still loadable.
    loaded = store.load("a")
    assert loaded is not None
    assert loaded.summary == "prior good"


# ---------------------------------------------------------------------------
# 1b. Batch path
# ---------------------------------------------------------------------------


def test_batch_path_issues_one_call_for_n_items():
    source = FakeSource(
        [("a", "1"), ("b", "1"), ("c", "1")],
        capabilities=frozenset({SummaryCapability.BATCHED}),
    )
    strategy = FakeStrategy(
        capabilities=frozenset({
            SummaryCapability.FLAT, SummaryCapability.BATCHED,
        }),
    )
    store = FakeStore()
    summ = Summarizer("test_batched", source, strategy, store)

    call_count = {"n": 0}

    def stub(*, system, user, output_schema=None, profile=None):
        call_count["n"] += 1
        # Response shape for parse_batch
        return {
            "summaries": [
                {"item_index": 0, "summary": "Sa"},
                {"item_index": 1, "summary": "Sb"},
                {"item_index": 2, "summary": "Sc"},
            ],
        }

    report = summ.refresh(
        days=7, max_items=10, force=False,
        llm_caller=as_caller(stub),
    )

    assert call_count["n"] == 1
    assert report.summarized == 3
    assert store.load("a").summary == "Sa"
    assert store.load("c").summary == "Sc"


def test_batch_path_missing_item_in_response_recorded_as_error():
    source = FakeSource(
        [("a", "1"), ("b", "1")],
        capabilities=frozenset({SummaryCapability.BATCHED}),
    )
    strategy = FakeStrategy(
        capabilities=frozenset({
            SummaryCapability.FLAT, SummaryCapability.BATCHED,
        }),
    )
    store = FakeStore()
    summ = Summarizer("test", source, strategy, store)

    def stub(*, system, user, output_schema=None, profile=None):
        return {"summaries": [{"item_index": 0, "summary": "Sa"}]}

    report = summ.refresh(
        days=7, max_items=10, force=False,
        llm_caller=as_caller(stub),
    )

    assert report.summarized == 1
    assert report.errored == 1
    assert any(iid == "b" for iid, _ in store.error_calls)


# ---------------------------------------------------------------------------
# Adapter (as_caller) edge cases
# ---------------------------------------------------------------------------


def test_as_caller_normalizes_bare_dict():
    fn = _stub_llm_dict_returner({"x": 1})
    caller = as_caller(fn)
    result = caller.call(system="s", user="u")
    assert result.structured_output == {"x": 1}
    assert result.error is None


def test_as_caller_normalizes_json_string():
    def fn(*, system, user, output_schema=None, profile=None):
        return json.dumps({"x": 2})

    caller = as_caller(fn)
    result = caller.call(system="s", user="u")
    assert result.structured_output == {"x": 2}


def test_as_caller_normalizes_none_to_error():
    def fn(*, system, user, output_schema=None, profile=None):
        return None

    caller = as_caller(fn)
    result = caller.call(system="s", user="u")
    assert result.is_error()


def test_as_caller_normalizes_response_object():
    class _Resp:
        structured_output = {"y": 3}
        content = "irrelevant"
        model = "m1"
        backend = "b1"

        def is_error(self):
            return False

    def fn(*, system, user, output_schema=None, profile=None):
        return _Resp()

    caller = as_caller(fn)
    result = caller.call(system="s", user="u")
    assert result.structured_output == {"y": 3}
    assert result.model == "m1"
    assert result.backend == "b1"
    assert not result.is_error()


def test_as_caller_returns_none_when_given_none():
    assert as_caller(None) is None

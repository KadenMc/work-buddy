"""Unit tests for the Tier-3 semantic-LLM condition (hermetic — no network/LLM)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import work_buddy.events.conditions.semantic_llm as S
from work_buddy.events.conditions.semantic_llm import SemanticLlmCondition
from work_buddy.events.envelope import new_event
from work_buddy.events.protocol import ConditionContext
from work_buddy.events.sources.definition import from_frontmatter

T0 = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)


def _src(**semantic):
    fm = {
        "kind": "event_source",
        "source": {"type": "fake", "url": "x", "interval": "5m"},
        "extract": {"mode": "json_path", "path": "$.p"},
        "semantic": {"question": "material?", "query": "nvda news", **semantic},
        "action": {"name": "notify"},
        "allowed_actions": ["notify"],
        "enabled": True,
    }
    return from_frontmatter("nvda", fm)


def _evt(current="B", prev="A"):
    return new_event(
        "/wb/source/nvda",
        "ai.workbuddy.source.nvda.changed",
        data={"current": current, "prev": prev, "source_name": "nvda"},
        modality="pull",
    )


def _verdict(relevant=True, confidence=1.0):
    return SimpleNamespace(relevant=relevant, confidence=confidence, reason="", evidence_urls=[])


def _hits(*urls):
    return [SimpleNamespace(url=u, title=u) for u in (urls or ("https://x/1",))]


def _install(monkeypatch, *, hits=None, hits_seq=None, verdict=None, verdicts=None, search_err=None):
    calls = {"search": 0, "classify": 0}
    _hseq = list(hits_seq) if hits_seq is not None else None
    _vseq = list(verdicts) if verdicts is not None else None

    def fake_search(query, max_results):
        calls["search"] += 1
        if search_err:
            raise search_err
        if _hseq is not None:
            return _hseq.pop(0)
        return hits if hits is not None else _hits()

    def fake_classify(question, h, *, watch_label):
        calls["classify"] += 1
        if _vseq is not None:
            return _vseq.pop(0)
        return verdict if verdict is not None else _verdict()

    monkeypatch.setattr(S, "_do_search", fake_search)
    monkeypatch.setattr(S, "_do_classify", fake_classify)
    return calls


def _freeze(monkeypatch, t):
    monkeypatch.setattr(S, "_now", lambda: t)


def test_relevant_fires(monkeypatch, tmp_path):
    calls = _install(monkeypatch, verdict=_verdict(relevant=True))
    cond = SemanticLlmCondition(_src(), state_directory=tmp_path)
    assert cond.evaluate(_evt(), None, ConditionContext()) is True
    assert calls["classify"] == 1


def test_irrelevant_suppresses(monkeypatch, tmp_path):
    _install(monkeypatch, verdict=_verdict(relevant=False))
    cond = SemanticLlmCondition(_src(), state_directory=tmp_path)
    assert cond.evaluate(_evt(), None, ConditionContext()) is False


def test_search_error_is_fail_closed(monkeypatch, tmp_path):
    calls = _install(monkeypatch, search_err=RuntimeError("websearch disabled"))
    cond = SemanticLlmCondition(_src(), state_directory=tmp_path)
    assert cond.evaluate(_evt(), None, ConditionContext()) is False
    assert calls["classify"] == 0  # never reached the LLM


def test_min_confidence_gate(monkeypatch, tmp_path):
    _install(monkeypatch, verdict=_verdict(relevant=True, confidence=0.4))
    cond = SemanticLlmCondition(_src(min_confidence=0.7), state_directory=tmp_path)
    assert cond.evaluate(_evt(), None, ConditionContext()) is False


def test_results_hash_prefilter_skips_classify(monkeypatch, tmp_path):
    # Identical search results across evals → reuse the verdict, no second classify.
    calls = _install(monkeypatch, hits=_hits("https://x/a"), verdict=_verdict(relevant=True))
    cond = SemanticLlmCondition(_src(), state_directory=tmp_path)
    assert cond.evaluate(_evt("B"), None, ConditionContext()) is True
    assert cond.evaluate(_evt("C"), None, ConditionContext()) is True   # value changed, news identical
    assert calls["classify"] == 1   # classify ran once; second reused the verdict
    assert calls["search"] == 2     # search ran both times (it's the cheap gate)


def test_changed_results_reclassify(monkeypatch, tmp_path):
    calls = _install(
        monkeypatch,
        hits_seq=[_hits("https://x/a"), _hits("https://x/b")],
        verdict=_verdict(relevant=False),
    )
    cond = SemanticLlmCondition(_src(), state_directory=tmp_path)
    cond.evaluate(_evt(), None, ConditionContext())
    cond.evaluate(_evt(), None, ConditionContext())
    assert calls["classify"] == 2   # different evidence each time → no prefilter


def test_cooldown_suppresses_then_expires(monkeypatch, tmp_path):
    calls = _install(
        monkeypatch,
        hits_seq=[_hits("https://x/a"), _hits("https://x/c")],
        verdict=_verdict(relevant=True),
    )
    cond = SemanticLlmCondition(_src(cooldown="1h"), state_directory=tmp_path)

    _freeze(monkeypatch, T0)
    assert cond.evaluate(_evt(), None, ConditionContext()) is True       # fires; last_fire_at = T0

    _freeze(monkeypatch, T0 + timedelta(minutes=30))
    assert cond.evaluate(_evt(), None, ConditionContext()) is False      # within cooldown → suppressed
    assert calls["search"] == 1                                          # short-circuited before search

    _freeze(monkeypatch, T0 + timedelta(hours=2))
    assert cond.evaluate(_evt(), None, ConditionContext()) is True       # cooldown expired → re-evaluates
    assert calls["search"] == 2


def test_debounce_n_of_m(monkeypatch, tmp_path):
    # "2/3" — fire only once 2 of the last 3 verdicts are positive.
    _install(
        monkeypatch,
        hits_seq=[_hits("a"), _hits("b"), _hits("c")],
        verdicts=[_verdict(relevant=True), _verdict(relevant=False), _verdict(relevant=True)],
    )
    cond = SemanticLlmCondition(_src(debounce="2/3"), state_directory=tmp_path)
    assert cond.evaluate(_evt(), None, ConditionContext()) is False   # votes [T]     → 1 < 2
    assert cond.evaluate(_evt(), None, ConditionContext()) is False   # votes [T,F]   → 1 < 2
    assert cond.evaluate(_evt(), None, ConditionContext()) is True    # votes [T,F,T] → 2 >= 2


def test_missing_question_fail_closed(monkeypatch, tmp_path):
    # A source whose semantic block lost its question → never fires (defensive).
    src = _src()
    object.__setattr__(src, "semantic", {"query": "x"})  # frozen dataclass
    _install(monkeypatch, verdict=_verdict(relevant=True))
    cond = SemanticLlmCondition(src, state_directory=tmp_path)
    assert cond.evaluate(_evt(), None, ConditionContext()) is False

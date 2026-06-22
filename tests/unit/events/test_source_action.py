"""Unit tests for the source-action consumer (condition + allowed_actions + consent)."""

from __future__ import annotations

import work_buddy.events.processors.registry as registry
import work_buddy.events.sources.loader as loader_mod
from work_buddy.events.consumers.source_action import SourceActionProcessor
from work_buddy.events.envelope import new_event
from work_buddy.events.processors.registry import Action
from work_buddy.events.protocol import ProcessorResult, RunContext
from work_buddy.events.sources.definition import from_frontmatter

BASE_FM = {
    "kind": "event_source",
    "source": {"type": "fake", "url": "x", "interval": "5m"},
    "extract": {"mode": "json_path", "path": "$.ceo"},
    "enabled": True,
}


def _src(*, condition=None, allowed=("notify",), action="notify", max_per_hour=None, semantic=None):
    fm = {
        **BASE_FM,
        "condition": condition,
        "action": {"name": action},
        "allowed_actions": list(allowed),
    }
    if max_per_hour is not None:
        fm["rate_limit"] = {"max_per_hour": max_per_hour}
    if semantic is not None:
        fm["semantic"] = semantic
    return from_frontmatter("nvda", fm)


def _evt(current, prev):
    return new_event(
        "/wb/source/nvda",
        "ai.workbuddy.source.nvda.changed",
        data={"current": current, "prev": prev, "source_name": "nvda"},
        modality="pull",
    )


def _install(monkeypatch, src, recorder):
    monkeypatch.setattr(loader_mod, "load_event_sources", lambda directory=None: ([src], []))

    def fake_notify(event, source, ctx):
        recorder.append((event, source))
        return ProcessorResult(text="notified")

    monkeypatch.setattr(
        registry, "ACTIONS", {"notify": Action(name="notify", run=fake_notify)}
    )


def test_runs_action_when_no_condition(monkeypatch):
    fired = []
    _install(monkeypatch, _src(condition=None), fired)
    SourceActionProcessor().run(_evt("B", "A"), RunContext(seq=1))
    assert len(fired) == 1


def test_runs_action_when_condition_passes(monkeypatch):
    fired = []
    _install(monkeypatch, _src(condition="event.data != prev.data"), fired)
    SourceActionProcessor().run(_evt("B", "A"), RunContext(seq=1))
    assert len(fired) == 1


def test_skips_action_when_condition_fails(monkeypatch):
    fired = []
    _install(monkeypatch, _src(condition="event.data != prev.data"), fired)
    SourceActionProcessor().run(_evt("A", "A"), RunContext(seq=1))
    assert fired == []


def test_denies_action_not_in_allowed(monkeypatch):
    fired = []
    _install(monkeypatch, _src(allowed=("task_create",)), fired)
    r = SourceActionProcessor().run(_evt("B", "A"), RunContext(seq=1))
    assert fired == []
    assert "denied" in r.text


def test_unknown_source_is_noop(monkeypatch):
    fired = []
    monkeypatch.setattr(loader_mod, "load_event_sources", lambda directory=None: ([], []))
    monkeypatch.setattr(
        registry, "ACTIONS", {"notify": Action(name="notify", run=lambda *a: fired.append(a))}
    )
    r = SourceActionProcessor().run(_evt("B", "A"), RunContext(seq=1))
    assert fired == []
    assert "not found" in r.text


def test_rate_limit_suspends_at_threshold(monkeypatch):
    import work_buddy.events.sources.ratelimit as rl

    fired = []
    _install(monkeypatch, _src(max_per_hour=2), fired)
    monkeypatch.setattr(rl, "fires_last_hour", lambda name, now, directory=None: 2)
    suspended = []
    monkeypatch.setattr(
        SourceActionProcessor, "_auto_suspend", lambda self, s: suspended.append(s.name)
    )
    r = SourceActionProcessor().run(_evt("B", "A"), RunContext(seq=1))
    assert fired == []                       # action suppressed
    assert suspended == ["nvda"]             # source auto-suspended
    assert "auto-suspended" in r.text


def test_rate_limit_under_threshold_fires_and_records(monkeypatch):
    import work_buddy.events.sources.ratelimit as rl

    fired = []
    _install(monkeypatch, _src(max_per_hour=5), fired)
    monkeypatch.setattr(rl, "fires_last_hour", lambda name, now, directory=None: 1)
    recorded = []
    monkeypatch.setattr(rl, "record_fire", lambda name, now, directory=None: recorded.append(name))
    SourceActionProcessor().run(_evt("B", "A"), RunContext(seq=1))
    assert len(fired) == 1                    # action ran
    assert recorded == ["nvda"]              # and the fire was recorded


def test_semantic_gate_pass_fires(monkeypatch):
    fired = []
    _install(monkeypatch, _src(semantic={"question": "material?"}), fired)
    monkeypatch.setattr(SourceActionProcessor, "_semantic_passes", lambda self, s, e: True)
    SourceActionProcessor().run(_evt("B", "A"), RunContext(seq=1))
    assert len(fired) == 1


def test_semantic_gate_fail_suppresses(monkeypatch):
    fired = []
    _install(monkeypatch, _src(semantic={"question": "material?"}), fired)
    monkeypatch.setattr(SourceActionProcessor, "_semantic_passes", lambda self, s, e: False)
    r = SourceActionProcessor().run(_evt("B", "A"), RunContext(seq=1))
    assert fired == []
    assert "semantic" in r.text


def test_no_semantic_block_skips_the_gate(monkeypatch):
    fired = []
    _install(monkeypatch, _src(), fired)  # no semantic block
    called = []
    monkeypatch.setattr(
        SourceActionProcessor, "_semantic_passes", lambda self, s, e: called.append(1) or True
    )
    SourceActionProcessor().run(_evt("B", "A"), RunContext(seq=1))
    assert len(fired) == 1
    assert called == []  # the expensive gate is never consulted when no block is configured


def test_known_actions_match_validator_allowlist():
    # The runtime registry and the validator's allow-list must not drift.
    from work_buddy.events.sources.definition import KNOWN_ACTIONS

    assert registry.known_actions() == KNOWN_ACTIONS

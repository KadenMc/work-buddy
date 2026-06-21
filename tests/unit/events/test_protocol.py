"""Unit tests for the events protocols."""

from __future__ import annotations

from work_buddy.events.envelope import new_event
from work_buddy.events.protocol import (
    Processor,
    ProcessorManifest,
    ProcessorResult,
    RunContext,
)


class _DummyProcessor:
    manifest = ProcessorManifest(name="dummy", consent_action=None)

    def run(self, event, ctx):
        return ProcessorResult(text="ok", structured={"type": event.type})


def test_dummy_satisfies_processor_protocol():
    d = _DummyProcessor()
    assert isinstance(d, Processor)  # runtime_checkable structural check


def test_processor_run_returns_result():
    d = _DummyProcessor()
    res = d.run(
        new_event("/wb/test", "ai.workbuddy.test.ping", {}),
        RunContext(seq=1, traceparent="tp"),
    )
    assert res.text == "ok"
    assert res.is_error is False
    assert res.structured == {"type": "ai.workbuddy.test.ping"}

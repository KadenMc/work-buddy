"""Tests for first-class inference-call provenance.

Covers the ambient call context, the record writer (description composition,
ambient call_id/detail, best-effort), the cost.log_call → provenance emit
(universal completion coverage), and the broker reusing the bound call_id as
its metrics id (the activity↔scheduler join).
"""
from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from work_buddy.inference.call_context import (
    bind_call_id,
    current_call_id,
    current_detail,
    inference_detail,
)
from work_buddy.llm import provenance


@pytest.fixture(autouse=True)
def _no_publish(monkeypatch):
    # Don't fire real bus events (cross-process HTTP) during unit tests.
    monkeypatch.setattr(
        "work_buddy.dashboard.events.publish_auto", lambda *a, **k: None
    )


def _tmp_log(monkeypatch) -> pathlib.Path:
    path = pathlib.Path(tempfile.mkdtemp()) / "inference_calls.jsonl"
    monkeypatch.setattr(provenance, "_provenance_log_path", lambda: path)
    return path


def _rows(path: pathlib.Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_context_vars_scope():
    assert current_call_id() is None and current_detail() is None
    with bind_call_id("x"), inference_detail("d"):
        assert current_call_id() == "x" and current_detail() == "d"
    assert current_call_id() is None and current_detail() is None
    with inference_detail(None):  # no-op
        assert current_detail() is None


def test_record_composes_description_and_fields(monkeypatch):
    path = _tmp_log(monkeypatch)
    with bind_call_id("cid1"):
        provenance.record_inference_call(
            kind="completion", model="claude-haiku", provider="anthropic_default",
            execution_mode="cloud", status="ok", input_tokens=10, output_tokens=5,
            call_site="Summarize", detail="My Tab",
        )
    [r] = _rows(path)
    assert r["call_id"] == "cid1"
    assert r["description"] == "Summarize: My Tab"
    assert r["kind"] == "completion" and r["execution_mode"] == "cloud"
    assert r["input_tokens"] == 10 and r["output_tokens"] == 5
    assert r["finished_at"]


def test_record_reads_ambient_detail_and_call_id(monkeypatch):
    path = _tmp_log(monkeypatch)
    with bind_call_id("ambient"), inference_detail("ambient detail"):
        provenance.record_inference_call(
            kind="embedding", model="leaf-ir", provider="lmstudio",
            execution_mode="local", status="ok", item_count=40, call_site="Embed",
        )
    [r] = _rows(path)
    assert r["call_id"] == "ambient"
    assert r["description"] == "Embed: ambient detail"
    assert r["item_count"] == 40


def test_record_call_site_only_when_no_detail(monkeypatch):
    path = _tmp_log(monkeypatch)
    provenance.record_inference_call(
        kind="completion", model="m", provider="p", execution_mode="cloud",
        status="ok", call_site="Classify",
    )
    [r] = _rows(path)
    assert r["description"] == "Classify" and r["detail"] is None


def test_record_is_best_effort(monkeypatch):
    def _boom():
        raise OSError("disk gone")
    monkeypatch.setattr(provenance, "_provenance_log_path", _boom)
    # Must not raise into the caller.
    provenance.record_inference_call(
        kind="completion", model="m", provider="p",
        execution_mode="cloud", status="ok",
    )


def test_cost_log_call_emits_provenance(monkeypatch):
    from work_buddy.llm import cost
    ppath = _tmp_log(monkeypatch)
    monkeypatch.setattr(
        cost, "_cost_log_path",
        lambda: pathlib.Path(tempfile.mkdtemp()) / "llm_costs.jsonl",
    )
    with bind_call_id("joinme"):
        cost.log_call(
            model="claude-haiku", input_tokens=3, output_tokens=2, task_id="t",
            execution_mode="local", backend="lmstudio_local",
        )
    [r] = _rows(ppath)
    assert r["call_id"] == "joinme" and r["kind"] == "completion"
    assert r["status"] == "ok" and r["execution_mode"] == "local"


def test_cost_cache_hit_records_cached_status(monkeypatch):
    from work_buddy.llm import cost
    ppath = _tmp_log(monkeypatch)
    monkeypatch.setattr(
        cost, "_cost_log_path",
        lambda: pathlib.Path(tempfile.mkdtemp()) / "llm_costs.jsonl",
    )
    cost.log_call(
        model="m", input_tokens=0, output_tokens=0, task_id="t",
        cached=True, execution_mode="cloud",
    )
    [r] = _rows(ppath)
    assert r["status"] == "cached"


def test_broker_uses_bound_call_id():
    from work_buddy.inference.broker import LocalInferenceBroker, ProfileConfig
    b = LocalInferenceBroker()
    b.configure_profile(ProfileConfig(name="p", max_concurrent=1))
    with bind_call_id("JOINID"):
        with b.slot(profile="p"):
            pass
    assert b.snapshot_metrics()[-1]["id"] == "JOINID"


def test_broker_falls_back_to_uuid_when_unbound():
    from work_buddy.inference.broker import LocalInferenceBroker, ProfileConfig
    b = LocalInferenceBroker()
    b.configure_profile(ProfileConfig(name="p", max_concurrent=1))
    with b.slot(profile="p"):
        pass
    mid = b.snapshot_metrics()[-1]["id"]
    assert mid and mid != "JOINID"


class _ErrResult:
    error = "boom: server unreachable"
    model = "claude-haiku"


class _OkResult:
    error = None
    model = "claude-haiku"


def test_decorator_emits_error_provenance_cloud(monkeypatch):
    """A failed run_task (no log_call) emits an error row via the decorator."""
    from work_buddy.llm import runner
    path = _tmp_log(monkeypatch)

    @runner._with_call_id
    def fake(*, task_id=None, trace_id=None, profile=None):
        return _ErrResult()

    fake(task_id="t", trace_id="tr")
    [r] = _rows(path)
    assert r["status"] == "error"
    assert r["error"] == "boom: server unreachable"
    assert r["execution_mode"] == "cloud" and r["model"] == "claude-haiku"
    assert r["trace_id"] == "tr"


def test_decorator_error_provenance_local_when_profile(monkeypatch):
    from work_buddy.llm import runner
    path = _tmp_log(monkeypatch)

    @runner._with_call_id
    def fake(*, task_id=None, trace_id=None, profile=None):
        return _ErrResult()

    fake(task_id="t", profile="local_general")
    [r] = _rows(path)
    assert r["execution_mode"] == "local" and r["provider"] == "local_general"


def test_decorator_success_does_not_double_write(monkeypatch):
    """Successful results carry no error → the decorator writes nothing (log_call owns success)."""
    from work_buddy.llm import runner
    path = _tmp_log(monkeypatch)

    @runner._with_call_id
    def fake(*, task_id=None, trace_id=None, profile=None):
        return _OkResult()

    fake(task_id="t")
    assert not path.exists() or _rows(path) == []

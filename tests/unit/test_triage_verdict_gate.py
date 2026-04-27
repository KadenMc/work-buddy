"""Producer-level test: verdict_pass_enabled=False uses submit_raw path."""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.triage import background as bg
from work_buddy.triage import sources
from work_buddy.triage.background import (
    BackgroundTriageProducer,
    TriagePool,
)
from work_buddy.triage.items import TriageItem


@pytest.fixture
def isolated_pool(tmp_path: Path, monkeypatch) -> TriagePool:
    pool = TriagePool(pool_dir=tmp_path / "triage_pool")
    bg.set_pool_for_tests(pool)
    monkeypatch.setattr(
        "work_buddy.artifacts.save",
        lambda *a, **kw: type("R", (), {"id": "stub"})(),
    )
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"vault_root": str(tmp_path / "vault")},
    )
    sources.reset_for_tests()
    yield pool
    bg.set_pool_for_tests(None)
    sources.reset_for_tests()


def _item(i: int = 0, text: str = "x") -> TriageItem:
    return TriageItem(
        id=f"j_{i:06x}", text=text, label=text[:20],
        source="journal_thread", metadata={},
    )


def test_gate_off_writes_raw_entries(isolated_pool: TriagePool) -> None:
    """When verdict_pass_enabled=False, the agent is never called and
    every item lands as a raw entry (verdict={"raw": True})."""
    items = [_item(1, "alpha"), _item(2, "beta")]
    agent_called: list[str] = []

    def stub_agent(item, run_id):
        agent_called.append(item.id)
        return {"content": "should not happen"}

    producer = BackgroundTriageProducer(
        adapter_name="gate_off",
        source="journal_thread",
        collect=lambda: (items, "HASH-A"),
        agent=stub_agent,
        enrich=False,
        verdict_pass_enabled=False,
    )
    result = producer.run()
    assert result.status == "ok"
    assert result.submitted == 2
    assert agent_called == []  # the agent must never be invoked

    pending = isolated_pool.pending()
    assert len(pending) == 2
    for pe in pending:
        assert pe.verdict == {"raw": True}
        assert pe.state == "pending"


def test_gate_on_uses_agent_path(isolated_pool: TriagePool) -> None:
    """Sanity: with the gate ON the existing agent flow runs unchanged.

    We verify by passing an agent that submits via triage_submit and
    expecting submitted=1 with verdict.recommended_action populated.
    """
    from work_buddy.triage.capabilities.triage_submit import triage_submit

    items = [_item(3, "gamma")]

    def real_agent(item, run_id):
        triage_submit(
            run_id=run_id, item_id=item.id,
            recommended_action="leave", rationale="r",
        )
        return {"content": "ok"}

    producer = BackgroundTriageProducer(
        adapter_name="gate_on",
        source="journal_thread",
        collect=lambda: (items, "HASH-B"),
        agent=real_agent,
        enrich=False,
        verdict_pass_enabled=True,  # explicit
    )
    result = producer.run()
    assert result.status == "ok"
    assert result.submitted == 1

    pe = isolated_pool.pending()[0]
    assert pe.verdict.get("recommended_action") == "leave"
    assert "raw" not in pe.verdict


def test_gate_off_default_in_capability(monkeypatch, tmp_path) -> None:
    """journal_triage_scan reads verdict_pass.enabled from config and
    defaults to False — confirming the Slice-1 default behavior."""
    from work_buddy.triage.config import load_triage_config
    cfg = load_triage_config()
    assert cfg.get("verdict_pass", {}).get("enabled") is False

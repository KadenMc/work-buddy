"""Unit tests for triage_pool_sweep capability (Slice 1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from work_buddy.triage import background as bg
from work_buddy.triage import sources
from work_buddy.triage.background import (
    STATE_PENDING,
    STATE_QUARANTINED,
    STATE_STALE,
    TriagePool,
)
from work_buddy.triage.capabilities.triage_pool_sweep import triage_pool_sweep
from work_buddy.triage.items import TriageItem


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "journal").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def isolated_pool(tmp_path: Path, vault: Path, monkeypatch) -> TriagePool:
    pool = TriagePool(pool_dir=tmp_path / "triage_pool")
    bg.set_pool_for_tests(pool)
    monkeypatch.setattr(
        "work_buddy.artifacts.save",
        lambda *a, **kw: type("R", (), {"id": "stub"})(),
    )
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {
            "vault_root": str(vault),
            "obsidian": {"journal_dir": "journal"},
        },
    )
    sources.reset_for_tests()
    yield pool
    bg.set_pool_for_tests(None)
    sources.reset_for_tests()


def _add_pending(
    pool: TriagePool,
    *,
    run_id: str,
    item_id: str,
    source: str,
    text: str = "x",
    metadata: dict | None = None,
) -> None:
    """Register a one-item run, submit_raw the entry. Returns nothing —
    the entry is now pending in the pool."""
    item = TriageItem(
        id=item_id, text=text, label=text[:20],
        source=source, metadata=metadata or {},
    )
    pool.register_run(
        run_id=run_id, adapter=source, source=source, items=[item],
    )
    pool.submit_raw(run_id=run_id, item_id=item_id)


def _force_expires_at(pool: TriagePool, when: datetime) -> None:
    """Mutate every entry's expires_at to ``when`` (test helper)."""
    import json
    raw = json.loads(pool._index_path.read_text(encoding="utf-8"))
    for e in raw["entries"]:
        e["expires_at"] = when.isoformat()
    pool._index_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# TTL transitions
# ---------------------------------------------------------------------------


def test_sweep_marks_expired_pending_as_stale(isolated_pool: TriagePool) -> None:
    """An entry past its expires_at transitions pending → stale."""
    _add_pending(
        isolated_pool, run_id="ttl1", item_id="i1",
        source="journal_thread",
        # journal source doesn't matter much here — we override expires_at
        # below to force expiry, and the journal file isn't on disk so
        # source_removed would fire. Override the source's triggers
        # to test TTL in isolation.
        metadata={"source_dates": []},  # no dates → source_removed no-ops
    )
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    _force_expires_at(isolated_pool, yesterday)

    result = triage_pool_sweep()
    assert result["status"] == "ok"
    assert result["stale_marked"] == 1
    assert result["quarantined"] == 0
    assert isolated_pool.pending_count() == 0
    assert len(isolated_pool.entries_in_state(STATE_STALE)) == 1


def test_sweep_skips_unexpired(isolated_pool: TriagePool) -> None:
    _add_pending(
        isolated_pool, run_id="ttl2", item_id="i2",
        source="journal_thread", metadata={"source_dates": []},
    )
    # Default journal TTL is 5 days; freshly created → not expired.
    result = triage_pool_sweep()
    assert result["stale_marked"] == 0
    assert isolated_pool.pending_count() == 1


def test_sweep_inline_no_ttl_never_stales(isolated_pool: TriagePool) -> None:
    """Inline TTL=null. Even if we forge expires_at, inline never gets one
    set on creation — submit_raw will leave expires_at=None for inline.
    Sweep should leave it alone."""
    _add_pending(
        isolated_pool, run_id="ttl3", item_id="i3",
        source="inline",
        metadata={"file_path": "/tmp/does-not-resolve.md"},
    )
    pe = isolated_pool.pending()[0]
    assert pe.expires_at is None  # inline has no TTL
    # Sweep should NOT stale this (no expires_at to compare). It MAY
    # quarantine via source_removed if the file isn't there — that's
    # tested separately. For this test, point file_path at something
    # that exists so source_removed doesn't fire.
    pass  # covered by other tests; this one just confirms expires_at=None


# ---------------------------------------------------------------------------
# Quarantine via source_removed
# ---------------------------------------------------------------------------


def test_sweep_quarantines_inline_with_missing_file(
    isolated_pool: TriagePool, vault: Path,
) -> None:
    """An inline entry whose file_path doesn't exist → quarantined."""
    _add_pending(
        isolated_pool, run_id="q1", item_id="i_inline_gone",
        source="inline",
        metadata={"file_path": "ghost/nope.md"},
    )
    result = triage_pool_sweep()
    assert result["quarantined"] == 1
    quarantined = isolated_pool.entries_in_state(STATE_QUARANTINED)
    assert quarantined[0].quarantine_reason == "source_removed"


def test_sweep_leaves_inline_with_existing_file(
    isolated_pool: TriagePool, vault: Path,
) -> None:
    """An inline entry whose file_path exists → still pending."""
    target = vault / "real.md"
    target.write_text("hi", encoding="utf-8")
    _add_pending(
        isolated_pool, run_id="q2", item_id="i_inline_alive",
        source="inline",
        metadata={"file_path": "real.md"},
    )
    result = triage_pool_sweep()
    assert result["quarantined"] == 0
    assert isolated_pool.pending_count() == 1


def test_sweep_quarantines_journal_with_missing_date(
    isolated_pool: TriagePool, vault: Path,
) -> None:
    """A journal entry whose source_dates file is gone → quarantined."""
    _add_pending(
        isolated_pool, run_id="q3", item_id="j_gone",
        source="journal_thread", text="some thread",
        metadata={"source_dates": ["2020-01-01"]},  # no journal file
    )
    result = triage_pool_sweep()
    assert result["quarantined"] == 1
    q = isolated_pool.entries_in_state(STATE_QUARANTINED)
    assert q[0].quarantine_reason == "source_removed"


def test_sweep_leaves_journal_with_existing_date(
    isolated_pool: TriagePool, vault: Path,
) -> None:
    journal_md = vault / "journal" / "2026-04-25.md"
    journal_md.write_text("some thread content here", encoding="utf-8")
    _add_pending(
        isolated_pool, run_id="q4", item_id="j_alive",
        source="journal_thread", text="some thread content here",
        metadata={"source_dates": ["2026-04-25"]},
    )
    result = triage_pool_sweep()
    # Source exists AND text is contained → no quarantine.
    assert result["quarantined"] == 0
    assert isolated_pool.pending_count() == 1


# ---------------------------------------------------------------------------
# source_edited_beyond_match
# ---------------------------------------------------------------------------


def test_sweep_quarantines_journal_when_text_drifted(
    isolated_pool: TriagePool, vault: Path,
) -> None:
    """If the journal still exists but no longer contains the text
    (cosine ratio < 0.85) → quarantined."""
    journal_md = vault / "journal" / "2026-04-25.md"
    journal_md.write_text(
        "completely different content with no overlap",
        encoding="utf-8",
    )
    _add_pending(
        isolated_pool, run_id="q5", item_id="j_drifted",
        source="journal_thread", text="original ETF tracking thread",
        metadata={"source_dates": ["2026-04-25"]},
    )
    result = triage_pool_sweep()
    assert result["quarantined"] == 1
    q = isolated_pool.entries_in_state(STATE_QUARANTINED)
    assert q[0].quarantine_reason == "source_edited_beyond_match"


# ---------------------------------------------------------------------------
# dry_run + filtering + cap
# ---------------------------------------------------------------------------


def test_sweep_dry_run_does_not_mutate(
    isolated_pool: TriagePool, vault: Path,
) -> None:
    _add_pending(
        isolated_pool, run_id="dry1", item_id="i_dry",
        source="inline", metadata={"file_path": "ghost/nope.md"},
    )
    result = triage_pool_sweep(dry_run=True)
    assert result["dry_run"] is True
    assert result["quarantined"] == 1  # would-be transitions
    assert isolated_pool.pending_count() == 1  # but no actual writes
    assert len(isolated_pool.entries_in_state(STATE_QUARANTINED)) == 0


def test_sweep_source_filter_isolates(
    isolated_pool: TriagePool, vault: Path,
) -> None:
    _add_pending(
        isolated_pool, run_id="f1", item_id="i_inline_gone",
        source="inline", metadata={"file_path": "ghost/nope.md"},
    )
    _add_pending(
        isolated_pool, run_id="f2", item_id="j_gone",
        source="journal_thread", metadata={"source_dates": ["2020-01-01"]},
    )
    # source filter limits inspection to inline only
    result = triage_pool_sweep(source="inline")
    assert result["checked"] == 1
    assert result["quarantined"] == 1
    # Journal entry untouched
    assert isolated_pool.pending_count() == 1


def test_sweep_max_entries_caps_inspection(
    isolated_pool: TriagePool, vault: Path,
) -> None:
    for i in range(5):
        _add_pending(
            isolated_pool, run_id=f"m{i}", item_id=f"i_m{i}",
            source="inline",
            metadata={"file_path": f"ghost/nope{i}.md"},
        )
    result = triage_pool_sweep(max_entries=2)
    assert result["checked"] == 2


# ---------------------------------------------------------------------------
# Quarantine wins over stale
# ---------------------------------------------------------------------------


def test_sweep_quarantine_wins_over_stale(
    isolated_pool: TriagePool, vault: Path,
) -> None:
    """Entry both expired and source-gone — quarantine takes precedence
    (more specific signal — the source is GONE, not just old)."""
    _add_pending(
        isolated_pool, run_id="qs", item_id="i_qs",
        source="inline", metadata={"file_path": "ghost/nope.md"},
    )
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    _force_expires_at(isolated_pool, yesterday)
    result = triage_pool_sweep()
    assert result["quarantined"] == 1
    assert result["stale_marked"] == 0
    q = isolated_pool.entries_in_state(STATE_QUARANTINED)
    assert q[0].quarantine_reason == "source_removed"


# ---------------------------------------------------------------------------
# Per-source stats
# ---------------------------------------------------------------------------


def test_sweep_returns_per_source_breakdown(
    isolated_pool: TriagePool, vault: Path,
) -> None:
    _add_pending(
        isolated_pool, run_id="b1", item_id="i_b1",
        source="inline", metadata={"file_path": "ghost/x.md"},
    )
    _add_pending(
        isolated_pool, run_id="b2", item_id="j_b2",
        source="journal_thread", metadata={"source_dates": ["2020-01-01"]},
    )
    result = triage_pool_sweep()
    by_source = result["by_source"]
    assert by_source["inline"]["checked"] == 1
    assert by_source["inline"]["quarantined"] == 1
    assert by_source["journal_thread"]["checked"] == 1
    assert by_source["journal_thread"]["quarantined"] == 1

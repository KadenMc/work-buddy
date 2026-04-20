"""Phase-6 unit tests — wave-2/3 sources + collect.py retrofit.

Focus:
  - All wave-2/3 sources register on import.
  - Markdown wrapper shape is consistent across sources.
  - ``collect.py::run_collection`` writes the expected files via
    ContextCollector + ContextCurator, preserving the legacy bundle
    filenames.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from work_buddy.context import registry
from work_buddy.context.types import ContextDepth, ContextRequest, ContextSection


# ---------------------------------------------------------------------------
# Source registration
# ---------------------------------------------------------------------------


def test_wave_2_3_sources_are_registered():
    """Import-time registration check for every wave-2/3 source."""
    import work_buddy.context  # noqa: F401 (force imports)

    expected_names = {
        # wave 1
        "git", "tasks", "projects", "chrome",
        # wave 2
        "obsidian", "obsidian_tasks", "obsidian_wellness",
        "calendar", "day_planner", "session_activity",
        # wave 3
        "chat", "message", "smart", "datacore",
    }
    assert expected_names <= set(registry.names())


# ---------------------------------------------------------------------------
# Markdown-wrapper shape
# ---------------------------------------------------------------------------


class TestMarkdownWrapper:
    def test_item_shape(self, tmp_path, monkeypatch):
        """A markdown-wrapping source emits ``[{markdown, length}]``."""
        import work_buddy.context  # register
        from work_buddy.context.sources.calendar import CalendarSource

        src = CalendarSource()
        src._collect_fn = lambda cfg: "# Calendar events\n- 10am meeting"
        section = src.collect(ContextRequest())
        assert len(section.items) == 1
        assert section.items[0]["markdown"].startswith("# Calendar events")
        assert section.items[0]["length"] == len(section.items[0]["markdown"])

    def test_collector_raising_is_swallowed(self):
        from work_buddy.context.sources.calendar import CalendarSource

        src = CalendarSource()
        def _boom(_cfg):
            raise RuntimeError("no calendar")
        src._collect_fn = _boom
        section = src.collect(ContextRequest())
        assert section.items == []
        assert "error" in section.metadata

    def test_render_respects_depth_budget(self):
        """BRIEF caps aggressively; DEEP returns everything."""
        from work_buddy.context.sources.calendar import CalendarSource

        # ~3000 chars of markdown
        raw = ("## Events\n" + "\n".join(f"- event {i}" for i in range(300)))
        src = CalendarSource()
        section = ContextSection(source="calendar", items=[{"markdown": raw, "length": len(raw)}])
        brief = src.render(section, ContextDepth.BRIEF)
        normal = src.render(section, ContextDepth.NORMAL)
        deep = src.render(section, ContextDepth.DEEP)
        assert len(brief) < len(normal) < len(deep)
        # Heading present regardless of depth (well, when it doesn't start with '#')
        assert deep.strip().startswith("## Events") or "Events" in deep


# ---------------------------------------------------------------------------
# Obsidian tuple splitting
# ---------------------------------------------------------------------------


class TestObsidianSources:
    def test_obsidian_and_tasks_indexes_split_tuple(self):
        """``obsidian`` → index 0, ``obsidian_tasks`` → index 1."""
        from work_buddy.context.sources.obsidian import (
            ObsidianSource, ObsidianTasksSource,
        )

        with patch(
            "work_buddy.collectors.obsidian_collector.collect",
            return_value=("JOURNAL_MD", "TASKS_MD"),
        ):
            journal_section = ObsidianSource().collect(ContextRequest())
            tasks_section = ObsidianTasksSource().collect(ContextRequest())

        assert journal_section.items[0]["markdown"] == "JOURNAL_MD"
        assert tasks_section.items[0]["markdown"] == "TASKS_MD"

    def test_obsidian_wellness_uses_separate_collect_wellness(self):
        """Wellness is NOT a tuple index — it calls ``collect_wellness``."""
        from work_buddy.context.sources.obsidian import ObsidianWellnessSource

        with patch(
            "work_buddy.collectors.obsidian_collector.collect_wellness",
            return_value="WELLNESS_MD",
        ):
            section = ObsidianWellnessSource().collect(ContextRequest())

        assert section.items[0]["markdown"] == "WELLNESS_MD"


# ---------------------------------------------------------------------------
# collect.py retrofit
# ---------------------------------------------------------------------------


class _StubSource:
    """Minimal ContextSource stand-in for run_collection tests."""

    def __init__(self, name, markdown):
        self.name = name
        self._md = markdown

    def collect(self, request):
        return ContextSection(
            source=self.name,
            items=[{"markdown": self._md, "length": len(self._md)}],
        )

    def render(self, section, depth):
        if not section.items:
            return ""
        return section.items[0]["markdown"]

    def is_stale(self, cached, request):
        return False

    def drill_down(self, item_id, field):
        raise NotImplementedError


@pytest.fixture
def session_cache_roots(tmp_path, monkeypatch):
    """Redirect session dir + cache root to ``tmp_path`` so tests are hermetic."""
    monkeypatch.setattr(
        "work_buddy.collect.get_session_context_dir",
        lambda: tmp_path,
    )
    import work_buddy.context.cache as cache_mod
    monkeypatch.setattr(cache_mod, "_cache_root", lambda: tmp_path / "_cache")
    return tmp_path


@pytest.fixture
def clean_registry():
    """Snapshot + restore the registry so stubs don't leak between tests."""
    snapshot = registry.all_sources()
    registry.clear()
    yield
    registry.clear()
    for name, src in snapshot.items():
        registry.register(src)


class TestCollectCLI:
    def test_run_collection_writes_expected_files(
        self, session_cache_roots, clean_registry,
    ):
        """Each stub source's markdown lands at its mapped filename."""
        from work_buddy.collect import run_collection

        registry.register(_StubSource("git", "# Git\n- abc first\n- def second"))
        registry.register(_StubSource("obsidian", "## Journal\n- entry"))
        registry.register(_StubSource("obsidian_tasks", "## Tasks\n- [ ] do thing"))
        registry.register(_StubSource("chat", "## Chat\n- session"))

        cfg = {"vault_root": "/vault", "repos_root": "/repos"}
        bundle = run_collection(cfg, only="git")
        assert bundle is not None
        assert (bundle / "git_summary.md").exists()
        assert (bundle / "git_summary.md").read_text(encoding="utf-8").strip().startswith("# Git")

    def test_only_obsidian_expands_to_three_sources(
        self, session_cache_roots, clean_registry,
    ):
        """Legacy ``--only obsidian`` writes all three obsidian files."""
        from work_buddy.collect import run_collection

        registry.register(_StubSource("obsidian", "# Journal"))
        registry.register(_StubSource("obsidian_tasks", "# Tasks"))
        registry.register(_StubSource("obsidian_wellness", "# Wellness"))

        cfg = {"vault_root": "/vault", "repos_root": "/repos"}
        bundle = run_collection(cfg, only="obsidian")
        assert bundle is not None
        assert (bundle / "obsidian_summary.md").exists()
        assert (bundle / "tasks_summary.md").exists()
        assert (bundle / "wellness_summary.md").exists()

    def test_empty_section_does_not_write_file(
        self, session_cache_roots, clean_registry,
    ):
        """A source that returns empty items shouldn't leave an empty .md."""
        from work_buddy.collect import run_collection

        registry.register(_StubSource("git", ""))
        cfg = {"vault_root": "/vault", "repos_root": "/repos"}
        bundle = run_collection(cfg, only="git")
        assert bundle is not None
        assert not (bundle / "git_summary.md").exists()

    def test_bundle_meta_written(
        self, session_cache_roots, clean_registry,
    ):
        from work_buddy.collect import run_collection
        import json

        registry.register(_StubSource("git", "# Git"))
        cfg = {"vault_root": "/vault", "repos_root": "/repos"}
        bundle = run_collection(cfg, only="git")
        assert bundle is not None
        meta_path = bundle / "bundle_meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "collectors_run" in meta
        assert "git" in meta["collectors_run"]
        assert meta["config"]["vault_root"] == "/vault"

    def test_dry_run_writes_nothing(
        self, session_cache_roots, clean_registry,
    ):
        from work_buddy.collect import run_collection

        registry.register(_StubSource("git", "# Git"))
        cfg = {"vault_root": "/vault", "repos_root": "/repos"}
        result = run_collection(cfg, dry_run=True)
        assert result is None
        assert list(session_cache_roots.glob("*_summary.md")) == []

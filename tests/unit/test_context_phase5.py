"""Phase-5 unit tests — ContextCollector / ContextCurator + wave-1 sources.

Source tests monkeypatch the underlying stores / subprocess calls so
they don't require a live vault, project DB, or git repo. Focus is on
the glue: does the collector respect cache + is_stale, does the
curator honor depth, do the sources shape items correctly, and does
the ``build_triage_context`` retrofit preserve the legacy dict shape?
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from work_buddy.context import (
    BaseContextSource,
    Context,
    ContextCollector,
    ContextCurator,
    ContextDepth,
    ContextRequest,
    ContextSection,
)
from work_buddy.context import cache as cache_mod
from work_buddy.context import registry


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_cache_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "_cache_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def clean_registry():
    """Snapshot the registry, clear it for the test, restore after.

    Wave-1 sources register at import time so the real registry has
    git/tasks/projects/chrome in it. Tests need a controlled registry.
    """
    snapshot = registry.all_sources()
    registry.clear()
    try:
        yield
    finally:
        registry.clear()
        for name, src in snapshot.items():
            registry.register(src)


class _StubSource(BaseContextSource):
    """In-process test source with controllable behavior."""

    def __init__(self, name: str, *, items=None, stale=False, raise_on_collect=False):
        self._name = name
        self._items = items if items is not None else [f"{name}-item"]
        self._stale = stale
        self._raise_on_collect = raise_on_collect
        self.collect_calls = 0
        self.render_calls = 0

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._name

    def collect(self, request):
        self.collect_calls += 1
        if self._raise_on_collect:
            raise RuntimeError(f"{self._name} boom")
        return ContextSection(source=self._name, items=list(self._items))

    def render(self, section, depth):
        self.render_calls += 1
        if depth == ContextDepth.BRIEF:
            return f"### {section.source} ({len(section.items)})"
        return "\n".join(f"- {i}" for i in section.items)

    def is_stale(self, cached, request):
        return self._stale


# ---------------------------------------------------------------------------
# ContextCollector
# ---------------------------------------------------------------------------


class TestCollectorDispatch:
    def test_collect_all_registered_sources_by_default(
        self, tmp_cache_root, clean_registry,
    ):
        registry.register(_StubSource("a", items=["a1"]))
        registry.register(_StubSource("b", items=["b1", "b2"]))
        ctx = ContextCollector().collect(ContextRequest())
        assert set(ctx.sections.keys()) == {"a", "b"}
        assert ctx.section("a").items == ["a1"]

    def test_respects_explicit_sources_list(
        self, tmp_cache_root, clean_registry,
    ):
        registry.register(_StubSource("a"))
        registry.register(_StubSource("b"))
        ctx = ContextCollector().collect(ContextRequest(sources=["a"]))
        assert set(ctx.sections.keys()) == {"a"}

    def test_respects_exclude(self, tmp_cache_root, clean_registry):
        registry.register(_StubSource("a"))
        registry.register(_StubSource("b"))
        ctx = ContextCollector().collect(ContextRequest(exclude=["b"]))
        assert set(ctx.sections.keys()) == {"a"}

    def test_unknown_source_is_skipped_with_warning(
        self, tmp_cache_root, clean_registry, caplog,
    ):
        registry.register(_StubSource("a"))
        ctx = ContextCollector().collect(
            ContextRequest(sources=["a", "ghost"]),
        )
        assert set(ctx.sections.keys()) == {"a"}

    def test_source_raising_is_omitted_not_propagated(
        self, tmp_cache_root, clean_registry,
    ):
        registry.register(_StubSource("a"))
        registry.register(_StubSource("bad", raise_on_collect=True))
        ctx = ContextCollector().collect(ContextRequest())
        assert set(ctx.sections.keys()) == {"a"}  # bad omitted


class TestCollectorCache:
    def test_cache_miss_fetches(self, tmp_cache_root, clean_registry):
        src = _StubSource("a", items=["x"])
        registry.register(src)
        ContextCollector().collect(ContextRequest(max_age_seconds=3600))
        assert src.collect_calls == 1

    def test_cache_hit_skips_fetch(self, tmp_cache_root, clean_registry):
        src = _StubSource("a", items=["x"])
        registry.register(src)
        # First call writes the cache.
        ContextCollector().collect(ContextRequest(max_age_seconds=3600))
        # Second call should hit cache.
        ContextCollector().collect(ContextRequest(max_age_seconds=3600))
        assert src.collect_calls == 1

    def test_source_marked_stale_forces_refetch(
        self, tmp_cache_root, clean_registry,
    ):
        src = _StubSource("a", items=["x"], stale=True)
        registry.register(src)
        ContextCollector().collect(ContextRequest(max_age_seconds=3600))
        ContextCollector().collect(ContextRequest(max_age_seconds=3600))
        assert src.collect_calls == 2

    def test_max_age_none_always_refetches(
        self, tmp_cache_root, clean_registry,
    ):
        src = _StubSource("a", items=["x"])
        registry.register(src)
        ContextCollector().collect(ContextRequest())  # max_age=None
        ContextCollector().collect(ContextRequest())
        assert src.collect_calls == 2

    def test_is_stale_raising_forces_refetch(
        self, tmp_cache_root, clean_registry,
    ):
        src = _StubSource("a", items=["x"])
        registry.register(src)
        ContextCollector().collect(ContextRequest(max_age_seconds=3600))

        def boom(cached, request):
            raise RuntimeError("stale check failed")

        src.is_stale = boom  # type: ignore[assignment]
        ContextCollector().collect(ContextRequest(max_age_seconds=3600))
        assert src.collect_calls == 2


# ---------------------------------------------------------------------------
# ContextCurator
# ---------------------------------------------------------------------------


class TestCurator:
    def test_renders_each_section(self, tmp_cache_root, clean_registry):
        registry.register(_StubSource("a", items=["one", "two"]))
        registry.register(_StubSource("b", items=["x"]))
        ctx = ContextCollector().collect(ContextRequest())
        rendered = ContextCurator().curate(ctx, depth=ContextDepth.NORMAL)
        assert "User's Current Context" in rendered
        assert "- one" in rendered and "- two" in rendered
        assert "- x" in rendered

    def test_header_suppressible(self, tmp_cache_root, clean_registry):
        registry.register(_StubSource("a"))
        ctx = ContextCollector().collect(ContextRequest())
        rendered = ContextCurator().curate(ctx, header=None)
        assert "User's Current Context" not in rendered

    def test_depth_brief_uses_terse_render(
        self, tmp_cache_root, clean_registry,
    ):
        registry.register(_StubSource("a", items=["x", "y"]))
        ctx = ContextCollector().collect(ContextRequest())
        rendered = ContextCurator().curate(ctx, depth=ContextDepth.BRIEF)
        assert "### a (2)" in rendered
        assert "- x" not in rendered

    def test_per_source_depth_overrides_global(
        self, tmp_cache_root, clean_registry,
    ):
        registry.register(_StubSource("a", items=["x", "y"]))
        registry.register(_StubSource("b", items=["z"]))
        ctx = ContextCollector().collect(ContextRequest())
        rendered = ContextCurator().curate(
            ctx,
            depth=ContextDepth.NORMAL,
            per_source_depth={"a": ContextDepth.BRIEF},
        )
        # a rendered as brief; b rendered as normal.
        assert "### a (2)" in rendered
        assert "- z" in rendered

    def test_max_chars_truncates_at_section_break_when_possible(
        self, tmp_cache_root, clean_registry,
    ):
        registry.register(_StubSource("a", items=[f"item-{i}" for i in range(20)]))
        registry.register(_StubSource("b", items=[f"foo-{i}" for i in range(20)]))
        ctx = ContextCollector().collect(ContextRequest())
        rendered = ContextCurator().curate(ctx, max_chars=120)
        assert len(rendered) <= 200  # account for truncation marker
        assert "[…truncated…]" in rendered or "..." in rendered or "…" in rendered

    def test_json_format(self, tmp_cache_root, clean_registry):
        registry.register(_StubSource("a", items=["x"]))
        ctx = ContextCollector().collect(ContextRequest())
        out = ContextCurator().curate(ctx, format="json")
        import json
        data = json.loads(out)
        assert "a" in data
        assert data["a"]["items"] == ["x"]

    def test_unknown_format_raises(self, tmp_cache_root, clean_registry):
        registry.register(_StubSource("a"))
        ctx = ContextCollector().collect(ContextRequest())
        with pytest.raises(ValueError, match="unknown format"):
            ContextCurator().curate(ctx, format="yaml")


# ---------------------------------------------------------------------------
# Tasks source
# ---------------------------------------------------------------------------


class TestTasksSource:
    def test_collect_shapes_items(self, tmp_cache_root, clean_registry):
        from work_buddy.context.sources.tasks import TasksSource

        src = TasksSource()
        with patch(
            "work_buddy.obsidian.tasks.store.query",
            side_effect=lambda state: {
                "focused": [{"task_id": "t-1", "contract": "c1"}],
                "mit": [{"task_id": "t-2", "contract": ""}],
                "inbox": [{"task_id": "t-3", "contract": ""}],
            }.get(state, []),
        ), patch(
            "work_buddy.triage.task_match._read_task_texts",
            return_value={"t-1": "first", "t-2": "second", "t-3": "third"},
        ):
            section = src.collect(ContextRequest())

        assert [it["task_id"] for it in section.items] == ["t-1", "t-2", "t-3"]
        assert section.items[0]["state"] == "focused"
        assert section.items[0]["contract"] == "c1"

    def test_render_brief_caps_at_5(self):
        from work_buddy.context.sources.tasks import TasksSource

        items = [{"task_id": f"t-{i}", "state": "mit", "text": f"task {i}"} for i in range(10)]
        section = ContextSection(source="tasks", items=items)
        rendered = TasksSource().render(section, ContextDepth.BRIEF)
        assert "Active Tasks (10)" in rendered
        # Count bullets for tasks (exclude the "… (N more)" line)
        bullets = [l for l in rendered.splitlines() if l.startswith("- [")]
        assert len(bullets) == 5

    def test_render_normal_caps_at_12(self):
        from work_buddy.context.sources.tasks import TasksSource

        items = [{"task_id": f"t-{i}", "state": "mit", "text": f"task {i}"} for i in range(20)]
        section = ContextSection(source="tasks", items=items)
        rendered = TasksSource().render(section, ContextDepth.NORMAL)
        bullets = [l for l in rendered.splitlines() if l.startswith("- [")]
        assert len(bullets) == 12


# ---------------------------------------------------------------------------
# Projects source
# ---------------------------------------------------------------------------


class TestProjectsSource:
    def test_collect_merges_projects_and_contracts(self, tmp_cache_root, clean_registry):
        from work_buddy.context.sources.projects import ProjectsSource

        src = ProjectsSource()
        with patch(
            "work_buddy.projects.store.list_projects",
            return_value=[{"slug": "p1", "name": "P1", "status": "active", "description": "d1"}],
        ), patch(
            "work_buddy.contracts.active_contracts",
            return_value=[{"title": "C1", "status": "active", "deadline": "", "claim": "claim text"}],
        ):
            section = src.collect(ContextRequest())

        types = [it["type"] for it in section.items]
        assert types == ["project", "contract"]
        assert section.metadata["project_count"] == 1
        assert section.metadata["contract_count"] == 1

    def test_render_truncates_long_descriptions_at_normal(self):
        from work_buddy.context.sources.projects import ProjectsSource

        desc = "First sentence. " + ("x" * 500)
        items = [{"type": "project", "slug": "p", "description": desc}]
        section = ContextSection(source="projects", items=items)
        rendered = ProjectsSource().render(section, ContextDepth.NORMAL)
        # First-sentence cut preferred; should see "First sentence." and
        # nothing from the tail.
        assert "First sentence." in rendered
        assert "xxxxx" not in rendered


# ---------------------------------------------------------------------------
# Git source
# ---------------------------------------------------------------------------


class TestGitSource:
    def _fake_subprocess(self, *, stdout, returncode=0):
        def _run(args, **kwargs):
            result = MagicMock()
            result.stdout = stdout
            result.stderr = ""
            result.returncode = returncode
            return result
        return _run

    def test_collect_parses_commits(self, tmp_cache_root, clean_registry, tmp_path):
        from work_buddy.context.sources.git import GitSource

        sample = (
            "abc123fullsha\x1fabc123\x1f2026-04-20T10:00:00+00:00\x1fAlice\x1ffirst commit\n"
            "def456fullsha\x1fdef456\x1f2026-04-20T11:00:00+00:00\x1fBob\x1fsecond commit\n"
        )
        # GitSource default path: git log + git rev-parse HEAD per repo.
        call_sequence = [
            MagicMock(stdout=sample, stderr="", returncode=0),
            MagicMock(stdout="abc123fullsha\n", stderr="", returncode=0),
        ]
        # Force single-repo scope via custom.repo_path so the test doesn't
        # depend on load_config's repos_root discovery.
        req = ContextRequest(custom={"git": {"repo_path": str(tmp_path)}})
        with patch("subprocess.run", side_effect=call_sequence):
            section = GitSource().collect(req)

        assert len(section.items) == 2
        # Multi-repo sorts by date desc so the newer commit surfaces first.
        assert section.items[0]["short"] == "def456"
        assert section.items[0]["subject"] == "second commit"
        assert section.items[1]["short"] == "abc123"
        # Multi-repo schema: head lives per-repo under metadata["repos"].
        assert section.metadata["repos"][0]["head"] == "abc123fullsha"

    def test_is_stale_when_head_moved(self, tmp_cache_root, clean_registry):
        from work_buddy.context.sources.git import GitSource

        cached = ContextSection(source="git", items=[], metadata={"head": "old_sha"})
        with patch(
            "subprocess.run",
            return_value=MagicMock(stdout="new_sha\n", stderr="", returncode=0),
        ):
            assert GitSource().is_stale(cached, ContextRequest()) is True

    def test_is_stale_false_when_head_matches(self, tmp_cache_root, clean_registry):
        from work_buddy.context.sources.git import GitSource

        cached = ContextSection(source="git", items=[], metadata={"head": "same_sha"})
        with patch(
            "subprocess.run",
            return_value=MagicMock(stdout="same_sha\n", stderr="", returncode=0),
        ):
            assert GitSource().is_stale(cached, ContextRequest()) is False


# ---------------------------------------------------------------------------
# build_triage_context retrofit
# ---------------------------------------------------------------------------


class TestBuildTriageContextRetrofit:
    def test_returns_expected_dict_shape(self, tmp_cache_root):
        from work_buddy.triage.recommend import build_triage_context

        with patch(
            "work_buddy.obsidian.tasks.store.query",
            side_effect=lambda state: {"focused": [{"task_id": "t-f", "contract": ""}]}.get(state, []),
        ), patch(
            "work_buddy.triage.task_match._read_task_texts",
            return_value={"t-f": "focus task"},
        ), patch(
            "work_buddy.projects.store.list_projects",
            return_value=[{"slug": "p", "name": "P", "status": "active", "description": ""}],
        ), patch(
            "work_buddy.contracts.active_contracts",
            return_value=[{"title": "C", "status": "active", "deadline": "", "claim": ""}],
        ), patch(
            # Constrain GitSource to a single repo so the subprocess mock
            # sequence matches (1 git log + 1 rev-parse HEAD).
            "work_buddy.context.sources.git._resolve_repos",
            return_value=[Path("/fake/repo")],
        ), patch(
            "subprocess.run",
            side_effect=[
                MagicMock(stdout="s1sha\x1fs1\x1f2026-04-20\x1fa\x1fm1", stderr="", returncode=0),
                MagicMock(stdout="s1sha\n", stderr="", returncode=0),
            ],
        ):
            result = build_triage_context()

        # Shape is preserved for backward compat.
        assert set(result.keys()) == {
            "active_tasks", "active_contracts",
            "active_projects", "recent_commits",
        }
        assert result["active_tasks"][0]["task_id"] == "t-f"
        assert result["active_projects"][0]["slug"] == "p"
        assert result["active_contracts"][0]["title"] == "C"
        # Commits come back as "<short> <subject>" strings, matching the
        # pre-refactor `git log --oneline` shape.
        assert result["recent_commits"] == ["s1 m1"]

    def test_max_tasks_cap_is_honored(self, tmp_cache_root):
        from work_buddy.triage.recommend import build_triage_context

        with patch(
            "work_buddy.obsidian.tasks.store.query",
            side_effect=lambda state: [
                {"task_id": f"t-{state}-{i}", "contract": ""} for i in range(10)
            ],
        ), patch(
            "work_buddy.triage.task_match._read_task_texts",
            return_value={
                f"t-{state}-{i}": f"{state} task {i}"
                for state in ("inbox", "mit", "focused")
                for i in range(10)
            },
        ), patch(
            "work_buddy.projects.store.list_projects",
            return_value=[],
        ), patch(
            "work_buddy.contracts.active_contracts",
            return_value=[],
        ), patch(
            "subprocess.run",
            side_effect=[
                MagicMock(stdout="", stderr="", returncode=0),
                MagicMock(stdout="", stderr="", returncode=0),
            ],
        ):
            result = build_triage_context(max_tasks=5)

        assert len(result["active_tasks"]) == 5

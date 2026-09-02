"""Tests for ``work_buddy.pipelines.capability.run_source_pipeline``
— the unified MCP entry point.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.pipelines.capability import (
    PIPELINES,
    UnknownSourceError,
    run_source_pipeline,
)
from work_buddy.pipelines.types import PipelineRun


class TestRegistry:
    def test_registry_contains_live_non_journal_sources(self):
        assert "chrome_triage" in PIPELINES
        assert "email_triage" in PIPELINES
        assert "journal_backlog" not in PIPELINES


class TestDispatch:
    def test_unknown_source_raises(self):
        with pytest.raises(UnknownSourceError, match="not.*known|Unknown"):
            run_source_pipeline(source="not_a_real_source")

    def test_retired_journal_backlog_is_not_dispatchable(self):
        with pytest.raises(UnknownSourceError, match="journal_backlog"):
            run_source_pipeline(
                source="journal_backlog",
                journal_date="2026-04-01",
            )

    def test_chrome_dispatches_to_pipeline(self):
        fake_run = PipelineRun(
            pipeline_name="chrome_triage",
            umbrella_id="th-chrome",
            child_thread_ids=(),
            item_count=0,
            cluster_count=0,
        )
        with patch(
            "work_buddy.pipelines.capability.run_pipeline",
            return_value=fake_run,
        ):
            result = run_source_pipeline(
                source="chrome_triage",
                engagement_window="24h",
            )
        assert result["pipeline_name"] == "chrome_triage"
        assert result["umbrella_id"] == "th-chrome"

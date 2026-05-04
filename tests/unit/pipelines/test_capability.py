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
    def test_registry_contains_chrome_and_journal(self):
        assert "chrome_triage" in PIPELINES
        assert "journal_backlog" in PIPELINES


class TestDispatch:
    def test_unknown_source_raises(self):
        with pytest.raises(UnknownSourceError, match="not.*known|Unknown"):
            run_source_pipeline(source="not_a_real_source")

    def test_journal_dispatches_to_pipeline(self):
        fake_run = PipelineRun(
            pipeline_name="journal_backlog",
            umbrella_id="th-fake",
            child_thread_ids=("th-c1", "th-c2"),
            item_count=4,
            cluster_count=2,
        )
        with patch(
            "work_buddy.pipelines.capability.run_pipeline",
            return_value=fake_run,
        ) as mock_run:
            result = run_source_pipeline(
                source="journal_backlog",
                journal_date="2026-04-01",
            )
        assert result["pipeline_name"] == "journal_backlog"
        assert result["umbrella_id"] == "th-fake"
        assert result["item_count"] == 4
        # The pipeline factory was instantiated, then run_pipeline
        # was called with kwargs forwarded.
        call_args = mock_run.call_args
        forwarded_kwargs = call_args.kwargs
        assert forwarded_kwargs["journal_date"] == "2026-04-01"

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

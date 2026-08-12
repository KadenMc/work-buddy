from __future__ import annotations

import ast
from pathlib import Path

from work_buddy.journal_capture.migration import JOURNAL_CONTENT_CALLSITES


ROOT = Path(__file__).resolve().parents[3]


def _function_source(path: str, name: str) -> str:
    source = (ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )
    segment = ast.get_source_segment(source, node)
    assert segment is not None
    return segment


def test_reviewed_journal_content_callsites_still_exist() -> None:
    for locator in JOURNAL_CONTENT_CALLSITES:
        path_text, _separator, symbol = locator.partition(":")
        path = ROOT / path_text
        assert path.is_file(), f"reviewed Journal callsite disappeared: {locator}"
        if symbol:
            assert f"def {symbol}(" in path.read_text(encoding="utf-8")


def test_production_journal_content_calls_route_through_one_adapter() -> None:
    routed = {
        ("work_buddy/journal.py", "read_journal_state"): "_journal_content_adapter",
        ("work_buddy/journal.py", "_append_to_journal_locked"): "adapter.write_day_cas",
        ("work_buddy/journal.py", "extract_sign_in"): "_journal_content_adapter_for_path",
        ("work_buddy/journal.py", "write_sign_in"): "adapter.write_day_cas",
        ("work_buddy/journal.py", "persist_briefing_to_journal"): "adapter.write_day_cas",
        ("work_buddy/journal_capture/service.py", "_materialize"): "self.adapter.append",
        ("work_buddy/journal_backlog/extract.py", "extract_running_notes"): "JournalContentAdapter",
        ("work_buddy/journal_backlog/rewrite.py", "rewrite_running_notes"): "adapter.write_day_cas",
        ("work_buddy/journal_backlog/route.py", "_append_to_note_impl"): "JournalContentAdapter",
        ("work_buddy/obsidian/day_planner/env.py", "get_todays_plan"): "adapter.read_day",
        ("work_buddy/obsidian/day_planner/env.py", "write_plan"): "adapter.write_day_cas",
        ("work_buddy/health/fixers.py", "_append_section"): "JournalContentAdapter",
        ("work_buddy/threads/cleanup_adapters.py", "_journal_note_cleanup"): "adapter.write_day_cas",
        ("work_buddy/collectors/obsidian_collector.py", "_get_journal_entries"): "JournalContentAdapter",
        ("work_buddy/collectors/obsidian_collector.py", "_get_journal_stats"): "JournalContentAdapter",
        ("work_buddy/collectors/obsidian_collector.py", "_parse_wellness"): "JournalContentAdapter",
        ("work_buddy/activity.py", "infer_activity"): "JournalContentAdapter",
    }
    for (path, function), token in routed.items():
        body = _function_source(path, function)
        assert token in body, f"{path}:{function} bypasses JournalContentAdapter"

    health = _function_source("work_buddy/health/fixers.py", "_append_section")
    assert ".write_text(" not in health
    assert "create_day_if_absent" in health

    writer = (ROOT / "work_buddy/obsidian/vault_writer.py").read_text(
        encoding="utf-8"
    )
    assert "assert_cowork_owned_sections_unchanged(" in writer
    assert "journal_owned_write" in writer

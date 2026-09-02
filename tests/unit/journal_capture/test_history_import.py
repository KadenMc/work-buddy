from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from work_buddy.journal_capture.history_import import (
    LegacyJournalImportError,
    freeze_inventory,
    inventory_sha256,
    parse_inventory_report,
    parse_legacy_journal,
    verify_frozen_inventory,
)


def _write(path: Path, value: bytes) -> Path:
    path.write_bytes(value)
    return path


def test_parser_dispositions_every_exact_byte_without_executing_residue(tmp_path: Path):
    raw = (
        b"\xef\xbb\xbf---\r\ncontext_anchor: 08:00\r\n---\r\n"
        b"<%* throw new Error('must remain inert') %>\r\n"
        b"# **Sign-In**\r\nfocus: 3\r\n"
        b"# **Log**\r\n* 9:00 AM - same text #wb/journal/log\r\n"
        b"# **Running Notes / Considerations**\r\n"
        b"<!-- wb:journal-entry/v1 id=entry_12345678 "
        b"content-sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->\r\n"
        b"same text\r\n<!-- /wb:journal-entry/v1 id=entry_12345678 -->\r\n"
        b"# **Sign-Off**\r\nDone.\r\n"
    )
    path = _write(tmp_path / "2026-08-27.md", raw)

    parsed = parse_legacy_journal(path, root=tmp_path, cohort_id="cohort-a")

    assert b"".join(span.content for span in parsed.spans) == raw
    assert [(span.start_byte, span.end_byte) for span in parsed.spans] == [
        (parsed.spans[index - 1].end_byte if index else 0, span.end_byte)
        for index, span in enumerate(parsed.spans)
    ]
    assert [span.disposition for span in parsed.spans] == [
        "frontmatter",
        "static_or_unknown_residue",
        "check_in_section",
        "log_section",
        "running_notes_section",
        "sign_off_section",
    ]
    marker = parsed.spans[4].managed_projections[0]
    assert marker.entry_id == "entry_12345678"
    assert marker.closing_marker_present is True
    assert "must remain inert" not in str(parsed.to_receipt())
    assert "same text" not in str(parsed.to_receipt())


def test_logically_repeated_text_uses_occurrence_location_not_body_alone(tmp_path: Path):
    path = tmp_path / "2026-08-26.md"
    path.write_text(
        "# **Log**\nrepeated\n# **Running Notes / Considerations**\nrepeated\n",
        encoding="utf-8",
    )

    parsed = parse_legacy_journal(path, root=tmp_path, cohort_id="cohort-a")

    assert len(parsed.spans) == 2
    assert parsed.spans[0].logical_id != parsed.spans[1].logical_id
    assert parsed.spans[0].raw_sha256 != parsed.spans[1].raw_sha256
    assert parsed.spans[0].section_key == "day_stream"
    assert parsed.spans[1].section_key == "notes"


def test_parser_version_is_receipt_metadata_not_logical_identity(tmp_path: Path):
    path = tmp_path / "2026-08-25.md"
    path.write_text("# **Log**\nhello\n", encoding="utf-8")
    parsed = parse_legacy_journal(path, root=tmp_path, cohort_id="cohort-a")

    revised_receipt = dataclasses.replace(parsed, parser_version="future-parser/v2")

    assert revised_receipt.spans[0].logical_id == parsed.spans[0].logical_id


def test_invalid_encoding_and_malformed_frontmatter_quarantine_whole_file(tmp_path: Path):
    invalid = _write(tmp_path / "2026-08-24.md", b"\xff\xfe\x00broken")
    malformed = _write(tmp_path / "2026-08-23.md", b"---\nkey: value\n# **Log**\ntext")

    invalid_parse = parse_legacy_journal(
        invalid, root=tmp_path, cohort_id="cohort-a"
    )
    malformed_parse = parse_legacy_journal(
        malformed, root=tmp_path, cohort_id="cohort-a"
    )

    assert invalid_parse.spans[0].reason_code == "encoding_failure"
    assert invalid_parse.spans[0].content == invalid.read_bytes()
    assert malformed_parse.spans[0].reason_code == "malformed_frontmatter"
    assert malformed_parse.spans[0].content == malformed.read_bytes()


def test_frozen_inventory_detects_content_metadata_and_file_set_drift(tmp_path: Path):
    first = _write(tmp_path / "2026-08-22.md", b"# **Log**\nvalue\n")
    frozen = freeze_inventory(tmp_path)
    digest = inventory_sha256(frozen)

    assert verify_frozen_inventory(tmp_path, frozen) == frozen
    assert len(digest) == 64

    first.write_bytes(b"# **Log**\nchanged\n")
    with pytest.raises(LegacyJournalImportError, match="corpus changed"):
        verify_frozen_inventory(tmp_path, frozen)

    frozen = freeze_inventory(tmp_path)
    _write(tmp_path / "2026-08-21.md", b"# **Log**\nnew\n")
    with pytest.raises(LegacyJournalImportError, match="file set changed"):
        verify_frozen_inventory(tmp_path, frozen)


def test_report_is_prose_free_and_counts_quarantine(tmp_path: Path):
    valid = _write(tmp_path / "2026-08-20.md", b"# **Log**\nprivate sentence\n")
    unknown = _write(tmp_path / "not-a-day.md", b"private unknown sentence")
    parsed = [
        parse_legacy_journal(valid, root=tmp_path, cohort_id="cohort-a"),
        parse_legacy_journal(unknown, root=tmp_path, cohort_id="cohort-a"),
    ]

    report = parse_inventory_report(parsed)

    assert report["fileCount"] == 2
    assert report["quarantineReasons"] == {"invalid_day_path": 1}
    assert report["containsProse"] is False
    assert "private" not in str(report)


def test_inventory_rejects_paths_outside_direct_allowlist(tmp_path: Path):
    outside = tmp_path / "nested"
    outside.mkdir()
    _write(outside / "2026-08-19.md", b"# **Log**\nvalue\n")

    with pytest.raises(LegacyJournalImportError, match="file names"):
        freeze_inventory(tmp_path, allowlist=["nested/2026-08-19.md"])

from __future__ import annotations

import hashlib

import pytest

from work_buddy.journal_capture.content_adapter import JournalContentAdapter, marker_for
from work_buddy.journal_capture.models import (
    CaptureTarget,
    JournalEntry,
    ProcessingState,
    ProjectionState,
    JournalProjectionDiverged,
)


def _entry(*, entry_id: str, kind: CaptureTarget, markdown: str) -> JournalEntry:
    digest = hashlib.sha256(markdown.encode()).hexdigest()
    return JournalEntry(
        entry_id=entry_id,
        capture_id=f"capture-{entry_id}",
        day_id="2026-08-09",
        entry_kind=kind,
        source_ref=f"wb-source://authority/{entry_id}",
        content_sha256=digest,
        markdown=markdown,
        created_at="2026-08-09T15:15:00-04:00",
        updated_at="2026-08-09T15:15:00-04:00",
        version=1,
        resolution_state="open",
        processing_status=ProcessingState.NOT_REQUESTED,
        annotation=None,
        processing_error_code=None,
        projection_state=ProjectionState.PENDING,
        projection_marker=marker_for(entry_id, digest),
        projection_base_sha256=None,
        projection_result_sha256=None,
    )


def _vault(tmp_path):
    journal = tmp_path / "journal"
    journal.mkdir()
    path = journal / "2026-08-09.md"
    path.write_text(
        "# **Log**\n\n* 9:00 AM - Existing. #wb/journal/log\n\n"
        "# **Running Notes / Considerations**\n\nlegacy text\n\n% RUNNING END\n",
        encoding="utf-8",
    )
    return path


def _write(_rel, abs_path, content, **_kw):
    abs_path.write_bytes(content.encode("utf-8"))
    return True


def test_create_day_if_absent_never_replaces_a_concurrent_day(
    tmp_path, monkeypatch
):
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setattr("work_buddy.obsidian.vault_writer.vault_write", _write)
    adapter = JournalContentAdapter(tmp_path)

    created = adapter.create_day_if_absent(
        "2026-08-09",
        content="# Log\n\n",
        content_hint="# Log",
    )
    assert created.content == "# Log\n\n"

    path = journal / "2026-08-09.md"
    path.write_bytes(b"# Log\n\nconcurrent\n")
    observed = adapter.create_day_if_absent(
        "2026-08-09",
        content="# Running Notes\n\n",
        content_hint="# Running Notes",
    )
    assert observed.content == "# Log\n\nconcurrent\n"
    assert path.read_bytes() == b"# Log\n\nconcurrent\n"


def test_generic_vault_writer_preserves_cowork_owned_blocks(
    tmp_path, monkeypatch
):
    from work_buddy.obsidian.vault_writer import vault_write

    journal = tmp_path / "journal"
    journal.mkdir()
    path = journal / "2026-08-09.md"
    marker_id = "a" * 32
    owned = (
        f"<!-- wb:journal-entry/v1 id={marker_id} content-sha256={'b' * 64} -->\n"
        "<!-- wb:cowork-projection/v1 binding=bound epoch=1 head=head -->\n"
        "canonical\n"
        "<!-- /wb:cowork-projection/v1 binding=bound -->\n"
        f"<!-- /wb:journal-entry/v1 id={marker_id} -->"
    )
    original = f"# Unknown\nold\n\n# **Running Notes / Considerations**\n\n{owned}\n"
    path.write_bytes(original.encode())
    monkeypatch.setattr("work_buddy.obsidian.bridge.is_available", lambda: False)
    monkeypatch.setattr(
        "work_buddy.obsidian.bridge.is_obsidian_running", lambda: False
    )

    changed_owned = original.replace("canonical", "generic overwrite")
    with pytest.raises(JournalProjectionDiverged):
        vault_write("journal/2026-08-09.md", path, changed_owned)
    assert path.read_bytes() == original.encode()

    changed_unknown = original.replace("# Unknown\nold", "# Unknown\nnew")
    assert vault_write("journal/2026-08-09.md", path, changed_unknown)
    assert path.read_bytes() == changed_unknown.encode()

    log_id = hashlib.sha256(b"journal-log/v1\0" + b"2026-08-09").hexdigest()[:32]
    log_owned = owned.replace(marker_id, log_id)
    logical_log = f"# **Log**\n\n{log_owned}\n\n# Unknown\nkept\n"
    path.write_bytes(logical_log.encode())
    with pytest.raises(JournalProjectionDiverged):
        vault_write(
            "journal/2026-08-09.md",
            path,
            logical_log.replace("\n\n# Unknown", "\nexternal log line\n\n# Unknown"),
        )
    assert path.read_bytes() == logical_log.encode()


def test_identical_running_notes_are_distinct_and_replay_safe(tmp_path, monkeypatch):
    path = _vault(tmp_path)
    monkeypatch.setattr(
        "work_buddy.obsidian.vault_writer.vault_write",
        _write,
    )
    adapter = JournalContentAdapter(tmp_path)
    one = _entry(entry_id="a" * 32, kind=CaptureTarget.RUNNING_NOTES, markdown="same")
    two = _entry(entry_id="b" * 32, kind=CaptureTarget.RUNNING_NOTES, markdown="same")

    first = adapter.append(one)
    replay = adapter.append(one)
    adapter.append(two)

    content = path.read_text(encoding="utf-8")
    assert first.recovered_existing_marker is False
    assert replay.recovered_existing_marker is True
    assert content.count("\nsame\n") == 2
    assert content.count("wb:journal-entry/v1") == 4


def test_log_preserves_multiline_domain_markdown_and_stable_marker(tmp_path, monkeypatch):
    path = _vault(tmp_path)
    monkeypatch.setattr(
        "work_buddy.obsidian.vault_writer.vault_write",
        _write,
    )
    adapter = JournalContentAdapter(tmp_path)
    entry = _entry(
        entry_id="c" * 32,
        kind=CaptureTarget.LOG,
        markdown="first line\nsecond line",
    )

    adapter.append(entry, stated_at="2026-08-09T15:15:00-04:00")
    content = path.read_text(encoding="utf-8")
    assert marker_for(entry.entry_id, entry.content_sha256) in content
    assert "* 3:15 PM - first line #wb/journal/log" in content
    assert "  second line" in content

"""Unit tests for _rewrite_namespace_tags (Phase 4 line-rewrite primitive)."""

from __future__ import annotations

import pytest

from work_buddy.obsidian.tasks.mutations import _rewrite_namespace_tags


def test_adds_tags_before_id():
    line = "- [ ] #todo draft outline 🆔 t-abc"
    new = _rewrite_namespace_tags(line, ["paper/ecg"])
    assert new == "- [ ] #todo draft outline #paper/ecg 🆔 t-abc"


def test_replaces_existing_namespace_tag():
    line = "- [ ] #todo draft #paper/old 🆔 t-abc"
    new = _rewrite_namespace_tags(line, ["paper/new"])
    # Old namespace tag dropped, new inserted before 🆔
    assert "#paper/old" not in new
    assert "#paper/new" in new
    assert new.endswith("🆔 t-abc")


def test_preserves_projects_tag():
    line = "- [ ] #todo review #projects/ecg-classifier 🆔 t-abc"
    new = _rewrite_namespace_tags(line, ["paper/review"])
    assert "#projects/ecg-classifier" in new
    assert "#paper/review" in new


def test_preserves_wikilink_and_due_date():
    line = (
        "- [ ] #todo run experiment "
        "[[aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee|📓]] "
        "#experiment/old 🆔 t-abc 📅 2026-05-01"
    )
    new = _rewrite_namespace_tags(line, ["experiment/new", "paper/ecg"])
    assert "[[aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee|📓]]" in new
    assert "📅 2026-05-01" in new
    assert "#experiment/old" not in new
    assert "#experiment/new" in new
    assert "#paper/ecg" in new


def test_removes_all_when_empty_list():
    line = "- [ ] #todo draft #paper/ecg #admin 🆔 t-abc"
    new = _rewrite_namespace_tags(line, [])
    assert "#paper/ecg" not in new
    assert "#admin" not in new
    assert "#todo" in new
    assert "🆔 t-abc" in new


def test_no_id_appends_at_end():
    line = "- [ ] #todo bare task"
    new = _rewrite_namespace_tags(line, ["admin/misc"])
    assert new.endswith("#admin/misc")


def test_dedup_input_tags():
    line = "- [ ] #todo thing 🆔 t-abc"
    new = _rewrite_namespace_tags(line, ["foo", "foo", "FOO"])
    # Only one instance inserted.
    assert new.count("#foo") + new.count("#FOO") == 1


def test_malformed_new_tag_rejected():
    line = "- [ ] #todo thing 🆔 t-abc"
    with pytest.raises(ValueError):
        _rewrite_namespace_tags(line, ["bad tag with space"])


def test_opt_in_prefix_stripped_then_reinserted():
    line = "- [ ] #todo thing #ns/keepme 🆔 t-abc"
    # Rewrite with the same tag: old is dropped and re-inserted in canonical
    # position (before 🆔), preserving semantics.
    new = _rewrite_namespace_tags(line, ["ns/keepme"])
    assert new.count("#ns/keepme") == 1
    assert new.endswith("🆔 t-abc")

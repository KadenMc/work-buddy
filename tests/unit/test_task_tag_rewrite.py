"""Unit tests for _rewrite_namespace_tags and project-slug validation."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.obsidian.tasks.mutations import (
    _normalize_tags,
    _project_slug_from_tag,
    _rewrite_namespace_tags,
)


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


def test_replaces_projects_tag():
    """Project tags are user-modifiable: passing a new list replaces them."""
    line = "- [ ] #todo review #projects/ecg-classifier 🆔 t-abc"
    new = _rewrite_namespace_tags(line, ["projects/ecg-classifier/writing-prep"])
    assert "#projects/ecg-classifier" in new  # the deeper subtree contains the slug
    assert "#projects/ecg-classifier/writing-prep" in new
    # The original shallow #projects/ecg-classifier (without subtree) is gone.
    assert " #projects/ecg-classifier " not in f" {new} "
    assert new.endswith("🆔 t-abc")


def test_strips_projects_tag_when_omitted():
    """Omitting a project tag from the new list strips it from the line."""
    line = "- [ ] #todo review #projects/ecg-classifier #paper/old 🆔 t-abc"
    new = _rewrite_namespace_tags(line, ["paper/new"])
    assert "#projects/" not in new
    assert "#paper/old" not in new
    assert "#paper/new" in new


def test_preserves_tasker_state_tag():
    """`#tasker/*` is store-owned — never rewritten by this primitive."""
    line = "- [ ] #todo draft #tasker/state/focused 🆔 t-abc"
    new = _rewrite_namespace_tags(line, ["paper/review"])
    assert "#tasker/state/focused" in new
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


# ── _project_slug_from_tag ──────────────────────────────────────


def test_project_slug_from_tag_extracts_first_segment():
    assert _project_slug_from_tag("projects/work-buddy") == "work-buddy"
    assert _project_slug_from_tag(
        "projects/work-buddy/systems/task-system"
    ) == "work-buddy"
    assert _project_slug_from_tag("projects/Work-Buddy") == "work-buddy"


def test_project_slug_from_tag_returns_none_for_non_project_tags():
    assert _project_slug_from_tag("admin/uhn") is None
    assert _project_slug_from_tag("paper/ecg-classifier") is None
    assert _project_slug_from_tag("projects/") is None
    assert _project_slug_from_tag("projects") is None


# ── _normalize_tags project validation ──────────────────────────


def test_normalize_tags_validates_known_project_slug():
    """Known slug passes validation and the tag survives normalization."""
    with patch(
        "work_buddy.obsidian.tasks.mutations.get_project",
        # Lazy import inside _validate_project_slug_exists pulls
        # `get_project` into the mutations module's namespace at call
        # time; patching the source location is the reliable hook.
        create=True,
    ):
        with patch(
            "work_buddy.projects.store.get_project",
            return_value={"slug": "work-buddy", "status": "active"},
        ):
            out = _normalize_tags(
                ["projects/work-buddy/systems/task-system", "admin/uhn"]
            )
    assert out == ["projects/work-buddy/systems/task-system", "admin/uhn"]


def test_normalize_tags_rejects_unknown_project_slug():
    with patch("work_buddy.projects.store.get_project", return_value=None):
        with pytest.raises(ValueError, match="Unknown project slug"):
            _normalize_tags(["projects/typo-slug/anywhere"])


def test_normalize_tags_skips_validation_when_disabled():
    """validate_project_slugs=False is the idempotency-cache hash escape hatch."""
    with patch("work_buddy.projects.store.get_project", return_value=None):
        out = _normalize_tags(
            ["projects/typo-slug/anywhere"], validate_project_slugs=False
        )
    assert out == ["projects/typo-slug/anywhere"]


def test_normalize_tags_validates_only_first_segment():
    """The slug is checked; the subtree below it is free-form."""
    calls: list[str] = []

    def fake_get(slug: str):
        calls.append(slug)
        return {"slug": slug, "status": "active"}

    with patch("work_buddy.projects.store.get_project", side_effect=fake_get):
        _normalize_tags(
            ["projects/work-buddy/systems/never-seen-before-subtree"]
        )
    assert calls == ["work-buddy"]

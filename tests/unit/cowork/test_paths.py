"""Contained Folder-relative path contract for Co-work."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from work_buddy.cowork.paths import CoworkPathError, resolve_markdown_path


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/absolute.md",
        "C:/drive.md",
        "//server/share.md",
        r"docs\windows.md",
        "../escape.md",
        "docs/../escape.md",
        "docs/./file.md",
        "docs//file.md",
        "docs/file.txt",
        ".wbuddy/secret.md",
        "docs/has:stream.md",
        "docs/question?.md",
        "docs/trailing./file.md",
        "docs/trailing /file.md",
        "CON.md",
        "docs/LPT1.markdown",
        "docs/control\x00.md",
        "docs/control\n.md",
    ],
)
def test_rejects_unsafe_or_non_markdown_paths(tmp_path: Path, value: str) -> None:
    with pytest.raises(CoworkPathError):
        resolve_markdown_path(tmp_path, value)


def test_returns_normalized_immutable_result_and_os_path_key(tmp_path: Path) -> None:
    target = tmp_path / "Notes" / "Plan.Markdown"
    target.parent.mkdir()
    target.write_text("throwaway", encoding="utf-8")

    result = resolve_markdown_path(tmp_path, "Notes/Plan.Markdown")

    assert result.normalized == "Notes/Plan.Markdown"
    assert result.path == target.resolve()
    expected_key = result.normalized.casefold() if os.name == "nt" else result.normalized
    assert result.path_key == expected_key
    with pytest.raises((AttributeError, TypeError)):
        result.normalized = "other.md"  # type: ignore[misc]


def test_create_allows_missing_contained_parents(tmp_path: Path) -> None:
    result = resolve_markdown_path(
        tmp_path,
        "new/nested/document.md",
        for_create=True,
    )
    assert result.path == (tmp_path / "new" / "nested" / "document.md").resolve()


def test_rejects_symlink_or_junction_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("test account cannot create directory links")

    with pytest.raises(CoworkPathError, match="symlink|reparse"):
        resolve_markdown_path(tmp_path, "linked/escape.md", for_create=True)

"""Contained Folder-relative path contract for Co-work."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from work_buddy.cowork.paths import (
    CoworkPathError,
    ResolvedMarkdownPath,
    ResolvedRelativeFilePath,
    resolve_document_source_path,
    resolve_markdown_path,
    resolve_relative_file_path,
    resolve_writeback_target,
)


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

    assert isinstance(result, ResolvedMarkdownPath)
    assert isinstance(result, ResolvedRelativeFilePath)
    assert result.normalized == "Notes/Plan.Markdown"
    assert result.path == target.resolve()
    expected_key = result.normalized.casefold() if os.name == "nt" else result.normalized
    assert result.path_key == expected_key
    with pytest.raises((AttributeError, TypeError)):
        result.normalized = "other.md"  # type: ignore[misc]


def test_generic_resolver_accepts_a_non_markdown_source(tmp_path: Path) -> None:
    target = tmp_path / "Sources" / "Paper.docx"
    target.parent.mkdir()
    target.write_bytes(b"future importer source")

    result = resolve_relative_file_path(tmp_path, "Sources/Paper.docx")

    assert type(result) is ResolvedRelativeFilePath
    assert result.normalized == "Sources/Paper.docx"
    assert result.path == target.resolve()
    expected_key = result.normalized.casefold() if os.name == "nt" else result.normalized
    assert result.path_key == expected_key


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/absolute.docx",
        "C:/drive.docx",
        "//server/share.docx",
        r"docs\windows.docx",
        "../escape.docx",
        "docs/../escape.docx",
        "docs/./file.docx",
        "docs//file.docx",
        ".wbuddy/secret.docx",
        "docs/has:stream.docx",
        "docs/question?.docx",
        "docs/trailing./file.docx",
        "docs/trailing /file.docx",
        "CON.docx",
        "docs/LPT1.docx",
        "docs/control\x00.docx",
        "docs/control\n.docx",
    ],
)
def test_generic_resolver_rejects_the_same_unsafe_path_shapes(
    tmp_path: Path,
    value: str,
) -> None:
    with pytest.raises(CoworkPathError):
        resolve_relative_file_path(tmp_path, value)


def test_create_allows_missing_contained_parents(tmp_path: Path) -> None:
    result = resolve_markdown_path(
        tmp_path,
        "new/nested/document.md",
        for_create=True,
    )
    assert result.path == (tmp_path / "new" / "nested" / "document.md").resolve()


def test_generic_create_allows_missing_contained_parents(tmp_path: Path) -> None:
    result = resolve_relative_file_path(
        tmp_path,
        "new/nested/source.docx",
        for_create=True,
    )
    assert result.path == (tmp_path / "new" / "nested" / "source.docx").resolve()


def test_document_source_resolution_distinguishes_imports_from_writeback(
    tmp_path: Path,
) -> None:
    imported = SimpleNamespace(
        path="Sources/Paper.docx",
        meta_json=json.dumps(
            {
                "source": {
                    "kind": "file_import",
                    "writeback_policy": "never",
                }
            }
        ),
    )
    file_backed = SimpleNamespace(path="Notes/Plan.md", meta_json=None)
    (tmp_path / "Sources").mkdir()
    (tmp_path / "Sources" / "Paper.docx").write_bytes(b"source")
    (tmp_path / "Notes").mkdir()
    (tmp_path / "Notes" / "Plan.md").write_text("plan", encoding="utf-8")

    assert (
        resolve_document_source_path(tmp_path, imported).normalized
        == "Sources/Paper.docx"
    )
    assert (
        resolve_document_source_path(tmp_path, file_backed).normalized
        == "Notes/Plan.md"
    )
    assert resolve_writeback_target(tmp_path, file_backed).normalized == "Notes/Plan.md"
    with pytest.raises(CoworkPathError, match="writeback target"):
        resolve_writeback_target(tmp_path, imported)


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

    with pytest.raises(CoworkPathError, match="symlink|reparse"):
        resolve_relative_file_path(
            tmp_path,
            "linked/escape.docx",
            for_create=True,
        )

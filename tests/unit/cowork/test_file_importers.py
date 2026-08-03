from __future__ import annotations

import pytest

from work_buddy.cowork import bootstrap
from work_buddy.cowork.bootstrap import MAX_SOURCE_BYTES
from work_buddy.cowork.file_importers import (
    DEFAULT_FILE_IMPORTERS,
    FileImporter,
    FileImporterRegistry,
    MARKDOWN_FILE_IMPORTER,
    MARKDOWN_MAX_SOURCE_BYTES,
)


@pytest.mark.parametrize("path", ["notes.md", "notes.markdown", "NOTES.MD"])
def test_default_registry_resolves_markdown(path: str) -> None:
    selection = DEFAULT_FILE_IMPORTERS.selection_for_path(
        path,
        relative_path=f"drafts/{path}",
        source_sha256="a" * 64,
    )

    assert selection is not None
    assert selection.to_dict() == {
        "path": f"drafts/{path}",
        "importer_id": "markdown/v1",
        "media_type": "text/markdown",
        "source_sha256": "a" * 64,
        "importer": {
            "importer_id": "markdown/v1",
            "display_name": "Markdown",
            "source_format": "markdown",
            "media_type": "text/markdown",
            "suffixes": [".md", ".markdown"],
            "max_source_bytes": MARKDOWN_MAX_SOURCE_BYTES,
        },
    }


def test_default_registry_rejects_unimplemented_formats() -> None:
    assert DEFAULT_FILE_IMPORTERS.importer_for_path("paper.docx") is None


def test_registry_resolves_a_frozen_id_and_path_binding() -> None:
    synthetic = FileImporter(
        "fixture/v2",
        (".wbtest",),
        "application/x-wbtest",
        4096,
        display_name="Fixture document",
        source_format="fixture",
    )
    registry = FileImporterRegistry((MARKDOWN_FILE_IMPORTER, synthetic))

    assert registry.importer_by_id("fixture/v2") is synthetic
    assert (
        registry.resolve_binding("inputs/source.wbtest", importer_id="fixture/v2")
        is synthetic
    )
    assert (
        registry.resolve_binding("inputs/source.md", importer_id="fixture/v2")
        is None
    )
    assert registry.resolve_binding(
        "inputs/source.wbtest",
        importer_id="missing/v1",
    ) is None


def test_registry_exposes_safe_picker_metadata() -> None:
    synthetic = FileImporter(
        "fixture/v1",
        (".wbtest",),
        "application/x-wbtest",
        4096,
    )
    registry = FileImporterRegistry((MARKDOWN_FILE_IMPORTER, synthetic))

    spec = registry.picker_spec()
    assert spec.display_name == "Supported files"
    assert spec.suffixes == (".md", ".markdown", ".wbtest")
    assert spec.patterns == ("*.md", "*.markdown", "*.wbtest")
    assert spec.extension_names == ("md", "markdown", "wbtest")
    assert synthetic.descriptor() == {
        "importer_id": "fixture/v1",
        "display_name": "Fixture",
        "source_format": "fixture",
        "media_type": "application/x-wbtest",
        "suffixes": [".wbtest"],
        "max_source_bytes": 4096,
    }


def test_markdown_importer_matches_bootstrap_source_limit() -> None:
    assert MARKDOWN_FILE_IMPORTER.max_source_bytes == MARKDOWN_MAX_SOURCE_BYTES
    assert MARKDOWN_MAX_SOURCE_BYTES == MAX_SOURCE_BYTES
    assert (
        DEFAULT_FILE_IMPORTERS.maximum_source_bytes
        == MARKDOWN_FILE_IMPORTER.max_source_bytes
    )
    assert (
        bootstrap.maximum_source_upload_bytes()
        == MARKDOWN_FILE_IMPORTER.max_source_bytes
    )


def test_registry_rejects_ambiguous_suffixes() -> None:
    with pytest.raises(ValueError, match="duplicate importer suffix"):
        FileImporterRegistry(
            (
                FileImporter(
                    "markdown/v1",
                    (".md",),
                    "text/markdown",
                    MARKDOWN_MAX_SOURCE_BYTES,
                ),
                FileImporter(
                    "other/v1",
                    (".MD",),
                    "application/octet-stream",
                    MARKDOWN_MAX_SOURCE_BYTES,
                ),
            )
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("importer_id", "Fixture/v1", "versioned identifier"),
        ("importer_id", "fixture/latest", "versioned identifier"),
        ("media_type", "not a media type", "media type"),
        ("source_format", "Word document", "lowercase identifier"),
        ("display_name", " ", "printable"),
    ],
)
def test_importer_rejects_unsafe_descriptor_fields(
    field: str,
    value: str,
    message: str,
) -> None:
    kwargs: dict[str, object] = {
        "importer_id": "fixture/v1",
        "suffixes": (".wbtest",),
        "media_type": "application/x-wbtest",
        "max_source_bytes": 4096,
        "display_name": "Fixture",
        "source_format": "fixture",
    }
    kwargs[field] = value
    with pytest.raises(ValueError, match=message):
        FileImporter(**kwargs)


@pytest.mark.parametrize(
    "suffix",
    [".", ".two words", '."quoted"', ".way-too-long-extension"],
)
def test_importer_rejects_unsafe_picker_suffixes(suffix: str) -> None:
    with pytest.raises(ValueError, match="bounded ASCII dotted extensions"):
        FileImporter(
            "fixture/v1",
            (suffix,),
            "application/x-wbtest",
            4096,
        )

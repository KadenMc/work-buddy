from __future__ import annotations

import json
import os

import pytest

from work_buddy.cowork.file_importers import MARKDOWN_MAX_SOURCE_BYTES
from work_buddy.cowork.source_observation import (
    SourceObservationError,
    observe_document_source_sha256,
    read_document_source,
    source_importer_for_document,
)
from work_buddy.truth.identity import sha256_bytes
from work_buddy.truth.store import DocumentRecord


def _document(path: str, source: dict[str, object]) -> DocumentRecord:
    return DocumentRecord(
        id="doc-source-observation",
        path=path,
        title="Source observation",
        document_class="co_authored",
        content_sha256="a" * 64,
        ydoc_snapshot_sha256=None,
        created_at="2026-07-31T00:00:00Z",
        created_by_kind="human",
        created_by_ref="dashboard-user",
        meta_json=json.dumps({"source": source}),
    )


def _detached_source(**overrides: object) -> dict[str, object]:
    return {
        "kind": "file_import",
        "writeback_policy": "never",
        "sha256": "b" * 64,
        "importer_id": "markdown/v1",
        "media_type": "text/markdown",
        **overrides,
    }


def test_detached_source_observation_is_bounded_by_persisted_importer(store_ctx):
    target = store_ctx["root"] / "imports" / "oversized.md"
    target.parent.mkdir(parents=True)
    with target.open("wb") as stream:
        stream.truncate(MARKDOWN_MAX_SOURCE_BYTES + 1)
    document = _document("imports/oversized.md", _detached_source())

    assert observe_document_source_sha256(store_ctx["store"], document) is None
    with pytest.raises(SourceObservationError) as blocked:
        read_document_source(store_ctx["store"], document)

    assert blocked.value.code == "source_too_large"
    assert blocked.value.status == 413
    assert blocked.value.details == {
        "max_source_bytes": MARKDOWN_MAX_SOURCE_BYTES,
        "source_byte_length": MARKDOWN_MAX_SOURCE_BYTES + 1,
        "importer_id": "markdown/v1",
    }


def test_historical_detached_markdown_gets_only_the_bounded_markdown_fallback(
    store_ctx,
):
    data = b"# Historical import\n"
    target = store_ctx["root"] / "imports" / "historical.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(data)
    historical = _document(
        "imports/historical.md",
        {
            "kind": "imported_markdown",
            "writeback_policy": "never",
            "sha256": sha256_bytes(data),
        },
    )

    result = read_document_source(store_ctx["store"], historical)
    assert result.data == data
    assert result.sha256 == sha256_bytes(data)
    assert result.importer_id == "markdown/v1"
    assert result.max_source_bytes == MARKDOWN_MAX_SOURCE_BYTES

    non_markdown = _document(
        "imports/historical.txt",
        {
            "kind": "file_import",
            "writeback_policy": "never",
            "sha256": sha256_bytes(data),
        },
    )
    with pytest.raises(SourceObservationError) as unavailable:
        source_importer_for_document(non_markdown)
    assert unavailable.value.code == "source_importer_unavailable"


def test_malformed_detached_metadata_is_not_guessed_as_markdown():
    malformed = _document(
        "imports/malformed.md",
        {"writeback_policy": "never"},
    )

    with pytest.raises(SourceObservationError) as unavailable:
        source_importer_for_document(malformed)

    assert unavailable.value.code == "source_metadata_invalid"


def test_directory_replacement_is_never_read_as_a_source(store_ctx):
    target = store_ctx["root"] / "imports" / "directory.md"
    target.mkdir(parents=True)
    document = _document("imports/directory.md", _detached_source())

    assert observe_document_source_sha256(store_ctx["store"], document) is None
    with pytest.raises(SourceObservationError) as unavailable:
        read_document_source(store_ctx["store"], document)
    assert unavailable.value.code == "source_unavailable"
    assert "regular file" in str(unavailable.value)


def test_link_replacement_is_never_followed(store_ctx):
    outside = store_ctx["root"].parent / "outside-detached-source.md"
    outside.write_bytes(b"# Outside\n")
    target = store_ctx["root"] / "imports" / "linked.md"
    target.parent.mkdir(parents=True)
    try:
        os.symlink(outside, target)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks is unavailable on this platform")
    document = _document("imports/linked.md", _detached_source())

    assert observe_document_source_sha256(store_ctx["store"], document) is None
    with pytest.raises(SourceObservationError) as unavailable:
        read_document_source(store_ctx["store"], document)
    assert unavailable.value.code == "source_unavailable"

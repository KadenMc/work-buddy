"""Canonical Markdown reads over genuine structured state.

Every other cowork fixture stores opaque placeholder bytes where a Y.Doc
snapshot belongs, which is fine for tests that never interpret them. A
serializer test cannot use those: the packaged worker rejects anything that is
not real Yjs state. These tests bootstrap through the worker so the snapshot
under test is the same shape production writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from work_buddy.cowork import bootstrap
from work_buddy.cowork import ops as cowork_ops
from work_buddy.document_kernel.protocol import structured_head_sha256
from work_buddy.document_kernel.runtime_service import shared_document_kernel
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.identity import sha256_bytes

from .conftest import HUMAN
from .test_ops import NOW


def _bootstrap_real_document(store: Any, *, source: bytes, path: str, title: str) -> str:
    """Register a document whose snapshot is genuine worker-produced Yjs state."""
    kernel = shared_document_kernel()
    built = kernel.request(
        {
            "kind": "bootstrap_markdown",
            "sourceBase64": source,
            "sourceSha256": sha256_bytes(source),
            "newlineStyle": "lf",
            "utf8Bom": False,
            "trailingNewlineCount": 1,
        },
        request_id=f"test_bootstrap_{sha256_bytes(source)[:16]}",
    )
    snapshot = built.snapshot
    assert snapshot is not None, "worker returned no snapshot"

    intent, _ = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "create",
            "path": path,
            "title": title,
            "initial_source_sha256": sha256_bytes(source),
            "idempotency_key": f"serialize-fixture-{sha256_bytes(path.encode())[:16]}",
        },
        source=source,
        actor=HUMAN,
    )
    receipt = bootstrap.commit_bootstrap(
        store,
        bootstrap_id=intent.id,
        snapshot=snapshot,
        source_sha256=intent.source_sha256,
        snapshot_sha256=sha256_bytes(snapshot),
        ydoc_schema=bootstrap.YDOC_SCHEMA,
        actor=HUMAN,
    )
    return receipt["document_id"]


@pytest.fixture
def serialize_ctx(
    store_ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    monkeypatch.setattr(cowork_ops, "_registry", lambda: store_ctx["registry"])
    return store_ctx


def test_serialize_returns_canonical_markdown_and_its_identities(
    serialize_ctx: dict[str, Any],
) -> None:
    store = serialize_ctx["store"]
    source = b"# Serialize fixture\n\nA plain sentence.\n"
    document_id = _bootstrap_real_document(
        store, source=source, path="docs/serialize.md", title="Serialize fixture"
    )

    result = cowork_ops.cowork_doc_serialize(serialize_ctx["store_id"], document_id)

    assert result["ok"] is True
    assert result["document_id"] == document_id
    assert "# Serialize fixture" in result["markdown"]
    assert "A plain sentence." in result["markdown"]
    assert result["byte_length"] == len(result["markdown"].encode("utf-8"))
    assert len(result["structured_head_sha256"]) == 64
    assert len(result["projection_sha256"]) == 64
    assert result["projection_sha256"] == sha256_bytes(
        result["markdown"].encode("utf-8")
    )


def test_serialize_head_matches_the_store_head_authority(
    serialize_ctx: dict[str, Any],
) -> None:
    """The reported head is the authority's, not a hash over unchecked reads."""
    store = serialize_ctx["store"]
    source = b"# Head\n\nBody.\n"
    document_id = _bootstrap_real_document(
        store, source=source, path="docs/head.md", title="Head"
    )
    document = documents.get_document(store, document_id)

    result = cowork_ops.cowork_doc_serialize(serialize_ctx["store_id"], document_id)

    assert result["structured_head_sha256"] == ydoc_store.current_structured_head(
        store,
        document_id=document_id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )


def test_serialize_preserves_the_canonical_escaping(
    serialize_ctx: dict[str, Any],
) -> None:
    """Entity encoding and backslash escaping are the canonical form.

    A renderer decodes both. Stripping them here would change the document, so
    the read must hand back exactly what the serializer produced.
    """
    store = serialize_ctx["store"]
    source = b"Walters & Wilder (2023) [Preprint].\n"
    document_id = _bootstrap_real_document(
        store, source=source, path="docs/escaping.md", title="Escaping"
    )

    markdown = cowork_ops.cowork_doc_serialize(
        serialize_ctx["store_id"], document_id
    )["markdown"]

    assert "&amp;" in markdown
    assert r"\[Preprint\]" in markdown


def test_serialize_reflects_uncompacted_updates(
    serialize_ctx: dict[str, Any],
) -> None:
    """Reading only the compacted snapshot would silently drop recent edits."""
    store = serialize_ctx["store"]
    source = b"# Tail\n\nFirst paragraph.\n"
    document_id = _bootstrap_real_document(
        store, source=source, path="docs/tail.md", title="Tail"
    )
    document = documents.get_document(store, document_id)
    snapshot = ydoc_store.read_snapshot(
        store, snapshot_sha256=document.ydoc_snapshot_sha256
    )

    revised = b"# Tail\n\nSecond paragraph.\n"
    kernel = shared_document_kernel()
    edited = kernel.request(
        {
            "kind": "apply_source_markdown",
            "snapshotBase64": snapshot,
            "updatesBase64": [],
            "expectedBaseStructuredHeadSha256": structured_head_sha256(snapshot, ()),
            "sourceBase64": revised,
            "sourceSha256": sha256_bytes(revised),
            "newlineStyle": "lf",
            "utf8Bom": False,
            "trailingNewlineCount": 1,
        },
        request_id="test_tail_edit",
    )
    update = edited.update
    assert update is not None, "worker returned no update"
    ydoc_store.append_update(store, document_id=document_id, update=update)

    markdown = cowork_ops.cowork_doc_serialize(
        serialize_ctx["store_id"], document_id
    )["markdown"]

    assert "Second paragraph." in markdown


def test_serialize_refuses_a_document_without_structured_state(
    serialize_ctx: dict[str, Any],
) -> None:
    store = serialize_ctx["store"]
    body = b"# No snapshot\n"
    record = documents.register_document(
        store,
        path="docs/no-snapshot.md",
        title="No snapshot",
        document_class="co_authored",
        content_sha256=sha256_bytes(body),
        actor=HUMAN,
        at=NOW,
    )

    with pytest.raises(InvariantViolation):
        cowork_ops.cowork_doc_serialize(serialize_ctx["store_id"], record.id)


def test_serialize_is_registered_as_an_op() -> None:
    from work_buddy.mcp_server import op_registry

    cowork_ops.register_ops()
    assert op_registry.get_op("op.wb.cowork_doc_serialize") is not None

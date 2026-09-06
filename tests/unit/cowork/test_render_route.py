"""The download route: freshness, identity headers, and format admission."""

from __future__ import annotations

from typing import Any

import pytest

from work_buddy.cowork import api as cowork_api

from .test_serialize import _bootstrap_real_document


@pytest.fixture
def rendered_ctx(
    store_ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    monkeypatch.setattr(cowork_api, "_registry", lambda: store_ctx["registry"])
    document_id = _bootstrap_real_document(
        store_ctx["store"],
        source=b"# Route fixture\n\nWalters & Wilder (2023).\n",
        path="docs/route.md",
        title="Route fixture",
    )
    return {**store_ctx, "document_id": document_id}


def _render(client, ctx, **params: str):
    query = {"store_id": ctx["store_id"], **params}
    query_string = "&".join(f"{key}={value}" for key, value in query.items())
    return client.get(f"/api/truth/doc/{ctx['document_id']}/render?{query_string}")


def test_markdown_download_carries_its_identity(client, rendered_ctx) -> None:
    response = _render(client, rendered_ctx, format="markdown")

    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "# Route fixture" in body
    # The canonical form reaches the caller unaltered. A renderer decodes it.
    assert "&amp;" in body

    head = response.headers["X-WB-Structured-Head-Sha256"]
    assert len(head) == 64
    disposition = response.headers["Content-Disposition"]
    assert disposition.startswith("attachment; ")
    # The filename carries the head, so a recipient's copy stays identifiable.
    assert head[:12] in disposition
    assert disposition.endswith('.md"')


def test_a_moved_head_is_a_conflict_not_a_substitution(client, rendered_ctx) -> None:
    """Rendering mid-drain must not hand back text older than the editor shows."""
    response = _render(
        client,
        rendered_ctx,
        format="markdown",
        expected_structured_head_sha256="0" * 64,
    )

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["error"]["code"] == "stale_head"
    assert len(payload["error"]["details"]["structured_head_sha256"]) == 64


def test_a_matching_head_is_served(client, rendered_ctx) -> None:
    observed = _render(client, rendered_ctx, format="markdown")
    head = observed.headers["X-WB-Structured-Head-Sha256"]

    response = _render(
        client, rendered_ctx, format="markdown", expected_structured_head_sha256=head
    )

    assert response.status_code == 200


def test_unknown_format_is_refused(client, rendered_ctx) -> None:
    response = _render(client, rendered_ctx, format="postscript")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_request"


def test_missing_document_is_not_found(client, rendered_ctx) -> None:
    response = client.get(
        f"/api/truth/doc/{'0' * 32}/render?store_id={rendered_ctx['store_id']}"
    )

    assert response.status_code == 404


def test_format_discovery_always_offers_markdown(client) -> None:
    response = client.get("/api/truth/cowork/render/formats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    names = [entry["format"] for entry in payload["formats"]]
    assert names[0] == "markdown"

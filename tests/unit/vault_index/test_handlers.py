"""Tests for the vault-index content-handler registry."""
from __future__ import annotations

from work_buddy.vault_index.chunker import chunk_markdown
from work_buddy.vault_index.handlers import (
    _HANDLER_REGISTRY,
    ContentHandler,
    MarkdownHandler,
    get_handler,
    register_handler,
)


def test_get_handler_md_is_markdown_handler():
    h = get_handler(".md")
    assert isinstance(h, MarkdownHandler)
    assert isinstance(h, ContentHandler)  # runtime_checkable Protocol


def test_get_handler_unknown_returns_none():
    assert get_handler(".pdf") is None


def test_markdown_handler_delegates_to_chunk_markdown():
    doc = "# H\n\nbody text here\n"
    via_handler = MarkdownHandler().chunk(doc, source_path="x.md")
    direct = chunk_markdown(doc, source_path="x.md")
    assert [c.key for c in via_handler] == [c.key for c in direct]
    assert [c.heading_path for c in via_handler] == [c.heading_path for c in direct]


def test_handler_extension_case_insensitive():
    assert isinstance(get_handler(".MD"), MarkdownHandler)


def test_register_handler_idempotent_by_extension():
    size_before = len(_HANDLER_REGISTRY)
    register_handler(MarkdownHandler())  # re-register the built-in
    assert len(_HANDLER_REGISTRY) == size_before
    assert isinstance(get_handler(".md"), MarkdownHandler)

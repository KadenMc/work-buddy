"""Content-handler registry for the vault index.

Markdown-only by default, open for extension. A ``ContentHandler`` claims one or
more file extensions and turns a file's text into ``Chunk``s. The pipeline asks
the registry for a handler by extension; **no handler -> the file is skipped.**
Adding ``.pdf`` / source-code / etc. later is a new handler registration with
zero change to the source walker, store, search, or capability — ``Chunk`` is the
stable contract between handlers and the rest of the system (DESIGN §6).

Extensions are stored and looked up **with the leading dot, lowercased**, so the
filesystem source can pass ``pathlib.Path(...).suffix`` straight through.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from work_buddy.vault_index.chunker import Chunk, chunk_markdown


@runtime_checkable
class ContentHandler(Protocol):
    """Turns a single file's text into chunks for one or more extensions."""

    extensions: set[str]

    def chunk(self, text: str, *, source_path: str) -> list[Chunk]:
        """Chunk ``text`` (the file body) into embeddable units."""
        ...


class MarkdownHandler:
    """The heading-aware Markdown chunker, wrapped as a content handler."""

    extensions = {".md"}

    def chunk(self, text: str, *, source_path: str) -> list[Chunk]:
        return chunk_markdown(text, source_path=source_path)


_HANDLER_REGISTRY: dict[str, ContentHandler] = {}


def register_handler(handler: ContentHandler) -> None:
    """Register a handler for each extension it claims.

    Idempotent by extension: re-registering the same extension replaces the
    previous handler (mirrors ``register_artifact``'s by-name semantics).
    """
    for ext in handler.extensions:
        _HANDLER_REGISTRY[ext.lower()] = handler


def get_handler(extension: str) -> ContentHandler | None:
    """Return the handler claiming ``extension`` (e.g. ``".md"``), or None."""
    return _HANDLER_REGISTRY.get(extension.lower())


# Markdown is the built-in default, registered at import (DESIGN §6).
register_handler(MarkdownHandler())

"""Native chunk-level semantic index over Markdown content.

Runs in work-buddy's own processes (not Obsidian's heap). See
``.data/designs/semantic-indexer/DESIGN.md`` and ``SEAM-DESIGN.md``.

Public API:
    chunk_markdown, Chunk      — heading-aware Markdown chunking
    ContentHandler, MarkdownHandler, register_handler, get_handler
                               — file-type-extensible handler registry
"""
from work_buddy.vault_index.chunker import Chunk, chunk_markdown  # noqa: F401
from work_buddy.vault_index.handlers import (  # noqa: F401
    ContentHandler,
    MarkdownHandler,
    get_handler,
    register_handler,
)

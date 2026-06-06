"""Heading-aware Markdown chunker for the work-buddy semantic indexer.

Chunks a Markdown document into non-overlapping, size-bounded leaf sections,
reimplemented in Python and INFORMED BY (not copied from) Smart Connections'
``smart-blocks/parsers/markdown.js`` (MIT, github.com/brianpetro/jsbrains). Two
deliberate departures from SC:

  1. NON-OVERLAPPING LEAF SECTIONS. SC emits a block for every heading
     (parents included) plus ``#{n}`` sub-blocks, then de-duplicates parents
     whose children fully cover them via a ``should_embed`` getter. This emits
     one chunk per heading-delimited *leaf section* directly — no overlap, no
     dedup pass. Hierarchy context is preserved via the breadcrumb prefix in
     ``embed_input`` instead of via overlapping parent blocks.

  2. MAX-SIZE FALLBACK SPLITTER. SC has NO upper bound on block size — a
     heading-light document collapses into one enormous block whose tail is
     silently truncated by the embedding model's context window (a correctness
     bug: the tail never reaches the vector). ``_split_oversized`` recursively
     divides oversized sections: blank-line paragraphs -> sentences -> hard
     character window.

The chunker is deliberately ``source_path``-agnostic: callers pass whatever
namespacing string they want (the filesystem source layer passes a
``{vault_id}/{relative_path}`` path). It imports only the standard library.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*```")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_PARA_RE = re.compile(r"\n\s*\n")


@dataclass
class Chunk:
    """One embeddable unit of a source document."""

    source_path: str
    heading_path: list[str]   # breadcrumb ancestry, e.g. ["Methods", "Preprocessing"]
    text: str                 # raw section text (breadcrumb NOT included)
    line_start: int            # 1-indexed inclusive (of the originating section)
    line_end: int              # 1-indexed inclusive (of the originating section)
    split_index: int = 0       # 0 unless the section was oversize-split
    split_count: int = 1       # >1 when this chunk is one piece of a split section
    dup_index: int = 0         # occurrence ordinal among identical heading_paths in the doc

    @property
    def char_size(self) -> int:
        return len(self.text)

    @property
    def key(self) -> str:
        """Stable namespaced key: ``source#Heading#Sub[#(dupidx)][#:splitidx]``.

        ``dup_index`` disambiguates duplicate sibling/top-level headings (two
        ``## Foo`` under one parent); ``split_index`` disambiguates oversize-split
        pieces of one section. The first occurrence (``dup_index == 0``) and
        unsplit sections (``split_count == 1``) stay bare. The two suffixes are
        ordered ``#(dup)`` before ``#:split`` so the encoding is unambiguous.

        This string is a human-readable namespacing aid, NOT a collision-proof
        identifier — a heading literally containing ``#`` / ``(1)`` / ``:`` could
        in principle forge one. Formal uniqueness is the tuple
        ``(source_path, heading_path, dup_index, split_index)``; a persistent
        store should key on that tuple (or a hash of it), not on this string.
        """
        base = self.source_path + "".join("#" + h for h in self.heading_path)
        if self.dup_index > 0:
            base += f"#({self.dup_index})"
        if self.split_count > 1:
            base += f"#:{self.split_index}"
        return base

    @property
    def embed_input(self) -> str:
        """Text actually sent to the embedding model.

        Breadcrumb-prefixed, mirroring SC's `get_embed_input()` — gives the
        embedding model the document + heading context the leaf text alone lacks.
        The disambiguating ``dup_index`` is deliberately excluded so the embedded
        text stays clean.
        """
        crumb = " > ".join([self.source_path, *self.heading_path])
        return f"{crumb}\n{self.text}"


def _strip_frontmatter(lines: list[str]) -> int:
    """Return the 0-based index where the body begins (after YAML frontmatter)."""
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return i + 1
    return 0


def _find_headings(lines: list[str], body_offset: int) -> list[tuple[int, int, str]]:
    """Return (line_index, level, title) for each heading, skipping code fences."""
    headings: list[tuple[int, int, str]] = []
    in_code = False
    for idx in range(body_offset, len(lines)):
        if _FENCE_RE.match(lines[idx]):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = _HEADING_RE.match(lines[idx])
        if m:
            headings.append((idx, len(m.group(1)), m.group(2).strip()))
    return headings


def _pack(parts: list[str], max_chars: int, sep: str) -> list[str]:
    """Greedily pack `parts` into groups <= max_chars; recurse on oversize parts."""
    out: list[str] = []
    buf = ""
    for part in parts:
        if len(part) > max_chars:
            if buf:
                out.append(buf)
                buf = ""
            out.extend(_split_oversized(part, max_chars))
            continue
        candidate = part if not buf else buf + sep + part
        if len(candidate) > max_chars:
            if buf:
                out.append(buf)
            buf = part
        else:
            buf = candidate
    if buf:
        out.append(buf)
    return out


def _split_oversized(text: str, max_chars: int) -> list[str]:
    """Recursively split text that exceeds max_chars: paragraphs -> sentences -> window."""
    if len(text) <= max_chars:
        return [text]
    paragraphs = _PARA_RE.split(text)
    if len(paragraphs) > 1:
        return _pack(paragraphs, max_chars, sep="\n\n")
    sentences = _SENTENCE_RE.split(text)
    if len(sentences) > 1:
        return _pack(sentences, max_chars, sep=" ")
    # Last resort: a single unbroken run longer than max_chars (e.g. a code blob).
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]


def chunk_markdown(
    text: str,
    *,
    source_path: str,
    max_chars: int = 1200,
    min_chars: int = 1,
) -> list[Chunk]:
    """Chunk a Markdown document into non-overlapping, size-bounded leaf sections.

    Args:
        text: full Markdown document.
        source_path: namespacing path, used in chunk keys and breadcrumbs.
        max_chars: sections longer than this are recursively split. Default 1200
            chars (~300 tokens) — the RAG-research retrieval-quality sweet spot;
            should be capped by the embedding model's token window in production.
        min_chars: sections shorter than this are dropped (SC's `min_chars` floor).
    """
    lines = text.split("\n")
    body_offset = _strip_frontmatter(lines)
    headings = _find_headings(lines, body_offset)

    # Build section spans: a preamble (before the first heading) plus one span
    # per heading running until the next heading of ANY level.
    sections: list[tuple[int, int, list[str]]] = []
    first_heading_idx = headings[0][0] if headings else len(lines)
    if first_heading_idx > body_offset:
        sections.append((body_offset, first_heading_idx, []))

    stack: list[tuple[int, str]] = []  # (level, title) ancestry
    for i, (idx, level, title) in enumerate(headings):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        heading_path = [t for (_, t) in stack]
        end = headings[i + 1][0] if i + 1 < len(headings) else len(lines)
        sections.append((idx, end, heading_path))

    chunks: list[Chunk] = []
    # Occurrence counter keyed on the FULL breadcrumb tuple — two sections collide
    # only when their heading_path is identical (duplicate siblings, repeated
    # top-level headings, or the single preamble). Bumped AFTER the min_chars skip
    # so a dropped first occurrence does not consume an ordinal (the lone survivor
    # keeps the bare key). All split pieces of one section share its ordinal.
    dup_counter: Counter[tuple[str, ...]] = Counter()
    for start, end, heading_path in sections:
        section_text = "\n".join(lines[start:end]).strip()
        if len(section_text) < min_chars:
            continue
        hp_key = tuple(heading_path)
        dup_index = dup_counter[hp_key]
        dup_counter[hp_key] += 1
        pieces = (
            _split_oversized(section_text, max_chars)
            if len(section_text) > max_chars
            else [section_text]
        )
        for j, piece in enumerate(pieces):
            chunks.append(
                Chunk(
                    source_path=source_path,
                    heading_path=list(heading_path),
                    text=piece,
                    line_start=start + 1,
                    line_end=end,
                    split_index=j,
                    split_count=len(pieces),
                    dup_index=dup_index,
                )
            )
    return chunks

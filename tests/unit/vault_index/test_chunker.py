"""Tests for the vault-index Markdown chunker.

The eight ``test_*`` functions below port the reference proof-of-concept's smoke
suite (``.data/designs/semantic-indexer/reference/test_chunker.py``) to pytest,
preserving every assertion. The remaining cases cover the duplicate-sibling-heading
fix (DESIGN §7 "Known PoC gap") and a sample-file smoke test.
"""
from __future__ import annotations

from pathlib import Path

from work_buddy.vault_index.chunker import Chunk, _split_oversized, chunk_markdown


# --------------------------------------------------------------------------
# Ported reference cases (t1–t8)
# --------------------------------------------------------------------------

def test_basic_heading_structure_and_preamble():
    doc = """Preamble line one.
Preamble line two.

# Alpha

Alpha body text.

## Beta

Beta body text.
"""
    chunks = chunk_markdown(doc, source_path="notes/t1.md")
    keys = [c.key for c in chunks]
    assert len(chunks) == 3
    assert chunks[0].heading_path == []
    assert chunks[1].heading_path == ["Alpha"]
    assert chunks[2].heading_path == ["Alpha", "Beta"]
    assert keys[2] == "notes/t1.md#Alpha#Beta"


def test_code_fence_headings_ignored():
    doc = """# Real Heading

text

```
## fake heading inside fence
```

more text
"""
    chunks = chunk_markdown(doc, source_path="notes/t2.md")
    assert len(chunks) == 1
    assert "fake heading" in chunks[0].text


def test_frontmatter_stripped():
    doc = """---
title: X
---
# Heading After Frontmatter

body
"""
    chunks = chunk_markdown(doc, source_path="notes/t3.md")
    assert len(chunks) == 1
    assert "title: X" not in chunks[0].text


def test_oversize_splitter():
    para = ("This is a sentence that repeats. " * 20).strip()  # ~640 chars
    big_section = "\n\n".join([para] * 10)  # ~6.5k chars across 10 paragraphs
    doc = f"# Big\n\n{big_section}\n"
    chunks = chunk_markdown(doc, source_path="notes/t4.md", max_chars=2000)
    assert len(chunks) > 1
    assert all(c.char_size <= 2000 for c in chunks)
    assert all(c.heading_path == ["Big"] for c in chunks)
    assert chunks[0].split_count == len(chunks)
    assert len({c.key for c in chunks}) == len(chunks)


def test_pathological_blob_hard_window():
    blob = "x" * 5000
    pieces = _split_oversized(blob, 1000)
    assert len(pieces) == 5
    assert all(len(p) <= 1000 for p in pieces)


def test_min_chars_drops_tiny():
    doc = "# Tiny\n\nok\n\n# Substantial\n\n" + ("word " * 50)
    chunks = chunk_markdown(doc, source_path="notes/t6.md", min_chars=20)
    assert len(chunks) == 1
    assert chunks[0].heading_path == ["Substantial"]


def test_embed_input_carries_breadcrumb():
    c = Chunk(source_path="notes/t7.md", heading_path=["A", "B"], text="body",
              line_start=1, line_end=2)
    assert c.embed_input == "notes/t7.md > A > B\nbody"


def test_heading_level_jump():
    doc = "# One\n\ntext\n\n### Three\n\ntext\n"
    chunks = chunk_markdown(doc, source_path="notes/t8.md")
    assert chunks[1].heading_path == ["One", "Three"]


# --------------------------------------------------------------------------
# Duplicate-sibling-heading fix (DESIGN §7 "Known PoC gap")
# --------------------------------------------------------------------------

def test_sibling_dup_headings_distinct_keys():
    doc = (
        "# P\n\n## Foo\n\nFirst foo body text here.\n\n"
        "## Foo\n\nSecond foo body text here.\n"
    )
    chunks = chunk_markdown(doc, source_path="notes/dup.md")
    foos = [c for c in chunks if c.heading_path == ["P", "Foo"]]
    assert len(foos) == 2
    assert foos[0].dup_index == 0
    assert foos[1].dup_index == 1
    assert foos[0].key == "notes/dup.md#P#Foo"
    assert foos[1].key == "notes/dup.md#P#Foo#(1)"
    assert len({c.key for c in chunks}) == len(chunks)


def test_top_level_dup_headings_distinct_keys():
    doc = "# Foo\n\nFirst body text.\n\n# Foo\n\nSecond body text.\n"
    chunks = chunk_markdown(doc, source_path="notes/top.md")
    foos = [c for c in chunks if c.heading_path == ["Foo"]]
    assert len(foos) == 2
    assert foos[0].key == "notes/top.md#Foo"
    assert foos[1].key == "notes/top.md#Foo#(1)"


def test_dup_and_split_compose():
    """A duplicated heading whose 2nd occurrence oversize-splits — dup_index
    composes with split_index, and the key orders #(dup) before #:split."""
    short = "Short first foo body.\n"
    para = ("This is a sentence that repeats. " * 20).strip()
    big = "\n\n".join([para] * 6)
    doc = f"# P\n\n## Foo\n\n{short}\n## Foo\n\n{big}\n"
    chunks = chunk_markdown(doc, source_path="notes/ds.md", max_chars=2000)
    keys = [c.key for c in chunks]
    assert len(set(keys)) == len(keys)

    second = [c for c in chunks if c.dup_index == 1]
    assert len(second) > 1
    assert all(c.heading_path == ["P", "Foo"] for c in second)
    assert {c.split_index for c in second} == set(range(len(second)))
    # Literal composed keys pin the segment order: #(dup) before #:split.
    assert "notes/ds.md#P#Foo#(1)#:0" in keys
    assert "notes/ds.md#P#Foo#(1)#:1" in keys


def test_dup_index_excluded_from_embed_input():
    doc = "# P\n\n## Foo\n\nFirst body.\n\n## Foo\n\nSecond body.\n"
    chunks = chunk_markdown(doc, source_path="notes/e.md")
    second = [c for c in chunks if c.heading_path == ["P", "Foo"]][1]
    assert second.dup_index == 1
    assert "(1)" not in second.embed_input
    assert second.embed_input.startswith("notes/e.md > P > Foo\n")


def test_dropped_first_occurrence_does_not_consume_ordinal():
    # First "## Foo" body is below min_chars and dropped; the surviving second
    # "## Foo" must still get the bare key (dup_index 0).
    doc = "# P\n\n## Foo\n\nok\n\n## Foo\n\n" + ("word " * 50) + "\n"
    chunks = chunk_markdown(doc, source_path="notes/drop.md", min_chars=20)
    foos = [c for c in chunks if c.heading_path == ["P", "Foo"]]
    assert len(foos) == 1
    assert foos[0].dup_index == 0
    assert foos[0].key == "notes/drop.md#P#Foo"


def test_distinct_parents_same_leaf_no_false_dup():
    doc = (
        "# A\n\n## Foo\n\nBody under A.\n\n"
        "# B\n\n## Foo\n\nBody under B.\n"
    )
    chunks = chunk_markdown(doc, source_path="notes/par.md")
    a_foo = [c for c in chunks if c.heading_path == ["A", "Foo"]]
    b_foo = [c for c in chunks if c.heading_path == ["B", "Foo"]]
    assert len(a_foo) == 1 and len(b_foo) == 1
    assert a_foo[0].key == "notes/par.md#A#Foo"
    assert b_foo[0].key == "notes/par.md#B#Foo"
    assert "(1)" not in a_foo[0].key and "(1)" not in b_foo[0].key


def test_preamble_key_is_bare_source_path():
    doc = "Some preamble prose before any heading.\n\n# H\n\nbody\n"
    chunks = chunk_markdown(doc, source_path="notes/pre.md")
    assert chunks[0].heading_path == []
    assert chunks[0].key == "notes/pre.md"


# --------------------------------------------------------------------------
# Sample-file smoke test
# --------------------------------------------------------------------------

def test_sample_md_smoke():
    sample = (Path(__file__).parent / "fixtures" / "sample.md").read_text(encoding="utf-8")
    chunks = chunk_markdown(sample, source_path="notes/sample.md")
    all_text = "\n".join(c.text for c in chunks)
    # frontmatter excluded
    assert "title: Sample Note" not in all_text
    # the fenced "## not a heading" did not become its own chunk
    assert not any(c.heading_path[-1:] == ["not a heading"] for c in chunks)
    # nested heading path present
    assert any(
        c.heading_path == ["Methods", "Data Preprocessing", "Normalization"]
        for c in chunks
    )
    # keys unique
    keys = [c.key for c in chunks]
    assert len(set(keys)) == len(keys)

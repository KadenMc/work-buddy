# Co-work projection-fidelity gate (K2 WP-B5)

A self-contained vitest package that is the executable gate for Co-work Markdown
projection fidelity. It proves that importing a real Markdown document and
materializing it back preserves the file byte-for-byte outside the regions a human
actually edits, and that unsupported constructs are preserved and flagged rather
than silently normalized.

The package has its own `package.json`, its own exact-pinned copies of the frozen
Markdown bundle (`@tiptap/markdown` 3.28.0 plus Table, TaskList, Image), and its
own vitest config, so it never touches the dashboard root package. It is DOM-free
(the standalone `MarkdownManager` needs no browser), so it runs under the node
environment.

## Layout

- `corpus/real/` real repo and design docs, copied verbatim and LF-normalized.
- `corpus/synthetic/` authored Obsidian-construct fixtures (wikilinks, callouts,
  setext, lazy lists, empty-paragraph runs, mixed fences, tables, task lists,
  frontmatter variants).
- `manifest.json` one entry per corpus file, each carrying exactly `path`,
  `expected_sha256`, and `required_extensions`. Regenerate with
  `node scripts/build-manifest.mjs`.
- `src/materializer.ts` the block-splice materializer reference implementation.
- `src/` supporting library (frozen bundle, frontmatter boundary, corpus loader,
  normalization inventory, edit simulation).
- `test/` the five fail-hard rules plus idempotency convergence, the normalization
  inventory regression, and the 10k-word materialization timing.

## Running

```
npm ci
npm test
```

## Reference implementation

`src/materializer.ts` is a pure function library and the executable specification
of block-splice materialization. The production materializer adopts this design: strip
frontmatter at the boundary, lex the body into top-level blocks keyed to exact
source ranges, copy unedited blocks byte-verbatim, re-serialize only edited blocks,
and flag edited blocks that hold a non-first-class Obsidian construct.

## The five fail-hard rules

1. No-edit materialize equals source byte-for-byte across the whole corpus.
2. A single-block edit preserves every byte outside the edited block.
3. Any corpus construct that lacks a schema node is a hard failure, never a silent
   drop (Table, TaskList, Image are the minimum coverage).
4. Unknown constructs are byte-identical inside unedited blocks and flagged, never
   silently normalized, inside edited blocks.
5. Import strips and re-attaches YAML frontmatter verbatim at the boundary.

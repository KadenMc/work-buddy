// The SP-3 normalization inventory, encoded as regression expectations. Each case
// is a minimal isolated input and the exact Markdown @tiptap/markdown 3.28.0 emits
// when it round-trips that input through the full fidelity bundle. These lock the
// serializer's whole-document behavior so a future version bump that changes any
// normalization is caught by the gate. The `kind` column classifies the change:
//   cosmetic   reversible reformatting (setext to ATX, underscore to star, ...)
//   escaping   backslash escaping of literal characters
//   rewrite    a link rewrite (bare or angle URL to inline link)
//   corruption a lossy change the block-splice materializer must confine to edits
//   reformat   in-schema but re-laid-out (tables re-padded, lossless)
//   preserved  round-trips byte-identical under the full bundle
//   structural block-boundary reshaping (blank line inserted between headings)
//
// This inventory is WHY the materializer splices per block: it demonstrates that
// whole-document serialization is not byte-preserving, so unedited blocks must be
// copied verbatim rather than re-serialized.

export type NormalizationKind =
  | "cosmetic"
  | "escaping"
  | "rewrite"
  | "corruption"
  | "reformat"
  | "preserved"
  | "structural";

export interface NormalizationCase {
  label: string;
  kind: NormalizationKind;
  input: string;
  output: string;
}

export const NORMALIZATION_INVENTORY: NormalizationCase[] = [
  { label: "trailing-newline-dropped", kind: "cosmetic", input: "# H\n", output: "# H" },
  { label: "setext-h1-to-atx", kind: "cosmetic", input: "Title\n=====\n\nbody", output: "# Title\n\nbody" },
  { label: "setext-h2-to-atx", kind: "cosmetic", input: "Title\n-----\n\nbody", output: "## Title\n\nbody" },
  { label: "underscore-italic-to-star", kind: "cosmetic", input: "_italic_", output: "*italic*" },
  { label: "underscore-bold-to-star", kind: "cosmetic", input: "__bold__", output: "**bold**" },
  { label: "star-bullet-to-dash", kind: "cosmetic", input: "* item", output: "- item" },
  { label: "plus-bullet-to-dash", kind: "cosmetic", input: "+ item", output: "- item" },
  { label: "ordered-all-ones-renumbered", kind: "cosmetic", input: "1. a\n1. b\n1. c", output: "1. a\n2. b\n3. c" },
  { label: "nested-list-4sp-to-2sp", kind: "cosmetic", input: "- a\n    - b", output: "- a\n  - b" },
  { label: "fence-tildes-to-backticks", kind: "cosmetic", input: "~~~js\ny=2\n~~~", output: "```js\ny=2\n```" },
  { label: "indented-code-to-fenced", kind: "cosmetic", input: "    code line\n    code two", output: "```\ncode line\ncode two\n```" },
  { label: "hr-stars-to-dashes", kind: "cosmetic", input: "***", output: "---" },
  { label: "hr-underscores-to-dashes", kind: "cosmetic", input: "___", output: "---" },
  { label: "hard-break-backslash-to-spaces", kind: "cosmetic", input: "line one\\\nline two", output: "line one  \nline two" },
  { label: "nested-blockquote-spacer", kind: "cosmetic", input: "> a\n> > b", output: "> a\n>\n> > b" },
  { label: "underscore-in-word-escaped", kind: "escaping", input: "entry_points and dev_notes", output: "entry\\_points and dev\\_notes" },
  { label: "bare-url-autolinked", kind: "rewrite", input: "see https://x.com here", output: "see [https://x.com](https://x.com) here" },
  { label: "angle-autolink-rewritten", kind: "rewrite", input: "see <https://x.com> here", output: "see [https://x.com](https://x.com) here" },
  { label: "inline-code-backtick-corrupted", kind: "corruption", input: "a ``co`de`` b", output: "a `co`de` b" },
  { label: "html-inline-entity-escaped", kind: "corruption", input: 'a <span class="x">y</span> b', output: 'a &lt;span class="x"&gt;y&lt;/span&gt; b' },
  { label: "table-reformatted-lossless", kind: "reformat", input: "| a | b |\n| - | - |\n| 1 | 2 |", output: "\n| a   | b   |\n| --- | --- |\n| 1   | 2   |\n" },
  { label: "task-list-preserved", kind: "preserved", input: "- [ ] todo\n- [x] done", output: "- [ ] todo\n- [x] done" },
  { label: "image-preserved", kind: "preserved", input: "![alt](img.png)", output: "![alt](img.png)" },
  { label: "two-blank-lines-preserved", kind: "preserved", input: "a\n\n\n\nb", output: "a\n\n\n\nb" },
  { label: "heading-run-blank-inserted", kind: "structural", input: "# h1\n# h2", output: "# h1\n\n# h2" },
];

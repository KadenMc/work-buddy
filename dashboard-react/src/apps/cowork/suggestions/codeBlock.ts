import { CodeBlock } from "@tiptap/extension-code-block";

/**
 * The code-block schema patch (SP-1 fork delta 4, the M item). The stock code_block
 * node forbids marks (marks: ""), so an agent edit inside a fenced code block applied
 * RAW with zero suggestion marks, a silent gate violation (SP-1 codeblock finding). The
 * fix lives in the cowork bundle seam, NOT dashboard core: this node override allows the
 * three suggestion marks inside a code block, so a tracked edit there carries insertion
 * and deletion marks like any other block and the review layer can render and decide it.
 *
 * The `code` node spec stays intact, so live authoring inside a code block still refuses
 * ordinary formatting marks. Only the tracked-change marks are admitted, and they are
 * display-only projections that never persist (parseHTML stripped, ledger re-derives).
 *
 * Wiring at the join: configure StarterKit with `codeBlock: false` and add this override
 * to the editor extension set, so exactly one code_block node is registered. This file
 * is owned here so dashboard core is untouched.
 */
export const CoworkCodeBlock = CodeBlock.extend({
  marks: "insertion deletion modification",
});

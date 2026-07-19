/**
 * The default materialize renderer the submit path calls to produce the R5 materialize block
 * (the post-apply Markdown plus its hash, section 1.5). It serializes the editor's current
 * document through the DOM-free MarkdownManager (the ONE serializer, I14). By the time it
 * runs, the sitting has already applied its accepts and rejects to the editor, so the
 * serialized content is the post-apply document.
 *
 * Scope note. This is the WIRING renderer, not the fidelity materializer. Byte-exact
 * block-splice materialization (copying unedited blocks verbatim, re-attaching YAML
 * frontmatter, so an undecided proposal in another block never reaches the file) is the
 * fidelity suite's obligation (gate conditions 6 and 8, the production-materializer item).
 * This renderer produces valid, hash-bound Markdown the server verifies and writes, and the
 * submit path takes it through a seam so the reference block-splice materializer can replace
 * it without touching the orchestration.
 */

import type { Editor } from "@tiptap/core";

import { createCoworkMarkdownManager } from "../editor/extensions";

/**
 * Build a materialize renderer bound to the live editor. Returns the post-apply document
 * serialized to Markdown. The suggestion marks are editor-runtime schema absent from the
 * MarkdownManager, so they are not serialized: an accepted edit has already had its marks
 * resolved to plain content, and any still-open proposal contributes its base text only.
 */
export const createEditorMaterializeRenderer = (
  getEditor: () => Editor | null,
): (() => Promise<string>) => {
  const manager = createCoworkMarkdownManager();
  return async () => {
    const editor = getEditor();
    if (editor === null) {
      throw new Error("the editor is not mounted, so the document cannot materialize");
    }
    return manager.serialize(editor.getJSON());
  };
};

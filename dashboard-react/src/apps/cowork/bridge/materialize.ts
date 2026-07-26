/**
 * The default materialize renderer the submit path calls to produce the R5 materialize block
 * (the post-apply Markdown plus its hash, section 1.5). It serializes the editor's current
 * document through the DOM-free MarkdownManager (the ONE serializer, I14). By the time it
 * runs, the sitting has already applied its accepts and rejects to the editor, so the
 * serialized content is the post-apply document.
 *
 * Registered files enter this path only after bootstrap's exact no-edit round-trip gate has
 * admitted the conservative canonical syntax subset. The renderer is therefore the production
 * boundary for that admitted subset: it uses the same MarkdownManager as admission and restores
 * verbatim frontmatter plus newline/BOM fidelity before the server verifies and writes the hash.
 * Syntax that would require a lossless block-splice implementation is rejected at registration
 * instead of being normalized silently on a later Save.
 */

import type { Editor } from "@tiptap/core";
import type * as Y from "yjs";

import { serializeCoworkEditorMarkdown } from "../editor/serializeCoworkMarkdown";

/**
 * Build a materialize renderer bound to the live editor. Returns the post-apply document
 * serialized to Markdown. The suggestion marks are editor-runtime schema absent from the
 * MarkdownManager, so they are not serialized: an accepted edit has already had its marks
 * resolved to plain content, and any still-open proposal contributes its base text only.
 */
export const createEditorMaterializeRenderer = (
  getEditor: () => Editor | null,
  document?: Y.Doc,
): (() => Promise<string>) => {
  return async () => {
    const editor = getEditor();
    if (editor === null) {
      throw new Error("the editor is not mounted, so the document cannot materialize");
    }
    return serializeCoworkEditorMarkdown(editor, document);
  };
};

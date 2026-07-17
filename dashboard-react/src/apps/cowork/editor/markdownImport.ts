import type { JSONContent } from "@tiptap/core";
import type { MarkdownManager } from "@tiptap/markdown";

import { createCoworkMarkdownManager } from "./extensions";
import { reattachFrontmatter, splitFrontmatter } from "./frontmatter";

/**
 * The result of importing a Markdown file into the Co-work document model. The body is
 * parsed to a Tiptap document, and the YAML frontmatter is held VERBATIM so it can be
 * re-attached on materialize without ever passing through the serializer (SP-3 case 3).
 */
export interface CoworkMarkdownImport {
  readonly doc: JSONContent;
  readonly frontmatter: string | null;
}

/**
 * The one Markdown import boundary (SP-3 case 5, audit A11). Frontmatter is split off
 * first, the body is parsed through the standalone DOM-free MarkdownManager exactly
 * once, and `contentType` handling lives here alone. The parse never touches the HTML
 * parser (the forgery surface), so no wb / suggestion / provenance mark is forgeable
 * from imported content.
 */
export const importCoworkMarkdown = (
  source: string,
  manager: MarkdownManager = createCoworkMarkdownManager(),
): CoworkMarkdownImport => {
  const { frontmatter, body } = splitFrontmatter(source);
  return { doc: manager.parse(body), frontmatter };
};

/**
 * Whole-document serialize with the frontmatter re-attached verbatim. This is the
 * fidelity-safe path ONLY for a no-edit round-trip and for reconstructing a body from a
 * parsed document. Per SP-3 and the fidelity gate, an EDITED document must materialize
 * through a block-splice materializer (copying unedited blocks byte-for-byte), never
 * through a whole-document serialize. The frontmatter is never fed to the serializer.
 */
export const serializeCoworkMarkdown = (
  imported: CoworkMarkdownImport,
  manager: MarkdownManager = createCoworkMarkdownManager(),
): string => reattachFrontmatter(imported.frontmatter, manager.serialize(imported.doc));

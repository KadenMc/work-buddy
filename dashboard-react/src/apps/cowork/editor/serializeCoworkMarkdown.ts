import type { Editor } from "@tiptap/core";
import * as Y from "yjs";

import { createCoworkMarkdownManager } from "./extensions";
import { reattachFrontmatter } from "./frontmatter";

export type CoworkNewlineStyle = "crlf" | "lf" | "cr" | "none";

export interface CoworkMarkdownFidelity {
  readonly newlineStyle: CoworkNewlineStyle;
  readonly utf8Bom: boolean;
  readonly trailingNewlineCount?: number;
}

/**
 * A canonical Markdown projection plus the serialized editor body before the
 * byte-envelope trailing-newline rule is applied. `bodyStart` is a UTF-16
 * string offset into `markdown`; callers that persist selectors must convert
 * it to the code-point offsets used by Truth.
 */
export interface CoworkMarkdownProjection {
  readonly markdown: string;
  readonly body: string;
  readonly bodyStart: number;
  readonly fidelity: CoworkMarkdownFidelity;
}

/** Normalize only line endings, without changing BOM or trailing-newline count. */
export const normalizeCoworkMarkdownNewlines = (
  markdown: string,
  newlineStyle: CoworkNewlineStyle,
): string => {
  if (newlineStyle === "crlf") {
    return markdown.replace(/\r\n|\r|\n/g, "\r\n");
  }
  if (newlineStyle === "cr") {
    return markdown.replace(/\r\n|\r|\n/g, "\r");
  }
  if (newlineStyle === "lf") {
    return markdown.replace(/\r\n|\r/g, "\n");
  }
  return markdown;
};

/** Restore the byte-significant envelope that sits outside the Markdown AST. */
export const restoreCoworkMarkdownFidelity = (
  markdown: string,
  fidelity: CoworkMarkdownFidelity,
): string => {
  let restored = normalizeCoworkMarkdownNewlines(
    markdown,
    fidelity.newlineStyle,
  );
  if (fidelity.trailingNewlineCount !== undefined) {
    restored = restored.replace(/(?:\r\n|\r|\n)+$/g, "");
    const newline =
      fidelity.newlineStyle === "crlf"
        ? "\r\n"
        : fidelity.newlineStyle === "cr"
          ? "\r"
          : "\n";
    restored += newline.repeat(Math.max(0, fidelity.trailingNewlineCount));
  }
  if (fidelity.utf8Bom && !restored.startsWith("\ufeff")) {
    restored = `\ufeff${restored}`;
  }
  return restored;
};

const coworkMarkdownFidelity = (document: Y.Doc): {
  readonly frontmatter: string | null;
  readonly fidelity: CoworkMarkdownFidelity;
} => {
  const stored = document.getMap<unknown>("wb-cowork:fidelity");
  const frontmatterValue = stored.get("frontmatter");
  const lineEnding = stored.get("newline_style");
  return {
    frontmatter:
      typeof frontmatterValue === "string" ? frontmatterValue : null,
    fidelity: {
      newlineStyle:
        lineEnding === "crlf" || lineEnding === "cr" || lineEnding === "none"
          ? lineEnding
          : "lf",
      utf8Bom: stored.get("utf8_bom") === true,
      trailingNewlineCount:
        typeof stored.get("trailing_newline_count") === "number"
          ? Number(stored.get("trailing_newline_count"))
          : undefined,
    },
  };
};

/**
 * Serialize once and retain the body/envelope boundary needed to translate a
 * block-aligned ProseMirror range into an exact canonical-Markdown selector.
 * This is the explicit mapping seam; ProseMirror positions must never be
 * relabelled as Markdown offsets.
 */
export const serializeCoworkEditorMarkdownProjection = (
  editor: Editor,
  document: Y.Doc,
): CoworkMarkdownProjection => {
  const { frontmatter, fidelity } = coworkMarkdownFidelity(document);
  const rawBody = createCoworkMarkdownManager().serialize(editor.getJSON());
  const normalizedFrontmatter =
    frontmatter === null
      ? ""
      : normalizeCoworkMarkdownNewlines(frontmatter, fidelity.newlineStyle);
  const body = normalizeCoworkMarkdownNewlines(
    rawBody,
    fidelity.newlineStyle,
  );
  const joined = reattachFrontmatter(
    frontmatter === null ? null : normalizedFrontmatter,
    body,
  );
  const markdown = restoreCoworkMarkdownFidelity(joined, fidelity);
  const bomWasAdded =
    fidelity.utf8Bom &&
    !joined.startsWith("\ufeff");
  return {
    markdown,
    body,
    bodyStart: normalizedFrontmatter.length + (bomWasAdded ? 1 : 0),
    fidelity,
  };
};

/**
 * The one live-editor Markdown boundary. Both explicit Save and scratch promotion use
 * this renderer, including the bootstrap-recorded frontmatter, newline, and UTF-8 BOM
 * fidelity facts that never belong in the ProseMirror document itself.
 */
export const serializeCoworkEditorMarkdown = (
  editor: Editor,
  document?: Y.Doc,
): string => {
  if (document === undefined) {
    return createCoworkMarkdownManager().serialize(editor.getJSON());
  }
  return serializeCoworkEditorMarkdownProjection(editor, document).markdown;
};

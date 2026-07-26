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

/** Restore the byte-significant envelope that sits outside the Markdown AST. */
export const restoreCoworkMarkdownFidelity = (
  markdown: string,
  fidelity: CoworkMarkdownFidelity,
): string => {
  let restored = markdown;
  if (fidelity.newlineStyle === "crlf") {
    restored = restored.replace(/\r\n|\r|\n/g, "\r\n");
  } else if (fidelity.newlineStyle === "cr") {
    restored = restored.replace(/\r\n|\r|\n/g, "\r");
  } else if (fidelity.newlineStyle === "lf") {
    restored = restored.replace(/\r\n|\r/g, "\n");
  }
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
  const fidelity = document.getMap<unknown>("wb-cowork:fidelity");
  const frontmatterValue = fidelity.get("frontmatter");
  const frontmatter = typeof frontmatterValue === "string" ? frontmatterValue : null;
  const markdown = reattachFrontmatter(
    frontmatter,
    createCoworkMarkdownManager().serialize(editor.getJSON()),
  );
  const lineEnding = fidelity.get("newline_style");
  return restoreCoworkMarkdownFidelity(markdown, {
    newlineStyle:
      lineEnding === "crlf" || lineEnding === "cr" || lineEnding === "none"
        ? lineEnding
        : "lf",
    utf8Bom: fidelity.get("utf8_bom") === true,
    trailingNewlineCount:
      typeof fidelity.get("trailing_newline_count") === "number"
        ? Number(fidelity.get("trailing_newline_count"))
        : undefined,
  });
};

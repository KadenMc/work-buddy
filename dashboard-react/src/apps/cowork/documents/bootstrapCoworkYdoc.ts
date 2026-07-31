import { Editor } from "@tiptap/core";
import * as Y from "yjs";

import { buildEditorExtensions, stopCapturingLoadTimeIds } from "../editor/extensions";
import {
  importCoworkMarkdown,
} from "../editor/markdownImport";
import { splitFrontmatter } from "../editor/frontmatter";
import { sha256Hex } from "../persistence/hashing";
import { serializeCoworkEditorMarkdown } from "../editor/serializeCoworkMarkdown";

export type CoworkBootstrapYdocResult =
  | {
      readonly ok: true;
      readonly snapshot: Uint8Array;
      readonly snapshotSha256: string;
      readonly sourceSha256: string;
      /** Canonical Markdown represented by the initialized structured document. */
      readonly projection: Uint8Array;
      readonly projectionSha256: string;
      readonly normalized: boolean;
    }
  | {
      readonly ok: false;
      readonly code: "invalid_utf8" | "unsupported_markdown";
      readonly message: string;
    };

export interface CoworkBootstrapYdocOptions {
  /**
   * Imported Markdown is a source used to initialize Co-work, not a writeback
   * target. Its formatting may therefore be normalized into the structured
   * document without blocking registration.
   */
  readonly allowNormalization?: boolean;
}

const newlineStyle = (source: string): "crlf" | "lf" | "cr" | "none" => {
  if (source.includes("\r\n")) return "crlf";
  if (source.includes("\n")) return "lf";
  if (source.includes("\r")) return "cr";
  return "none";
};

const trailingNewlineCount = (source: string): number => {
  const matches = source.match(/(?:\r\n|\r|\n)/g);
  if (matches === null) return 0;
  let cursor = source.length;
  let count = 0;
  for (let index = matches.length - 1; index >= 0; index -= 1) {
    const newline = matches[index];
    if (!source.slice(0, cursor).endsWith(newline)) break;
    cursor -= newline.length;
    count += 1;
  }
  return count;
};

interface MarkdownFence {
  readonly marker: "`" | "~";
  readonly length: number;
}

const lineAfter = (
  source: string,
  start: number,
): { readonly content: string; readonly end: number } => {
  let end = start;
  while (
    end < source.length &&
    source[end] !== "\r" &&
    source[end] !== "\n"
  ) {
    end += 1;
  }
  if (source[end] === "\r" && source[end + 1] === "\n") {
    end += 2;
  } else if (end < source.length) {
    end += 1;
  }
  return {
    content: source.slice(start, end).replace(/(?:\r\n|\r|\n)$/u, ""),
    end,
  };
};

const fenceAtLine = (line: string): MarkdownFence | null => {
  const match = /^ {0,3}(`{3,}|~{3,})/u.exec(line);
  const token = match?.[1];
  if (token === undefined) return null;
  return {
    marker: token[0] as "`" | "~",
    length: token.length,
  };
};

const isClosingFence = (
  line: string,
  active: MarkdownFence,
): boolean => {
  const match = /^ {0,3}(`{3,}|~{3,})[ \t]*$/u.exec(line);
  const token = match?.[1];
  return (
    token !== undefined &&
    token[0] === active.marker &&
    token.length >= active.length
  );
};

const isEscapedAt = (source: string, index: number): boolean => {
  let slashes = 0;
  for (let cursor = index - 1; cursor >= 0 && source[cursor] === "\\"; cursor -= 1) {
    slashes += 1;
  }
  return slashes % 2 === 1;
};

/**
 * Coerce Markdown HTML comments only where Markdown treats them as comments.
 *
 * Co-work has no invisible comment node, so an acquisition import exposes the
 * comment body as ordinary text instead of silently dropping it. Frontmatter,
 * fenced/indented code, and inline code stay byte-literal: comment-looking
 * examples in those contexts are content, not comments.
 */
export const exposeMarkdownCommentText = (source: string): string => {
  const { frontmatter, body } = splitFrontmatter(source);
  let output = "";
  let cursor = 0;
  let atLineStart = true;
  let fence: MarkdownFence | null = null;
  let inlineBackticks: number | null = null;

  while (cursor < body.length) {
    if (atLineStart && inlineBackticks === null) {
      const line = lineAfter(body, cursor);
      const candidateFence = fenceAtLine(line.content);
      if (fence !== null) {
        if (isClosingFence(line.content, fence)) fence = null;
        output += body.slice(cursor, line.end);
        cursor = line.end;
        atLineStart = true;
        continue;
      }
      if (candidateFence !== null) {
        fence = candidateFence;
        output += body.slice(cursor, line.end);
        cursor = line.end;
        atLineStart = true;
        continue;
      }
      // Conservatively preserve comment-looking text on indented code lines.
      if (/^(?: {4}|\t)/u.test(line.content)) {
        output += body.slice(cursor, line.end);
        cursor = line.end;
        atLineStart = true;
        continue;
      }
    }

    if (body[cursor] === "`" && !isEscapedAt(body, cursor)) {
      let end = cursor + 1;
      while (body[end] === "`") end += 1;
      const runLength = end - cursor;
      if (inlineBackticks === null) {
        inlineBackticks = runLength;
      } else if (inlineBackticks === runLength) {
        inlineBackticks = null;
      }
      output += body.slice(cursor, end);
      cursor = end;
      atLineStart = false;
      continue;
    }

    if (
      inlineBackticks === null &&
      body.startsWith("<!--", cursor)
    ) {
      const end = body.indexOf("-->", cursor + 4);
      if (end >= 0) {
        const visible = body.slice(cursor + 4, end).trim();
        if (visible.length > 0) {
          const prior = output.length > 0 ? output[output.length - 1] : undefined;
          if (prior !== undefined && !/\s/u.test(prior)) output += " ";
          output += visible;
          const following = body[end + 3];
          if (following !== undefined && !/\s/u.test(following)) output += " ";
        }
        cursor = end + 3;
        continue;
      }
    }

    const character = body[cursor] ?? "";
    output += character;
    cursor += 1;
    atLineStart = character === "\n" || character === "\r";
  }

  return `${frontmatter ?? ""}${output}`;
};

/**
 * Serialize the document another editor will actually open, rather than the
 * transient importer editor. The collaboration extension can normalize its
 * ProseMirror view while encoding Yjs state, so the importer view is not a
 * sufficient fidelity check.
 */
const serializeCoworkSnapshot = (snapshot: Uint8Array): string => {
  const verificationDocument = new Y.Doc();
  Y.applyUpdate(verificationDocument, snapshot);
  const verificationEditor = new Editor({
    extensions: buildEditorExtensions(verificationDocument),
  });
  try {
    return serializeCoworkEditorMarkdown(
      verificationEditor,
      verificationDocument,
    );
  } finally {
    verificationEditor.destroy();
    verificationDocument.destroy();
  }
};

/** Build the canonical initialized browser Y.Doc from exact staged Markdown bytes. */
export const bootstrapCoworkYdoc = async (
  sourceBytes: Uint8Array,
  options: CoworkBootstrapYdocOptions = {},
): Promise<CoworkBootstrapYdocResult> => {
  const hasBom =
    sourceBytes.length >= 3 &&
    sourceBytes[0] === 0xef &&
    sourceBytes[1] === 0xbb &&
    sourceBytes[2] === 0xbf;
  let source: string;
  try {
    source = new TextDecoder("utf-8", { fatal: true }).decode(
      hasBom ? sourceBytes.slice(3) : sourceBytes,
    );
  } catch {
    return {
      ok: false,
      code: "invalid_utf8",
      message: "Co-work can only register UTF-8 Markdown files.",
    };
  }

  // Tiptap's collaborative document drops Markdown HTML comments because
  // comments have no visible editor node. Imported files are acquisition
  // sources, so make their comment text visible instead of silently losing it.
  const importSource =
    options.allowNormalization === true
      ? exposeMarkdownCommentText(source)
      : source;
  const imported = importCoworkMarkdown(importSource);
  const sourceNewlineStyle = newlineStyle(source);
  const sourceTrailingNewlines = trailingNewlineCount(source);
  const sourceSha256 = await sha256Hex(sourceBytes);
  const document = new Y.Doc();
  const fidelity = document.getMap<unknown>("wb-cowork:fidelity");
  fidelity.set("schema", "cowork-fidelity/v1");
  fidelity.set("source_sha256", sourceSha256);
  fidelity.set("utf8_bom", hasBom);
  fidelity.set("newline_style", sourceNewlineStyle);
  fidelity.set("trailing_newline_count", sourceTrailingNewlines);
  fidelity.set("frontmatter", imported.frontmatter);

  const editor = new Editor({
    extensions: buildEditorExtensions(document),
  });
  if (importSource.length > 0) editor.commands.setContent(imported.doc);
  stopCapturingLoadTimeIds(editor);
  const importedSnapshot = Y.encodeStateAsUpdate(document);
  let projectionText = serializeCoworkSnapshot(importedSnapshot);
  let projectionBytes = new TextEncoder().encode(projectionText);
  let normalized =
    projectionBytes.length !== sourceBytes.length ||
    projectionBytes.some((byte, index) => byte !== sourceBytes[index]);
  // File-backed create/repair still require a lossless baseline. From file
  // opts into normalization because the selected source is detached from
  // Co-work writeback.
  if (normalized && options.allowNormalization !== true) {
    editor.destroy();
    document.destroy();
    return {
      ok: false,
      code: "unsupported_markdown",
      message:
        "Co-work can’t safely preserve parts of this file yet. The original file was not changed.",
    };
  }
  fidelity.set("normalized_on_import", normalized);
  let snapshot = Y.encodeStateAsUpdate(document);
  // `normalized_on_import` is part of the final Yjs state. Rehydrate that
  // final snapshot before declaring the managed projection so the committed
  // bytes exactly match what the opened editor will materialize.
  projectionText = serializeCoworkSnapshot(snapshot);
  projectionBytes = new TextEncoder().encode(projectionText);
  const finalNormalized =
    projectionBytes.length !== sourceBytes.length ||
    projectionBytes.some((byte, index) => byte !== sourceBytes[index]);
  if (finalNormalized !== normalized) {
    normalized = finalNormalized;
    fidelity.set("normalized_on_import", normalized);
    snapshot = Y.encodeStateAsUpdate(document);
    projectionText = serializeCoworkSnapshot(snapshot);
    projectionBytes = new TextEncoder().encode(projectionText);
  }
  const snapshotSha256 = await sha256Hex(snapshot);
  editor.destroy();
  document.destroy();

  return {
    ok: true,
    snapshot,
    snapshotSha256,
    sourceSha256,
    projection: projectionBytes,
    projectionSha256: await sha256Hex(projectionBytes),
    normalized,
  };
};

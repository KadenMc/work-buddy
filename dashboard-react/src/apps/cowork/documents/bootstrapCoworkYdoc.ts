import { Editor } from "@tiptap/core";
import * as Y from "yjs";

import { buildEditorExtensions, stopCapturingLoadTimeIds } from "../editor/extensions";
import {
  importCoworkMarkdown,
  serializeCoworkMarkdown,
} from "../editor/markdownImport";
import { sha256Hex } from "../persistence/hashing";
import { restoreCoworkMarkdownFidelity } from "../editor/serializeCoworkMarkdown";

export type CoworkBootstrapYdocResult =
  | {
      readonly ok: true;
      readonly snapshot: Uint8Array;
      readonly snapshotSha256: string;
      readonly sourceSha256: string;
    }
  | {
      readonly ok: false;
      readonly code: "invalid_utf8" | "unsupported_markdown";
      readonly message: string;
    };

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

/** Build the canonical initialized browser Y.Doc from exact staged Markdown bytes. */
export const bootstrapCoworkYdoc = async (
  sourceBytes: Uint8Array,
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

  const imported = importCoworkMarkdown(source);
  const sourceNewlineStyle = newlineStyle(source);
  const sourceTrailingNewlines = trailingNewlineCount(source);
  const roundTrip = restoreCoworkMarkdownFidelity(
    serializeCoworkMarkdown(imported),
    {
      newlineStyle: sourceNewlineStyle,
      utf8Bom: hasBom,
      trailingNewlineCount: sourceTrailingNewlines,
    },
  );
  const roundTripBytes = new TextEncoder().encode(roundTrip);
  // Until block-splice metadata covers a construct, refusing a lossy import is safer than
  // opening content the first Save could rewrite. Empty source is a valid blank document.
  if (
    sourceBytes.length > 0 &&
    (roundTripBytes.length !== sourceBytes.length ||
      roundTripBytes.some((byte, index) => byte !== sourceBytes[index]))
  ) {
    return {
      ok: false,
      code: "unsupported_markdown",
      message:
        "Co-work can’t safely preserve parts of this file yet. The original file was not changed.",
    };
  }

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
  if (source.length > 0) editor.commands.setContent(imported.doc);
  stopCapturingLoadTimeIds(editor);
  const snapshot = Y.encodeStateAsUpdate(document);
  const snapshotSha256 = await sha256Hex(snapshot);
  editor.destroy();
  document.destroy();

  return { ok: true, snapshot, snapshotSha256, sourceSha256 };
};

import { Editor } from "@tiptap/core";
import { describe, expect, it } from "vitest";
import * as Y from "yjs";

import {
  buildEditorExtensions,
  COWORK_FRAGMENT_FIELD,
} from "../editor/extensions";
import { serializeCoworkEditorMarkdown } from "../editor/serializeCoworkMarkdown";
import { sha256Hex } from "../persistence/hashing";
import { bootstrapCoworkYdoc } from "./bootstrapCoworkYdoc";

interface CorpusCase {
  readonly name: string;
  readonly filePath: string;
  readonly source: string;
  readonly expected: "admit" | "unsupported";
  readonly newline?: "crlf" | "lf" | "cr" | "none";
  readonly bom?: boolean;
}

// This corpus intentionally includes both the canonical syntax Co-work can preserve today
// and representative Markdown it must refuse until its lossless model expands. `filePath` is
// descriptive corpus metadata (bootstrap receives staged bytes), including the real-world
// spaces/mixed-case path case that lifecycle staging passes through unchanged.
const CORPUS: readonly CorpusCase[] = [
  { name: "empty file", filePath: "empty.md", source: "", expected: "admit", newline: "none", bom: false },
  { name: "blank CRLF lines", filePath: "blank.md", source: "\r\n\r\n", expected: "admit", newline: "crlf", bom: false },
  { name: "whitespace-only line", filePath: "spaces.md", source: "   \n", expected: "unsupported" },
  { name: "LF heading", filePath: "heading.md", source: "# Heading\n", expected: "admit", newline: "lf", bom: false },
  { name: "CRLF heading", filePath: "heading-crlf.md", source: "# Heading\r\n", expected: "admit", newline: "crlf", bom: false },
  { name: "bare CR line endings", filePath: "heading-cr.md", source: "# Heading\r", expected: "admit", newline: "cr", bom: false },
  { name: "UTF-8 BOM and CRLF", filePath: "bom.md", source: "\ufeff# Heading\r\n", expected: "admit", newline: "crlf", bom: true },
  {
    name: "verbatim YAML frontmatter",
    filePath: "frontmatter.md",
    source: "---\r\ntitle: Demo\r\ndev_notes: keep_me\r\n---\r\n# Body\r\n",
    expected: "admit",
    newline: "crlf",
    bom: false,
  },
  { name: "plain paragraph", filePath: "paragraph.md", source: "A plain paragraph.\n", expected: "admit" },
  { name: "emphasis and strong", filePath: "emphasis.md", source: "Paragraph with **bold** and *italic*.\n", expected: "admit" },
  { name: "inline code", filePath: "inline-code.md", source: "Use `wbuddy cowork` here.\n", expected: "admit" },
  { name: "HTTPS link", filePath: "links.md", source: "Read [the guide](https://example.com/guide).\n", expected: "admit" },
  { name: "unordered list", filePath: "lists/bullets.md", source: "- one\n- two\n", expected: "admit" },
  { name: "ordered list", filePath: "lists/ordered.md", source: "1. one\n2. two\n", expected: "admit" },
  { name: "task list", filePath: "lists/tasks.md", source: "- [ ] todo\n- [x] done\n", expected: "admit" },
  { name: "nested list", filePath: "lists/nested.md", source: "- parent\n  - child\n", expected: "admit" },
  { name: "blockquote", filePath: "quotes.md", source: "> Quoted truth.\n", expected: "admit" },
  { name: "fenced code block", filePath: "code/fenced.md", source: "```ts\nconst answer = 42;\n```\n", expected: "admit" },
  { name: "GFM table normalization", filePath: "tables.md", source: "| A | B |\n| --- | --- |\n| 1 | 2 |\n", expected: "unsupported" },
  { name: "image", filePath: "images.md", source: "![diagram](https://example.com/diagram.png)\n", expected: "admit" },
  { name: "horizontal rule", filePath: "rule.md", source: "Before\n\n---\n\nAfter\n", expected: "admit" },
  { name: "Unicode prose", filePath: "i18n/日本語.md", source: "Café — 日本語 — 🙂\n", expected: "admit" },
  {
    name: "large repeated blocks",
    filePath: "large/repeated.md",
    source: Array.from({ length: 80 }, (_, index) => `Block ${index + 1}.`).join("\n\n") + "\n",
    expected: "admit",
  },
  { name: "spaces and mixed-case path", filePath: "Docs/My Working Note.MD", source: "# Mixed path\n", expected: "admit" },
  { name: "raw HTML element", filePath: "unsupported/html.md", source: "<custom-element>value</custom-element>\n", expected: "unsupported" },
  { name: "HTML comment", filePath: "unsupported/comment.md", source: "<!-- private note -->\n", expected: "unsupported" },
  { name: "setext heading normalization", filePath: "unsupported/setext.md", source: "Heading\n=======\n", expected: "unsupported" },
  { name: "asterisk bullet normalization", filePath: "unsupported/star-list.md", source: "* one\n* two\n", expected: "unsupported" },
];

describe("bootstrapCoworkYdoc", () => {
  it("creates a nonempty valid canonical snapshot for an intentionally blank document", async () => {
    const result = await bootstrapCoworkYdoc(new Uint8Array(0));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.snapshot.byteLength).toBeGreaterThan(0);
    const document = new Y.Doc();
    expect(() => Y.applyUpdate(document, result.snapshot)).not.toThrow();
    expect(document.getMap("wb-cowork:fidelity").get("schema")).toBe(
      "cowork-fidelity/v1",
    );
    expect(document.getXmlFragment(COWORK_FRAGMENT_FIELD)).toBeDefined();
    document.destroy();
  });

  it("rejects invalid UTF-8 without producing a snapshot", async () => {
    const result = await bootstrapCoworkYdoc(new Uint8Array([0xc3, 0x28]));
    expect(result).toMatchObject({ ok: false, code: "invalid_utf8" });
  });

  it.each(CORPUS)("handles corpus file: $name ($filePath)", async ({ source, expected, newline, bom }) => {
    const bytes = new TextEncoder().encode(source);
    const result = await bootstrapCoworkYdoc(bytes);

    if (expected === "unsupported") {
      expect(result).toMatchObject({ ok: false, code: "unsupported_markdown" });
      return;
    }
    expect(result).toMatchObject({
      ok: true,
      sourceSha256: await sha256Hex(bytes),
    });
    if (!result.ok) return;
    const document = new Y.Doc();
    Y.applyUpdate(document, result.snapshot);
    const fidelity = document.getMap<unknown>("wb-cowork:fidelity");
    const expectedNewline =
      newline ??
      (source.includes("\r\n")
        ? "crlf"
        : source.includes("\n")
          ? "lf"
          : source.includes("\r")
            ? "cr"
            : "none");
    expect(fidelity.get("newline_style")).toBe(expectedNewline);
    expect(fidelity.get("utf8_bom")).toBe(bom ?? source.startsWith("\ufeff"));
    if (source.includes("dev_notes")) {
      expect(fidelity.get("frontmatter")).toBe(
        "---\r\ntitle: Demo\r\ndev_notes: keep_me\r\n---\r\n",
      );
    }
    const editor = new Editor({ extensions: buildEditorExtensions(document) });
    const serializedBytes = new TextEncoder().encode(
      serializeCoworkEditorMarkdown(editor, document),
    );
    expect(serializedBytes).toEqual(bytes);
    editor.destroy();
    document.destroy();
  });
});

// Fail-hard rule 4 (C1 surface contract section 7.2, gate condition 9): unknown
// Obsidian constructs (wikilinks, embeds, callouts, footnotes) are byte-identical
// inside UNEDITED blocks, and an EDITED block that holds one emits a flag rather
// than a silent normalization. Feeding such a block through the serializer
// backslash-escapes its brackets (SP-3 case 2), so the block-splice materializer
// flags it and keeps its raw source instead.
import { describe, it, expect } from "vitest";
import { readCorpus } from "../src/corpus.js";
import {
  importDocument,
  materialize,
  serializeBlockJson,
  detectUnknownConstructs,
  type Block,
  type ImportedDocument,
} from "../src/materializer.js";

const corpus = readCorpus();

interface Located {
  path: string;
  doc: ImportedDocument;
  block: Block;
}

function locateUnknownBlocks(): Located[] {
  const found: Located[] = [];
  for (const file of corpus) {
    const doc = importDocument(file.source);
    for (const block of doc.blocks) {
      if (block.unknownConstructs.length > 0) {
        found.push({ path: file.entry.path, doc, block });
      }
    }
  }
  return found;
}

const located = locateUnknownBlocks();

describe("rule 4: unknown constructs preserved-and-flagged, never silently normalized", () => {
  it("detects the Obsidian construct families in the corpus", () => {
    const labels = new Set<string>();
    for (const { block } of located) {
      for (const label of block.unknownConstructs) labels.add(label);
    }
    expect(labels.has("wikilink")).toBe(true);
    expect(labels.has("embed")).toBe(true);
    expect(labels.has("callout")).toBe(true);
    expect(labels.has("footnote")).toBe(true);
  });

  it("keeps unknown constructs byte-identical inside unedited blocks", () => {
    for (const { path, doc, block } of located) {
      const result = materialize(doc);
      expect(
        result.markdown.includes(block.raw),
        `unedited construct altered in ${path}`,
      ).toBe(true);
    }
  });

  it("flags an edited unknown block and keeps it verbatim (no silent corruption)", () => {
    for (const { path, doc, block } of located) {
      const edits = new Map([[block.id, "SILENTLY NORMALIZED CONTENT"]]);
      const result = materialize(doc, edits);
      const flagged = result.flaggedUnknowns.find((f) => f.blockId === block.id);
      expect(flagged, `no flag for edited construct in ${path}`).toBeDefined();
      expect(flagged?.constructs).toEqual(block.unknownConstructs);
      // The lossy replacement never reaches the file, the raw is kept.
      expect(result.markdown.includes(block.raw)).toBe(true);
      expect(result.markdown.includes("SILENTLY NORMALIZED CONTENT")).toBe(false);
    }
  });

  it("proves flagging is necessary: serializing a bracket block corrupts it", () => {
    const bracketBlocks = located.filter((l) =>
      l.block.unknownConstructs.some((c) => c === "wikilink" || c === "embed" || c === "callout"),
    );
    expect(bracketBlocks.length).toBeGreaterThan(0);
    for (const { block } of bracketBlocks) {
      if (block.json === null) continue;
      const serialized = serializeBlockJson(block.json);
      // The serializer alters the block (escapes brackets or reflows the callout),
      // which is exactly the silent corruption the flag prevents.
      expect(serialized).not.toBe(block.raw);
    }
  });

  it("only opts into serializing a flagged block when explicitly asked", () => {
    const one = located.find((l) => l.block.json !== null);
    expect(one).toBeDefined();
    if (!one) return;
    const edits = new Map([[one.block.id, "REPLACEMENT"]]);
    const forced = materialize(one.doc, edits, { serializeFlaggedBlocks: true });
    expect(forced.markdown.includes("REPLACEMENT")).toBe(true);
    // Even when serialized, the block is still reported as flagged.
    expect(forced.flaggedUnknowns.some((f) => f.blockId === one.block.id)).toBe(true);
  });

  it("detectUnknownConstructs distinguishes embeds from plain wikilinks", () => {
    expect(detectUnknownConstructs("See [[Note]] here")).toEqual(["wikilink"]);
    expect(detectUnknownConstructs("Embed ![[Note]] here")).toEqual(["embed"]);
    expect(detectUnknownConstructs("plain paragraph")).toEqual([]);
  });
});

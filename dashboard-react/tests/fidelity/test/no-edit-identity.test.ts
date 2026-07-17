// Fail-hard rule 1 (C1 surface contract section 7.2): a no-edit materialize MUST
// equal the source byte-for-byte across the whole corpus. The frontmatter is
// stripped at import and re-attached at materialize, so the reconstructed output
// equals the full source including its frontmatter (SP-3 case 4 proved 100 percent
// on the prototype, re-verified here across every corpus file).
import { describe, it, expect } from "vitest";
import { readCorpus } from "../src/corpus.js";
import { importDocument, materialize } from "../src/materializer.js";
import { splitFrontmatter } from "../src/frontmatter.js";

const corpus = readCorpus();

describe("rule 1: no-edit materialize equals source byte-for-byte", () => {
  it("has a non-trivial corpus", () => {
    expect(corpus.length).toBeGreaterThanOrEqual(25);
  });

  for (const file of corpus) {
    it(`round-trips ${file.entry.path} with zero edits`, () => {
      const doc = importDocument(file.source);
      const result = materialize(doc);
      expect(result.markdown).toBe(file.source);
      expect(result.flaggedUnknowns).toEqual([]);
      expect(result.dirtyBlockIds).toEqual([]);
    });
  }

  it("preserves the frontmatter-stripped body exactly (block raw reconstruction)", () => {
    for (const file of corpus) {
      const { body } = splitFrontmatter(file.source);
      const doc = importDocument(file.source);
      const reconstructedBody = doc.blocks.map((b) => b.raw).join("");
      expect(reconstructedBody, `body mismatch in ${file.entry.path}`).toBe(body);
    }
  });
});

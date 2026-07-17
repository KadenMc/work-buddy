// Fail-hard rule 2 (C1 surface contract section 7.2): a single-block edit MUST
// byte-preserve every byte OUTSIDE the edited block. Block-splice is mandatory,
// whole-document serialization forbidden for materialization. This edits one clean
// content block per file, re-serializes only that block, and asserts the source
// prefix and suffix around the edited range survive exactly (SP-3 case 4 measured
// 100 percent outside-block preservation on the prototype).
import { describe, it, expect } from "vitest";
import { readCorpus } from "../src/corpus.js";
import {
  importDocument,
  materialize,
  contentBlocks,
  type Block,
  type ImportedDocument,
} from "../src/materializer.js";
import { simulateBlockEdit } from "../src/editing.js";

const corpus = readCorpus();

/** A block is a clean edit target when it has a structured form, holds no
 *  non-first-class construct, and a text leaf can be appended to. Pick from the
 *  middle so both the source prefix and suffix are non-empty and meaningful. */
function pickEditTarget(
  doc: ImportedDocument,
): { block: Block; editedMarkdown: string } | null {
  const candidates = contentBlocks(doc).filter(
    (b) => b.json !== null && b.unknownConstructs.length === 0,
  );
  const ordered = [
    ...candidates.slice(Math.floor(candidates.length / 2)),
    ...candidates.slice(0, Math.floor(candidates.length / 2)),
  ];
  for (const block of ordered) {
    const editedMarkdown = simulateBlockEdit(block);
    if (editedMarkdown !== null && editedMarkdown !== block.raw) {
      return { block, editedMarkdown };
    }
  }
  return null;
}

describe("rule 2: single-block edit preserves every byte outside the edit", () => {
  let exercised = 0;

  for (const file of corpus) {
    it(`confines the edit in ${file.entry.path}`, () => {
      const doc = importDocument(file.source);
      const target = pickEditTarget(doc);
      if (target === null) {
        return; // no clean editable block (tiny single-construct file)
      }
      exercised += 1;

      const edits = new Map([[target.block.id, target.editedMarkdown]]);
      const result = materialize(doc, edits);

      const editStart = doc.frontmatter.length + target.block.start;
      const editEnd = doc.frontmatter.length + target.block.end;
      const prefix = file.source.slice(0, editStart);
      const suffix = file.source.slice(editEnd);

      // Every byte before and after the edited block is byte-identical.
      expect(result.markdown.startsWith(prefix), "prefix drifted").toBe(true);
      expect(result.markdown.endsWith(suffix), "suffix drifted").toBe(true);
      // The edit actually landed (guards against a trivial pass).
      expect(result.markdown).not.toBe(file.source);
      expect(result.dirtyBlockIds).toEqual([target.block.id]);
    });
  }

  it("exercised a broad slice of the corpus", () => {
    expect(exercised).toBeGreaterThanOrEqual(20);
  });
});

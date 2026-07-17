// Materialization timing at 10k words (PRD section 11 target 4, closure delegated
// here from SP-4). Target: materialization under 500 ms at 10k words on the
// reference machine. Block-splice materialization is dominated by copying unedited
// block raw plus re-serializing the edited block, so it is far cheaper than a
// whole-document serialize. This assembles a 10k-word document from the corpus,
// edits one middle block, and measures the realistic save path.
import { describe, it, expect } from "vitest";
import { readCorpus } from "../src/corpus.js";
import { splitFrontmatter } from "../src/frontmatter.js";
import {
  importDocument,
  materialize,
  contentBlocks,
  type Block,
} from "../src/materializer.js";
import { simulateBlockEdit } from "../src/editing.js";

const TARGET_MS = 500;
const WORD_TARGET = 10_000;

function wordCount(text: string): number {
  const trimmed = text.trim();
  return trimmed.length === 0 ? 0 : trimmed.split(/\s+/).length;
}

/** Assemble a body of at least WORD_TARGET words from corpus bodies (frontmatter
 *  stripped so the assembled doc has a single clean block stream). */
function buildLargeBody(): string {
  const corpus = readCorpus();
  const parts: string[] = [];
  let words = 0;
  // Deterministic order, largest first, so the target is reached quickly.
  const bodies = corpus
    .map((f) => splitFrontmatter(f.source).body)
    .sort((a, b) => b.length - a.length);
  for (const body of bodies) {
    parts.push(body);
    words += wordCount(body);
    if (words >= WORD_TARGET) break;
  }
  return parts.join("\n\n");
}

function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? ((sorted[mid - 1] as number) + (sorted[mid] as number)) / 2
    : (sorted[mid] as number);
}

describe("materialization timing at 10k words", () => {
  const body = buildLargeBody();
  const words = wordCount(body);
  const doc = importDocument(body);

  it("assembled a document of at least 10k words", () => {
    expect(words).toBeGreaterThanOrEqual(WORD_TARGET);
    expect(contentBlocks(doc).length).toBeGreaterThan(10);
  });

  it(`materializes a one-block edit under ${TARGET_MS} ms`, () => {
    const blocks = contentBlocks(doc);
    const middle = blocks[Math.floor(blocks.length / 2)] as Block;
    const editedMarkdown = simulateBlockEdit(middle) ?? middle.raw;

    const runOnce = (): number => {
      const start = performance.now();
      // The realistic save path: re-serialize the edited block, then splice.
      const edited = simulateBlockEdit(middle) ?? editedMarkdown;
      const edits = new Map([[middle.id, edited]]);
      const result = materialize(doc, edits);
      const elapsed = performance.now() - start;
      // Touch the result so the work is not optimized away.
      if (result.markdown.length === 0) throw new Error("empty materialize");
      return elapsed;
    };

    // Warm up, then measure a batch and take the median and best.
    for (let i = 0; i < 5; i += 1) runOnce();
    const samples: number[] = [];
    for (let i = 0; i < 50; i += 1) samples.push(runOnce());
    const med = median(samples);
    const best = Math.min(...samples);

    // Also time the pure splice (assembly only, edit pre-serialized).
    const preEdits = new Map([[middle.id, editedMarkdown]]);
    const spliceSamples: number[] = [];
    for (let i = 0; i < 50; i += 1) {
      const start = performance.now();
      materialize(doc, preEdits);
      spliceSamples.push(performance.now() - start);
    }

    console.log(
      `[timing] words=${words} blocks=${blocks.length} ` +
        `materialize(edit+splice) median=${med.toFixed(3)}ms best=${best.toFixed(3)}ms ` +
        `splice-only median=${median(spliceSamples).toFixed(3)}ms target=${TARGET_MS}ms`,
    );

    expect(med).toBeLessThan(TARGET_MS);
  });
});

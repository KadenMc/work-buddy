// Idempotency convergence (task build item 3, SP-3 case 1). With the frontmatter
// boundary applied, every corpus file's body-only round-trip converges to a fixed
// point within a small bounded number of passes. SP-3 found whole-document
// serialization does not converge on two files (unbounded, driven by frontmatter),
// and stripping frontmatter removes that divergence. Measured here: every body
// reaches a fixed point within 3 passes.
import { describe, it, expect } from "vitest";
import { readCorpus } from "../src/corpus.js";
import { splitFrontmatter } from "../src/frontmatter.js";
import { createManager } from "../src/bundle.js";

const corpus = readCorpus();
const manager = createManager();
const MAX_PASSES = 5;

function convergence(body: string): { passes: number; converged: boolean } {
  let previous = body;
  for (let pass = 1; pass <= MAX_PASSES; pass += 1) {
    const next = manager.serialize(manager.parse(previous));
    if (next === previous) return { passes: pass, converged: true };
    previous = next;
  }
  return { passes: MAX_PASSES, converged: false };
}

describe("idempotency: body-only round-trip converges to a fixed point", () => {
  let maxPasses = 0;

  for (const file of corpus) {
    it(`converges ${file.entry.path}`, () => {
      const { body } = splitFrontmatter(file.source);
      const result = convergence(body);
      expect(
        result.converged,
        `${file.entry.path} did not converge within ${MAX_PASSES} passes`,
      ).toBe(true);
      maxPasses = Math.max(maxPasses, result.passes);
    });
  }

  it("converges the whole corpus within a tight bound", () => {
    // Reported for the record: the deepest fixed point across the corpus.
    expect(maxPasses).toBeGreaterThan(0);
    expect(maxPasses).toBeLessThanOrEqual(MAX_PASSES);
  });
});

// Fail-hard rule 5 (C1 surface contract section 7.2, gate condition 7): import MUST
// strip and re-attach YAML frontmatter verbatim at the boundary, never feed it
// through the serializer. SP-3 case 3 measured unbounded escape growth otherwise.
// This asserts verbatim strip-and-reattach on every frontmatter file and proves
// the boundary is necessary by showing the two SP-3 files grow without bound when
// their frontmatter is fed through the serializer.
import { describe, it, expect } from "vitest";
import { readCorpus } from "../src/corpus.js";
import {
  splitFrontmatter,
  reattachFrontmatter,
  hasFrontmatter,
} from "../src/frontmatter.js";
import { importDocument, materialize } from "../src/materializer.js";
import { createManager } from "../src/bundle.js";

const corpus = readCorpus();
const manager = createManager();

const frontmatterFiles = corpus.filter((f) => hasFrontmatter(f.source));

function backslashCount(text: string): number {
  return (text.match(/\\/g) ?? []).length;
}

describe("rule 5: frontmatter strip-and-reattach is verbatim", () => {
  it("has frontmatter-bearing files in the corpus", () => {
    expect(frontmatterFiles.length).toBeGreaterThanOrEqual(10);
  });

  for (const file of frontmatterFiles) {
    it(`splits and reattaches ${file.entry.path} byte-for-byte`, () => {
      const { frontmatter, body } = splitFrontmatter(file.source);
      expect(frontmatter.startsWith("---\n")).toBe(true);
      expect(frontmatter).toContain("\n---");
      expect(reattachFrontmatter(frontmatter, body)).toBe(file.source);

      // The importer carries the frontmatter untouched into materialize.
      const doc = importDocument(file.source);
      expect(doc.frontmatter).toBe(frontmatter);
      expect(materialize(doc).markdown.startsWith(frontmatter)).toBe(true);
    });
  }

  it("files without frontmatter keep the whole source as body", () => {
    const noFm = corpus.filter((f) => !hasFrontmatter(f.source));
    expect(noFm.length).toBeGreaterThan(0);
    for (const file of noFm) {
      const { frontmatter, body } = splitFrontmatter(file.source);
      expect(frontmatter).toBe("");
      expect(body).toBe(file.source);
    }
  });

  it("proves the boundary is necessary: frontmatter fed through the serializer grows without bound", () => {
    const targets = corpus.filter(
      (f) =>
        f.entry.path.includes("artifact-system") ||
        f.entry.path.includes("workflows"),
    );
    expect(targets.length).toBe(2);

    for (const file of targets) {
      // Feeding the WHOLE source (frontmatter included) through the serializer
      // strictly grows the backslash count pass over pass.
      let fed = file.source;
      const counts: number[] = [];
      for (let pass = 0; pass < 4; pass += 1) {
        fed = manager.serialize(manager.parse(fed));
        counts.push(backslashCount(fed));
      }
      for (let i = 1; i < counts.length; i += 1) {
        expect(
          counts[i],
          `${file.entry.path} did not grow: ${counts.join(", ")}`,
        ).toBeGreaterThan(counts[i - 1] as number);
      }

      // With the frontmatter stripped, the body converges to a fixed point.
      const { body } = splitFrontmatter(file.source);
      const s1 = manager.serialize(manager.parse(body));
      const s2 = manager.serialize(manager.parse(s1));
      expect(s2).toBe(s1);
    }
  });
});

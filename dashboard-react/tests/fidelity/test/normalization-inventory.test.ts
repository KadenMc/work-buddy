// The SP-3 normalization inventory as regression expectations. Each isolated input
// serializes to a pinned output under the full fidelity bundle. These lock the
// serializer's whole-document normalization behavior so a future @tiptap/markdown
// bump that changes any normalization is caught by the gate, and they document WHY
// block-splice is mandatory: whole-document serialization is not byte-preserving.
import { describe, it, expect } from "vitest";
import { createManager } from "../src/bundle.js";
import { NORMALIZATION_INVENTORY } from "../src/normalizationInventory.js";

const manager = createManager();
const roundTrip = (input: string) => manager.serialize(manager.parse(input));

describe("normalization inventory regression", () => {
  it("covers a representative span of normalization classes", () => {
    const kinds = new Set(NORMALIZATION_INVENTORY.map((c) => c.kind));
    expect(kinds.has("cosmetic")).toBe(true);
    expect(kinds.has("corruption")).toBe(true);
    expect(kinds.has("rewrite")).toBe(true);
    expect(kinds.has("preserved")).toBe(true);
    expect(NORMALIZATION_INVENTORY.length).toBeGreaterThanOrEqual(20);
  });

  for (const testCase of NORMALIZATION_INVENTORY) {
    it(`locks ${testCase.label} (${testCase.kind})`, () => {
      expect(roundTrip(testCase.input)).toBe(testCase.output);
    });
  }

  it("preserved-class inputs round-trip byte-identical", () => {
    for (const testCase of NORMALIZATION_INVENTORY) {
      if (testCase.kind === "preserved") {
        expect(roundTrip(testCase.input)).toBe(testCase.input);
      }
    }
  });

  it("corruption-class inputs are genuinely altered (so block-splice is required)", () => {
    for (const testCase of NORMALIZATION_INVENTORY) {
      if (testCase.kind === "corruption") {
        expect(roundTrip(testCase.input)).not.toBe(testCase.input);
      }
    }
  });
});

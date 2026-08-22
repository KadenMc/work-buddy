import { describe, expect, it } from "vitest";

import provenanceStyles from "./styles.css?raw";

describe("Provenance selection affordance styles", () => {
  it("sizes the floating wrapper to its control instead of covering the editor", () => {
    expect(provenanceStyles).toMatch(
      /\.wb-cowork-provenance-selection\s*\{[^}]*inline-size:\s*max-content;/su,
    );
    expect(provenanceStyles).toMatch(
      /\.wb-cowork-provenance-selection\s*\{[^}]*max-inline-size:/su,
    );
  });
});

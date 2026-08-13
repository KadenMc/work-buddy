import { describe, expect, it } from "vitest";

import bridgeStyles from "../bridge/styles.css?raw";
import surfaceStyles from "../surface/styles.css?raw";

describe("Co-work editor decoration style contract", () => {
  it("gives every ledger state a distinct non-colour encoding", () => {
    expect(surfaceStyles).toContain(".wb-cowork-suggestion--insertion");
    expect(surfaceStyles).toContain("text-decoration-style: solid");
    expect(surfaceStyles).toContain(".wb-cowork-suggestion--deletion");
    expect(surfaceStyles).toContain("text-decoration-line: line-through");
    expect(surfaceStyles).toContain(".wb-cowork-suggestion--modification");
    expect(surfaceStyles).toContain("text-decoration-style: dotted");
    expect(surfaceStyles).toContain(".wb-cowork-flag-mark");
    expect(surfaceStyles).toContain("text-decoration-style: wavy");
    expect(surfaceStyles).toContain(".wb-cowork-expression-mark");
    expect(surfaceStyles).toContain("text-decoration-style: dashed");
    expect(surfaceStyles).toContain(".wb-cowork-provenance-tint::after");
    expect(surfaceStyles).toContain('content: "\\00a0\\2713"');
    expect(surfaceStyles).toContain(".wb-cowork-provenance--ai");
    expect(surfaceStyles).toContain(".wb-cowork-provenance--unrecorded");
    expect(surfaceStyles).toContain(".wb-cowork-provenance--review-not-reviewed");
    expect(surfaceStyles).toContain("underline dashed");
    expect(surfaceStyles).toContain(".wb-cowork-provenance--review-unknown");
    expect(surfaceStyles).toContain("underline dotted");
    expect(surfaceStyles).toContain(".wb-cowork-provenance--conflict");
  });

  it("keeps focus/highlight visible for reduced-motion and forced-colors users", () => {
    const combined = `${surfaceStyles}\n${bridgeStyles}`;
    expect(combined).toContain("@media (prefers-reduced-motion: reduce)");
    expect(combined).toContain("@media (forced-colors: active)");
    expect(combined).toContain(".wb-cowork-anchor--active");
    expect(combined).toContain(".wb-cowork-anchor--flash");
    expect(combined).toContain(".wb-cowork-passage-highlight");
    expect(combined).toContain("outline: 2px solid Highlight");
  });
});

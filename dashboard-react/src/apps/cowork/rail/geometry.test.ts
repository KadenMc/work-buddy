import { describe, expect, it } from "vitest";

import { computeAlignedLayout, placementsEqual } from "./geometry";

describe("computeAlignedLayout", () => {
  it("places well-separated cards at their exact anchor tops", () => {
    const layout = computeAlignedLayout(
      [
        { id: "a", anchorTop: 0, height: 40 },
        { id: "b", anchorTop: 200, height: 40 },
      ],
      { gap: 8 },
    );
    expect(layout).toEqual([
      { id: "a", top: 0 },
      { id: "b", top: 200 },
    ]);
  });

  it("pushes clustered cards down to keep the minimum gap and no overlap", () => {
    const layout = computeAlignedLayout(
      [
        { id: "a", anchorTop: 100, height: 50 },
        { id: "b", anchorTop: 110, height: 50 },
        { id: "c", anchorTop: 120, height: 50 },
      ],
      { gap: 10 },
    );
    // a sits at its anchor, b and c cascade below by height plus gap.
    expect(layout).toEqual([
      { id: "a", top: 100 },
      { id: "b", top: 160 },
      { id: "c", top: 220 },
    ]);
  });

  it("preserves document order even when anchors are out of order", () => {
    const layout = computeAlignedLayout([
      { id: "late", anchorTop: 300, height: 20 },
      { id: "early", anchorTop: 10, height: 20 },
    ]);
    expect(layout.map((placement) => placement.id)).toEqual(["early", "late"]);
  });

  it("clamps to minTop", () => {
    const layout = computeAlignedLayout(
      [{ id: "a", anchorTop: -50, height: 10 }],
      { minTop: 12 },
    );
    expect(layout[0].top).toBe(12);
  });
});

describe("placementsEqual", () => {
  it("is true for identical placements and false when a top moves", () => {
    const base = [{ id: "a", top: 0 }];
    expect(placementsEqual(base, [{ id: "a", top: 0 }])).toBe(true);
    expect(placementsEqual(base, [{ id: "a", top: 1 }])).toBe(false);
    expect(placementsEqual(base, [])).toBe(false);
  });
});

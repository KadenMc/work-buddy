import { describe, expect, it } from "vitest";

import {
  EDITOR_DEFAULT_SIZE,
  EDITOR_MIN_SIZE,
  EDITOR_PANEL_ID,
  LAYOUT_STORAGE_ID,
  RAIL_DEFAULT_SIZE,
  RAIL_MAX_SIZE,
  RAIL_MIN_SIZE,
  RAIL_PANEL_ID,
} from "./workspacePanelLayout";

const percent = (value: string): number => {
  expect(value).toMatch(/^\d+(\.\d+)?%$/);
  return Number.parseFloat(value);
};

describe("Co-work split size policy", () => {
  it("expresses every size as a percentage, never a fixed pixel width", () => {
    for (const size of [
      EDITOR_DEFAULT_SIZE,
      EDITOR_MIN_SIZE,
      RAIL_DEFAULT_SIZE,
      RAIL_MIN_SIZE,
      RAIL_MAX_SIZE,
    ]) {
      expect(size).toMatch(/%$/);
    }
  });

  it("gives the rail real travel: narrow floor, wide ceiling, a wider default in between", () => {
    const min = percent(RAIL_MIN_SIZE);
    const def = percent(RAIL_DEFAULT_SIZE);
    const max = percent(RAIL_MAX_SIZE);

    expect(min).toBeLessThan(def);
    expect(def).toBeLessThan(max);
    // Genuinely narrow at the floor, a clear majority at the ceiling.
    expect(min).toBeLessThanOrEqual(20);
    expect(max).toBeGreaterThanOrEqual(60);
    // A default noticeably wider than the old fixed 320px rail (~19% of a 1680px body).
    expect(def).toBeGreaterThan(25);
  });

  it("keeps the editor and rail constraints jointly satisfiable", () => {
    // The rail ceiling is the editor floor, and the two defaults tile the whole body.
    expect(percent(EDITOR_MIN_SIZE) + percent(RAIL_MAX_SIZE)).toBe(100);
    expect(percent(EDITOR_DEFAULT_SIZE) + percent(RAIL_DEFAULT_SIZE)).toBe(100);
  });
});

describe("Co-work layout compatibility", () => {
  it("keeps the existing storage id and persisted panel keys for the shared split", () => {
    expect(LAYOUT_STORAGE_ID).toBe("wb.cowork.workspace-layout");
    expect(EDITOR_PANEL_ID).toBe("editor");
    expect(RAIL_PANEL_ID).toBe("rail");
  });
});

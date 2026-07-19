import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  EDITOR_DEFAULT_SIZE,
  EDITOR_MIN_SIZE,
  LAYOUT_STORAGE_ID,
  RAIL_DEFAULT_SIZE,
  RAIL_MAX_SIZE,
  RAIL_MIN_SIZE,
  resolveLayoutStorage,
  useResizableRail,
} from "./useResizableRail";

// react-resizable-panels owns the drag, keyboard, and DOM measurement, which jsdom cannot run
// (getBoundingClientRect returns zeros, no ResizeObserver). The real resize behavior is proven
// against the built app in a browser. Here we mock the library's persistence hook so we can
// assert our own wiring deterministically without rendering a Group. vitest hoists vi.mock and
// vi.hoisted above the imports, so the mock is in place before the hook module loads.
const { useDefaultLayout } = vi.hoisted(() => ({ useDefaultLayout: vi.fn() }));
vi.mock("react-resizable-panels", () => ({ useDefaultLayout }));

const percent = (value: string): number => {
  expect(value).toMatch(/^\d+(\.\d+)?%$/);
  return Number.parseFloat(value);
};

afterEach(() => {
  useDefaultLayout.mockReset();
  window.localStorage.clear();
});

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

describe("layout persistence wiring", () => {
  it("resolves window.localStorage as the layout store when a window exists", () => {
    expect(resolveLayoutStorage()).toBe(window.localStorage);
  });

  it("persists to localStorage under a stable id and only for user-driven resizes", () => {
    useDefaultLayout.mockReturnValue({
      defaultLayout: undefined,
      onLayoutChange: () => {},
      onLayoutChanged: () => {},
    });

    renderHook(() => useResizableRail());

    expect(useDefaultLayout).toHaveBeenCalledWith({
      id: LAYOUT_STORAGE_ID,
      storage: window.localStorage,
      onlySaveAfterUserInteractions: true,
    });
  });

  it("returns the restored layout and the settled-change callback for the Group", () => {
    const restored = { editor: 60, rail: 40 };
    const onLayoutChanged = vi.fn();
    useDefaultLayout.mockReturnValue({
      defaultLayout: restored,
      onLayoutChange: () => {},
      onLayoutChanged,
    });

    const { result } = renderHook(() => useResizableRail());

    expect(result.current.defaultLayout).toBe(restored);
    expect(result.current.onLayoutChanged).toBe(onLayoutChanged);
  });
});

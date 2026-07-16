import { describe, expect, it } from "vitest";

import { asWidgetInstanceId } from "../contributions/contracts";
import type { DashboardLayout, WidgetLayoutItem } from "./contracts";
import {
  addLayoutItem,
  applyLayoutCommand,
  findFirstAvailablePlacement,
  moveLayoutItem,
  resizeLayoutItem,
  tidyDashboardLayout,
  validateDashboardLayout,
} from "./operations";

const item = (
  id: string,
  x: number,
  y: number,
  w = 6,
  h = 4,
  constraints: Partial<WidgetLayoutItem> = {},
): WidgetLayoutItem => ({
  instanceId: asWidgetInstanceId(id),
  x,
  y,
  w,
  h,
  ...constraints,
});

describe("layout operations", () => {
  it("preserves gaps and never moves unrelated widgets", () => {
    const opening = [item("a", 0, 8), item("b", 12, 0)] as const;
    const moved = moveLayoutItem(opening, asWidgetInstanceId("a"), 0, 12);

    expect(moved).toMatchObject({ accepted: true });
    expect(moved.items).toEqual([item("a", 0, 12), opening[1]]);
  });

  it("rejects collision instead of pushing either item", () => {
    const opening = [item("a", 0, 0), item("b", 8, 0)] as const;
    const moved = moveLayoutItem(opening, asWidgetInstanceId("a"), 8, 0);

    expect(moved).toEqual({ accepted: false, items: opening, reason: "collision" });
  });

  it("enforces min/max size for pointer-independent menu operations", () => {
    const opening = [item("a", 0, 0, 6, 4, { minW: 6, minH: 4, maxW: 8 })];

    expect(resizeLayoutItem(opening, asWidgetInstanceId("a"), 5, 4).reason).toBe(
      "size-limit",
    );
    expect(
      applyLayoutCommand(opening, {
        kind: "resize",
        instanceId: asWidgetInstanceId("a"),
        direction: "grow-width",
      }).items[0]?.w,
    ).toBe(7);
  });

  it("uses deterministic first-fit placement for add and occupied restoration", () => {
    const occupied = [item("a", 0, 0, 8, 4), item("b", 8, 0, 8, 4)];
    expect(
      findFirstAvailablePlacement(occupied, { w: 8, h: 4 }, { preferred: { x: 0, y: 0 } }),
    ).toEqual({ x: 16, y: 0 });

    const added = addLayoutItem(occupied, item("c", 0, 0, 8, 4));
    expect(added.accepted).toBe(true);
    expect(added.items[2]).toMatchObject({ instanceId: "c", x: 16, y: 0 });
  });

  it("compacts only when explicit Tidy is invoked", () => {
    const opening: DashboardLayout = [item("a", 0, 8), item("b", 8, 12)];
    expect(validateDashboardLayout(opening)).toEqual([]);

    const ordinary = moveLayoutItem(opening, asWidgetInstanceId("a"), 0, 9);
    expect(ordinary.items[1]?.y).toBe(12);
    expect(tidyDashboardLayout(opening).map(({ y }) => y)).toEqual([0, 0]);
  });
});

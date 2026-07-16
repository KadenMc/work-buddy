import { describe, expect, it } from "vitest";

import { asWidgetInstanceId, asWidgetSlotId } from "../contributions/contracts";
import type { MobileOrderItem } from "./mobileOrder";
import { deriveMobileOrder, moveMobileOrderItem, orderItemsForMobile } from "./mobileOrder";

const mobileItem = (
  instanceId: string,
  slotId: string | undefined,
  x: number,
  y: number,
  visibility: "shown" | "hidden" = "shown",
): MobileOrderItem => ({
  instanceId: asWidgetInstanceId(instanceId),
  ...(slotId === undefined ? {} : { slotId: asWidgetSlotId(slotId) }),
  visibility,
  layout: { instanceId: asWidgetInstanceId(instanceId), x, y, w: 6, h: 4 },
});

describe("mobile order", () => {
  it("keeps valid user order, filters hidden entries, and appends missing items deterministically", () => {
    const items = [
      mobileItem("capture", "capture", 0, 0),
      mobileItem("timeline", "timeline", 8, 0),
      mobileItem("notes", "notes", 0, 4, "hidden"),
      mobileItem("personal", undefined, 12, 8),
    ];
    const order = deriveMobileOrder(
      items,
      [asWidgetSlotId("capture"), asWidgetSlotId("timeline"), asWidgetSlotId("notes")],
      [asWidgetInstanceId("timeline"), asWidgetInstanceId("missing")],
    );

    expect(order).toEqual(["timeline", "capture", "personal"]);
    expect(orderItemsForMobile(items, order).map((entry) => entry.instanceId)).toEqual(order);
  });

  it("supports menu-based move before/after without drag", () => {
    const order = ["a", "b", "c"].map(asWidgetInstanceId);
    expect(moveMobileOrderItem(order, asWidgetInstanceId("b"), "before")).toEqual([
      "b",
      "a",
      "c",
    ]);
    expect(moveMobileOrderItem(order, asWidgetInstanceId("b"), "after")).toEqual([
      "a",
      "c",
      "b",
    ]);
  });
});

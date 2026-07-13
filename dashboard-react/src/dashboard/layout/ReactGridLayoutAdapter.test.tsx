import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { asWidgetInstanceId } from "../contributions/contracts";
import type { DashboardLayout } from "./contracts";
import {
  ReactGridLayoutAdapter,
  WORK_BUDDY_NO_COMPACTION,
  fromRglLayout,
  toRglLayout,
} from "./ReactGridLayoutAdapter";

const items: DashboardLayout = [
  {
    instanceId: asWidgetInstanceId("first"),
    x: 0,
    y: 6,
    w: 8,
    h: 4,
    minW: 6,
    maxW: 12,
    positionLocked: true,
  },
];

describe("ReactGridLayoutAdapter", () => {
  it("uses the no-compaction, no-overlap, collision-rejecting RGL compactor", () => {
    expect(WORK_BUDDY_NO_COMPACTION).toMatchObject({
      type: null,
      allowOverlap: false,
      preventCollision: true,
    });
  });

  it("round-trips only portable Work Buddy fields", () => {
    const rgl = toRglLayout(items);
    expect(rgl[0]).toMatchObject({
      i: "first",
      minW: 6,
      maxW: 12,
      isDraggable: false,
      isResizable: true,
    });

    const translated = fromRglLayout([{ ...rgl[0]!, x: 2, y: 9 }], items);
    expect(translated[0]).toEqual({ ...items[0], x: 2, y: 9 });
    expect(translated[0]).not.toHaveProperty("i");
    expect(translated[0]).not.toHaveProperty("moved");
  });

  it("mounts one controlled grid and adds a dedicated handle only in Customize mode", () => {
    const { rerender } = render(
      <ReactGridLayoutAdapter
        items={items}
        editMode
        onDraftChange={vi.fn()}
        renderItem={() => <div>Widget body</div>}
      />,
    );
    expect(screen.getByText("Widget body")).toBeInTheDocument();
    expect(document.querySelectorAll(".wb-widget-drag-handle")).toHaveLength(1);

    rerender(
      <ReactGridLayoutAdapter
        items={items}
        editMode={false}
        onDraftChange={vi.fn()}
        renderItem={() => <div>Widget body</div>}
      />,
    );
    expect(document.querySelectorAll(".wb-widget-drag-handle")).toHaveLength(0);
  });
});


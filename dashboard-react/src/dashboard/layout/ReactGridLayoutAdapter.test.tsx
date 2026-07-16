import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { asWidgetInstanceId } from "../contributions/contracts";
import type { DashboardLayout } from "./contracts";
import {
  ReactGridLayoutAdapter,
  WORK_BUDDY_NO_COMPACTION,
  WORK_BUDDY_RESIZE_HANDLES,
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

    expect(toRglLayout(items, false)[0]).toMatchObject({
      isDraggable: false,
      isResizable: false,
    });
  });

  it("mounts only after measurement and adds an aligned guide and handle in Customize mode", async () => {
    const onKeyboardCommand = vi.fn();
    const { rerender } = render(
      <ReactGridLayoutAdapter
        items={items}
        editMode
        onDraftChange={vi.fn()}
        onKeyboardCommand={onKeyboardCommand}
        renderItem={() => <div>Widget body</div>}
      />,
    );
    expect(await screen.findByText("Widget body")).toBeInTheDocument();
    expect(document.querySelector(".wb-dashboard-grid-container")).toHaveAttribute(
      "data-grid-measured",
      "true",
    );
    expect(document.querySelectorAll(".wb-dashboard-grid-guide")).toHaveLength(1);
    expect(document.querySelectorAll(".wb-widget-drag-handle")).toHaveLength(1);
    expect(document.querySelectorAll(".wb-widget-resize-handle")).toHaveLength(
      WORK_BUDDY_RESIZE_HANDLES.length,
    );
    expect(
      [...document.querySelectorAll(".wb-widget-resize-handle")].map((handle) =>
        handle.getAttribute("data-wb-resize-axis"),
      ),
    ).toEqual([...WORK_BUDDY_RESIZE_HANDLES]);

    const keyboardHandle = screen.getByRole("button", {
      name: /Move or resize widget/,
    });
    fireEvent.keyDown(keyboardHandle, { key: "ArrowRight" });
    fireEvent.keyDown(keyboardHandle, { key: "ArrowDown", shiftKey: true });
    expect(onKeyboardCommand).toHaveBeenNthCalledWith(1, {
      kind: "move",
      instanceId: "first",
      direction: "right",
    });
    expect(onKeyboardCommand).toHaveBeenNthCalledWith(2, {
      kind: "resize",
      instanceId: "first",
      direction: "grow-height",
    });

    rerender(
      <ReactGridLayoutAdapter
        items={items}
        editMode={false}
        onDraftChange={vi.fn()}
        renderItem={() => <div>Widget body</div>}
      />,
    );
    expect(document.querySelectorAll(".wb-dashboard-grid-guide")).toHaveLength(0);
    expect(document.querySelectorAll(".wb-widget-drag-handle")).toHaveLength(0);
    expect(document.querySelectorAll(".wb-widget-resize-handle")).toHaveLength(0);
  });

  it("commits the last valid preview when the window sees a release that RGL misses", async () => {
    const onInteractionEnd = vi.fn();
    const onInteractionCancel = vi.fn();
    render(
      <ReactGridLayoutAdapter
        items={items}
        editMode
        onDraftChange={vi.fn()}
        onInteractionEnd={onInteractionEnd}
        onInteractionCancel={onInteractionCancel}
        renderItem={() => <div>Widget body</div>}
      />,
    );
    await screen.findByText("Widget body");

    const handle = document.querySelector<HTMLElement>(".react-resizable-handle-se");
    expect(handle).not.toBeNull();
    fireEvent.mouseDown(handle!, { clientX: 100, clientY: 100, buttons: 1 });
    fireEvent.mouseMove(document, { clientX: 180, clientY: 180, buttons: 1 });
    expect(document.querySelectorAll(".react-grid-item.resizing")).toHaveLength(1);

    fireEvent.mouseUp(window, { clientX: 180, clientY: 180, buttons: 0 });

    await waitFor(() => {
      expect(document.querySelectorAll(".react-grid-item.resizing")).toHaveLength(0);
      expect(document.querySelectorAll(".react-grid-placeholder")).toHaveLength(0);
    });
    expect(onInteractionEnd).toHaveBeenCalledWith("resize", expect.any(Array), "first");
    expect(onInteractionCancel).not.toHaveBeenCalled();
  });

  it("cancels and remounts a resize when the window loses the pointer release", async () => {
    const onInteractionCancel = vi.fn();
    render(
      <ReactGridLayoutAdapter
        items={items}
        editMode
        onDraftChange={vi.fn()}
        onInteractionCancel={onInteractionCancel}
        renderItem={() => <div>Widget body</div>}
      />,
    );
    await screen.findByText("Widget body");

    const handle = document.querySelector<HTMLElement>(".react-resizable-handle-se");
    expect(handle).not.toBeNull();
    fireEvent.mouseDown(handle!, { clientX: 100, clientY: 100, buttons: 1 });
    fireEvent.mouseMove(document, { clientX: 180, clientY: 180, buttons: 1 });
    expect(document.querySelectorAll(".react-grid-item.resizing")).toHaveLength(1);
    expect(document.querySelectorAll(".react-grid-placeholder")).toHaveLength(1);

    fireEvent(window, new Event("blur"));

    await waitFor(() => {
      expect(document.querySelectorAll(".react-grid-item.resizing")).toHaveLength(0);
      expect(document.querySelectorAll(".react-grid-placeholder")).toHaveLength(0);
    });
    expect(onInteractionCancel).toHaveBeenCalledWith("resize", "first", "window-blurred");
  });

  it("hard-resets RGL interaction state when Customize mode ends", async () => {
    const onInteractionCancel = vi.fn();
    const { rerender } = render(
      <ReactGridLayoutAdapter
        items={items}
        editMode
        onDraftChange={vi.fn()}
        onInteractionCancel={onInteractionCancel}
        renderItem={() => <div>Widget body</div>}
      />,
    );
    await screen.findByText("Widget body");

    const handle = document.querySelector<HTMLElement>(".react-resizable-handle-se");
    expect(handle).not.toBeNull();
    fireEvent.mouseDown(handle!, { clientX: 100, clientY: 100, buttons: 1 });
    fireEvent.mouseMove(document, { clientX: 180, clientY: 180, buttons: 1 });
    expect(document.querySelectorAll(".react-grid-placeholder")).toHaveLength(1);

    rerender(
      <ReactGridLayoutAdapter
        items={items}
        editMode={false}
        onDraftChange={vi.fn()}
        onInteractionCancel={onInteractionCancel}
        renderItem={() => <div>Widget body</div>}
      />,
    );

    await waitFor(() => {
      expect(document.querySelectorAll(".react-grid-item.resizing")).toHaveLength(0);
      expect(document.querySelectorAll(".react-grid-placeholder")).toHaveLength(0);
    });
    expect(onInteractionCancel).toHaveBeenCalledWith("resize", "first", "edit-mode-ended");
  });
});

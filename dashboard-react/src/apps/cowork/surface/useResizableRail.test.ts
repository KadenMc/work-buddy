import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useResizableRail } from "./useResizableRail";

const STORAGE_KEY = "wb.cowork.rail-width";
const MIN_WIDTH = 256;
const MAX_WIDTH = 720;

const keyDown = (
  props: ReturnType<typeof useResizableRail>["separatorProps"],
  key: string,
): void => {
  act(() => {
    props.onKeyDown({
      key,
      preventDefault: () => {},
    } as unknown as React.KeyboardEvent<HTMLDivElement>);
  });
};

afterEach(() => {
  window.localStorage.clear();
});

describe("useResizableRail", () => {
  it("defaults to the prior fixed width when nothing is stored", () => {
    const { result } = renderHook(() => useResizableRail());
    expect(result.current.width).toBe(320);
    expect(result.current.separatorProps.role).toBe("separator");
    expect(result.current.separatorProps["aria-orientation"]).toBe("vertical");
    expect(result.current.separatorProps["aria-valuenow"]).toBe(320);
  });

  it("reads and clamps a stored width", () => {
    window.localStorage.setItem(STORAGE_KEY, "5000");
    const { result } = renderHook(() => useResizableRail());
    expect(result.current.width).toBe(MAX_WIDTH);
  });

  it("clamps a too-small stored width up to the minimum", () => {
    window.localStorage.setItem(STORAGE_KEY, "40");
    const { result } = renderHook(() => useResizableRail());
    expect(result.current.width).toBe(MIN_WIDTH);
  });

  it("grows on ArrowLeft, shrinks on ArrowRight, and persists", () => {
    const { result } = renderHook(() => useResizableRail());
    keyDown(result.current.separatorProps, "ArrowLeft");
    expect(result.current.width).toBe(344);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("344");
    keyDown(result.current.separatorProps, "ArrowRight");
    expect(result.current.width).toBe(320);
  });

  it("clamps at the bounds with End and Home", () => {
    const { result } = renderHook(() => useResizableRail());
    keyDown(result.current.separatorProps, "End");
    expect(result.current.width).toBe(MIN_WIDTH);
    keyDown(result.current.separatorProps, "Home");
    expect(result.current.width).toBe(MAX_WIDTH);
  });

  it("grows while dragging the separator toward the editor and persists on release", () => {
    const { result } = renderHook(() => useResizableRail());
    act(() => {
      result.current.separatorProps.onPointerDown({
        button: 0,
        clientX: 500,
        preventDefault: () => {},
      } as unknown as React.PointerEvent<HTMLDivElement>);
    });
    act(() => {
      window.dispatchEvent(new MouseEvent("pointermove", { clientX: 400 }));
    });
    // The rail is on the inline-end, so a smaller clientX widens it: 320 + 100.
    expect(result.current.width).toBe(420);
    act(() => {
      window.dispatchEvent(new MouseEvent("pointerup", {}));
    });
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("420");
  });
});

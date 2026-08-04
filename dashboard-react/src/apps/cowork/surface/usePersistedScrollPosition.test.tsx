import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  coworkScrollPositionStorageKey,
  loadCoworkScrollPosition,
  saveCoworkScrollPosition,
  usePersistedScrollPosition,
} from "./usePersistedScrollPosition";

class TrackingStorage implements Storage {
  readonly values = new Map<string, string>();
  readonly writes: Array<{ key: string; value: string }> = [];

  get length(): number {
    return this.values.size;
  }

  clear(): void {
    this.values.clear();
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.values.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
    this.writes.push({ key, value });
  }
}

function scrollBox({
  clientHeight = 100,
  initialScrollHeight = 100,
}: {
  readonly clientHeight?: number;
  readonly initialScrollHeight?: number;
} = {}) {
  const element = document.createElement("div");
  let scrollHeight = initialScrollHeight;
  let scrollTop = 0;

  Object.defineProperties(element, {
    clientHeight: { configurable: true, get: () => clientHeight },
    scrollHeight: { configurable: true, get: () => scrollHeight },
    scrollTop: {
      configurable: true,
      get: () => scrollTop,
      set: (next: number) => {
        scrollTop = Math.max(
          0,
          Math.min(Number(next), Math.max(0, scrollHeight - clientHeight)),
        );
        element.dispatchEvent(new Event("scroll"));
      },
    },
  });

  return {
    element,
    get top() {
      return scrollTop;
    },
    growTo(nextScrollHeight: number) {
      scrollHeight = nextScrollHeight;
      // Editor hydration and Review loading both replace descendants. This is the browser
      // signal the hook observes while the outer scroll container remains mounted.
      element.append(document.createElement("div"));
    },
    userScrollTo(nextTop: number) {
      element.dispatchEvent(new WheelEvent("wheel"));
      element.scrollTop = nextTop;
    },
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("Co-work scroll-position storage", () => {
  it("isolates live documents by store while retaining an explicit document-only fallback", () => {
    const first = coworkScrollPositionStorageKey(
      { storeId: "store/a", documentId: "shared doc" },
      "review",
    );
    const second = coworkScrollPositionStorageKey(
      { storeId: "store/b", documentId: "shared doc" },
      "review",
    );
    const fallback = coworkScrollPositionStorageKey(
      { documentId: "shared doc" },
      "review",
    );

    expect(first).not.toBe(second);
    expect(fallback).toContain("document-only");
    expect(first).not.toBe(fallback);
    expect(
      coworkScrollPositionStorageKey(
        { storeId: "store/a", documentId: "shared doc" },
        "editor",
      ),
    ).not.toBe(first);
  });

  it("round-trips a valid offset and ignores corrupt or unsafe records", () => {
    const storage = new TrackingStorage();
    saveCoworkScrollPosition(storage, "valid", 312.5);
    expect(loadCoworkScrollPosition(storage, "valid")).toBe(312.5);

    storage.setItem("negative", JSON.stringify({ version: 1, top: -1 }));
    storage.setItem("old", JSON.stringify({ version: 0, top: 20 }));
    storage.setItem("broken", "not-json");
    expect(loadCoworkScrollPosition(storage, "negative")).toBeNull();
    expect(loadCoworkScrollPosition(storage, "old")).toBeNull();
    expect(loadCoworkScrollPosition(storage, "broken")).toBeNull();
  });
});

describe("usePersistedScrollPosition", () => {
  it("waits for late content before restoring and never persists the loading-shell clamp", async () => {
    const storage = new TrackingStorage();
    const box = scrollBox();
    saveCoworkScrollPosition(storage, "editor", 640);
    const writesBeforeMount = storage.writes.length;
    const { result } = renderHook(() =>
      usePersistedScrollPosition({
        key: "editor",
        storage,
        restoreSettleMs: 1,
        restoreDeadlineMs: 1_000,
        writeDelayMs: 1,
      }),
    );

    act(() => result.current(box.element));
    expect(box.top).toBe(0);
    expect(loadCoworkScrollPosition(storage, "editor")).toBe(640);

    act(() => box.growTo(1_000));
    await waitFor(() => expect(box.top).toBe(640));
    await new Promise((resolve) => window.setTimeout(resolve, 10));

    expect(storage.writes).toHaveLength(writesBeforeMount);
    expect(loadCoworkScrollPosition(storage, "editor")).toBe(640);
  });

  it("abandons a pending late restore after the user actually scrolls", async () => {
    const storage = new TrackingStorage();
    const box = scrollBox({ initialScrollHeight: 300 });
    saveCoworkScrollPosition(storage, "review", 640);
    const { result } = renderHook(() =>
      usePersistedScrollPosition({
        key: "review",
        storage,
        restoreSettleMs: 1,
        restoreDeadlineMs: 1_000,
        writeDelayMs: 1,
      }),
    );

    act(() => result.current(box.element));
    expect(box.top).toBe(200);
    act(() => box.userScrollTo(30));
    await waitFor(() =>
      expect(loadCoworkScrollPosition(storage, "review")).toBe(30),
    );

    act(() => box.growTo(1_000));
    await new Promise((resolve) => window.setTimeout(resolve, 10));
    expect(box.top).toBe(30);
  });

  it("abandons a pending restore when scroll intent cannot move the loading shell", async () => {
    const storage = new TrackingStorage();
    const box = scrollBox();
    saveCoworkScrollPosition(storage, "review", 640);
    const { result } = renderHook(() =>
      usePersistedScrollPosition({
        key: "review",
        storage,
        restoreSettleMs: 1,
        restoreDeadlineMs: 1_000,
        writeDelayMs: 1,
      }),
    );

    act(() => result.current(box.element));
    act(() => box.element.dispatchEvent(new WheelEvent("wheel")));
    await waitFor(() =>
      expect(loadCoworkScrollPosition(storage, "review")).toBe(0),
    );

    act(() => box.growTo(1_000));
    await new Promise((resolve) => window.setTimeout(resolve, 10));
    expect(box.top).toBe(0);
  });

  it("does not mistake clicks or descendant keyboard input for scrolling", async () => {
    const storage = new TrackingStorage();
    const box = scrollBox({ initialScrollHeight: 300 });
    const control = document.createElement("button");
    box.element.append(control);
    saveCoworkScrollPosition(storage, "review", 640);
    const writesBeforeMount = storage.writes.length;
    const { result } = renderHook(() =>
      usePersistedScrollPosition({
        key: "review",
        storage,
        restoreSettleMs: 1,
        restoreDeadlineMs: 1_000,
        writeDelayMs: 1,
      }),
    );

    act(() => result.current(box.element));
    expect(box.top).toBe(200);
    act(() => {
      control.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
      control.dispatchEvent(
        new KeyboardEvent("keydown", { bubbles: true, key: " " }),
      );
    });

    act(() => box.growTo(1_000));
    await waitFor(() => expect(box.top).toBe(640));
    await new Promise((resolve) => window.setTimeout(resolve, 10));
    expect(storage.writes).toHaveLength(writesBeforeMount);
    expect(loadCoworkScrollPosition(storage, "review")).toBe(640);
  });

  it("treats an external programmatic jump as the new position", async () => {
    const storage = new TrackingStorage();
    const box = scrollBox({ initialScrollHeight: 300 });
    saveCoworkScrollPosition(storage, "editor", 640);
    const { result } = renderHook(() =>
      usePersistedScrollPosition({
        key: "editor",
        storage,
        restoreSettleMs: 1,
        restoreDeadlineMs: 1_000,
        writeDelayMs: 1,
      }),
    );

    act(() => result.current(box.element));
    expect(box.top).toBe(200);
    // Passage navigation calls scrollIntoView inside the editor rather than emitting wheel or
    // pointer intent on this outer container. Its resulting scroll event must still win.
    act(() => {
      box.element.scrollTop = 75;
    });
    await waitFor(() =>
      expect(loadCoworkScrollPosition(storage, "editor")).toBe(75),
    );

    act(() => box.growTo(1_000));
    await new Promise((resolve) => window.setTimeout(resolve, 10));
    expect(box.top).toBe(75);
  });

  it("throttles ordinary writes and flushes the latest position on unmount", () => {
    vi.useFakeTimers();
    const storage = new TrackingStorage();
    const box = scrollBox({ initialScrollHeight: 1_000 });
    const { result, unmount } = renderHook(() =>
      usePersistedScrollPosition({
        key: "review",
        storage,
        writeDelayMs: 100,
      }),
    );
    act(() => result.current(box.element));

    act(() => {
      box.element.scrollTop = 10;
      box.element.scrollTop = 20;
      box.element.scrollTop = 30;
      vi.advanceTimersByTime(99);
    });
    expect(storage.writes).toHaveLength(0);

    unmount();
    expect(storage.writes).toHaveLength(1);
    expect(loadCoworkScrollPosition(storage, "review")).toBe(30);
  });

  it("settles to and persists a safe clamp when saved geometry never returns", async () => {
    const storage = new TrackingStorage();
    const box = scrollBox({ initialScrollHeight: 300 });
    saveCoworkScrollPosition(storage, "review", 640);
    const { result } = renderHook(() =>
      usePersistedScrollPosition({
        key: "review",
        storage,
        restoreSettleMs: 1,
        restoreDeadlineMs: 5,
        writeDelayMs: 1,
      }),
    );

    act(() => result.current(box.element));
    await waitFor(() =>
      expect(loadCoworkScrollPosition(storage, "review")).toBe(200),
    );
    expect(box.top).toBe(200);
  });
});

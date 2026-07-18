import { describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";

import { useAlignedStream } from "../rail/useAlignedStream";
import type { AdapterEvents, WbTrackedChangesAdapter } from "../suggestions/types";
import { DomAnchorRectSource } from "./DomAnchorRectSource";

const rect = (top: number, bottom: number): DOMRect =>
  ({
    top,
    bottom,
    height: bottom - top,
    left: 0,
    right: 0,
    width: 0,
    x: 0,
    y: top,
    toJSON: () => ({}),
  }) as DOMRect;

const markElement = (id: string, top: number, bottom: number): HTMLElement => {
  const element = document.createElement("ins");
  element.setAttribute("data-id", JSON.stringify(id));
  element.setAttribute("data-wb-suggestion", "insertion");
  element.getBoundingClientRect = () => rect(top, bottom);
  return element;
};

const railRoot = (top: number, scrollTop = 0): HTMLElement => {
  const element = document.createElement("ul");
  element.getBoundingClientRect = () => rect(top, top + 400);
  Object.defineProperty(element, "scrollTop", { value: scrollTop, configurable: true });
  return element;
};

/** A minimal adapter double exposing only the geometry-change event surface. */
const fakeAdapter = () => {
  const listeners = new Map<keyof AdapterEvents, Set<() => void>>();
  const on = (ev: keyof AdapterEvents, cb: () => void) => {
    const set = listeners.get(ev) ?? new Set();
    set.add(cb);
    listeners.set(ev, set);
    return () => set.delete(cb);
  };
  const fire = (ev: keyof AdapterEvents) => {
    for (const cb of listeners.get(ev) ?? []) cb();
  };
  return { adapter: { on } as unknown as WbTrackedChangesAdapter, fire };
};

describe("DomAnchorRectSource", () => {
  it("activates: reports a mark rect in the rail coordinate space", () => {
    const editorRoot = document.createElement("div");
    editorRoot.append(markElement("s1", 120, 140));
    const rail = railRoot(80, 10);

    const source = new DomAnchorRectSource({
      getEditorRoot: () => editorRoot,
      getRailRoot: () => rail,
    });

    // top = markTop(120) - railTop(80) + scrollTop(10) = 50, height = 20.
    expect(source.anchorRect("s1")).toEqual({ top: 50, height: 20 });
  });

  it("unions the rects of a multi-node mark", () => {
    const editorRoot = document.createElement("div");
    editorRoot.append(markElement("s1", 100, 120), markElement("s1", 130, 160));
    const rail = railRoot(0);

    const source = new DomAnchorRectSource({
      getEditorRoot: () => editorRoot,
      getRailRoot: () => rail,
    });

    // top = min(100,130) - 0 = 100, height = max(120,160) - min(100,130) = 60.
    expect(source.anchorRect("s1")).toEqual({ top: 100, height: 60 });
  });

  it("degrades to null for a proposal with no rendered mark (a flag)", () => {
    const editorRoot = document.createElement("div");
    editorRoot.append(markElement("s1", 100, 120));
    const source = new DomAnchorRectSource({
      getEditorRoot: () => editorRoot,
      getRailRoot: () => railRoot(0),
    });
    expect(source.anchorRect("f1")).toBeNull();
  });

  it("degrades to null when the editor is not mounted", () => {
    const source = new DomAnchorRectSource({
      getEditorRoot: () => null,
      getRailRoot: () => railRoot(0),
    });
    expect(source.anchorRect("s1")).toBeNull();
  });

  it("degrades to null when the rail coordinate root is absent", () => {
    const editorRoot = document.createElement("div");
    editorRoot.append(markElement("s1", 100, 120));
    const source = new DomAnchorRectSource({
      getEditorRoot: () => editorRoot,
      getRailRoot: () => null,
    });
    expect(source.anchorRect("s1")).toBeNull();
  });

  it("scrolls a mark into view and flashes it on the degrade path", () => {
    const editorRoot = document.createElement("div");
    const mark = markElement("s1", 100, 120);
    const scrollIntoView = vi.fn();
    mark.scrollIntoView = scrollIntoView;
    editorRoot.append(mark);

    const source = new DomAnchorRectSource({
      getEditorRoot: () => editorRoot,
      getRailRoot: () => railRoot(0),
    });
    source.scrollToAnchor("s1");

    expect(scrollIntoView).toHaveBeenCalledOnce();
    expect(mark.classList.contains("wb-cowork-suggestion--flash")).toBe(true);
  });

  it("fires a geometry change on a decoration rebuild and on resize", () => {
    const { adapter, fire } = fakeAdapter();
    const source = new DomAnchorRectSource({
      getEditorRoot: () => document.createElement("div"),
      getRailRoot: () => railRoot(0),
      adapter,
    });

    const onChange = vi.fn();
    const unsubscribe = source.subscribe(onChange);

    fire("proposals:changed");
    expect(onChange).toHaveBeenCalledTimes(1);

    window.dispatchEvent(new Event("resize"));
    expect(onChange).toHaveBeenCalledTimes(2);

    unsubscribe();
    window.dispatchEvent(new Event("resize"));
    expect(onChange).toHaveBeenCalledTimes(2);
  });
});

describe("useAlignedStream activation with the source", () => {
  it("aligns when the source is wired and degrades to normal flow without it", () => {
    const source = new DomAnchorRectSource({
      getEditorRoot: () => null,
      getRailRoot: () => null,
    });
    const withSource = renderHook(() =>
      useAlignedStream({ anchorRects: source, ids: ["s1"] }),
    );
    expect(withSource.result.current.aligned).toBe(true);

    const withoutSource = renderHook(() =>
      useAlignedStream({ anchorRects: undefined, ids: ["s1"] }),
    );
    expect(withoutSource.result.current.aligned).toBe(false);
  });
});

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useState } from "react";
import type { GroupProps } from "react-resizable-panels";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DashboardHelpProvider } from "../help";
import {
  WorkspaceSidePanel,
  type WorkspaceSidePanelMode,
  type WorkspaceSidePanelProps,
} from "./WorkspaceSidePanel";

const { groupProps, layoutOptions } = vi.hoisted(() => ({
  groupProps: vi.fn(),
  layoutOptions: vi.fn(),
}));

// Observe our wiring, but render the installed library. In particular, help
// exercises Separator's real ref/focus behavior and keyboard resize listener.
vi.mock("react-resizable-panels", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-resizable-panels")>();
  return {
    ...actual,
    Group: (props: GroupProps) => {
      groupProps(props);
      return <actual.Group {...props} />;
    },
    useDefaultLayout: (options: Parameters<typeof actual.useDefaultLayout>[0]) => {
      layoutOptions(options);
      return actual.useDefaultLayout(options);
    },
  };
});

const STORAGE_KEY = "react-resizable-panels:test.workspace";
const lastGroup = (): GroupProps => groupProps.mock.lastCall![0] as GroupProps;
const panel = (props: Partial<WorkspaceSidePanelProps> = {}) => (
  <WorkspaceSidePanel
    layoutId="test.workspace"
    mode="split"
    resizeLabel="Resize workspace side panel"
    primary={<input aria-label="Form title" defaultValue="Private title" />}
    side={<textarea aria-label="Chat draft" defaultValue="Unsent message" />}
    {...props}
  />
);

/** Model real container measurements and deliver the installed library's observer. */
function resizableContainer(initialWidth = 1200) {
  let width = initialWidth;
  const observers = new Set<GeometryObserver>();
  class GeometryObserver implements ResizeObserver {
    readonly targets = new Set<Element>();
    readonly callback: ResizeObserverCallback;
    constructor(callback: ResizeObserverCallback) {
      this.callback = callback;
      observers.add(this);
    }
    observe(target: Element) { this.targets.add(target); }
    unobserve(target: Element) { this.targets.delete(target); }
    disconnect() { observers.delete(this); }
  }
  vi.stubGlobal("ResizeObserver", GeometryObserver);
  document.documentElement.style.fontSize = "16px";

  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockImplementation(function (this: HTMLElement) {
    if (this.hidden) return 0;
    if (this.hasAttribute("data-group")) return width;
    if (this.hasAttribute("data-separator")) return 11;
    if (this.dataset.workspacePane) {
      if (this.parentElement?.dataset.workspacePanelMode !== "split") return width;
      const percent = Number.parseFloat(this.style.flexGrow || this.style.flexBasis);
      return (width - 11) * percent / 100;
    }
    return 0;
  });
  vi.spyOn(HTMLElement.prototype, "offsetLeft", "get").mockImplementation(function (this: HTMLElement) {
    if (this.parentElement?.dataset.workspacePanelMode !== "split") return 0;
    const primary = this.parentElement.querySelector<HTMLElement>("[data-workspace-pane='primary']");
    if (this.dataset.workspacePane === "side") return (primary?.offsetWidth ?? 0) + 11;
    return this.hasAttribute("data-separator") ? primary?.offsetWidth ?? 0 : 0;
  });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    return new DOMRect(this.offsetLeft, 0, this.offsetWidth, 600);
  });

  return {
    resize(nextWidth: number) {
      width = nextWidth;
      act(() => {
        for (const observer of [...observers]) {
          const entries = [...observer.targets].map((target): ResizeObserverEntry => {
            const inlineSize = (target as HTMLElement).offsetWidth;
            const size = { inlineSize, blockSize: 600 };
            return {
              target,
              borderBoxSize: [size],
              contentBoxSize: [size],
              devicePixelContentBoxSize: [size],
              contentRect: new DOMRect(0, 0, inlineSize, 600),
            };
          });
          if (entries.length) observer.callback(entries, observer);
        }
      });
    },
  };
}

beforeEach(() => {
  window.localStorage.clear();
  groupProps.mockClear();
  layoutOptions.mockClear();

  // jsdom has no layout. Supply a 1000px content area plus the 11px divider
  // so the real library can derive constraints and handle resize events.
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockImplementation(function (this: HTMLElement) {
    if (this.dataset.workspacePane === "primary") return 670;
    if (this.dataset.workspacePane === "side") return 330;
    if (this.hasAttribute("data-separator")) return 11;
    return this.hasAttribute("data-group") ? 1011 : 0;
  });
  vi.spyOn(HTMLElement.prototype, "offsetLeft", "get").mockImplementation(function (this: HTMLElement) {
    if (this.dataset.workspacePane === "side") return 681;
    return this.hasAttribute("data-separator") ? 670 : 0;
  });
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(600);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.dataset.workspacePane === "primary") return new DOMRect(0, 0, 670, 600);
    if (this.dataset.workspacePane === "side") return new DOMRect(681, 0, 330, 600);
    if (this.hasAttribute("data-separator")) return new DOMRect(670, 0, 11, 600);
    return new DOMRect(0, 0, 1011, 600);
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.documentElement.style.removeProperty("font-size");
  window.localStorage.clear();
});

describe("WorkspaceSidePanel", () => {
  it("restores the existing Co-work storage shape without changing its key or panel ids", () => {
    const key = "react-resizable-panels:wb.cowork.workspace-layout";
    window.localStorage.setItem(key, JSON.stringify({ editor: 61, rail: 39 }));
    render(panel({ layoutId: "wb.cowork.workspace-layout", primaryId: "editor", sideId: "rail" }));

    expect(lastGroup().defaultLayout).toEqual({ editor: 61, rail: 39 });
    expect(layoutOptions).toHaveBeenLastCalledWith({
      id: "wb.cowork.workspace-layout",
      storage: expect.objectContaining({ getItem: expect.any(Function), setItem: expect.any(Function) }),
      onlySaveAfterUserInteractions: true,
    });
    expect(screen.getByRole("separator")).toHaveAttribute("aria-controls", "editor");
    expect(screen.getByTestId("editor")).toHaveAttribute("data-workspace-pane", "primary");
    expect(screen.getByTestId("rail")).toHaveAttribute("data-workspace-pane", "side");
    expect(window.localStorage.getItem(key)).toBe(JSON.stringify({ editor: 61, rail: 39 }));
  });

  it("can still restore the library's legacy layout format", () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ "primary,side": { layout: [58, 42] } }));
    render(panel());
    expect(lastGroup().defaultLayout).toEqual({ primary: 58, side: 42 });
  });

  it("persists only settled user changes in split mode", () => {
    const { rerender } = render(panel());
    act(() => lastGroup().onLayoutChanged?.({ primary: 55, side: 45 }, { isUserInteraction: false }));
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    act(() => lastGroup().onLayoutChanged?.({ primary: 55, side: 45 }, { isUserInteraction: true }));
    const chosen = JSON.stringify({ primary: 55, side: 45 });
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe(chosen);

    for (const mode of ["primary-only", "side-only", "stacked"] as const) {
      rerender(panel({ mode }));
      expect(lastGroup().disabled).toBe(true);
      act(() => lastGroup().onLayoutChanged?.({ primary: 100, side: 0 }, { isUserInteraction: true }));
      expect(window.localStorage.getItem(STORAGE_KEY)).toBe(chosen);
    }
    rerender(panel());
    expect(lastGroup().defaultLayout).toEqual({ primary: 55, side: 45 });
    expect(lastGroup().disabled).toBe(false);
  });

  it("keeps both content trees and their local edits mounted through every mode", async () => {
    const mounted = vi.fn();
    const unmounted = vi.fn();
    function StatefulPane({ name }: { readonly name: string }) {
      const [value, setValue] = useState("");
      useEffect(() => { mounted(name); return () => { unmounted(name); }; }, [name]);
      return <input aria-label={name} value={value} onChange={(event) => setValue(event.target.value)} />;
    }
    const primary = <StatefulPane name="Form" />;
    const side = <StatefulPane name="Chat" />;
    const user = userEvent.setup();
    const { container, rerender } = render(panel({ primary, side }));
    const group = container.querySelector("[data-group]");
    const form = screen.getByRole("textbox", { name: "Form" });
    const chat = screen.getByRole("textbox", { name: "Chat" });
    const primaryPanel = screen.getByTestId("primary");
    const sidePanel = screen.getByTestId("side");
    await user.type(form, "Kept form edit");
    await user.type(chat, "Kept chat draft");

    for (const mode of ["primary-only", "side-only", "stacked", "split"] as const) {
      rerender(panel({ primary, side, mode }));
      expect(container.querySelector("[data-group]")).toBe(group);
      expect(screen.getByTestId("primary")).toBe(primaryPanel);
      expect(screen.getByTestId("side")).toBe(sidePanel);
      expect(primaryPanel.querySelector("input")).toBe(form);
      expect(sidePanel.querySelector("input")).toBe(chat);
      expect(form).toHaveValue("Kept form edit");
      expect(chat).toHaveValue("Kept chat draft");
      expect(primaryPanel.hidden).toBe(mode === "side-only");
      expect(sidePanel.hidden).toBe(mode === "primary-only");
      expect(primaryPanel.hasAttribute("inert")).toBe(mode === "side-only");
      expect(sidePanel.hasAttribute("inert")).toBe(mode === "primary-only");
      expect(screen.queryByRole("separator") !== null).toBe(mode === "split");
    }
    expect(mounted.mock.calls).toEqual([["Form"], ["Chat"]]);
    expect(unmounted).not.toHaveBeenCalled();
  });

  it("retains a real resize through closed/compact modes and remount, with double-click reset", () => {
    const { rerender, unmount } = render(panel());
    fireEvent.keyDown(screen.getByRole("separator"), { key: "ArrowLeft" });
    expect(screen.getByRole("separator")).toHaveAttribute("aria-valuenow", "62");
    const chosen = JSON.stringify({ primary: 62, side: 38 });

    for (const mode of ["primary-only", "side-only", "stacked"] as const) {
      rerender(panel({ mode }));
      fireEvent.keyDown(screen.getByRole("separator", { hidden: true }), { key: "ArrowRight" });
      expect(window.localStorage.getItem(STORAGE_KEY)).toBe(chosen);
      rerender(panel());
      expect(screen.getByRole("separator")).toHaveAttribute("aria-valuenow", "62");
    }
    unmount();
    const remounted = render(panel());
    const separator = screen.getByRole("separator");
    expect(separator).toHaveAttribute("aria-valuenow", "62");
    fireEvent.doubleClick(separator, { clientX: 675, clientY: 100 });
    expect(separator).toHaveAttribute("aria-valuenow", "67");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe(JSON.stringify({ primary: 67, side: 33 }));
    remounted.rerender(panel({ mode: "side-only" }));
    remounted.rerender(panel());
    expect(screen.getByRole("separator")).toHaveAttribute("aria-valuenow", "67");
  });

  it.each([
    ["primary-only", "measure-first"],
    ["primary-only", "mode-first"],
    ["side-only", "measure-first"],
    ["side-only", "mode-first"],
  ] as const)("restores the intended split after a narrow %s round-trip (%s)", async (mode, order) => {
    const geometry = resizableContainer();
    const props = { sideMinSize: "18rem" };
    const { rerender } = render(panel(props));
    const primary = screen.getByTestId("primary");
    const side = screen.getByTestId("side");
    const group = primary.parentElement;
    const form = screen.getByRole("textbox", { name: "Form title" });
    const chat = screen.getByRole("textbox", { name: "Chat draft" });
    fireEvent.change(form, { target: { value: "Keep my form edit" } });
    fireEvent.change(chat, { target: { value: "Keep my unsent message" } });
    fireEvent.keyDown(screen.getByRole("separator"), { key: "ArrowLeft" });
    fireEvent.keyDown(screen.getByRole("separator"), { key: "ArrowLeft" });
    expect(screen.getByRole("separator")).toHaveAttribute("aria-valuenow", "57");
    const chosen = JSON.stringify({ primary: 57, side: 43 });
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe(chosen);
    const chosenWidth = side.offsetWidth;

    // A rem minimum clamps the library's percentage layout as the container
    // shrinks, even when the Group is disabled and persistence is suppressed.
    geometry.resize(343);
    rerender(panel({ ...props, mode }));
    geometry.resize(343);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe(chosen);
    if (order === "measure-first") {
      geometry.resize(1200);
      rerender(panel(props));
    } else {
      rerender(panel(props));
      geometry.resize(1200);
    }

    await waitFor(() => expect(screen.getByRole("separator")).toHaveAttribute("aria-valuenow", "57"));
    expect(side.offsetWidth).toBeCloseTo(chosenWidth);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe(chosen);
    expect(primary.parentElement).toBe(group);
    expect(screen.getByTestId("primary")).toBe(primary);
    expect(screen.getByTestId("side")).toBe(side);
    expect(screen.getByRole("textbox", { name: "Form title" })).toBe(form);
    expect(screen.getByRole("textbox", { name: "Chat draft" })).toBe(chat);
    expect(form).toHaveValue("Keep my form edit");
    expect(chat).toHaveValue("Keep my unsent message");
  });

  it.each([undefined, { primary: 61, side: 39 }])(
    "restores the declared or saved split when first mounted compact: %j",
    async (saved) => {
      const geometry = resizableContainer(343);
      if (saved) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(saved));
      const props = { sideMinSize: "18rem" };
      const { rerender } = render(panel({ ...props, mode: "side-only" }));
      geometry.resize(1200);
      rerender(panel(props));

      await waitFor(() => expect(screen.getByRole("separator"))
        .toHaveAttribute("aria-valuenow", String(saved?.primary ?? 67)));
      expect(window.localStorage.getItem(STORAGE_KEY)).toBe(saved ? JSON.stringify(saved) : null);
    },
  );

  it("does not restore over an active pointer drag when panel pixels change", async () => {
    const geometry = resizableContainer();
    render(panel({ sideMinSize: "18rem" }));
    geometry.resize(1200);
    await act(() => new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve())));
    const separator = screen.getByRole("separator");
    const startX = separator.getBoundingClientRect().x + 5;
    fireEvent.pointerDown(separator, { pointerType: "mouse", pointerId: 1, button: 0, buttons: 1, clientX: startX, clientY: 100 });
    fireEvent.pointerMove(document, { pointerType: "mouse", pointerId: 1, buttons: 1, clientX: startX + 60, clientY: 100 });
    const draggingSize = separator.getAttribute("aria-valuenow");
    expect(Number(draggingSize)).toBeGreaterThan(70);
    // The real observer sees panels resize on each drag frame even though the
    // containing workspace has not changed width. That is not a clamp to undo.
    geometry.resize(1200);
    await act(() => new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve())));
    expect(separator).toHaveAttribute("aria-valuenow", draggingSize);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    fireEvent.pointerUp(document, { pointerType: "mouse", pointerId: 1, button: 0, clientX: startX + 60, clientY: 100 });
    const saved = JSON.parse(window.localStorage.getItem(STORAGE_KEY)!);
    expect(saved.primary).toBeCloseTo(Number(draggingSize), 3);
    expect(saved.side).toBeCloseTo(100 - Number(draggingSize), 3);
  });

  it.each(["not json", "null", "[]", '{"primary":-1,"side":101}', '{"other":50,"side":50}', '{"primary":40,"side":40}'])(
    "falls back safely from an invalid preference: %s",
    (stored) => {
      window.localStorage.setItem(STORAGE_KEY, stored);
      render(panel());
      expect(lastGroup().defaultLayout).toBeUndefined();
      expect(screen.getByRole("separator")).toHaveAttribute("aria-valuenow", "67");
    },
  );

  it("works when the localStorage getter is unavailable", () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(() => { throw new Error("Storage denied"); });
    expect(() => render(panel())).not.toThrow();
    expect(() => act(() => lastGroup().onLayoutChanged?.({ primary: 50, side: 50 }, { isUserInteraction: true }))).not.toThrow();
    expect(screen.getByRole("textbox", { name: "Form title" })).toHaveValue("Private title");
  });

  it("works when storage reads and writes fail", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("Read denied"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Quota exceeded"); });
    expect(() => render(panel())).not.toThrow();
    expect(() => act(() => lastGroup().onLayoutChanged?.({ primary: 50, side: 50 }, { isUserInteraction: true }))).not.toThrow();
    expect(screen.getByRole("separator")).toHaveAttribute("aria-valuenow", "67");
  });

  it("keeps help on the actual accessible separator without replacing keyboard resizing", async () => {
    const user = userEvent.setup();
    render(<DashboardHelpProvider enabled>{panel()}</DashboardHelpProvider>);
    const separator = screen.getByRole("separator", { name: "Resize workspace side panel" });
    expect(separator.parentElement).toHaveAttribute("data-group");
    expect(separator).toHaveAttribute("data-help-target", "true");
    expect(separator).toHaveAttribute("aria-orientation", "vertical");
    expect(separator).toHaveAttribute("aria-valuemin", "30");
    expect(separator).toHaveAttribute("aria-valuemax", "85");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    await user.tab();
    await user.tab();
    expect(separator).toHaveFocus();
    const tooltip = await screen.findByRole("tooltip");
    expect(separator).toHaveAttribute("aria-describedby", tooltip.id);
    expect(tooltip).toHaveTextContent("Left and Right arrow keys");
    // The library's bubble focus handler still runs alongside contextual help.
    expect(separator).toHaveAttribute("data-separator", "focus");
    await user.keyboard("{ArrowRight}");
    expect(separator).toHaveAttribute("aria-valuenow", "72");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe(JSON.stringify({ primary: 72, side: 28 }));
    await user.keyboard("{Home}");
    expect(separator).toHaveAttribute("aria-valuenow", "30");
    await user.keyboard("{End}");
    expect(separator).toHaveAttribute("aria-valuenow", "85");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument());
    expect(separator).toHaveFocus();
  });

  it("shows custom help on hover and still accepts pointer resizing", async () => {
    const user = userEvent.setup();
    render(
      <DashboardHelpProvider enabled>
        {panel({ resizeHelp: { summary: "Size this workspace.", details: "Adjust the room shared by these panes." } })}
      </DashboardHelpProvider>,
    );
    const separator = screen.getByRole("separator");
    // Establish pointer modality after the preceding keyboard-help scenario.
    await user.click(screen.getByRole("textbox", { name: "Form title" }));
    await user.hover(separator);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Adjust the room shared by these panes.");
    fireEvent.pointerDown(separator, { pointerType: "mouse", pointerId: 1, button: 0, buttons: 1, clientX: 675, clientY: 100 });
    fireEvent.pointerMove(document, { pointerType: "mouse", pointerId: 1, buttons: 1, clientX: 725, clientY: 100 });
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    fireEvent.pointerUp(document, { pointerType: "mouse", pointerId: 1, button: 0, clientX: 725, clientY: 100 });
    expect(separator).toHaveAttribute("aria-valuenow", "72");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe(JSON.stringify({ primary: 72, side: 28 }));
  });

  it.each(["primary-only", "side-only", "stacked"] as WorkspaceSidePanelMode[])(
    "does not expose help or an interactive divider in %s mode",
    (mode) => {
      render(<DashboardHelpProvider enabled>{panel({ mode })}</DashboardHelpProvider>);
      const separator = screen.getByRole("separator", { hidden: true });
      expect(separator).toHaveAttribute("hidden");
      expect(separator).toHaveAttribute("aria-disabled", "true");
      expect(separator).not.toHaveAttribute("tabindex");
      expect(separator).not.toHaveAttribute("data-help-target");
      expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    },
  );
});

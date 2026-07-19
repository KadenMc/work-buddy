import { useDefaultLayout, type LayoutStorage } from "react-resizable-panels";

/**
 * Percentage sizes for the Co-work two-pane split. Every size is a fraction of the workspace
 * body, so the split keeps its proportion as the viewport changes instead of pinning a fixed
 * pixel width. The rail opens wide enough to read a review card, shrinks to a narrow strip,
 * and can take the majority of the width, while the editor stays above a legible minimum.
 *
 * Sizes are fractions, not pixels, on purpose: a percentage rail widens with the window and
 * survives a resize, and the generous min/max give the handle real travel in both directions.
 */
export const EDITOR_MIN_SIZE = "30%";
export const EDITOR_DEFAULT_SIZE = "67%";
export const RAIL_DEFAULT_SIZE = "33%";
export const RAIL_MIN_SIZE = "15%";
export const RAIL_MAX_SIZE = "70%";

/** Stable id for the persisted layout. The stored map keys each Panel id to its percentage. */
export const LAYOUT_STORAGE_ID = "wb.cowork.workspace-layout";

/** Panel ids the persisted layout keys on. They double as the Panel `id` and `data-panel`. */
export const EDITOR_PANEL_ID = "editor";
export const RAIL_PANEL_ID = "rail";

/**
 * `window.localStorage` satisfies `LayoutStorage` (it exposes `getItem`/`setItem`). Returns
 * `undefined` when there is no window (server render or a non-DOM test), so persistence
 * degrades to the default layout rather than throwing on a missing global.
 */
export const resolveLayoutStorage = (): LayoutStorage | undefined =>
  typeof window === "undefined" ? undefined : window.localStorage;

export interface CoworkPanelLayout {
  /** Pass to the panel `Group` as `defaultLayout`. Restores the last persisted split. */
  readonly defaultLayout: ReturnType<typeof useDefaultLayout>["defaultLayout"];
  /** Pass to the panel `Group` as `onLayoutChanged`. Persists a settled split. */
  readonly onLayoutChanged: ReturnType<typeof useDefaultLayout>["onLayoutChanged"];
}

/**
 * Wires localStorage persistence for the Co-work split and returns the two props the panel
 * `Group` needs to restore and re-save it. react-resizable-panels owns the drag, the keyboard
 * separator, and the measurement. This hook only holds the size policy and the persistence
 * seam in one place. `onlySaveAfterUserInteractions` keeps window-resize reflows and imperative
 * resizes from overwriting a width the user chose on purpose.
 */
export function useResizableRail(): CoworkPanelLayout {
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({
    id: LAYOUT_STORAGE_ID,
    storage: resolveLayoutStorage(),
    onlySaveAfterUserInteractions: true,
  });
  return { defaultLayout, onLayoutChanged };
}

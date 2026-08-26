/**
 * Co-work's size policy and persisted identifiers; WorkspaceSidePanel owns
 * resizing and storage. Every size is a fraction of the workspace
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

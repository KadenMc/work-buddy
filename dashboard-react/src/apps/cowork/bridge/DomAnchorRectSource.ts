/**
 * The editor-backed AnchorRectSource that completes the aligned margin stream. This seam was
 * left dormant until proposals actually ingest into the editor, because no real geometry
 * exists before then. The editor is the only owner of live anchor geometry, so this measures
 * the suggestion-mark decoration DOM
 * rects: each mark renders with `data-id` set to the JSON-encoded proposal id (marks.ts), so
 * a proposal's on-screen span is `[data-id]` inside the editor root. It reports each anchor's
 * top and height in the rail card-list coordinate space, which is exactly what
 * useAlignedStream feeds computeAlignedLayout.
 *
 * Degrade path (section on AnchorRectSource): a proposal with no rendered mark (a flag mints
 * no mark, or an anchor is off-screen or lost, or the editor is not mounted) reports null, so
 * useAlignedStream leaves that card in normal flow and the rail falls back to
 * scroll-to-and-highlight. scrollToAnchor drives that fallback by bringing the mark into view
 * and flashing it.
 */

import type { AnchorRectSource, ReviewUnsubscribe } from "../rail/provider";
import type { WbTrackedChangesAdapter } from "../suggestions/types";

/** How long the scroll-to flash class stays on a mark before it is removed. */
const FLASH_MS = 1200;
const FLASH_CLASS = "wb-cowork-suggestion--flash";

export interface DomAnchorRectSourceOptions {
  /** The editor's ProseMirror DOM root (editor.view.dom). Null until the editor mounts. */
  readonly getEditorRoot: () => HTMLElement | null;
  /**
   * The rail card-list element the aligned cards are positioned within (position: relative).
   * Card tops are reported relative to this element, matching useAlignedStream's transform.
   */
  readonly getRailRoot: () => HTMLElement | null;
  /** The adapter, so a decoration rebuild or a re-anchor fires a geometry-change. */
  readonly adapter?: WbTrackedChangesAdapter;
  /** Injectable window for tests, else the global window. */
  readonly windowRef?: Window;
}

const parseMarkId = (raw: string | null): string | null => {
  if (raw === null) return null;
  try {
    const value: unknown = JSON.parse(raw);
    return typeof value === "string" ? value : null;
  } catch {
    // A non-JSON data-id is not one of our marks, so it never matches a proposal.
    return null;
  }
};

export class DomAnchorRectSource implements AnchorRectSource {
  readonly #options: DomAnchorRectSourceOptions;
  readonly #window: Window | undefined;

  constructor(options: DomAnchorRectSourceOptions) {
    this.#options = options;
    this.#window =
      options.windowRef ??
      (typeof window === "undefined" ? undefined : window);
  }

  /** Every mark element for a proposal id (an inserted span can render as several nodes). */
  #markElements(proposalId: string): HTMLElement[] {
    const root = this.#options.getEditorRoot();
    if (root === null) return [];
    const matches: HTMLElement[] = [];
    for (const element of root.querySelectorAll<HTMLElement>("[data-id]")) {
      if (parseMarkId(element.getAttribute("data-id")) === proposalId) {
        matches.push(element);
      }
    }
    return matches;
  }

  anchorRect(
    proposalId: string,
  ): { readonly top: number; readonly height: number } | null {
    const railRoot = this.#options.getRailRoot();
    const elements = this.#markElements(proposalId);
    if (railRoot === null || elements.length === 0) return null;

    let top = Number.POSITIVE_INFINITY;
    let bottom = Number.NEGATIVE_INFINITY;
    for (const element of elements) {
      const rect = element.getBoundingClientRect();
      top = Math.min(top, rect.top);
      bottom = Math.max(bottom, rect.bottom);
    }
    if (!Number.isFinite(top) || !Number.isFinite(bottom)) return null;

    const railRect = railRoot.getBoundingClientRect();
    // Convert to the card-list coordinate space. scrollTop covers the case where the card
    // list is itself the scroll container, and is zero when an ancestor scrolls instead.
    const relativeTop = top - railRect.top + railRoot.scrollTop;
    return { top: relativeTop, height: Math.max(0, bottom - top) };
  }

  scrollToAnchor(proposalId: string): void {
    const [element] = this.#markElements(proposalId);
    if (element === undefined) return;
    element.scrollIntoView({ block: "center", behavior: "smooth" });
    element.classList.add(FLASH_CLASS);
    const view = this.#window;
    if (view !== undefined) {
      view.setTimeout(() => element.classList.remove(FLASH_CLASS), FLASH_MS);
    }
  }

  subscribe(onGeometryChange: () => void): ReviewUnsubscribe {
    const view = this.#window;
    const unsubscribers: Array<() => void> = [];

    if (view !== undefined) {
      // Capture-phase scroll catches the editor's own scroll container and any ancestor.
      view.addEventListener("scroll", onGeometryChange, true);
      view.addEventListener("resize", onGeometryChange);
      unsubscribers.push(() => {
        view.removeEventListener("scroll", onGeometryChange, true);
        view.removeEventListener("resize", onGeometryChange);
      });
    }

    const adapter = this.#options.adapter;
    if (adapter !== undefined) {
      unsubscribers.push(adapter.on("proposals:changed", onGeometryChange));
      unsubscribers.push(adapter.on("anchor:reanchored", onGeometryChange));
    }

    return () => {
      for (const unsubscribe of unsubscribers) unsubscribe();
    };
  }
}

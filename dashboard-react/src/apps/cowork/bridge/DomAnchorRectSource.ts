/**
 * The editor-backed AnchorRectSource that completes the aligned margin stream. The editor
 * is the only owner of live anchor geometry, so this measures
 * namespace-qualified ledger-decoration DOM rects. Every current anchor renders a plain
 * `data-wb-anchor-kind` plus `data-wb-anchor-id`; old suggestion marks additionally retain
 * their JSON `data-id` for adapter compatibility. It reports each anchor's top and height
 * in the rail card-list coordinate space, which is exactly what
 * useAlignedStream feeds computeAlignedLayout.
 *
 * Degrade path (section on AnchorRectSource): an unresolved, off-screen, or currently
 * unmounted anchor reports null, so useAlignedStream leaves that card in normal flow and the
 * rail falls back to scroll-to-and-highlight. Flags and expression-backed claims now receive
 * real ledger decorations and participate in the same geometry path.
 */

import type { Editor } from "@tiptap/core";

import {
  clearCoworkLedgerAnchorFocus,
  focusCoworkLedgerAnchor,
  setCoworkLedgerAnchorFlash,
} from "../editor/ledgerDecorations";
import type {
  AnchorFocusOptions,
  AnchorRectSource,
  ReviewUnsubscribe,
} from "../rail/provider";
import type { RailSelectionKind } from "../rail/store";

/** How long the scroll-to flash class stays on a mark before it is removed. */
const FLASH_MS = 1200;
const FLASH_CLASS = "wb-cowork-anchor--flash";
const ACTIVE_CLASS = "wb-cowork-anchor--active";

export interface DomAnchorRectSourceOptions {
  /** The editor's ProseMirror DOM root (editor.view.dom). Null until the editor mounts. */
  readonly getEditorRoot: () => HTMLElement | null;
  /** The live editor, used to persist focus through decoration rebuilds. */
  readonly getEditor?: () => Editor | null;
  /**
   * The rail card-list element the aligned cards are positioned within (position: relative).
   * Card tops are reported relative to this element, matching useAlignedStream's transform.
   */
  readonly getRailRoot: () => HTMLElement | null;
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

const parseStringList = (raw: string | null): readonly string[] => {
  if (raw === null) return [];
  try {
    const value: unknown = JSON.parse(raw);
    return Array.isArray(value)
      ? value.filter((item): item is string => typeof item === "string")
      : [];
  } catch {
    return [];
  }
};

export class DomAnchorRectSource implements AnchorRectSource {
  readonly #options: DomAnchorRectSourceOptions;
  readonly #window: Window | undefined;
  #flashTimer: number | undefined;
  #focused:
    | {
        readonly id: string;
        readonly kind: RailSelectionKind;
        readonly options: AnchorFocusOptions;
      }
    | null = null;
  #pendingFocus = false;
  readonly #geometryListeners = new Set<() => void>();
  #attachedEditor: Editor | null = null;
  readonly #onEditorTransaction = (): void => {
    this.#emitGeometryChange();
  };

  constructor(options: DomAnchorRectSourceOptions) {
    this.#options = options;
    this.#window =
      options.windowRef ??
      (typeof window === "undefined" ? undefined : window);
  }

  /**
   * Every rendered anchor element for one namespace-qualified identity. New ledger
   * decorations use plain data-wb-anchor attributes. The legacy JSON data-id fallback
   * keeps older suggestion marks and focused tests interoperable during the transition.
   */
  #anchorElements(id: string, kind: RailSelectionKind): HTMLElement[] {
    const root = this.#options.getEditorRoot();
    if (root === null) return [];
    const matches = new Set<HTMLElement>();
    for (const element of root.querySelectorAll<HTMLElement>(
      "[data-wb-anchor-kind][data-wb-anchor-id]",
    )) {
      if (
        element.getAttribute("data-wb-anchor-kind") === kind &&
        element.getAttribute("data-wb-anchor-id") === id
      ) {
        matches.add(element);
      }
    }
    if (kind === "claim") {
      for (const element of root.querySelectorAll<HTMLElement>(
        "[data-wb-claim-ids]",
      )) {
        if (parseStringList(element.getAttribute("data-wb-claim-ids")).includes(id)) {
          matches.add(element);
        }
      }
    }
    if (kind === "proposal") {
      for (const element of root.querySelectorAll<HTMLElement>("[data-id]")) {
        if (parseMarkId(element.getAttribute("data-id")) === id) {
          matches.add(element);
        }
      }
    }
    return [...matches];
  }

  #clearDomFocus(): void {
    const root = this.#options.getEditorRoot();
    if (root === null) return;
    for (const element of root.querySelectorAll<HTMLElement>(
      `.${ACTIVE_CLASS}, .${FLASH_CLASS}`,
    )) {
      element.classList.remove(ACTIVE_CLASS, FLASH_CLASS);
    }
  }

  #cancelFlashTimer(): void {
    if (this.#window !== undefined && this.#flashTimer !== undefined) {
      this.#window.clearTimeout(this.#flashTimer);
    }
    this.#flashTimer = undefined;
  }

  #emitGeometryChange(): void {
    for (const listener of this.#geometryListeners) listener();
  }

  /**
   * Bind editor transaction geometry and replay a focus requested while the
   * editor was still mounting.
   */
  attachEditor(editor: Editor): void {
    if (this.#attachedEditor === editor) {
      this.refresh();
      return;
    }
    this.detachEditor();
    this.#attachedEditor = editor;
    editor.on("transaction", this.#onEditorTransaction);
    this.refresh();
  }

  detachEditor(): void {
    this.#attachedEditor?.off("transaction", this.#onEditorTransaction);
    this.#attachedEditor = null;
    // The Review selection outlives an editor remount. The next attachment
    // must project that still-current focus into the fresh plugin state.
    this.#pendingFocus = this.#focused !== null;
  }

  /** Notify aligned cards after a decoration projection and replay pending focus. */
  refresh(): void {
    const pending = this.#focused;
    if (this.#pendingFocus && pending !== null) {
      this.focusAnchor(pending.id, pending.kind, pending.options);
    }
    this.#emitGeometryChange();
  }

  anchorRect(
    id: string,
    kind: RailSelectionKind,
  ): { readonly top: number; readonly height: number } | null {
    const railRoot = this.#options.getRailRoot();
    const elements = this.#anchorElements(id, kind);
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
    this.focusAnchor(proposalId, "proposal", { scroll: true, flash: true });
  }

  focusAnchor(
    id: string,
    kind: RailSelectionKind,
    options: AnchorFocusOptions = {},
  ): void {
    this.#cancelFlashTimer();
    this.#focused = { id, kind, options };
    const editor = this.#options.getEditor?.() ?? null;
    const projected =
      editor !== null &&
      focusCoworkLedgerAnchor(editor, { id, kind }, options.flash === true);
    this.#pendingFocus = !projected;
    const pluginManaged = this.#options.getEditor !== undefined;

    /*
     * ProseMirror owns every node under editor.view.dom. Mutating those class lists
     * directly wakes its DOM observer, which can reparse CSS-backed presentation as
     * document marks (for example a deletion's line-through as a `strike` mark). Use
     * the decoration transaction whenever the live plugin is available; retain the
     * direct-DOM branch only for the legacy/no-editor degrade path.
     */
    if (!projected && !pluginManaged) {
      this.#clearDomFocus();
    }

    const elements = this.#anchorElements(id, kind);
    if (!projected && !pluginManaged) {
      for (const element of elements) {
        element.classList.add(ACTIVE_CLASS);
        if (options.flash === true) element.classList.add(FLASH_CLASS);
      }
    }

    const [first] = elements;
    if (
      options.scroll === true &&
      first !== undefined &&
      typeof first.scrollIntoView === "function"
    ) {
      const reducedMotion =
        typeof this.#window?.matchMedia === "function" &&
        this.#window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      first.scrollIntoView({
        block: "center",
        behavior: reducedMotion ? "auto" : "smooth",
      });
    }

    if (options.flash === true && this.#window !== undefined) {
      this.#flashTimer = this.#window.setTimeout(() => {
        this.#flashTimer = undefined;
        if (this.#focused?.id === id && this.#focused.kind === kind) {
          this.#focused = {
            ...this.#focused,
            options: { ...this.#focused.options, flash: false },
          };
        }
        const currentEditor = this.#options.getEditor?.() ?? null;
        const cleared =
          currentEditor !== null &&
          setCoworkLedgerAnchorFlash(currentEditor, false);
        if (!cleared && !pluginManaged) {
          for (const element of this.#anchorElements(id, kind)) {
            element.classList.remove(FLASH_CLASS);
          }
        }
      }, FLASH_MS);
    }
  }

  clearFocusedAnchor(): void {
    this.#cancelFlashTimer();
    this.#focused = null;
    this.#pendingFocus = false;
    const editor = this.#options.getEditor?.() ?? null;
    const cleared =
      editor !== null && clearCoworkLedgerAnchorFocus(editor);
    if (!cleared && this.#options.getEditor === undefined) {
      this.#clearDomFocus();
    }
  }

  subscribe(onGeometryChange: () => void): ReviewUnsubscribe {
    const view = this.#window;
    const unsubscribers: Array<() => void> = [];
    this.#geometryListeners.add(onGeometryChange);
    unsubscribers.push(() => {
      this.#geometryListeners.delete(onGeometryChange);
    });

    if (view !== undefined) {
      // Capture-phase scroll catches the editor's own scroll container and any ancestor.
      view.addEventListener("scroll", onGeometryChange, true);
      view.addEventListener("resize", onGeometryChange);
      unsubscribers.push(() => {
        view.removeEventListener("scroll", onGeometryChange, true);
        view.removeEventListener("resize", onGeometryChange);
      });
    }

    return () => {
      for (const unsubscribe of unsubscribers) unsubscribe();
    };
  }
}

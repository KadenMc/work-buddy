/**
 * Editor-backed Review-anchor controller. The editor owns ledger-decoration
 * focus; Review uses this controller to select, reveal, and flash the matching
 * passage without coupling card layout to editor layout. Focus identity is
 * refreshable state; reveal is deliberately one-shot so an R2/decorations
 * refresh cannot snap the editor back to a formerly activated passage.
 *
 * Every current anchor renders `data-wb-anchor-kind` plus
 * `data-wb-anchor-id`; old suggestion marks retain their JSON `data-id` for
 * adapter compatibility.
 */

import type { Editor } from "@tiptap/core";

import {
  clearCoworkLedgerAnchorFocus,
  focusCoworkLedgerAnchor,
  setCoworkLedgerAnchorFlash,
} from "../editor/ledgerDecorations";
import type {
  AnchorRevealOptions,
  ReviewAnchorController,
  ReviewAnchorKind,
} from "../rail/provider";

/** How long the scroll-to flash class stays on a mark before it is removed. */
const FLASH_MS = 1200;
const FLASH_CLASS = "wb-cowork-anchor--flash";
const ACTIVE_CLASS = "wb-cowork-anchor--active";

export interface DomReviewAnchorControllerOptions {
  /** The editor's ProseMirror DOM root (editor.view.dom). Null until the editor mounts. */
  readonly getEditorRoot: () => HTMLElement | null;
  /** The live editor, used to persist focus through decoration rebuilds. */
  readonly getEditor?: () => Editor | null;
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

export class DomReviewAnchorController implements ReviewAnchorController {
  readonly #options: DomReviewAnchorControllerOptions;
  readonly #window: Window | undefined;
  #flashTimer: number | undefined;
  #focused:
    | {
        readonly id: string;
        readonly kind: ReviewAnchorKind;
      }
    | null = null;
  #flashing = false;
  #attachedEditor: Editor | null = null;

  constructor(options: DomReviewAnchorControllerOptions) {
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
  #anchorElements(id: string, kind: ReviewAnchorKind): HTMLElement[] {
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

  /** Bind a live editor and replay focus requested before it mounted. */
  attachEditor(editor: Editor): void {
    if (this.#attachedEditor === editor) {
      this.refresh();
      return;
    }
    this.detachEditor();
    this.#attachedEditor = editor;
    this.refresh();
  }

  detachEditor(): void {
    this.#attachedEditor = null;
  }

  /** Replay focus after a decoration projection or editor attachment. */
  refresh(): void {
    const focused = this.#focused;
    if (focused === null) return;
    this.#projectFocus(focused.id, focused.kind, this.#flashing);
  }

  #projectFocus(
    id: string,
    kind: ReviewAnchorKind,
    flash: boolean,
  ): HTMLElement[] {
    const editor = this.#options.getEditor?.() ?? null;
    const projected =
      editor !== null &&
      focusCoworkLedgerAnchor(editor, { id, kind }, flash);
    const pluginManaged = this.#options.getEditor !== undefined;

    /*
     * ProseMirror owns every node under editor.view.dom. Mutating those class lists
     * directly wakes its DOM observer, which can reparse CSS-backed presentation as
     * document marks. Use the decoration transaction whenever the live plugin is
     * available; retain the direct-DOM branch only for the legacy/no-editor path.
     */
    if (!projected && !pluginManaged) {
      this.#clearDomFocus();
    }

    const elements = this.#anchorElements(id, kind);
    if (!projected && !pluginManaged) {
      for (const element of elements) {
        element.classList.add(ACTIVE_CLASS);
        if (flash) element.classList.add(FLASH_CLASS);
      }
    }
    return elements;
  }

  #scrollToFirst(elements: readonly HTMLElement[]): boolean {
    const [first] = elements;
    if (first === undefined || typeof first.scrollIntoView !== "function") {
      return false;
    }
    const reducedMotion =
      typeof this.#window?.matchMedia === "function" &&
      this.#window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    first.scrollIntoView({
      block: "center",
      behavior: reducedMotion ? "auto" : "smooth",
    });
    return true;
  }

  #startFlashTimer(id: string, kind: ReviewAnchorKind): void {
    if (this.#window !== undefined) {
      const pluginManaged = this.#options.getEditor !== undefined;
      this.#flashTimer = this.#window.setTimeout(() => {
        this.#flashTimer = undefined;
        this.#flashing = false;
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

  focusAnchor(id: string, kind: ReviewAnchorKind): void {
    this.#cancelFlashTimer();
    this.#focused = { id, kind };
    this.#flashing = false;
    this.#projectFocus(id, kind, false);
  }

  revealAnchor(
    id: string,
    kind: ReviewAnchorKind,
    options: AnchorRevealOptions = {},
  ): void {
    this.#cancelFlashTimer();
    const flash = options.flash === true;
    this.#focused = { id, kind };
    this.#flashing = flash;
    const elements = this.#projectFocus(id, kind, flash);
    this.#scrollToFirst(elements);
    if (flash) this.#startFlashTimer(id, kind);
  }

  clearFocusedAnchor(): void {
    this.#cancelFlashTimer();
    this.#focused = null;
    this.#flashing = false;
    const editor = this.#options.getEditor?.() ?? null;
    const cleared =
      editor !== null && clearCoworkLedgerAnchorFocus(editor);
    if (!cleared && this.#options.getEditor === undefined) {
      this.#clearDomFocus();
    }
  }
}

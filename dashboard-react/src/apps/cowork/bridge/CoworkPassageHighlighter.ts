import type { Editor } from "@tiptap/core";

import type { ScrollAnchorTarget } from "../chat";
import {
  clearCoworkPassageHighlight,
  showCoworkPassageHighlight,
} from "../editor/ledgerDecorations";
import { resolveQuoteAnchor } from "../suggestions/anchor";

/** The visible navigation cue is brief, while the editor selection remains untouched. */
export const PASSAGE_HIGHLIGHT_MS = 1_200;

export interface CoworkPassageHighlighterOptions {
  readonly getEditor: () => Editor | null;
  readonly windowRef?: Window;
  readonly durationMs?: number;
}

/**
 * Scroll and temporarily decorate one quote-anchored Chat passage without focusing
 * the editor or replacing its text selection. The decoration is ProseMirror view
 * state only, so neither Yjs nor Markdown observes it.
 */
export class CoworkPassageHighlighter {
  readonly #options: CoworkPassageHighlighterOptions;
  readonly #window: Window | undefined;
  readonly #durationMs: number;
  #timer: number | undefined;
  #active:
    | { readonly editor: Editor; readonly highlightId: string }
    | undefined;

  constructor(options: CoworkPassageHighlighterOptions) {
    this.#options = options;
    this.#window =
      options.windowRef ??
      (typeof window === "undefined" ? undefined : window);
    this.#durationMs = options.durationMs ?? PASSAGE_HIGHLIGHT_MS;
  }

  #project(
    target: ScrollAnchorTarget,
    {
      reveal,
      temporary,
      restoreFocus,
    }: {
      readonly reveal: boolean;
      readonly temporary: boolean;
      readonly restoreFocus: boolean;
    },
  ): boolean {
    const editor = this.#options.getEditor();
    const anchor = target.anchor;
    if (editor === null || anchor === undefined) return false;
    const range = resolveQuoteAnchor(editor.state.doc, {
      exact: anchor.exact,
      prefix: anchor.prefix ?? "",
      suffix: anchor.suffix ?? "",
    });
    if (range === null) return false;

    this.clear();
    const highlightId = `feedback:${target.spanId}`;
    if (
      !showCoworkPassageHighlight(editor, {
        id: highlightId,
        from: range.from,
        to: range.to,
      })
    ) {
      return false;
    }
    this.#active = { editor, highlightId };

    const targetElement = reveal
      ? [...editor.view.dom.querySelectorAll<HTMLElement>(
          '[data-wb-anchor-kind="passage"]',
        )].find(
          (element) => element.getAttribute("data-wb-anchor-id") === highlightId,
        )
      : undefined;
    if (
      targetElement !== undefined &&
      typeof targetElement.scrollIntoView === "function"
    ) {
      const reducedMotion =
        typeof this.#window?.matchMedia === "function" &&
        this.#window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      targetElement.scrollIntoView({
        block: "center",
        behavior: reducedMotion ? "auto" : "smooth",
      });
    }

    if (temporary && this.#window !== undefined) {
      this.#timer = this.#window.setTimeout(() => {
        this.#timer = undefined;
        const active = this.#active;
        this.#active = undefined;
        if (active !== undefined && !active.editor.isDestroyed) {
          clearCoworkPassageHighlight(active.editor, active.highlightId);
        }
        if (restoreFocus) this.focus(target);
      }, this.#durationMs);
    }
    return true;
  }

  /** One-shot user navigation used by explicit “Show in document” actions. */
  show(target: ScrollAnchorTarget): boolean {
    return this.#project(target, {
      reveal: true,
      temporary: true,
      restoreFocus: false,
    });
  }

  /** Explicit navigation that restores the expanded candidate's passive focus. */
  showAndRefocus(target: ScrollAnchorTarget): boolean {
    return this.#project(target, {
      reveal: true,
      temporary: true,
      restoreFocus: true,
    });
  }

  /** Persistent view-only emphasis with no scrolling for passive rail focus. */
  focus(target: ScrollAnchorTarget): boolean {
    return this.#project(target, {
      reveal: false,
      temporary: false,
      restoreFocus: false,
    });
  }

  clear(): void {
    if (this.#timer !== undefined && this.#window !== undefined) {
      this.#window.clearTimeout(this.#timer);
    }
    this.#timer = undefined;
    const active = this.#active;
    this.#active = undefined;
    if (active !== undefined && !active.editor.isDestroyed) {
      clearCoworkPassageHighlight(active.editor, active.highlightId);
    }
  }

  dispose(): void {
    this.clear();
  }
}

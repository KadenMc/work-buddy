export type FocusTarget = HTMLElement | null | (() => HTMLElement | null);

const resolveTarget = (target: FocusTarget): HTMLElement | null =>
  typeof target === "function" ? target() : target;

export function focusSafely(target: FocusTarget): boolean {
  const element = resolveTarget(target);
  if (element === null || !element.isConnected) {
    return false;
  }
  element.focus({ preventScroll: true });
  return document.activeElement === element;
}

export function focusFirst(container: HTMLElement): boolean {
  const candidate = container.querySelector<HTMLElement>(
    [
      "button:not([disabled])",
      "[href]",
      "input:not([disabled])",
      "select:not([disabled])",
      "textarea:not([disabled])",
      '[tabindex]:not([tabindex="-1"])',
    ].join(","),
  );
  return focusSafely(candidate);
}

/** Capture focus before a drawer/menu/edit transition and restore it afterward. */
export function createFocusRestorer(fallback?: FocusTarget): () => void {
  const captured =
    typeof document !== "undefined" && document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;

  return () => {
    const schedule =
      typeof requestAnimationFrame === "function"
        ? requestAnimationFrame
        : (callback: FrameRequestCallback) => window.setTimeout(callback, 0);
    schedule(() => {
      if (!focusSafely(captured)) {
        focusSafely(fallback ?? null);
      }
    });
  };
}

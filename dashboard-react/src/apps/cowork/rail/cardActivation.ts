import type { MouseEvent } from "react";

const INTERACTIVE_DESCENDANT =
  "button, a, input, textarea, select, summary, [role='button'], [role='link'], [contenteditable]:not([contenteditable='false'])";

/**
 * Let pointer users activate a Review card without turning the whole card into
 * a nested button. The real title button remains the keyboard and assistive-
 * technology control; embedded passage/inspector controls keep their own
 * actions, and selecting detail text does not unexpectedly navigate away.
 */
export function activateCardFromContainer(
  event: MouseEvent<HTMLElement>,
  onActivate: () => void,
): void {
  if (event.defaultPrevented) return;

  const target = event.target as Element | null;
  const interactive = target?.closest(INTERACTIVE_DESCENDANT);
  if (interactive !== undefined && interactive !== null && event.currentTarget.contains(interactive)) {
    return;
  }

  const selection = event.currentTarget.ownerDocument.getSelection?.();
  const selectionBelongsToCard =
    selection !== undefined &&
    selection !== null &&
    ((selection.anchorNode !== null && event.currentTarget.contains(selection.anchorNode)) ||
      (selection.focusNode !== null && event.currentTarget.contains(selection.focusNode)));
  if (
    selectionBelongsToCard &&
    selection !== undefined &&
    selection !== null &&
    !selection.isCollapsed &&
    selection.toString().trim()
  ) {
    return;
  }
  onActivate();
}

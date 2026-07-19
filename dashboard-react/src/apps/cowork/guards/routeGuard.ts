/**
 * Route-change dirty guards for the Co-work surface (PRD section 6 "route-change guard",
 * section 7 writing / feedback walkthroughs). Two kinds of unsaved work must survive and
 * warn before navigation: a staged-but-unsubmitted mark sitting (rail/dirty.ts) and an
 * unsent chat draft (guards/chatDraft.ts). This module composes both dirty signals into one
 * guard.
 *
 * The dashboard host mounts the router as `BrowserRouter`, which does not expose the
 * data-router `useBlocker`, so an in-app `<Link>` cannot be intercepted from here. What this
 * guard does cover, fully and testably: (1) browser-level route changes (reload, close,
 * typing a new URL, external back / forward) through a beforeunload prompt armed on the union
 * of both dirty signals, and (2) programmatic navigation and the coarse open-doc / close-doc
 * document switch, through a confirm seam the surface calls before it dispatches. Full
 * in-app tab interception arrives with the data-router migration and its useBlocker.
 */

import { useEffect } from "react";

/** The prompt shown before discarding staged marks or an unsent message. */
export const UNSAVED_WORK_PROMPT =
  "You have staged review marks or an unsent message. Leave and discard them?";

/** True when any supplied dirty signal is set. The union the guard protects. */
export function anyDirty(...flags: readonly boolean[]): boolean {
  return flags.some((flag) => flag);
}

/**
 * Arm a beforeunload prompt while any unsaved work is present. `getDirty` is read at event
 * time (not closed over once), so the caller composes the live union, for example
 * `() => isDirty(store.getState()) || isChatDraftDirty(draft)`. Mirrors the rail's mark-only
 * guard but covers both dirty sources, so the surface uses this one guard for the union.
 */
export function useUnsavedWorkGuard(
  getDirty: () => boolean,
  active = true,
): void {
  useEffect(() => {
    if (!active) return undefined;
    const handler = (event: BeforeUnloadEvent) => {
      if (!getDirty()) return;
      event.preventDefault();
      // Legacy browsers require a returnValue to raise the prompt.
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => {
      window.removeEventListener("beforeunload", handler);
    };
  }, [getDirty, active]);
}

export interface ConfirmDiscardOptions {
  readonly message?: string;
  /** Injectable confirm, defaults to window.confirm. Returns true to proceed. */
  readonly confirmImpl?: (message: string) => boolean;
}

/**
 * The in-app navigation confirm seam. Returns true when it is safe to proceed: either there
 * is no unsaved work, or the human confirmed discarding it. Called before a programmatic
 * navigation or the coarse open-doc / close-doc document switch.
 */
export function confirmDiscardUnsavedWork(
  dirty: boolean,
  options: ConfirmDiscardOptions = {},
): boolean {
  if (!dirty) return true;
  const confirmImpl =
    options.confirmImpl ?? ((message: string) => window.confirm(message));
  return confirmImpl(options.message ?? UNSAVED_WORK_PROMPT);
}

/**
 * Guard a programmatic navigation. When unsaved work is present the human is asked to confirm
 * the discard, and the navigation runs only on confirm. Returns whether it proceeded. The
 * navigate function is the router-agnostic `(to) => void` shape, so this wraps a react-router
 * NavigateFunction or the coarse document-switch dispatch equally.
 */
export function guardedNavigate(
  navigate: (to: string) => void,
  to: string,
  dirty: boolean,
  options: ConfirmDiscardOptions = {},
): boolean {
  if (!confirmDiscardUnsavedWork(dirty, options)) return false;
  navigate(to);
  return true;
}

/**
 * A controllable `matchMedia` stub for the conformance suite. jsdom evaluates no
 * `@media` query, so a test that asks whether the surface honors forced-colors or
 * reduced-motion drives those environment signals through this stub and reads the
 * result off the shared theme runtime. The shape mirrors the one in
 * `theme/ThemeProvider.test.tsx` so the two stay legible together, kept here as a
 * conformance-owned helper rather than shared production code.
 */

import { vi } from "vitest";

interface MediaController {
  readonly query: string;
  matches: boolean;
  readonly listeners: Set<(event: MediaQueryListEvent) => void>;
}

const controllers = new Map<string, MediaController>();

/**
 * Install a `window.matchMedia` stub whose match results are driven by
 * `setMedia`. Every query starts unmatched. Call once per test (a `beforeEach`
 * is a good home) and pair with `resetMedia` so state never leaks across tests.
 */
export function installMatchMedia(): void {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => {
      const controller =
        controllers.get(query) ??
        ({ query, matches: false, listeners: new Set() } satisfies MediaController);
      controllers.set(query, controller);
      return {
        media: query,
        get matches() {
          return controller.matches;
        },
        onchange: null,
        addEventListener: (
          _type: "change",
          listener: (event: MediaQueryListEvent) => void,
        ) => controller.listeners.add(listener),
        removeEventListener: (
          _type: "change",
          listener: (event: MediaQueryListEvent) => void,
        ) => controller.listeners.delete(listener),
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => true,
      };
    }),
  );
}

/**
 * Set whether a media query currently matches, notifying any subscriber the theme
 * runtime registered. The query must be one the runtime asks for, for example
 * `(forced-colors: active)` or `(prefers-reduced-motion: reduce)`.
 */
export function setMedia(query: string, matches: boolean): void {
  const controller =
    controllers.get(query) ??
    ({ query, matches: false, listeners: new Set() } satisfies MediaController);
  controllers.set(query, controller);
  controller.matches = matches;
  controller.listeners.forEach((listener) =>
    listener({ matches, media: query } as MediaQueryListEvent),
  );
}

/** Clear every controller so a following test starts from all-unmatched. */
export function resetMedia(): void {
  controllers.clear();
}

export const FORCED_COLORS_QUERY = "(forced-colors: active)";
export const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";
export const DARK_SCHEME_QUERY = "(prefers-color-scheme: dark)";

import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, expect } from "vitest";

// jsdom ships no ResizeObserver, which react-resizable-panels (the Co-work split) and cmdk
// construct on mount. Install a no-op default so any component that observes an element renders
// without throwing. Tests that exercise ResizeObserver behavior still override it locally with
// vi.stubGlobal and restore it with vi.unstubAllGlobals, so this only fills the unset case.
globalThis.ResizeObserver ??= class {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
};

afterEach(() => {
  cleanup();
});

/**
 * Run axe against rendered test output and retain the actionable rule details
 * in Vitest's failure message. Shared widgets should call this from their
 * ready and non-ready state tests rather than inventing one-off assertions.
 */
export async function expectNoAccessibilityViolations(
  container: Element | Document = document,
): Promise<void> {
  const results = await axe.run(container);
  const details = results.violations
    .map(
      (violation) =>
        `${violation.id}: ${violation.help} (${violation.nodes.length} node(s))`,
    )
    .join("\n");

  expect(results.violations, details || "Expected no axe violations").toEqual([]);
}

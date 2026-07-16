import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, expect } from "vitest";

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

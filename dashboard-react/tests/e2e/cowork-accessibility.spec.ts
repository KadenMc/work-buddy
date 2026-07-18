import { expect, test } from "@playwright/test";

import { installThemePreference } from "./helpers";
import {
  blockingViolations,
  openCowork,
  seriousAxeViolations,
} from "./cowork-helpers";

/**
 * Dashboard-citizenship proof (PRD I18) for the Co-work surface in a real browser,
 * where the theme @media blocks jsdom cannot evaluate are live: axe in the default
 * and dark schemes, motion-token collapse under reduced motion, and system-colour
 * override under forced colors with the redundant text encoding still legible. The
 * axe gate is strict on structure and ARIA and allowlists only the documented rail
 * contrast near-misses (see KNOWN_RAIL_CONTRAST_GAPS), which are production CSS gaps
 * reported as findings rather than fixed from this tests-only work package.
 */

test("has no blocking axe violations in the default scheme", async ({ page }) => {
  await openCowork(page);
  expect(blockingViolations(await seriousAxeViolations(page))).toEqual([]);
});

test("stays accessible in the dark scheme", async ({ page }) => {
  await installThemePreference(page, "dark");
  await openCowork(page);
  await expect(page.locator("html")).toHaveAttribute("data-wb-scheme", "dark");
  expect(blockingViolations(await seriousAxeViolations(page))).toEqual([]);
});

test("collapses motion tokens under reduced motion", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openCowork(page);
  expect(
    await page.evaluate(() =>
      getComputedStyle(document.documentElement)
        .getPropertyValue("--wb-motion-duration-standard")
        .trim(),
    ),
  ).toBe("0ms");
});

test("overrides semantic surfaces and keeps a text encoding under forced colors", async ({
  page,
  browserName,
}) => {
  test.skip(
    browserName !== "chromium",
    "Playwright forced-colors emulation is Chromium-only here",
  );
  await page.emulateMedia({ forcedColors: "active" });
  await openCowork(page);

  expect(
    await page.evaluate(() => matchMedia("(forced-colors: active)").matches),
  ).toBe(true);
  expect(
    await page.evaluate(() =>
      getComputedStyle(document.documentElement)
        .getPropertyValue("--wb-color-surface-canvas")
        .trim(),
    ),
  ).toMatch(/canvas/i);

  // The drift state keeps a text label beside its data attribute, so the meaning a
  // colour would carry survives when forced colors replace the palette.
  const drift = page.locator("[data-drift]").first();
  await expect(drift).toBeVisible();
  await expect(drift).not.toHaveText("");
});

import { expect, test } from "@playwright/test";

import { installThemePreference, openJournal } from "./helpers";

test.beforeEach(({ browserName }) => {
  test.skip(browserName !== "chromium", "Canonical visual baselines use Chromium");
  test.skip(process.platform !== "win32", "Canonical visual baselines use Windows");
});

test("default dark desktop Journal visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "dark");
  await openJournal(page);

  await expect(page).toHaveScreenshot("journal-dark-desktop.png", {
    animations: "disabled",
    fullPage: true,
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test("default light desktop Journal visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "light");
  await openJournal(page);

  await expect(page).toHaveScreenshot("journal-light-desktop.png", {
    animations: "disabled",
    fullPage: true,
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test("adversarial skin desktop Journal visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "dark", "wb.conformance-stress");
  await openJournal(page);

  await expect(page).toHaveScreenshot("journal-stress-skin-desktop.png", {
    animations: "disabled",
    fullPage: true,
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test("mobile one-column Journal visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installThemePreference(page, "dark");
  await openJournal(page);
  // Full-page screenshots do not scroll each below-the-fold widget into the
  // rendering viewport. Materialize virtualized list content so the golden
  // proves the compact renderers rather than their intrinsic-size placeholders.
  await page.addStyleTag({
    content: `
      .wb-temporal-list li,
      .wb-markdown-item {
        content-visibility: visible !important;
        contain-intrinsic-size: none !important;
      }
    `,
  });
  await expect(page.getByRole("list", { name: "Day timeline items" })).toContainText(
    "Mapped Journal data contracts",
  );
  await expect(page.getByRole("region", { name: "Running Notes" })).toContainText(
    "Prototype mobile timeline edge case",
  );

  await expect(page).toHaveScreenshot("journal-mobile.png", {
    animations: "disabled",
    fullPage: true,
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

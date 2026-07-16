import { expect, test } from "@playwright/test";

import { installThemePreference, openJournal } from "./helpers";

async function materializeVisualContent(page: import("@playwright/test").Page) {
  await page.addStyleTag({
    content: `
      .wb-temporal-item,
      .wb-temporal-list li,
      .wb-markdown-item {
        content-visibility: visible !important;
        contain-intrinsic-size: none !important;
      }
    `,
  });
}

test.beforeEach(({ browserName }) => {
  test.skip(browserName !== "chromium", "Canonical visual baselines use Chromium");
  test.skip(process.platform !== "win32", "Canonical visual baselines use Windows");
});

test("default dark desktop Journal visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "dark");
  await openJournal(page);
  await materializeVisualContent(page);

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
  await materializeVisualContent(page);

  await expect(page).toHaveScreenshot("journal-light-desktop.png", {
    animations: "disabled",
    fullPage: true,
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test("default light Accessibility settings visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "light");
  await page.goto("/app/settings/accessibility", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Accessibility" })).toBeVisible();

  await expect(page).toHaveScreenshot("settings-accessibility-light-desktop.png", {
    animations: "disabled",
    fullPage: true,
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test("maximum-text dark Accessibility settings visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "dark");
  await page.addInitScript(() => {
    localStorage.setItem("wb.accessibility.type-scale.v1", "maximum");
  });
  await page.goto("/app/settings/accessibility", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("slider", { name: "Text size" })).toHaveAttribute(
    "aria-valuetext",
    "Maximum, 137.5%",
  );

  await expect(page).toHaveScreenshot("settings-accessibility-maximum-dark-desktop.png", {
    animations: "disabled",
    fullPage: true,
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test("Calm Workshop dark desktop Journal visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "dark", "wb.calm-workshop");
  await openJournal(page);
  await materializeVisualContent(page);

  await expect(page).toHaveScreenshot("journal-calm-workshop-dark-desktop.png", {
    animations: "disabled",
    fullPage: true,
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test("default dark widget catalog visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "dark");
  await openJournal(page);
  await page.getByRole("button", { name: "Customize view" }).click();
  await page.getByRole("button", { name: "Widgets" }).click();
  await expect(page.getByRole("dialog", { name: "Widgets" })).toBeVisible();

  await expect(page).toHaveScreenshot("widget-catalog-dark-desktop.png", {
    animations: "disabled",
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test("default dark mobile-order editor visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "dark");
  await openJournal(page);
  await page.getByRole("button", { name: "Customize view" }).click();
  await page.getByRole("button", { name: "Mobile order" }).click();
  await expect(page.getByRole("grid", { name: "Mobile widget order" })).toBeVisible();

  await expect(page).toHaveScreenshot("mobile-order-editor-dark-desktop.png", {
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
  await materializeVisualContent(page);

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
  await materializeVisualContent(page);
  await expect(page.getByRole("radiogroup", { name: "Timeline display mode" })).toBeVisible();
  await page
    .getByRole("radiogroup", { name: "Timeline display mode" })
    .getByText("List", { exact: true })
    .click();
  const listProjection = page.getByRole("region", { name: /Calendar surface for/ });
  await expect(listProjection.getByRole("table")).toBeVisible();
  await expect(
    listProjection.getByRole("button", { name: /Mapped Journal data contracts/ }),
  ).toBeVisible();
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

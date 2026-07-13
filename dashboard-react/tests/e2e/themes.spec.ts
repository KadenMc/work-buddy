import { expect, test } from "@playwright/test";

import {
  installThemePreference,
  openJournal,
  THEME_KEY,
} from "./helpers";

test("the inline bootstrap resolves an explicit light scheme before React executes", async ({
  page,
}) => {
  await installThemePreference(page, "light");
  await page.route("**/src/main.tsx", (route) => route.abort());
  await page.goto("/app/journal", { waitUntil: "domcontentloaded" });

  await expect(page.locator("html")).toHaveAttribute("data-wb-scheme", "light");
  await expect(page.locator("html")).toHaveAttribute("data-wb-skin", "wb.default");
  await expect(page.locator('meta[name="theme-color"]')).toHaveAttribute(
    "content",
    "#f4f2ed",
  );
  await expect(page.locator("#root")).toBeEmpty();
});

test("system and explicit preferences persist across page reloads", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await installThemePreference(page, "system");
  await openJournal(page);
  await expect(page.locator("html")).toHaveAttribute("data-wb-scheme", "dark");

  await page.evaluate((key) => {
    localStorage.setItem(
      key,
      JSON.stringify({ version: 1, scheme: "light", skinId: "wb.default" }),
    );
  }, THEME_KEY);
  await page.reload({ waitUntil: "domcontentloaded" });
  expect(
    await page.evaluate((key) =>
      JSON.parse(localStorage.getItem(key) ?? "null") as {
        scheme?: string;
      } | null,
    THEME_KEY),
  ).toMatchObject({ scheme: "light" });
  await expect(page.locator("html")).toHaveAttribute("data-wb-scheme", "light");
  await expect(page.getByRole("region", { name: "Quick Capture", exact: true })).toBeVisible();
});

test("the Appearance control persists scheme, product skin, and density", async ({ page }) => {
  await openJournal(page);

  await page.getByRole("button", { name: "Appearance" }).click();
  await expect(page.getByRole("heading", { name: "Appearance" })).toBeVisible();

  await page.getByRole("button", { name: /Color scheme/ }).click();
  await page.getByRole("option", { name: "Dark" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-wb-scheme", "dark");

  await page.getByRole("button", { name: /Skin/ }).click();
  await page.getByRole("option", { name: /Studio Slate/ }).click();
  await expect(page.locator("html")).toHaveAttribute("data-wb-skin", "wb.studio");

  await page.getByRole("button", { name: /Density/ }).click();
  await page.getByRole("option", { name: /Compact/ }).click();
  await expect(page.locator("html")).toHaveAttribute("data-wb-density", "compact");

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator("html")).toHaveAttribute("data-wb-scheme", "dark");
  await expect(page.locator("html")).toHaveAttribute("data-wb-skin", "wb.studio");
  await expect(page.locator("html")).toHaveAttribute("data-wb-density", "compact");
});

test("the adversarial validated skin supplies real semantic values in both schemes", async ({
  page,
}) => {
  await installThemePreference(page, "dark", "wb.conformance-stress");
  await openJournal(page);

  await expect(page.locator("html")).toHaveAttribute(
    "data-wb-skin",
    "wb.conformance-stress",
  );
  const darkSurface = await page.evaluate(() =>
    getComputedStyle(document.documentElement)
      .getPropertyValue("--wb-color-surface-canvas")
      .trim(),
  );
  expect(darkSurface).toBe("#1e1b4b");

  await page.evaluate((key) => {
    localStorage.setItem(
      key,
      JSON.stringify({
        version: 1,
        scheme: "light",
        skinId: "wb.conformance-stress",
      }),
    );
  }, THEME_KEY);
  await page.reload({ waitUntil: "domcontentloaded" });
  const lightSurface = await page.evaluate(() =>
    getComputedStyle(document.documentElement)
      .getPropertyValue("--wb-color-surface-canvas")
      .trim(),
  );
  expect(lightSurface).toBe("#fff7ed");
});

test("reduced motion collapses public motion tokens", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openJournal(page);

  expect(
    await page.evaluate(() =>
      getComputedStyle(document.documentElement)
        .getPropertyValue("--wb-motion-duration-standard")
        .trim(),
    ),
  ).toBe("0ms");
});

test("forced colors override semantic surfaces", async ({ page, browserName }) => {
  test.skip(browserName !== "chromium", "Playwright forced-colors emulation is Chromium-only here");
  await page.emulateMedia({ forcedColors: "active" });
  await openJournal(page);

  expect(await page.evaluate(() => matchMedia("(forced-colors: active)").matches)).toBe(
    true,
  );
  expect(
    await page.evaluate(() =>
      getComputedStyle(document.documentElement)
        .getPropertyValue("--wb-color-surface-canvas")
        .trim(),
    ),
  ).toMatch(/canvas/i);
});

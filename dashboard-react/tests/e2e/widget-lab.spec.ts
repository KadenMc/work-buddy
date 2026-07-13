import { expect, test } from "@playwright/test";

test("keeps the development Widget Lab off navigation while mounting real widgets", async ({
  page,
}) => {
  await page.goto("/app/__widget-lab?count=50");

  await expect(page.getByRole("heading", { name: "Widget Lab", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Widget Lab" })).toHaveCount(0);
  await expect(page.getByTestId("widget-lab-host")).toHaveCount(50);
  await expect(page.locator(".wb-widget-frame")).toHaveCount(50);

  await page.getByLabel("Widget Lab scheme").selectOption("dark");
  await expect(page.locator("html")).toHaveAttribute("data-wb-scheme", "dark");
  await page
    .getByLabel("Widget Lab skin")
    .selectOption("wb.conformance-stress");
  await expect(page.locator("html")).toHaveAttribute(
    "data-wb-skin",
    "wb.conformance-stress",
  );
});

test("reports the shared forced-colors and reduced-motion theme hooks", async ({
  page,
}) => {
  await page.emulateMedia({ forcedColors: "active", reducedMotion: "reduce" });
  await page.goto("/app/__widget-lab?count=3");

  await expect(page.getByTestId("widget-lab-forced-colors")).toHaveText("active");
  await expect(page.getByTestId("widget-lab-reduced-motion")).toHaveText("active");
  await expect(page.getByTestId("widget-lab-host")).toHaveCount(3);
});

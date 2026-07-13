import { expect, test } from "@playwright/test";

test("the app root resolves to the default registered view", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "domcontentloaded" });

  await expect(page).toHaveURL(/\/app\/journal$/);
  await expect(
    page.getByRole("link", { name: "Journal", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  await expect(page.getByText("Journal: coming soon")).toBeVisible();
});

test("the Journal view supports direct navigation and refresh", async ({ page }) => {
  await page.goto("/app/journal", { waitUntil: "domcontentloaded" });
  await expect(page.getByText("Journal: coming soon")).toBeVisible();

  await page.reload({ waitUntil: "domcontentloaded" });

  await expect(page).toHaveURL(/\/app\/journal$/);
  await expect(page.getByText("Journal: coming soon")).toBeVisible();
});

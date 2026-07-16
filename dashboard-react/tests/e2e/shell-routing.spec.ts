import { expect, test } from "@playwright/test";

test("the app root resolves to the default registered view", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "domcontentloaded" });

  await expect(page).toHaveURL(/\/app\/journal$/);
  await expect(
    page.getByRole("link", { name: "Journal", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { name: "Journal", level: 1 })).toBeVisible();
  await expect(page.getByRole("region", { name: "Quick Capture", exact: true })).toBeVisible();
});

test("the Journal view supports direct navigation and refresh", async ({ page }) => {
  await page.goto("/app/journal", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("region", { name: "Day Timeline", exact: true })).toBeVisible();

  await page.reload({ waitUntil: "domcontentloaded" });

  await expect(page).toHaveURL(/\/app\/journal$/);
  await expect(page.getByRole("region", { name: "Running Notes", exact: true })).toBeVisible();
});

test("Quick Capture persists exact text and updates bound sibling input through the provider", async ({
  page,
}) => {
  await page.goto("/app/journal", { waitUntil: "domcontentloaded" });
  const capture = page.getByRole("region", { name: "Quick Capture", exact: true });

  await capture.getByRole("textbox", { name: "Capture text" }).fill("Meeting ran long");
  await capture.getByRole("button", { name: /Destination/ }).click();
  await page.getByRole("option", { name: /^Running notes/ }).click();
  await capture.getByRole("button", { name: "Capture", exact: true }).click();

  const submittedCapture = capture
    .getByRole("region", { name: "Recent captures" })
    .locator("li")
    .filter({ hasText: "Meeting ran long" });
  await expect(submittedCapture).toContainText("Meeting ran long");
  await expect(submittedCapture).toContainText("persisted");
  await expect(page.getByRole("region", { name: "Running Notes", exact: true })).toContainText(
    "Meeting ran long",
  );
  await expect(submittedCapture).toContainText("succeeded");
  await expect(page.getByRole("region", { name: "Running Notes", exact: true })).toContainText(
    "The meeting ran long; only the open afternoon was replanned.",
  );
});

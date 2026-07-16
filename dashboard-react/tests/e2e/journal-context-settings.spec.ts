import { expect, test } from "@playwright/test";

test("the Journal contextual settings launcher remains discoverable on mobile", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/app/journal?provider=demo#day-timeline", {
    waitUntil: "domcontentloaded",
  });

  const launcher = page.getByRole("button", { name: "Journal settings" });
  await expect(launcher).toBeVisible();
  await expect(launcher.locator("..")).toHaveAttribute("title", "Journal settings");

  await launcher.click();
  await expect(page).toHaveURL(/\/app\/settings\/apps\/journal$/);
  await expect(
    page.getByRole("heading", { name: "Journal settings" }),
  ).toBeVisible();

  await page.getByRole("button", { name: "Back to Journal" }).click();
  await expect(page).toHaveURL(
    /\/app\/journal\?provider=demo#day-timeline$/,
  );
});

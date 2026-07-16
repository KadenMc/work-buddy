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
  const destination = capture.getByRole("button", { name: /Destination/ });
  await expect(destination).toHaveText("Auto");
  await expect(capture.getByText(/Let Smart infer whether/i)).toHaveCount(0);
  const smart = capture.getByRole("switch", { name: "Smart" });
  const smartControl = capture.locator(".wb-capture__smart");
  const captureButton = capture.getByRole("button", { name: "Capture", exact: true });
  const [smartBox, destinationBox, captureBox] = await Promise.all([
    smartControl.boundingBox(),
    destination.boundingBox(),
    captureButton.boundingBox(),
  ]);
  expect(smartBox).not.toBeNull();
  expect(destinationBox).not.toBeNull();
  expect(captureBox).not.toBeNull();
  expect(smartBox!.x + smartBox!.width).toBeLessThan(destinationBox!.x);
  expect(destinationBox!.x + destinationBox!.width).toBeLessThan(captureBox!.x);
  expect(Math.abs(
    destinationBox!.y + destinationBox!.height / 2 -
      (captureBox!.y + captureBox!.height / 2),
  )).toBeLessThanOrEqual(2);
  await expect(smart).toBeChecked();
  await smartControl.click();
  await expect(captureButton).toBeDisabled();
  await expect(capture.getByText("Turn on Smart to use Auto.")).toBeVisible();
  await smartControl.click();
  await destination.click();
  await expect(page.getByRole("option", { name: /^Auto/ })).toContainText(
    "Let Smart infer whether this belongs in Log or Running notes.",
  );
  await page.getByRole("option", { name: /^Running notes/ }).click();
  await captureButton.click();

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

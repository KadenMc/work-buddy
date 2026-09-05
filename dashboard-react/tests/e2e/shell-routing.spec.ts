import { expect, test } from "@playwright/test";

test("the app root resolves to the default registered view", async ({ page }) => {
  await page.goto("/app/?provider=demo", { waitUntil: "domcontentloaded" });

  await expect(page).toHaveURL(/\/app\/journal\?provider=demo$/);
  await expect(
    page.getByRole("link", { name: "Journal", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { name: "Journal", level: 1 })).toBeVisible();
  await expect(page.getByRole("region", { name: "Quick Capture", exact: true })).toBeVisible();

  const brand = page.locator(".header__brand");
  await expect(brand.getByRole("heading", { level: 1 })).toHaveText("work-buddy");
  const logo = brand.locator(".header__brand-logo");
  await expect(logo).toBeVisible();
  const mark = await logo.evaluate((element) => {
    const style = getComputedStyle(element);
    const box = element as HTMLElement;
    return {
      maskImage: style.maskImage || style.webkitMaskImage,
      backgroundColor: style.backgroundColor,
      width: box.offsetWidth,
      height: box.offsetHeight,
    };
  });
  // The mark is painted through a mask so it takes the shell accent rather
  // than a baked-in brand colour, and the asset must actually resolve.
  expect(mark.maskImage).toMatch(/^url\(".*\.svg"\)$/u);
  expect(mark.backgroundColor).not.toBe("rgba(0, 0, 0, 0)");
  expect(mark.width).toBeGreaterThan(0);
  expect(mark.height).toBeGreaterThan(0);
});

test("the shared loading indicator has stable geometry and honors reduced motion", async ({
  page,
}) => {
  let releaseJournalRequest: (() => void) | undefined;
  const journalRequestGate = new Promise<void>((resolve) => {
    releaseJournalRequest = resolve;
  });
  await page.route("**/api/local-identity/session/csrf", async (route) => {
    await route.fulfill({ status: 401 });
  });
  await page.route("**/api/state", async (route) => {
    await route.fulfill({ json: { status: "running" } });
  });
  await page.route("**/api/events", async (route) => {
    await route.fulfill({
      contentType: "text/event-stream",
      body: "",
    });
  });
  await page.route("**/api/journal/view", async (route) => {
    await journalRequestGate;
    await route.fulfill({
      status: 503,
      json: { error: "test_unavailable" },
    });
  });
  await page.emulateMedia({ reducedMotion: "no-preference" });

  try {
    await page.goto("/app/journal");
    const spinner = page.locator(".wb-widget-state .wb-spinner").first();
    await expect(spinner).toBeVisible();
    const normal = await spinner.evaluate((element) => {
      const style = getComputedStyle(element);
      const box = element as HTMLElement;
      return {
        display: style.display,
        width: box.offsetWidth,
        height: box.offsetHeight,
        animationName: style.animationName,
        animationDuration: style.animationDuration,
        animationIterationCount: style.animationIterationCount,
      };
    });
    // The duration is the point of the assertion: paced from a loop token, a
    // full turn reads as progress. Paced from a transition token it strobes.
    expect(normal).toEqual({
      // An inline box ignores width and height, which collapsed the ring to a
      // sliver. Any non-inline display keeps the declared square.
      display: "block",
      width: 18,
      height: 18,
      animationName: "wb-spin",
      animationDuration: "0.8s",
      animationIterationCount: "infinite",
    });

    await page.emulateMedia({ reducedMotion: "reduce" });
    await expect.poll(() =>
      spinner.evaluate((element) => getComputedStyle(element).animationIterationCount),
    ).toBe("1");
  } finally {
    releaseJournalRequest?.();
  }
});

test("the Journal view supports direct navigation and refresh", async ({ page }) => {
  await page.goto("/app/journal?provider=demo", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("region", { name: "Day Timeline", exact: true })).toBeVisible();

  await page.reload({ waitUntil: "domcontentloaded" });

  await expect(page).toHaveURL(/\/app\/journal\?provider=demo$/);
  await expect(page.getByRole("region", { name: "Running Notes", exact: true })).toBeVisible();
});

test("Quick Capture persists exact text and updates bound sibling input through the provider", async ({
  page,
}) => {
  await page.goto("/app/journal?provider=demo", { waitUntil: "domcontentloaded" });
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

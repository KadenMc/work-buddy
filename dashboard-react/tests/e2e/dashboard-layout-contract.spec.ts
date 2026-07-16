import { expect, test, type Page } from "@playwright/test";

const openJournal = async (page: Page) => {
  await page.goto("/app/journal");
  await expect(page.getByRole("textbox", { name: "Capture text" })).toBeVisible();
  await expect(page.locator(".wb-dashboard-grid-container")).toHaveAttribute(
    "data-grid-measured",
    "true",
  );
};

test("dashboard canvas remains fluid while page and widget insets survive the reset", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await openJournal(page);

  const grid = page.locator(".wb-dashboard-grid-container");
  const baseWidth = await grid.evaluate((element) => element.getBoundingClientRect().width);
  const insets = await page.getByRole("region", { name: "Quick Capture" }).evaluate((frame) => {
    const host = document.querySelector<HTMLElement>(".wb-view-host")!;
    const header = frame.querySelector<HTMLElement>(".wb-widget-frame__header")!;
    const content = frame.querySelector<HTMLElement>(".wb-widget-frame__content")!;
    const textarea = frame.querySelector<HTMLElement>(".wb-textarea")!;
    return {
      hostMaxWidth: getComputedStyle(host).maxWidth,
      hostPaddingLeft: Number.parseFloat(getComputedStyle(host).paddingLeft),
      headerPaddingLeft: Number.parseFloat(getComputedStyle(header).paddingLeft),
      contentPaddingLeft: Number.parseFloat(getComputedStyle(content).paddingLeft),
      textareaPaddingLeft: Number.parseFloat(getComputedStyle(textarea).paddingLeft),
    };
  });

  expect(insets.hostMaxWidth).toBe("none");
  expect(insets.hostPaddingLeft).toBeGreaterThanOrEqual(16);
  expect(insets.headerPaddingLeft).toBeGreaterThanOrEqual(12);
  expect(insets.contentPaddingLeft).toBeGreaterThanOrEqual(12);
  expect(insets.textareaPaddingLeft).toBeGreaterThanOrEqual(12);

  await page.setViewportSize({ width: 1920, height: 900 });
  await expect
    .poll(() => grid.evaluate((element) => element.getBoundingClientRect().width))
    .toBeGreaterThan(baseWidth + 500);

  await expect
    .poll(() =>
      page.evaluate(() => {
        const gridRight = document
          .querySelector<HTMLElement>(".wb-dashboard-grid-container")!
          .getBoundingClientRect().right;
        const itemRight = document
          .querySelector<HTMLElement>('[data-widget-instance-id="default:timeline"]')!
          .getBoundingClientRect().right;
        return Math.abs(gridRight - itemRight);
      }),
    )
    .toBeLessThan(1.5);

  const wideGeometry = await page.evaluate(() => {
    const gridRect = document
      .querySelector<HTMLElement>(".wb-dashboard-grid-container")!
      .getBoundingClientRect();
    const rightmost = document
      .querySelector<HTMLElement>('[data-widget-instance-id="default:timeline"]')!
      .getBoundingClientRect();
    return {
      gridLeft: gridRect.left,
      gridRight: gridRect.right,
      gridWidth: gridRect.width,
      rightmostEdge: rightmost.right,
      viewportWidth: window.innerWidth,
    };
  });
  expect(wideGeometry.gridWidth).toBeGreaterThan(1800);
  expect(wideGeometry.gridLeft).toBeGreaterThanOrEqual(16);
  expect(wideGeometry.viewportWidth - wideGeometry.gridRight).toBeGreaterThanOrEqual(16);
  expect(Math.abs(wideGeometry.rightmostEdge - wideGeometry.gridRight)).toBeLessThan(1.5);
});

test("Customize mode explains placement policy and uses an engine-aligned guide", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await openJournal(page);
  await page.getByRole("button", { name: "Customize view" }).click();

  await expect(
    page.getByText("24 columns · gaps allowed · no overlap · resize from any edge"),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Tidy upward" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Restore view defaults" })).toBeVisible();
  await expect(page.locator(".wb-dashboard-grid-guide")).toHaveCount(1);
  await expect(page.locator(".wb-dashboard-grid-guide rect")).toHaveCount(24 * 20);

  const timelineHandle = page.locator(
    '[data-widget-instance-id="default:timeline"] .wb-widget-drag-handle',
  );
  const handleBox = await timelineHandle.boundingBox();
  expect(handleBox).not.toBeNull();
  await page.mouse.move(handleBox!.x + handleBox!.width / 2, handleBox!.y + handleBox!.height / 2);
  await page.mouse.down();
  await page.mouse.move(handleBox!.x + 180, handleBox!.y + handleBox!.height / 2, {
    steps: 5,
  });
  await page.mouse.up();
  await expect(page.getByRole("status").filter({ hasText: "Placement unchanged" })).toContainText(
    "Empty space is allowed",
  );
});

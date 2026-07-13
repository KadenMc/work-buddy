import { expect, test } from "@playwright/test";

import {
  beginCustomize,
  openJournal,
  openWidgetMenu,
  readPersonalization,
  widget,
} from "./helpers";

test("required widget menus protect view purpose while optional widgets remain hideable", async ({
  page,
}) => {
  await openJournal(page);
  await beginCustomize(page);

  const captureMenu = await openWidgetMenu(page, "Quick Capture");
  await expect(captureMenu.getByRole("menuitem", { name: "Hide" })).toHaveAttribute("aria-disabled", "true");
  await expect(captureMenu.getByRole("menuitem", { name: "Remove" })).toHaveAttribute("aria-disabled", "true");
  await expect(page.getByText(/required|cannot record/i)).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(captureMenu).toBeHidden();

  const notesMenu = await openWidgetMenu(page, "Running Notes");
  await expect(notesMenu.getByRole("menuitem", { name: "Hide" })).not.toHaveAttribute("aria-disabled", "true");
  await expect(notesMenu.getByRole("menuitem", { name: "Remove" })).not.toHaveAttribute("aria-disabled", "true");
  await notesMenu.getByRole("menuitem", { name: "Hide" }).click();
  await expect(widget(page, "Running Notes")).toHaveCount(0);

  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(widget(page, "Running Notes")).toBeVisible();
  expect(await readPersonalization(page)).toBeNull();
});

test("keyboard-opened menus provide move and resize alternatives and reject collisions", async ({
  page,
}) => {
  await openJournal(page);
  await beginCustomize(page);

  let timelineMenu = await openWidgetMenu(page, "Day Timeline", true);
  await timelineMenu.getByRole("menuitem", { name: "down", exact: true }).click();
  await expect(page.getByRole("button", { name: "Done" })).toBeEnabled();
  await expect(page.locator("[aria-live='polite']")).toContainText("Widget moved");

  timelineMenu = await openWidgetMenu(page, "Day Timeline", true);
  await timelineMenu.getByRole("menuitem", { name: "Taller" }).click();
  await expect(page.locator("[aria-live='polite']")).toContainText("Widget resized");

  const captureMenu = await openWidgetMenu(page, "Quick Capture");
  await captureMenu.getByRole("menuitem", { name: "Taller" }).click();
  await expect(page.getByRole("status").filter({ hasText: "collision" })).toBeVisible();

  await page.getByRole("button", { name: "Cancel" }).click();
});

test("pointer drag and resize preserve unrelated widget geometry", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 1800 });
  await openJournal(page);
  await beginCustomize(page);

  const notes = widget(page, "Running Notes");
  const timeline = widget(page, "Day Timeline");
  const notesGridItem = page.locator(
    '.wb-dashboard-grid-item[data-widget-instance-id="default:running-notes"]',
  );
  const beforeNotes = await notes.boundingBox();
  const beforeTimeline = await timeline.boundingBox();
  const dragHandle = notesGridItem.locator(".wb-widget-drag-handle");
  const handle = await dragHandle.boundingBox();
  const grid = page.locator(".wb-dashboard-grid-container");
  const gridBox = await grid.boundingBox();
  expect(beforeNotes).not.toBeNull();
  expect(beforeTimeline).not.toBeNull();
  expect(handle).not.toBeNull();
  expect(gridBox).not.toBeNull();
  if (beforeNotes === null || beforeTimeline === null || handle === null || gridBox === null) return;
  expect(
    await page.evaluate(
      ({ x, y }) =>
        document.elementFromPoint(x, y)?.closest(".wb-widget-drag-handle") !== null,
      { x: handle.x + handle.width / 2, y: handle.y + handle.height / 2 },
    ),
  ).toBe(true);

  const dragX = handle.x + handle.width / 2;
  const dragY = handle.y + handle.height / 2;
  await page.mouse.move(dragX, dragY);
  await page.mouse.down();
  // Move Notes completely below the 16-row Timeline
  // before widening it; otherwise collision rejection may correctly veto
  // the resize depending on which grid row the drag snaps to.
  await page.mouse.move(dragX, dragY + 396, { steps: 24 });
  const placeholder = page.locator(".react-grid-placeholder");
  await expect(placeholder).toBeVisible();
  const placeholderStyle = await placeholder.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      backgroundColor: style.backgroundColor,
      borderRadius: style.borderRadius,
      borderStyle: style.borderStyle,
    };
  });
  expect(placeholderStyle.backgroundColor).not.toBe("rgb(255, 0, 0)");
  expect(placeholderStyle.borderRadius).not.toBe("0px");
  expect(placeholderStyle.borderStyle).toBe("dashed");
  await page.mouse.up();

  const movedNotes = await notes.boundingBox();
  const unchangedTimeline = await timeline.boundingBox();
  expect(movedNotes?.y).toBeGreaterThan(beforeNotes.y + 300);
  expect(unchangedTimeline).toMatchObject({
    x: beforeTimeline.x,
    y: beforeTimeline.y,
    width: beforeTimeline.width,
    height: beforeTimeline.height,
  });

  const resizeHandleLocator = notesGridItem.locator(".react-resizable-handle-se");
  const resizeHandle = await resizeHandleLocator.boundingBox();
  expect(resizeHandle).not.toBeNull();
  if (resizeHandle === null || movedNotes === null) return;
  expect(
    await page.evaluate(
      ({ x, y }) => document.elementFromPoint(x, y)?.className ?? "",
      {
        x: resizeHandle.x + resizeHandle.width / 2,
        y: resizeHandle.y + resizeHandle.height / 2,
      },
    ),
  ).toContain("react-resizable-handle-se");
  const resizeX = resizeHandle.x + resizeHandle.width / 2;
  const resizeY = resizeHandle.y + resizeHandle.height / 2;
  await page.mouse.move(resizeX, resizeY);
  await page.mouse.down();
  await page.mouse.move(resizeX + 70, resizeY + 90, { steps: 16 });
  await page.mouse.up();

  const resizedNotes = await notes.boundingBox();
  expect(resizedNotes?.width).toBeGreaterThan(movedNotes.width);
  expect(resizedNotes?.height).toBeGreaterThan(movedNotes.height);
  await page.getByRole("button", { name: "Cancel" }).click();
});

test("undo, cancel, done, reload, and reset preserve the personal-patch lifecycle", async ({
  page,
}) => {
  await openJournal(page);
  await beginCustomize(page);

  let timelineMenu = await openWidgetMenu(page, "Day Timeline");
  await timelineMenu.getByRole("menuitem", { name: "down", exact: true }).click();
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.getByRole("button", { name: "Done" })).toBeDisabled();

  timelineMenu = await openWidgetMenu(page, "Day Timeline");
  await timelineMenu.getByRole("menuitem", { name: "down", exact: true }).click();
  await page.getByRole("button", { name: "Cancel" }).click();
  expect(await readPersonalization(page)).toBeNull();

  await beginCustomize(page);
  timelineMenu = await openWidgetMenu(page, "Day Timeline");
  await timelineMenu.getByRole("menuitem", { name: "down", exact: true }).click();
  await page.getByRole("button", { name: "Done" }).click();

  let patch = await readPersonalization(page);
  expect(patch).not.toBeNull();
  expect(patch?.defaultSlotOverrides).toMatchObject({
    timeline: { layout: { y: 1 } },
  });

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(widget(page, "Day Timeline")).toBeVisible();
  patch = await readPersonalization(page);
  expect(patch?.defaultSlotOverrides).toMatchObject({
    timeline: { layout: { y: 1 } },
  });

  await beginCustomize(page);
  await page.getByRole("button", { name: "Reset" }).click();
  await page.getByRole("button", { name: "Done" }).click();
  patch = await readPersonalization(page);
  expect(patch).toBeNull();
});

test("desktop drag-and-drop persists canonical mobile DOM order", async ({ page }) => {
  await openJournal(page);
  await beginCustomize(page);
  await page.getByRole("button", { name: "Mobile order" }).click();
  await expect(page.getByText("Earlier")).toHaveCount(0);
  await expect(page.getByText("Later")).toHaveCount(0);
  const timelineRow = page.getByRole("row", { name: "Day Timeline" });
  const captureRow = page.getByRole("row", { name: "Quick Capture" });
  const captureBox = await captureRow.boundingBox();
  expect(captureBox).not.toBeNull();
  if (captureBox === null) return;
  await timelineRow.dragTo(captureRow, {
    targetPosition: { x: captureBox.width / 2, y: 2 },
  });
  await page.getByRole("button", { name: "Done" }).click();

  const patch = await readPersonalization(page);
  expect(patch?.mobileOrderOverride).toEqual([
    "default:timeline",
    "default:capture",
    "default:running-notes",
  ]);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator(".react-grid-layout")).toHaveCount(0);
  await expect(page.locator(".wb-dashboard-mobile-stack .wb-widget-frame__title")).toHaveText([
    "Day Timeline",
    "Quick Capture",
    "Running Notes",
  ]);
});

test("resizing Quick Capture cannot remove its shared scroll boundary", async ({ page }) => {
  await openJournal(page);
  await beginCustomize(page);

  for (let index = 0; index < 8; index += 1) {
    const captureMenu = await openWidgetMenu(page, "Quick Capture");
    const shorter = captureMenu.getByRole("menuitem", { name: "Shorter" });
    if ((await shorter.getAttribute("aria-disabled")) === "true") {
      await page.keyboard.press("Escape");
      break;
    }
    await shorter.click();
  }

  const content = widget(page, "Quick Capture").locator(".wb-widget-frame__content");
  const metrics = await content.evaluate((element) => {
    const style = getComputedStyle(element);
    element.scrollTop = element.scrollHeight;
    return {
      overflowY: style.overflowY,
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      scrollTop: element.scrollTop,
    };
  });
  expect(metrics.overflowY).toBe("auto");
  expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight);
  expect(metrics.scrollTop).toBeGreaterThan(0);
  await page.getByRole("button", { name: "Cancel" }).click();
});

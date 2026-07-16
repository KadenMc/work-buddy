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
  await expect(page.getByText(/\d+ × \d+ grid units/)).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Actions for Quick Capture" }),
  ).toHaveCount(0);
  await beginCustomize(page);

  await expect(widget(page, "Quick Capture").getByText(/\d+ × \d+ grid units/)).toBeVisible();

  const captureMenu = await openWidgetMenu(page, "Quick Capture");
  await expect(captureMenu.getByRole("menuitem")).toHaveCount(2);
  await expect(captureMenu.getByRole("menuitem", { name: "Retry" })).toHaveCount(0);
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

test("the focused drag handle provides keyboard layout control and friendly rejection feedback", async ({
  page,
}) => {
  await openJournal(page);
  await beginCustomize(page);

  const timelineHandle = page
    .locator('[data-widget-instance-id="default:timeline"]')
    .getByRole("button", { name: /Move or resize widget/ });
  await timelineHandle.focus();
  await timelineHandle.press("ArrowDown");
  await expect(page.getByRole("button", { name: "Done" })).toBeEnabled();
  await expect(page.locator("[aria-live='polite']")).toContainText("Widget moved");

  await timelineHandle.press("Shift+ArrowDown");
  await expect(page.locator("[aria-live='polite']")).toContainText("Widget resized");

  const captureHandle = page
    .locator('[data-widget-instance-id="default:capture"]')
    .getByRole("button", { name: /Move or resize widget/ });
  await captureHandle.focus();
  await captureHandle.press("Shift+ArrowDown");
  await expect(
    page.getByRole("status").filter({ hasText: "would overlap another widget" }),
  ).toBeVisible();
  await expect(page.getByText("collision", { exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "Cancel" }).click();
});

test("Arrange is inert while interaction Preview is functional and disposable", async ({ page }) => {
  await openJournal(page);
  await beginCustomize(page);

  const capture = widget(page, "Quick Capture");
  await expect(capture.locator(".wb-widget-frame__content")).toHaveAttribute("inert", "");
  await expect(capture.getByText("Interactions paused while arranging")).toBeVisible();

  await page.getByRole("button", { name: "Preview interactions" }).click();
  await expect(page.locator(".wb-view-toolbar__mode")).toContainText(
    "Previewing interactions",
  );
  await expect(capture.locator(".wb-widget-frame__content")).not.toHaveAttribute("inert", "");
  await expect(page.locator(".wb-widget-resize-handle")).toHaveCount(0);

  const previewText = "This must remain inside the disposable preview";
  const previewInput = capture.getByRole("textbox", { name: "Capture text" });
  await previewInput.fill(previewText);
  await previewInput.press("Control+Enter");
  await expect(previewInput).toHaveValue(previewText);
  await expect(
    page.getByText(/Quick Capture did not run that action in Preview/i),
  ).toBeVisible();

  const timeline = widget(page, "Day Timeline");
  await timeline.getByText("List", { exact: true }).click();
  await expect(timeline.getByRole("radio", { name: "List" })).toBeChecked();
  await expect(page.getByText(/Day Timeline previewed that action locally/i)).toBeVisible();

  await page.getByRole("button", { name: "Back to arranging" }).click();
  await expect(capture.locator(".wb-widget-frame__content")).toHaveAttribute("inert", "");
  await page.getByRole("button", { name: "Cancel" }).click();

  await expect(widget(page, "Quick Capture").getByRole("textbox", { name: "Capture text" })).toHaveValue("");
  await expect(widget(page, "Day Timeline").getByRole("radio", { name: "Timeline" })).toBeChecked();
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
  await resizeHandleLocator.scrollIntoViewIfNeeded();
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

  const timelineHandle = page
    .locator('[data-widget-instance-id="default:timeline"]')
    .getByRole("button", { name: /Move or resize widget/ });
  await timelineHandle.press("ArrowDown");
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.getByRole("button", { name: "Done" })).toBeDisabled();

  await timelineHandle.press("ArrowDown");
  await page.getByRole("button", { name: "Cancel" }).click();
  expect(await readPersonalization(page)).toBeNull();

  await beginCustomize(page);
  await timelineHandle.press("ArrowDown");
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
  await page.getByRole("button", { name: "Restore view defaults" }).click();
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

test("resize handles are Customize-only and every card edge has a usable constraint", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openJournal(page);
  await expect(page.locator(".wb-widget-resize-handle")).toHaveCount(0);

  await beginCustomize(page);
  const timeline = page.locator('[data-widget-instance-id="default:timeline"]');
  await expect(timeline.locator(".wb-widget-resize-handle")).toHaveCount(8);
  const handleAxes = await timeline
    .locator(".wb-widget-resize-handle")
    .evaluateAll((handles) =>
      handles.map((handle) => handle.getAttribute("data-wb-resize-axis")),
    );
  expect(handleAxes).toEqual(["n", "e", "s", "w", "ne", "nw", "se", "sw"]);

  const before = await timeline.boundingBox();
  const westHandle = timeline.locator('[data-wb-resize-axis="w"]');
  await westHandle.scrollIntoViewIfNeeded();
  const westBox = await westHandle.boundingBox();
  expect(before).not.toBeNull();
  expect(westBox).not.toBeNull();
  if (before === null || westBox === null) return;

  const westX = westBox.x + westBox.width / 2;
  const westY = westBox.y + westBox.height / 2;
  await page.mouse.move(westX, westY);
  await page.mouse.down();
  await page.mouse.move(westX + 400, westY);
  await page.mouse.up();

  await expect(timeline).toBeVisible();
  await expect(timeline).not.toHaveClass(/resizing/);
  await expect(page.locator(".react-grid-placeholder")).toHaveCount(0);
  const atMinimum = await timeline.boundingBox();
  expect(atMinimum).not.toBeNull();
  expect(atMinimum?.x).toBeGreaterThan(before.x + 150);
  expect(atMinimum?.width).toBeLessThan(before.width - 150);

  const constrainedWestHandle = timeline.locator('[data-wb-resize-axis="w"]');
  const constrainedWestBox = await constrainedWestHandle.boundingBox();
  expect(constrainedWestBox).not.toBeNull();
  if (constrainedWestBox === null || atMinimum === null) return;
  const constrainedX = constrainedWestBox.x + constrainedWestBox.width / 2;
  const constrainedY = constrainedWestBox.y + constrainedWestBox.height / 2;
  await page.mouse.move(constrainedX, constrainedY);
  await page.mouse.down();
  await page.mouse.move(constrainedX + 120, constrainedY);
  await page.mouse.up();
  await expect(timeline).not.toHaveClass(/resizing/);

  await expect(page.getByRole("status").filter({ hasText: "Size unchanged for Day Timeline" }))
    .toContainText("Allowed size: 12×8–24×24 grid units");
  await expect(page.getByRole("status").filter({ hasText: "Size unchanged for Day Timeline" }))
    .toContainText("24-column canvas");
  await expect(page.getByRole("status").filter({ hasText: "Size unchanged for Day Timeline" }))
    .toContainText("Empty space is allowed");

  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator(".wb-widget-resize-handle")).toHaveCount(0);
});

test("an interrupted resize cannot survive pointer loss or Customize exit", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openJournal(page);
  await beginCustomize(page);

  const capture = page.locator('[data-widget-instance-id="default:capture"]');
  const resizeHandle = capture.locator(".react-resizable-handle-se");
  await resizeHandle.scrollIntoViewIfNeeded();
  const handleBox = await resizeHandle.boundingBox();
  expect(handleBox).not.toBeNull();
  if (handleBox === null) return;

  const startX = handleBox.x + handleBox.width / 2;
  const startY = handleBox.y + handleBox.height / 2;
  await page.mouse.move(startX, startY);
  await page.mouse.down();
  await page.mouse.move(startX + 120, startY + 80, { steps: 12 });
  await expect(capture).toHaveClass(/resizing/);
  await expect(page.locator(".react-grid-placeholder")).toHaveCount(1);

  await page.evaluate(() => window.dispatchEvent(new Event("blur")));
  await expect(capture).not.toHaveClass(/resizing/);
  await expect(page.locator(".react-grid-placeholder")).toHaveCount(0);
  await expect(page.getByText(/Resize canceled because the pointer interaction ended/)).toBeVisible();
  await page.mouse.up();

  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator(".react-grid-placeholder")).toHaveCount(0);
  await expect(page.locator(".react-grid-item.resizing")).toHaveCount(0);
});

test("resizing Quick Capture cannot remove its shared scroll boundary", async ({ page }) => {
  await openJournal(page);
  await beginCustomize(page);

  const captureHandle = page
    .locator('[data-widget-instance-id="default:capture"]')
    .getByRole("button", { name: /Move or resize widget/ });
  for (let index = 0; index < 8; index += 1) {
    await captureHandle.press("Shift+ArrowUp");
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

test("wheel gestures over a fitting widget continue scrolling the page", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 500 });
  await openJournal(page);

  const widgetContents = page.locator(".wb-dashboard-grid-item .wb-widget-frame__content");
  const fittingWidgetIndex = await widgetContents.evaluateAll((elements) =>
    elements.findIndex((element) => element.scrollHeight <= element.clientHeight + 1),
  );
  expect(fittingWidgetIndex).toBeGreaterThanOrEqual(0);

  const fittingWidget = widgetContents.nth(fittingWidgetIndex);
  await fittingWidget.scrollIntoViewIfNeeded();
  await fittingWidget.hover();
  const before = await page.evaluate(() => window.scrollY);
  const wheelDelta = before > 0 ? -400 : 400;
  await page.mouse.wheel(0, wheelDelta);

  if (wheelDelta > 0) {
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(before);
  } else {
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBeLessThan(before);
  }
});

test("a scrollable widget owns available movement and exposes the native boundary policy", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 500 });
  await openJournal(page);
  await beginCustomize(page);

  const captureHandle = page
    .locator('[data-widget-instance-id="default:capture"]')
    .getByRole("button", { name: /Move or resize widget/ });
  for (let index = 0; index < 8; index += 1) {
    await captureHandle.press("Shift+ArrowUp");
  }
  const arrangedContent = widget(page, "Quick Capture").locator(
    ".wb-widget-frame__content",
  );
  const arrangeScrollMetrics = await arrangedContent.evaluate((element) => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
  }));
  expect(arrangeScrollMetrics.scrollHeight).toBeGreaterThan(
    arrangeScrollMetrics.clientHeight,
  );
  await page.getByRole("button", { name: "Preview interactions" }).click();

  const content = widget(page, "Quick Capture").locator(".wb-widget-frame__content");
  await content.scrollIntoViewIfNeeded();
  await content.evaluate((element) => {
    element.scrollTop = 0;
  });
  const previewScrollMetrics = await content.evaluate((element) => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
  }));
  expect(previewScrollMetrics.scrollHeight).toBeGreaterThan(
    previewScrollMetrics.clientHeight,
  );
  await content.hover();

  const pageBeforeInternalScroll = await page.evaluate(() => window.scrollY);
  await page.mouse.wheel(0, 80);
  await expect.poll(() => content.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
  expect(await page.evaluate(() => window.scrollY)).toBe(pageBeforeInternalScroll);

  const boundary = await content.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
    return {
      scrollTop: element.scrollTop,
      maximumScrollTop: element.scrollHeight - element.clientHeight,
      overscrollBehaviorY: getComputedStyle(element).overscrollBehaviorY,
      policy: element.getAttribute("data-scroll-boundary-policy"),
    };
  });
  expect(boundary.scrollTop).toBe(boundary.maximumScrollTop);
  expect(boundary.overscrollBehaviorY).toBe("auto");
  expect(boundary.policy).toBe("native");
});

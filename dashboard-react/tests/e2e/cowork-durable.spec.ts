import { expect, test } from "@playwright/test";

import { openCowork } from "./cowork-helpers";

/**
 * The durable-widget proof in a real browser. The Co-work workspace is one composite durable
 * card, so the keep-alive host keeps its live element mounted across a customize round-trip
 * instead of re-hydrating it from a snapshot. Playwright cannot compare a DOM node's identity
 * across execution contexts, so this asserts the observable consequences instead: text typed
 * into the live editor survives the round-trip, the element stays inside the durable slot and
 * is never made inert while the grid is arranged, and Done persists the layout under the
 * Co-work view's personalization key.
 */

const COWORK_PERSONALIZATION_KEY =
  "work-buddy.dashboard.personalization.v1:wb.cowork.workspace";

const EDITOR_MARKER = "durable-marker-7f3a";

test("the Co-work workspace stays live through a navbar customize round-trip", async ({
  page,
}) => {
  await openCowork(page);

  // The live editor renders inside the single durable slot the keep-alive host owns.
  const slot = page.locator(".wb-durable-slot");
  await expect(slot).toHaveCount(1);
  await expect(slot.locator(".ProseMirror")).toHaveCount(1);

  // Type a marker at the end of the document. It rides the live editor state, not a snapshot.
  const editor = page.getByRole("textbox", { name: "Document editor" });
  await editor.click();
  await page.keyboard.press("Control+End");
  await page.keyboard.type(` ${EDITOR_MARKER}`);
  await expect(editor).toContainText(EDITOR_MARKER);

  // Enter customize from the navbar entry control.
  await page.getByRole("button", { name: "Customize view" }).click();
  await expect(page.locator(".wb-view-host")).toHaveClass(/is-customizing/);

  // A durable widget skips the arrange inert shield by design, so the editor stays usable
  // while the grid is rearranged and never leaves the durable slot.
  await expect(slot.locator(".ProseMirror")).toHaveCount(1);
  await expect(
    page.locator(".wb-durable-slot .wb-widget-frame__content"),
  ).not.toHaveAttribute("inert", "");

  // Drag the card by its handle far enough to move it, so Done has a layout change to save.
  const handle = page
    .locator('.wb-dashboard-grid-item[data-widget-instance-id="wb-cowork:workspace"]')
    .locator(".wb-widget-drag-handle");
  const handleBox = await handle.boundingBox();
  expect(handleBox).not.toBeNull();
  if (handleBox === null) return;
  const dragX = handleBox.x + handleBox.width / 2;
  const dragY = handleBox.y + handleBox.height / 2;
  await page.mouse.move(dragX, dragY);
  await page.mouse.down();
  await page.mouse.move(dragX, dragY + 220, { steps: 24 });
  await expect(page.locator(".react-grid-placeholder")).toBeVisible();
  await page.mouse.up();

  await page.getByRole("button", { name: "Done" }).click();
  await expect(page.locator(".wb-view-host")).not.toHaveClass(/is-customizing/);

  // The same live element carried the typed marker through the whole round-trip, and the
  // durable slot still holds exactly one editor.
  await expect(page.getByRole("textbox", { name: "Document editor" })).toContainText(
    EDITOR_MARKER,
  );
  await expect(page.locator(".wb-durable-slot")).toHaveCount(1);

  // Done wrote the personalization patch under the Co-work view id.
  const stored = await page.evaluate(
    (key) => localStorage.getItem(key),
    COWORK_PERSONALIZATION_KEY,
  );
  expect(stored).not.toBeNull();
});

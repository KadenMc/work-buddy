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
  await expect(captureMenu.getByRole("button", { name: "Hide" })).toBeDisabled();
  await expect(captureMenu.getByRole("button", { name: "Remove" })).toBeDisabled();
  await expect(captureMenu).toContainText(/required|cannot record/i);

  const notesMenu = await openWidgetMenu(page, "Running Notes");
  await expect(notesMenu.getByRole("button", { name: "Hide" })).toBeEnabled();
  await expect(notesMenu.getByRole("button", { name: "Remove" })).toBeEnabled();
  await notesMenu.getByRole("button", { name: "Hide" }).click();
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

  const timelineMenu = await openWidgetMenu(page, "Day Timeline", true);
  await timelineMenu.getByRole("button", { name: "down", exact: true }).click();
  await expect(page.getByRole("button", { name: "Done" })).toBeEnabled();
  await expect(page.locator("[aria-live='polite']")).toContainText("Widget moved");

  await timelineMenu.getByRole("button", { name: "Taller" }).click();
  await expect(page.locator("[aria-live='polite']")).toContainText("Widget resized");

  const captureMenu = await openWidgetMenu(page, "Quick Capture");
  await captureMenu.getByRole("button", { name: "Taller" }).click();
  await expect(page.getByRole("status").filter({ hasText: "collision" })).toBeVisible();

  await page.getByRole("button", { name: "Cancel" }).click();
});

test("undo, cancel, done, reload, and reset preserve the personal-patch lifecycle", async ({
  page,
}) => {
  await openJournal(page);
  await beginCustomize(page);

  let timelineMenu = await openWidgetMenu(page, "Day Timeline");
  await timelineMenu.getByRole("button", { name: "down", exact: true }).click();
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.getByRole("button", { name: "Done" })).toBeDisabled();

  await timelineMenu.getByRole("button", { name: "down", exact: true }).click();
  await page.getByRole("button", { name: "Cancel" }).click();
  expect(await readPersonalization(page)).toBeNull();

  await beginCustomize(page);
  timelineMenu = await openWidgetMenu(page, "Day Timeline");
  await timelineMenu.getByRole("button", { name: "down", exact: true }).click();
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
  expect(patch?.defaultSlotOverrides).toEqual({});
});

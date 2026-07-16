import { expect, test } from "@playwright/test";

import { openJournal, widget } from "./helpers";

test.setTimeout(120_000);

test("Running Notes supports edit, cancel-safe deletion, and active-list removal", async ({
  page,
}) => {
  await openJournal(page);
  const notes = widget(page, "Running Notes");
  const original = "Prototype mobile timeline edge case";
  const revised = "Prototype mobile timeline edge case — revised in place";

  await notes.getByRole("button", { name: "Edit" }).click();
  await notes.getByRole("textbox", { name: "Edit note" }).fill(revised);
  await notes.getByRole("button", { name: "Save" }).click();
  await expect(notes.getByText(revised, { exact: true })).toBeVisible();
  await expect(notes.getByText(original, { exact: true })).toHaveCount(0);

  await notes.getByRole("button", { name: "Delete" }).click();
  const firstDialog = page.getByRole("alertdialog", {
    name: "Delete this running note?",
  });
  await expect(firstDialog).toContainText(/keeps a tombstone/i);
  await firstDialog.getByRole("button", { name: "Keep note" }).click();
  await expect(notes.getByText(revised, { exact: true })).toBeVisible();

  await notes.getByRole("button", { name: "Delete" }).click();
  await page
    .getByRole("alertdialog", { name: "Delete this running note?" })
    .getByRole("button", { name: "Delete note" })
    .click();
  await expect(notes.getByText(revised, { exact: true })).toHaveCount(0);
  await expect(notes).toContainText("No running notes for this collection.");
});

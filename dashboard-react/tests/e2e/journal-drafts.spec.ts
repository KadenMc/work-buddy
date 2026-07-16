import { expect, test } from "@playwright/test";

import { beginCustomize, openJournal, openWidgetMenu, widget } from "./helpers";

test.setTimeout(120_000);

test("Quick Capture restores its exact semantic draft and exposes a truthful clear action", async ({
  page,
}) => {
  await openJournal(page);
  const capture = widget(page, "Quick Capture");
  const text = "  Preserve this exact unfinished capture.  ";
  await capture.getByRole("textbox", { name: "Capture text" }).fill(text);
  const smart = capture.getByRole("switch", { name: "Smart" });
  if (!(await smart.isChecked())) await smart.click();

  await expect(capture.getByRole("button", { name: "Clear Quick Capture draft" })).toBeVisible();
  await page.reload({ waitUntil: "domcontentloaded" });
  await openJournal(page);
  await expect(
    widget(page, "Quick Capture").getByRole("textbox", { name: "Capture text" }),
  ).toHaveValue(text);
  await expect(widget(page, "Quick Capture").getByRole("switch", { name: "Smart" })).toBeChecked();

  await widget(page, "Quick Capture")
    .getByRole("button", { name: "Clear Quick Capture draft" })
    .click();
  const dialog = page.getByRole("alertdialog", { name: "Clear this draft?" });
  await expect(dialog).toContainText("Saved items, widget settings, and the view layout will not be affected");
  await dialog.getByRole("button", { name: "Keep draft" }).click();
  await expect(widget(page, "Quick Capture").getByRole("textbox", { name: "Capture text" })).toHaveValue(text);
  await expect(
    widget(page, "Quick Capture").getByRole("button", { name: "Clear Quick Capture draft" }),
  ).toBeFocused();

  await widget(page, "Quick Capture")
    .getByRole("button", { name: "Clear Quick Capture draft" })
    .click();
  await page.getByRole("alertdialog", { name: "Clear this draft?" }).getByRole("button", { name: "Clear draft" }).click();
  await expect(widget(page, "Quick Capture").getByRole("textbox", { name: "Capture text" })).toHaveValue("");
  await expect(widget(page, "Quick Capture").getByRole("textbox", { name: "Capture text" })).toBeFocused();
  await expect(page.getByText("Quick Capture draft cleared.")).toBeVisible();
  await expect(widget(page, "Quick Capture").getByRole("button", { name: "Clear Quick Capture draft" })).toHaveCount(0);

  await page.reload({ waitUntil: "domcontentloaded" });
  await openJournal(page);
  await expect(widget(page, "Quick Capture").getByRole("textbox", { name: "Capture text" })).toHaveValue("");
});

test("Journal drafts survive Customize remounts and Running Notes edits survive refresh", async ({
  page,
}) => {
  await openJournal(page);
  const captureText = widget(page, "Quick Capture").getByRole("textbox", {
    name: "Capture text",
  });
  await captureText.fill("survives layout editing");
  await beginCustomize(page);
  await expect(widget(page, "Quick Capture").getByRole("textbox", { name: "Capture text" })).toHaveValue(
    "survives layout editing",
  );
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(widget(page, "Quick Capture").getByRole("textbox", { name: "Capture text" })).toHaveValue(
    "survives layout editing",
  );
  await page.setViewportSize({ width: 700, height: 900 });
  await expect(widget(page, "Quick Capture").getByRole("textbox", { name: "Capture text" })).toHaveValue(
    "survives layout editing",
  );
  await page.setViewportSize({ width: 1280, height: 900 });
  await expect(widget(page, "Quick Capture").getByRole("textbox", { name: "Capture text" })).toHaveValue(
    "survives layout editing",
  );

  const notes = widget(page, "Running Notes");
  await notes.getByRole("button", { name: "Edit" }).first().click();
  const exactMarkdown = "  Unfinished **Markdown** survives.  ";
  await notes.getByRole("textbox", { name: "Edit note" }).fill(exactMarkdown);
  await expect(notes.getByRole("button", { name: "Clear Running Notes draft" })).toBeVisible();
  await beginCustomize(page);
  const notesMenu = await openWidgetMenu(page, "Running Notes");
  await notesMenu.getByRole("menuitem", { name: "Hide" }).click();
  await expect(widget(page, "Running Notes")).toHaveCount(0);
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(widget(page, "Running Notes").getByRole("textbox", { name: "Edit note" })).toHaveValue(
    exactMarkdown,
  );

  await page.reload({ waitUntil: "domcontentloaded" });
  await openJournal(page);
  await expect(widget(page, "Running Notes").getByRole("textbox", { name: "Edit note" })).toHaveValue(
    exactMarkdown,
  );
  await widget(page, "Running Notes").getByRole("button", { name: "Cancel" }).click();
  await expect(widget(page, "Running Notes").getByRole("textbox", { name: "Edit note" })).toHaveCount(0);
});

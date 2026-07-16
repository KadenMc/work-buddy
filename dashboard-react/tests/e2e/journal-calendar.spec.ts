import { expect, test } from "@playwright/test";

import { openJournal, widget } from "./helpers";

test("Journal uses the calendar surface and Log capture creates a point record", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 1000 });
  await openJournal(page);

  const timeline = widget(page, "Day Timeline");
  const calendar = timeline.getByRole("region", {
    name: "Calendar surface for 2026-07-11",
  });
  await expect(calendar).toBeVisible();
  await expect(calendar).toHaveAttribute("data-wb-calendar-view", "calendar:day");
  await expect(
    timeline.locator(
      '[data-wb-calendar-item-id="timeline:mobile-edge-capture"]',
    ),
  ).toHaveClass(/wb-calendar-event--point/);

  const capture = widget(page, "Quick Capture");
  const exactText = "Shipped the point-record Journal integration";
  await capture.getByRole("button", { name: /Destination/ }).click();
  await page.getByRole("option", { name: /^Log/ }).click();
  await capture.getByRole("textbox", { name: "Capture text" }).fill(exactText);
  await capture.getByRole("button", { name: "Capture", exact: true }).click();

  await expect(capture.getByRole("textbox", { name: "Capture text" })).toHaveValue("");
  const addedRecord = timeline.locator("[data-wb-calendar-item-id]", {
    has: page.getByText(exactText, { exact: true }),
  });
  await expect(addedRecord).toHaveCount(1);
  await expect(addedRecord).toHaveClass(/wb-calendar-event--point/);
  await expect(addedRecord).toHaveAttribute("aria-label", /12:18 PM, record, observed/);

  await addedRecord.click();
  const inspector = page.getByRole("dialog");
  await expect(inspector).toContainText(exactText);
  await expect(inspector).toContainText("12:18 PM");
  await expect(inspector).toContainText(
    "Records describe observed work and are not rescheduled from the calendar.",
  );
});

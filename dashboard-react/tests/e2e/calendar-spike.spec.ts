import { expect, test, type Page } from "@playwright/test";

async function openCalendarSpike(page: Page) {
  await page.goto("/app/__calendar-spike", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("heading", { name: "FullCalendar surface spike" }),
  ).toBeVisible();
  await expect(page.locator('[data-wb-calendar-surface="fullcalendar"]')).toBeVisible();
}

async function selectFieldOption(page: Page, label: string, option: RegExp) {
  const field = page.locator(".wb-select-field").filter({
    has: page.getByText(label, { exact: true }),
  });
  await field.getByRole("button").click();
  await page.getByRole("option", { name: option }).click();
}

test("keeps one intentional scroller and opens one inspector per activation key", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openCalendarSpike(page);

  const surface = page.locator('[data-wb-calendar-surface="fullcalendar"]');
  const scrollOwner = surface.locator("[data-wb-calendar-scroll-owner]");
  await expect(scrollOwner).toHaveCount(1);
  await expect(scrollOwner).toHaveClass(/fc-scroller-liquid-absolute/);

  const geometry = await scrollOwner.evaluate((element) => {
    const frameContent = element.closest(".wb-widget-frame__content");
    return {
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      frameOverflowY: frameContent ? getComputedStyle(frameContent).overflowY : null,
    };
  });
  expect(geometry.scrollHeight).toBeGreaterThan(geometry.clientHeight);
  expect(geometry.frameOverflowY).toBe("hidden");

  const event = page.locator('[data-wb-calendar-item-id="product-standup"]');
  await event.focus();
  await event.press("Enter");
  await expect(page.getByRole("dialog")).toHaveCount(1);
  await expect(page.getByRole("heading", { name: "Product stand-up" })).toBeVisible();
  await expect(page.getByTestId("calendar-spike-open-count")).toHaveText("0");
  await page.getByRole("button", { name: "Close calendar item details" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await event.focus();
  await event.press("Space");
  await expect(page.getByRole("dialog")).toHaveCount(1);
  await page.getByRole("button", { name: "Open event" }).click();
  await expect(page.getByTestId("calendar-spike-open-count")).toHaveText("1");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("resolves distinct and capability-gated actions for records, plans, and calendars", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openCalendarSpike(page);

  await page.locator('[data-wb-calendar-item-id="mobile-edge-capture"]').click();
  await expect(page.getByRole("button", { name: "Open record" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Go to record source" })).toBeVisible();
  await expect(page.getByText(/Records describe observed work/)).toBeVisible();
  await expect(page.getByRole("button", { name: /Edit/ })).toHaveCount(0);
  await page.getByRole("button", { name: "Close calendar item details" }).click();

  await page.locator('[data-wb-calendar-item-id="prototype-mobile"]').click();
  await expect(page.getByRole("button", { name: "Open plan" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Edit scheduled time" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Change duration" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Remove plan" })).toBeVisible();
  await page.getByRole("button", { name: "Close calendar item details" }).click();

  await page.locator('[data-wb-calendar-item-id="product-standup"]').click();
  await expect(page.getByRole("button", { name: "Open event" })).toBeVisible();
  await expect(page.getByRole("button", { name: "View in source calendar" })).toBeVisible();
  await expect(page.getByText(/Provider editing is not connected/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Edit event time" })).toHaveCount(0);
  await page.getByRole("button", { name: "Close calendar item details" }).click();

  await selectFieldOption(page, "Fixture", /^Mixed sources/);
  await page.locator('[data-wb-calendar-item-id="mixed-editable-calendar"]').click();
  await expect(page.getByRole("button", { name: "Edit event time" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Change duration" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Remove calendar event" })).toBeVisible();
});

test("separates range from presentation and removes the duplicate Agenda mode", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openCalendarSpike(page);

  await expect(page.getByRole("radio", { name: "Agenda" })).toHaveCount(0);
  await page
    .getByRole("radiogroup", { name: "Calendar range" })
    .getByText("Week", { exact: true })
    .click();
  await page
    .getByRole("radiogroup", { name: "Calendar presentation" })
    .getByText("List", { exact: true })
    .click();
  await expect(
    page.locator('[data-wb-calendar-surface="fullcalendar"]'),
  ).toHaveAttribute("data-wb-calendar-view", "list:week");
  await expect(page.locator(".fc-list-day")).toContainText("July 11, 2026");
});

test("uses compact short-event content and FullCalendar's overflow popover", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openCalendarSpike(page);

  const point = page.locator('[data-wb-calendar-item-id="mobile-edge-capture"]');
  await expect(point.locator(".wb-calendar-event__micro")).toBeVisible();
  await expect(point.locator(".wb-calendar-event__full")).toBeHidden();
  const pointGeometry = await point.evaluate((element) => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
  }));
  expect(pointGeometry.scrollHeight).toBeLessThanOrEqual(pointGeometry.clientHeight);

  await selectFieldOption(page, "Fixture", /^Overlapping items/);
  const visibleEvents = page.locator(".fc-timegrid-event[data-wb-calendar-item-id]");
  await expect(visibleEvents).toHaveCount(4);
  const horizontalRanges = await visibleEvents.evaluateAll((elements) =>
    elements
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return { left: rect.left, right: rect.right };
      })
      .sort((left, right) => left.left - right.left),
  );
  for (let index = 1; index < horizontalRanges.length; index += 1) {
    expect(horizontalRanges[index - 1]!.right).toBeLessThanOrEqual(
      horizontalRanges[index]!.left + 1,
    );
  }
  await page.getByText("+1", { exact: true }).click();
  await expect(page.locator(".fc-popover")).toBeVisible();
  await expect(page.locator(".fc-popover")).toContainText("Captured during overlap");
});

test("uses semantic Work Buddy skins and passes the serious axe gate", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openCalendarSpike(page);

  await selectFieldOption(page, "Scheme", /^Light$/);
  await expect(page.locator("html")).toHaveAttribute("data-wb-scheme", "light");
  await selectFieldOption(page, "Skin", /Conformance stress/i);
  await expect(page.locator("html")).toHaveAttribute(
    "data-wb-skin",
    "wb.conformance-stress",
  );

  const surface = page.locator('[data-wb-calendar-surface="fullcalendar"]');
  const semanticColors = await surface.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      surfaceBackground: style.getPropertyValue("--wb-color-surface-inset").trim(),
      calendarBackground: style.getPropertyValue("--fc-page-bg-color").trim(),
      eventBackground: style.getPropertyValue("--fc-event-bg-color").trim(),
    };
  });
  expect(semanticColors.calendarBackground).toBe(semanticColors.surfaceBackground);
  expect(semanticColors.eventBackground).toBeTruthy();

  await page.locator('[data-wb-calendar-item-id="product-standup"]').click();
  await expect(page.getByRole("dialog")).toBeVisible();

  await page.addScriptTag({ path: "node_modules/axe-core/axe.min.js" });
  const violations = await page.evaluate(async () => {
    const axeWindow = window as typeof window & {
      axe: {
        run(
          context: Document,
          options: { resultTypes: readonly string[] },
        ): Promise<{
          violations: readonly { id: string; impact: string | null }[];
        }>;
      };
    };
    const result = await axeWindow.axe.run(document, { resultTypes: ["violations"] });
    return result.violations.filter(
      (violation) =>
        violation.impact === "serious" || violation.impact === "critical",
    );
  });
  expect(violations).toEqual([]);
});

test("updates geometry during dashboard resize and switches to list mode", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openCalendarSpike(page);

  const surface = page.locator('[data-wb-calendar-surface="fullcalendar"]');
  const resizeHandle = page.locator(".react-resizable-handle-se");
  await resizeHandle.scrollIntoViewIfNeeded();
  const beforeCount = Number(
    (await surface.getAttribute("data-wb-calendar-resize-count")) ?? "0",
  );
  const handleBox = await resizeHandle.boundingBox();
  expect(handleBox).not.toBeNull();
  if (handleBox === null) return;

  const startX = handleBox.x + handleBox.width / 2;
  const startY = handleBox.y + handleBox.height / 2;
  expect(
    await page.evaluate(
      ({ x, y }) =>
        document.elementFromPoint(x, y)?.closest(".react-resizable-handle-se") !== null,
      { x: startX, y: startY },
    ),
  ).toBe(true);
  await page.mouse.move(startX, startY);
  await page.mouse.down();
  await page.mouse.move(Math.max(20, startX - 800), startY, { steps: 30 });
  await page.mouse.up();

  await expect(page.locator('[data-wb-calendar-responsive-mode="list"]')).toBeVisible();
  await expect(surface).toHaveAttribute("data-wb-calendar-view", "list:day");
  await expect
    .poll(async () =>
      Number((await surface.getAttribute("data-wb-calendar-resize-count")) ?? "0"),
    )
    .toBeGreaterThan(beforeCount);
  await expect(surface.locator("[data-wb-calendar-scroll-owner]")).toHaveCount(1);
});

test("reverts a rejected drag through the Work Buddy intent result", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openCalendarSpike(page);
  await selectFieldOption(page, "Mutation response", /^Reject and revert$/);

  const event = page.locator('[data-wb-calendar-item-id="prototype-mobile"]');
  await event.scrollIntoViewIfNeeded();
  const before = await event.boundingBox();
  expect(before).not.toBeNull();
  if (before === null) return;

  const x = before.x + before.width / 2;
  const y = before.y + before.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x, y + 90, { steps: 24 });
  await page.mouse.up();

  await expect(page.getByTestId("calendar-spike-last-intent")).toHaveText(
    "calendar.item-move-requested",
  );
  await expect(page.locator(".wb-calendar-spike__announcement")).toContainText(
    "rejected",
  );
  await expect
    .poll(async () => (await event.boundingBox())?.y ?? Number.NaN)
    .toBeCloseTo(before.y, 0);
});

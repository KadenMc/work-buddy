import { expect, test } from "@playwright/test";

test("system accessibility settings scale and persist dashboard typography", async ({
  page,
}) => {
  await page.goto("/app/journal?settings-return-probe=1");
  await page.getByRole("button", { name: "Open settings" }).click();

  await expect(page).toHaveURL(/\/app\/settings\/system\/accessibility$/);
  await expect(
    page.getByRole("navigation", { name: "Dashboard navigation" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Close settings" }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "Close settings" }),
  ).toHaveAttribute("aria-pressed", "true");

  await page.getByRole("button", { name: "Close settings" }).click();
  await expect(page).toHaveURL(/\/app\/journal\?settings-return-probe=1$/);

  await page.getByRole("button", { name: "Open settings" }).click();

  await page.getByRole("button", { name: "Back to dashboard" }).click();
  await expect(page).toHaveURL(/\/app\/journal\?settings-return-probe=1$/);

  await page.goto("/app/settings/accessibility");
  await expect(page).toHaveURL(/\/app\/settings\/system\/accessibility$/);

  await expect(
    page.getByRole("heading", { name: "Accessibility" }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Accessibility" }),
  ).toHaveClass(/is-active/);

  const slider = page.getByRole("slider", { name: "Text size" });
  const standardMetrics = await page.evaluate(() => ({
    root: getComputedStyle(document.documentElement).fontSize,
    body: getComputedStyle(document.body).fontSize,
  }));
  expect(standardMetrics).toEqual({ root: "16px", body: "16px" });
  await expect(slider).toHaveAttribute("aria-valuetext", "Standard, 100%");
  await slider.fill("2");
  await expect(slider).toHaveAttribute(
    "aria-valuetext",
    "Extra large, 125%",
  );
  await expect(page.locator("html")).toHaveAttribute(
    "data-wb-type-scale",
    "extra-large",
  );
  const enlargedMetrics = await page.evaluate(() => ({
    root: getComputedStyle(document.documentElement).fontSize,
    body: getComputedStyle(document.body).fontSize,
  }));
  expect(enlargedMetrics).toEqual({ root: "16px", body: "20px" });

  await page.reload();
  await expect(page.locator("html")).toHaveAttribute(
    "data-wb-type-scale",
    "extra-large",
  );
  await expect(slider).toHaveAttribute(
    "aria-valuetext",
    "Extra large, 125%",
  );

  await page.getByRole("button", { name: "Reset to standard" }).click();
  await expect(page.locator("html")).toHaveAttribute(
    "data-wb-type-scale",
    "standard",
  );
});

test("Journal uses one App settings page with App-only navigation and scoped search", async ({
  page,
}) => {
  await page.goto(
    "/app/settings/apps/journal?setting=wb.journal.day-boundary",
  );

  await expect(
    page.getByRole("heading", { name: "Journal settings" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Back to dashboard" })).toBeVisible();
  await expect(page.getByText("Built-in", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Journal" })).toHaveClass(/is-active/);
  await expect(page.getByText("Views", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Day starts")).toBeVisible();
  await expect(
    page.getByText("wb.journal.day-boundary", { exact: true }),
  ).toBeVisible();
  const pageSearch = page.getByRole("searchbox", {
    name: "Search within Journal settings",
  });
  await pageSearch.fill("font");
  await expect(page.getByText("0 settings")).toBeVisible();
  await expect(page.getByLabel("Day starts")).toBeHidden();
  await pageSearch.fill("late night");
  await expect(page.getByText("1 setting")).toBeVisible();
  await expect(page.getByLabel("Day starts")).toBeVisible();

  await page.getByRole("searchbox", { name: "Search all settings" }).fill("late night");
  const results = page.getByRole("region", { name: "Search results" });
  await expect(results.locator("li")).toHaveCount(1);
  await expect(
    results.getByRole("link", { name: /Day starts|Journal day starts/i }),
  ).toHaveAttribute(
    "href",
    /\/app\/settings\/apps\/journal\?setting=wb\.journal\.day-boundary$/,
  );

  await results.getByRole("link", { name: /Day starts|Journal day starts/i }).click();
  await expect(page).toHaveURL(
    /\/app\/settings\/apps\/journal\?setting=wb\.journal\.day-boundary$/,
  );
  await expect(
    page.getByRole("heading", { name: "Journal settings" }),
  ).toBeVisible();

  await page.goto(
    "/app/settings/views/journal?setting=wb.journal.day-boundary",
  );
  await expect(page).toHaveURL(
    /\/app\/settings\/apps\/journal\?setting=wb\.journal\.day-boundary$/,
  );

  await page.goto("/app/settings/setting/wb.journal.day-boundary");
  await expect(page).toHaveURL(
    /\/app\/settings\/apps\/journal\?setting=wb\.journal\.day-boundary$/,
  );
  await expect(
    page.locator('[data-setting-id="wb.journal.day-boundary"]'),
  ).toBeFocused();

  await page.goto("/app/settings/setting/example.missing");
  await expect(
    page.getByRole("heading", { name: "Setting not found" }),
  ).toBeVisible();
  await expect(page.getByText(/example\.missing/)).toBeVisible();
});

test("Journal day-boundary save waits for the latest authoritative preview", async ({
  page,
}) => {
  const previewRequests: string[] = [];
  await page.route("**/api/settings/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/settings/registry") {
      await route.fulfill({ status: 404 });
      return;
    }
    if (url.pathname === "/api/settings/values" && request.method() === "GET") {
      await route.fulfill({
        json: {
          schema_version: 1,
          registry_revision: "settings-registry:1",
          timezone: "America/Toronto",
          configured_timezone: "America/Toronto",
          observed_at: "2026-07-15T12:00:00Z",
          read_only: false,
          diagnostics: [],
          values: [{
            setting_id: "wb.journal.day-boundary",
            scope: { kind: "profile", subject_id: "default" },
            effective_value: "05:00",
            configured_value: "05:00",
            source: "default",
            is_modified: false,
            revision: "value:0",
            diagnostics: [],
          }],
        },
      });
      return;
    }
    if (url.pathname.endsWith("/wb.journal.day-boundary/preview")) {
      const body = request.postDataJSON() as { value: string };
      previewRequests.push(body.value);
      await new Promise((resolve) => setTimeout(resolve, 200));
      await route.fulfill({
        json: {
          schema_version: 1,
          registry_revision: "settings-registry:1",
          timezone: "America/Toronto",
          configured_timezone: "America/Toronto",
          value_revision: "value:0",
          preview: {
            setting_id: "wb.journal.day-boundary",
            scope: { kind: "profile", subject_id: "default" },
            value: body.value,
            effective_at: "2026-07-16T05:00:00-04:00",
            apply_status: "pending",
            impact_preview: {},
          },
          diagnostics: [],
        },
      });
      return;
    }
    await route.fulfill({ status: 404 });
  });

  await page.goto("/app/settings/apps/journal");
  const input = page.getByLabel("Day starts");
  const save = page.getByRole("button", { name: "Save change" });
  await expect(input).toBeEnabled();

  await input.fill("04:00");
  await input.fill("03:30");
  await expect(save).toBeDisabled();
  await expect(
    page.getByRole("region", { name: "Unsaved change preview" }),
  ).toBeVisible();
  await expect(save).toBeEnabled();
  expect(previewRequests).not.toContain("04:00");
  expect(new Set(previewRequests)).toEqual(new Set(["03:30"]));
});

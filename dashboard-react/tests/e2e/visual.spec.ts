import { expect, test } from "@playwright/test";

import { installThemePreference, openJournal } from "./helpers";

const COWORK_VISUAL_FOLDER = {
  store_id: "visual-store",
  folder_name: "research-notes",
  folder_path: "C:/Projects/research-notes",
  layout: "wbuddy_cowork_v1",
  reachable: true,
  eligibility: "eligible",
  ineligible_reason: null,
  document_surface: {
    enabled: true,
    allowed_document_classes: ["co_authored"],
    feedback_capture: true,
  },
  permissions: {
    read: true,
    create: true,
    import: true,
    materialize: true,
    retire: true,
  },
  document_count: 1,
};

const COWORK_VISUAL_DOCUMENT = {
  document_id: "visual-document",
  path: "notes/research-brief.md",
  title: "Research brief",
  profile: "co_authored",
  lifecycle: "active",
  initialization_state: "ready",
  drift_state: "clean",
  open_proposal_count: 2,
  open_flag_count: 0,
  updated_at: "2026-07-25T16:30:00Z",
  permissions: {
    open: true,
    edit: true,
    materialize: true,
    repair: false,
    retire: true,
  },
};

async function installCoworkVisualState(page: import("@playwright/test").Page) {
  // Co-work pulls its editor, collaboration, and lifecycle graph through a lazy
  // widget chunk. A cold Vite compile can take longer than Playwright's default
  // assertion timeout, especially when the visual suite starts with Journal.
  test.setTimeout(120_000);
  await page.addInitScript(() => {
    localStorage.setItem(
      "work-buddy.cowork.scratches.v1",
      JSON.stringify({
        version: 1,
        scratches: [
          {
            scratchId: "visual-scratch",
            title: "Untitled",
            createdAt: "2026-07-25T14:00:00Z",
            updatedAt: "2026-07-25T15:00:00Z",
            recoveredFromPreviousEditor: false,
          },
        ],
      }),
    );
  });
  await page.route("**/api/truth/cowork/folders?*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        read_only: false,
        folders: [COWORK_VISUAL_FOLDER],
        diagnostics: [],
        chooser: {
          available: true,
          kind: "host_native",
          markdown_available: true,
          location_available: true,
        },
      }),
    });
  });
  await page.route("**/api/truth/doc/list?*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ docs: [COWORK_VISUAL_DOCUMENT] }),
    });
  });
}

async function materializeVisualContent(page: import("@playwright/test").Page) {
  await page.addStyleTag({
    content: `
      .wb-temporal-item,
      .wb-temporal-list li,
      .wb-markdown-item {
        content-visibility: visible !important;
        contain-intrinsic-size: none !important;
      }
    `,
  });
}

test.beforeEach(({ browserName }) => {
  test.skip(browserName !== "chromium", "Canonical visual baselines use Chromium");
  test.skip(process.platform !== "win32", "Canonical visual baselines use Windows");
});

test("default dark desktop Journal visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "dark");
  await openJournal(page);
  await materializeVisualContent(page);

  await expect(page).toHaveScreenshot("journal-dark-desktop.png", {
    animations: "disabled",
    fullPage: true,
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test("default light desktop Journal visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "light");
  await openJournal(page);
  await materializeVisualContent(page);

  await expect(page).toHaveScreenshot("journal-light-desktop.png", {
    animations: "disabled",
    fullPage: true,
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test("default light Accessibility settings visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "light");
  await page.goto("/app/settings/accessibility", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Accessibility" })).toBeVisible();

  await expect(page).toHaveScreenshot("settings-accessibility-light-desktop.png", {
    animations: "disabled",
    fullPage: true,
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test("maximum-text dark Accessibility settings visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "dark");
  await page.addInitScript(() => {
    localStorage.setItem("wb.accessibility.type-scale.v1", "maximum");
  });
  await page.goto("/app/settings/accessibility", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("slider", { name: "Text size" })).toHaveAttribute(
    "aria-valuetext",
    "Maximum, 137.5%",
  );

  await expect(page).toHaveScreenshot("settings-accessibility-maximum-dark-desktop.png", {
    animations: "disabled",
    fullPage: true,
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test("Calm Workshop dark desktop Journal visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "dark", "wb.calm-workshop");
  await openJournal(page);
  await materializeVisualContent(page);

  await expect(page).toHaveScreenshot("journal-calm-workshop-dark-desktop.png", {
    animations: "disabled",
    fullPage: true,
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test("default dark widget catalog visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "dark");
  await openJournal(page);
  await page.getByRole("button", { name: "Customize view" }).click();
  await page.getByRole("button", { name: "Widgets" }).click();
  await expect(page.getByRole("dialog", { name: "Widgets" })).toBeVisible();

  await expect(page).toHaveScreenshot("widget-catalog-dark-desktop.png", {
    animations: "disabled",
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test("default dark mobile-order editor visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "dark");
  await openJournal(page);
  await page.getByRole("button", { name: "Customize view" }).click();
  await page.getByRole("button", { name: "Mobile order" }).click();
  await expect(page.getByRole("grid", { name: "Mobile widget order" })).toBeVisible();
  // Beginning customize remounts every non-durable widget, which briefly shows a
  // draft-restore loader. Let the capture widget settle so the shot baselines its
  // resolved content rather than the transient state.
  await expect(page.getByText("Restoring draft")).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "Capture text" })).toBeVisible();

  await expect(page).toHaveScreenshot("mobile-order-editor-dark-desktop.png", {
    animations: "disabled",
    fullPage: true,
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test("adversarial skin desktop Journal visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installThemePreference(page, "dark", "wb.conformance-stress");
  await openJournal(page);
  await materializeVisualContent(page);

  await expect(page).toHaveScreenshot("journal-stress-skin-desktop.png", {
    animations: "disabled",
    fullPage: true,
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test("mobile one-column Journal visual baseline", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installThemePreference(page, "dark");
  await openJournal(page);
  // Full-page screenshots do not scroll each below-the-fold widget into the
  // rendering viewport. Materialize virtualized list content so the golden
  // proves the compact renderers rather than their intrinsic-size placeholders.
  await materializeVisualContent(page);
  await expect(page.getByRole("radiogroup", { name: "Timeline display mode" })).toBeVisible();
  await page
    .getByRole("radiogroup", { name: "Timeline display mode" })
    .getByText("List", { exact: true })
    .click();
  const listProjection = page.getByRole("region", { name: /Calendar surface for/ });
  await expect(listProjection.getByRole("table")).toBeVisible();
  await expect(
    listProjection.getByRole("button", { name: /Mapped Journal data contracts/ }),
  ).toBeVisible();
  await expect(page.getByRole("region", { name: "Running Notes" })).toContainText(
    "Prototype mobile timeline edge case",
  );

  await expect(page).toHaveScreenshot("journal-mobile.png", {
    animations: "disabled",
    fullPage: true,
    mask: [page.locator(".clock")],
    maskColor: "#808080",
  });
});

test.describe("Co-work launcher visuals", () => {
  test.use({ locale: "en-US", timezoneId: "America/New_York" });

  test("Co-work Folder-neutral launcher visual baseline", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await installThemePreference(page, "light");
    await installCoworkVisualState(page);
    await page.goto("/app/cowork?mode=launcher", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { name: "Documents", exact: true })).toBeVisible({
      timeout: 60_000,
    });
    await expect(page.getByRole("heading", { name: "Folders", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "New", exact: true })).toBeVisible();

    await expect(page).toHaveScreenshot("cowork-launcher-light-desktop.png", {
      animations: "disabled",
      fullPage: true,
      mask: [page.locator(".clock")],
      maskColor: "#808080",
    });
  });

  test("Co-work active Folder launcher visual baseline", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await installThemePreference(page, "light");
    await installCoworkVisualState(page);
    await page.goto("/app/cowork?store_id=visual-store", {
      waitUntil: "domcontentloaded",
    });
    await expect(page.getByRole("button", { name: "Close folder", exact: true })).toBeVisible({
      timeout: 60_000,
    });
    await expect(page.getByRole("heading", { name: "Documents", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "New", exact: true })).toBeVisible();

    await expect(page).toHaveScreenshot("cowork-folder-light-desktop.png", {
      animations: "disabled",
      fullPage: true,
      mask: [page.locator(".clock")],
      maskColor: "#808080",
    });
  });

  test("Co-work active Folder mobile toolbar visual baseline", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await installThemePreference(page, "light");
    await installCoworkVisualState(page);
    await page.goto("/app/cowork?store_id=visual-store", {
      waitUntil: "domcontentloaded",
    });
    await expect(page.getByRole("button", { name: "Close folder", exact: true })).toBeVisible({
      timeout: 60_000,
    });
    await expect(page.getByRole("heading", { name: "Documents", exact: true })).toBeVisible();

    await expect(page).toHaveScreenshot("cowork-folder-light-mobile.png", {
      animations: "disabled",
      fullPage: true,
      mask: [page.locator(".clock")],
      maskColor: "#808080",
    });
  });
});

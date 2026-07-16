import { expect, type Locator, type Page } from "@playwright/test";

export const PERSONALIZATION_KEY =
  "work-buddy.dashboard.personalization.v1:wb.journal.main";
export const THEME_KEY = "wb.theme.preference.v1";

export async function openJournal(page: Page): Promise<void> {
  await page.goto("/app/journal", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Journal", level: 1 })).toBeVisible();
  await expect(page.getByRole("region", { name: "Quick Capture", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Day Timeline", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Running Notes", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Loading widget" })).toHaveCount(0, {
    timeout: 30_000,
  });
  await expect(page.getByRole("status", { name: "Refreshing…" })).toHaveCount(0, {
    timeout: 30_000,
  });
}

export function widget(page: Page, name: string): Locator {
  return page.getByRole("region", { name, exact: true });
}

export async function beginCustomize(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Customize view" }).click();
  await expect(page.locator(".wb-view-host")).toHaveClass(/is-customizing/);
}

export async function openWidgetMenu(
  page: Page,
  name: string,
  keyboard = false,
): Promise<Locator> {
  const frame = widget(page, name);
  const details = frame.locator("details.wb-widget-menu");
  const summary = frame.locator("summary").filter({ hasText: `Actions for ${name}` });
  if ((await details.getAttribute("open")) === null) {
    if (keyboard) {
      await summary.focus();
      await summary.press("Enter");
    } else {
      await summary.click();
    }
  }
  const popover = frame.locator(".wb-widget-menu__popover");
  await expect(popover).toBeVisible();
  return popover;
}

export async function readPersonalization(page: Page) {
  return page.evaluate((key) => {
    const raw = localStorage.getItem(key);
    return raw === null ? null : (JSON.parse(raw) as Record<string, unknown>);
  }, PERSONALIZATION_KEY);
}

export async function installThemePreference(
  page: Page,
  scheme: "system" | "light" | "dark",
  skinId = "wb.default",
): Promise<void> {
  await page.addInitScript(
    ({ key, value }) => {
      if (localStorage.getItem(key) === null) {
        localStorage.setItem(key, JSON.stringify(value));
      }
    },
    {
      key: THEME_KEY,
      value: { version: 1, scheme, skinId },
    },
  );
}

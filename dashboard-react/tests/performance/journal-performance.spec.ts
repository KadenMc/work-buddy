import { expect, type Page, test } from "@playwright/test";

import { openJournal } from "../e2e/helpers";
import { JOURNAL_BROWSER_BUDGET } from "../fixtures/performanceBudget";

const WIDGET_LAB_50_BUDGET = {
  domContentLoadedMs: 10_000,
  // Same development-server accounting as the Journal budget; the 50-host
  // trace should add DOM work without loading a second UI runtime.
  decodedScriptAndStyleBytes: 4_500_000,
  longTaskCount: 40,
  longestTaskMs: 750,
} as const;

async function installLongTaskObserver(page: Page) {
  await page.addInitScript(() => {
    const target = window as typeof window & { __wbLongTasks?: number[] };
    target.__wbLongTasks = [];
    new PerformanceObserver((list) => {
      target.__wbLongTasks?.push(
        ...list.getEntries().map((entry) => Math.round(entry.duration)),
      );
    }).observe({ type: "longtask", buffered: true });
  });
}

async function collectBrowserMetrics(page: Page) {
  return page.evaluate(() => {
    const navigation = performance.getEntriesByType(
      "navigation",
    )[0] as PerformanceNavigationTiming;
    const resources = performance
      .getEntriesByType("resource")
      .filter((entry) => /\.(?:js|css)(?:\?|$)/.test(entry.name)) as PerformanceResourceTiming[];
    const longTasks =
      (window as typeof window & { __wbLongTasks?: number[] }).__wbLongTasks ?? [];
    return {
      domContentLoadedMs: Math.round(navigation.domContentLoadedEventEnd),
      measuredAtMs: Math.round(performance.now()),
      decodedScriptAndStyleBytes: resources.reduce(
        (total, resource) => total + resource.decodedBodySize,
        0,
      ),
      scriptAndStyleResources: resources.length,
      longTaskCount: longTasks.length,
      longestTaskMs: Math.max(0, ...longTasks),
      widgetFrameCount: document.querySelectorAll(".wb-widget-frame").length,
      widgetLabHostCount: document.querySelectorAll('[data-testid="widget-lab-host"]')
        .length,
      rendererCounts: {
        capture: document.querySelectorAll(".wb-capture").length,
        timeline: document.querySelectorAll(".wb-day-timeline").length,
        notes: document.querySelectorAll(".wb-running-notes").length,
      },
    };
  });
}

test("records representative three-widget Journal navigation and bundle evidence", async ({
  page,
  browserName,
}) => {
  test.skip(browserName !== "chromium", "Long Task API evidence is collected in Chromium");
  await installLongTaskObserver(page);

  await openJournal(page);
  await page.waitForTimeout(250);

  const metrics = await collectBrowserMetrics(page);
  test.info().annotations.push({
    type: "performance-journal",
    description: JSON.stringify(metrics),
  });
  expect(metrics.widgetFrameCount).toBe(3);
  expect(metrics.domContentLoadedMs).toBeLessThan(
    JOURNAL_BROWSER_BUDGET.domContentLoadedMs,
  );
  expect(metrics.decodedScriptAndStyleBytes).toBeLessThan(
    JOURNAL_BROWSER_BUDGET.decodedScriptAndStyleBytes,
  );
  expect(metrics.longTaskCount).toBeLessThan(JOURNAL_BROWSER_BUDGET.longTaskCount);
});

test("mounts and budgets a synthetic trace of exactly 50 real widget hosts", async ({
  page,
  browserName,
}) => {
  test.skip(browserName !== "chromium", "Long Task API evidence is collected in Chromium");
  await installLongTaskObserver(page);

  await page.goto("/app/__widget-lab?count=50");
  await expect(page.getByTestId("widget-lab-host")).toHaveCount(50);
  await expect(page.locator(".wb-widget-frame")).toHaveCount(50);
  await expect(page.locator(".wb-capture")).toHaveCount(17);
  await expect(page.locator(".wb-day-timeline")).toHaveCount(17);
  await expect(page.locator(".wb-running-notes")).toHaveCount(16);
  await page.waitForTimeout(250);

  const metrics = await collectBrowserMetrics(page);
  test.info().annotations.push({
    type: "performance-widget-lab-50",
    description: JSON.stringify(metrics),
  });
  expect(metrics.widgetFrameCount).toBe(50);
  expect(metrics.widgetLabHostCount).toBe(50);
  expect(metrics.rendererCounts).toEqual({ capture: 17, timeline: 17, notes: 16 });
  expect(metrics.domContentLoadedMs).toBeLessThan(
    WIDGET_LAB_50_BUDGET.domContentLoadedMs,
  );
  expect(metrics.measuredAtMs).toBeLessThan(WIDGET_LAB_50_BUDGET.domContentLoadedMs);
  expect(metrics.decodedScriptAndStyleBytes).toBeLessThan(
    WIDGET_LAB_50_BUDGET.decodedScriptAndStyleBytes,
  );
  expect(metrics.longTaskCount).toBeLessThan(WIDGET_LAB_50_BUDGET.longTaskCount);
  expect(metrics.longestTaskMs).toBeLessThan(WIDGET_LAB_50_BUDGET.longestTaskMs);
});

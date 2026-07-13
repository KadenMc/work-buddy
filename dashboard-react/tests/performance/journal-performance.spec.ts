import { expect, test } from "@playwright/test";

import { openJournal } from "../e2e/helpers";
import { JOURNAL_BROWSER_BUDGET } from "../fixtures/performanceBudget";

test("records representative Journal navigation and bundle evidence", async ({
  page,
  browserName,
}) => {
  test.skip(browserName !== "chromium", "Long Task API evidence is collected in Chromium");
  await page.addInitScript(() => {
    const target = window as typeof window & { __wbLongTasks?: number[] };
    target.__wbLongTasks = [];
    new PerformanceObserver((list) => {
      target.__wbLongTasks?.push(
        ...list.getEntries().map((entry) => Math.round(entry.duration)),
      );
    }).observe({ type: "longtask", buffered: true });
  });

  await openJournal(page);
  await page.waitForTimeout(250);

  const metrics = await page.evaluate(() => {
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
      decodedScriptAndStyleBytes: resources.reduce(
        (total, resource) => total + resource.decodedBodySize,
        0,
      ),
      scriptAndStyleResources: resources.length,
      longTaskCount: longTasks.length,
      longestTaskMs: Math.max(0, ...longTasks),
      widgetFrameCount: document.querySelectorAll(".wb-widget-frame").length,
    };
  });

  test.info().annotations.push({
    type: "performance",
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

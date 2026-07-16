import { expect, test } from "@playwright/test";

import { openJournal } from "./helpers";

test("mobile uses canonical one-column DOM and visual order without mounting RGL", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openJournal(page);

  await expect(page.locator(".wb-dashboard-mobile-stack")).toBeVisible();
  await expect(page.locator(".react-grid-layout")).toHaveCount(0);
  await expect(page.locator(".wb-widget-drag-handle")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Customize view" })).toHaveCount(0);

  const titles = await page
    .locator(
      ".wb-dashboard-mobile-stack > div > .wb-widget-frame > .wb-widget-frame__header .wb-widget-frame__title",
    )
    .allTextContents();
  expect(titles).toEqual(["Quick Capture", "Day Timeline", "Running Notes"]);

  const positions = await page
    .locator(".wb-dashboard-mobile-stack > div")
    .evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().top));
  expect(positions).toEqual([...positions].sort((left, right) => left - right));
});

test("the rendered Journal has no serious or critical axe violations", async ({ page }) => {
  await openJournal(page);
  await page.addScriptTag({ path: "node_modules/axe-core/axe.min.js" });

  const violations = await page.evaluate(async () => {
    const axeWindow = window as typeof window & {
      axe: {
        run(
          context: Document,
          options: { resultTypes: readonly string[] },
        ): Promise<{
          violations: readonly {
            id: string;
            impact: string | null;
            help: string;
            nodes: readonly {
              target: readonly string[];
              failureSummary?: string;
            }[];
          }[];
        }>;
      };
    };
    const result = await axeWindow.axe.run(document, { resultTypes: ["violations"] });
    return result.violations
      .filter((violation) =>
        violation.impact === "serious" || violation.impact === "critical",
      )
      .map((violation) => ({
        id: violation.id,
        impact: violation.impact,
        help: violation.help,
        nodes: violation.nodes.map((node) => ({
          target: node.target,
          failureSummary: node.failureSummary,
        })),
      }));
  });

  expect(violations).toEqual([]);
});

test("Journal exposes textual timeline semantics and stable page landmarks", async ({ page }) => {
  await openJournal(page);

  await expect(page.getByRole("navigation", { name: "Dashboard navigation" })).toBeVisible();
  await expect(page.getByRole("main")).toHaveCount(1);
  await expect(page.getByText("record", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("calendar", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("plan", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: /fixed commitment/ }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: /past — protected/ }).first()).toBeVisible();
});

test("the integrated page avoids uncaught runtime and layout-loop errors", async ({ page }) => {
  const pageErrors: string[] = [];
  const suspiciousConsole: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    const text = message.text();
    if (
      message.type() === "error" &&
      !text.includes("Failed to load resource") &&
      !text.includes("ERR_CONNECTION_REFUSED")
    ) {
      suspiciousConsole.push(text);
    }
  });

  await openJournal(page);
  await page.getByRole("button", { name: "Customize view" }).click();
  await page.waitForTimeout(250);

  expect(pageErrors).toEqual([]);
  expect(
    suspiciousConsole.filter((message) =>
      /ResizeObserver loop|findDOMNode|maximum update depth|uncaught/i.test(message),
    ),
  ).toEqual([]);
});

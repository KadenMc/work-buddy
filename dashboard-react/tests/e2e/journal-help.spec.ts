import { expect, test } from "@playwright/test";

import { beginCustomize, openJournal, widget } from "./helpers";

test("Hover help explains view purposes and primitives without permanent copy", async ({
  page,
}) => {
  await openJournal(page);

  await expect(page.getByText("Run a smart follow-up after saving.")).toHaveCount(0);
  await expect(page.getByText("Press Ctrl + Enter to capture")).toHaveCount(0);

  const smart = page.getByRole("switch", { name: "Smart" });
  const smartField = page.locator(".wb-capture__smart");
  const smartShape = await smartField.evaluate((element) => {
    const control = getComputedStyle(element);
    const track = element.querySelector<HTMLElement>(".wb-switch-field__track");
    const trackStyle = track === null ? undefined : getComputedStyle(track);
    return {
      borderWidth: control.borderTopWidth,
      controlRadius: control.borderTopLeftRadius,
      trackRadius: trackStyle?.borderTopLeftRadius,
    };
  });
  expect(smartShape.borderWidth).toBe("0px");
  expect(Number.parseFloat(smartShape.controlRadius)).toBeGreaterThan(12);
  expect(Number.parseFloat(smartShape.trackRadius ?? "0")).toBeGreaterThan(8);

  const help = page.getByRole("button", { name: "Hover help" });
  await help.click();
  await expect(help).toHaveAttribute("aria-pressed", "true");

  const captureTitle = page.getByLabel("About Quick Capture in this view");
  const targetPresentation = await captureTitle.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      outlineWidth: style.outlineWidth,
      outlineStyle: style.outlineStyle,
      animationName: style.animationName,
    };
  });
  expect(targetPresentation).toEqual({
    outlineWidth: "2px",
    outlineStyle: "dashed",
    animationName: "wb-help-target-reveal",
  });

  // Force avoids Playwright waiting on the one-time target-reveal animation before
  // it dispatches pointer entry, so the assertion measures our dwell gate itself.
  await captureTitle.hover({ force: true });
  const purposeTooltip = page.getByRole("tooltip");
  await page.waitForTimeout(450);
  await expect(purposeTooltip).toHaveCount(0);
  await expect(purposeTooltip).toContainText(
    "Capture what is happening without leaving the Journal.",
  );
  await expect(purposeTooltip).toContainText("required Journal slot");
  const tooltipType = await purposeTooltip.evaluate((element) => {
    const summary = element.querySelector<HTMLElement>(".wb-contextual-help__summary");
    const details = element.querySelector<HTMLElement>(".wb-contextual-help__details");
    return {
      summary: Number.parseFloat(getComputedStyle(summary!).fontSize),
      details: Number.parseFloat(getComputedStyle(details!).fontSize),
    };
  });
  expect(tooltipType.summary).toBeGreaterThanOrEqual(15);
  expect(tooltipType.details).toBeGreaterThanOrEqual(14);
  await purposeTooltip.hover();
  await expect(purposeTooltip).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(purposeTooltip).toBeHidden();

  await smartField.hover();
  const smartTooltip = page.getByRole("tooltip");
  await expect(smartTooltip).toContainText("Run a smart follow-up after capturing.");
  await expect(smartTooltip).toContainText("permission and confirmation rules");
  await page.keyboard.press("Escape");

  await beginCustomize(page);
  // Hover help is a navbar control now, so it stays present through a customize session.
  // Entering customize turns help off, so the toggle reads unpressed and every HelpTarget
  // drops back to its plain child, which is why no help affordance remains to reveal.
  const helpToggle = page.getByRole("button", { name: "Hover help" });
  await expect(helpToggle).toBeVisible();
  await expect(helpToggle).toHaveAttribute("aria-pressed", "false");
  await expect(widget(page, "Quick Capture").locator(".wb-help-target")).toHaveCount(0);
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("button", { name: "Hover help" })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
});

import { expect, test } from "@playwright/test";

import { openCowork } from "./cowork-helpers";

/**
 * Keyboard-completeness proof in a real browser: the queue-mode sitting is
 * driveable with the inverted j/k pair and Enter, no pointer past entering queue
 * mode. The Escape-to-cancel gap on the inline verb input is characterised in the
 * jsdom suite (keyboardSitting.test.tsx), which can assert the input-open state
 * deterministically.
 */

const SUBMIT = /Submit sitting/;

test("drives the queue sitting with j/k and Enter", async ({ page }) => {
  await openCowork(page);

  // Enter queue mode, the keyboard focus layout.
  await page.getByRole("button", { name: "Queue" }).click();
  await expect(page.getByText(/Item 1/)).toBeVisible();
  await expect(page.getByText(/of 5/)).toBeVisible();

  // Navigate forward with k, back with j (the inverted binding).
  await page.keyboard.press("k");
  await expect(page.getByText(/Item 2/)).toBeVisible();
  await page.keyboard.press("k");
  await expect(page.getByText(/Item 3/)).toBeVisible();
  await page.keyboard.press("j");
  await expect(page.getByText(/Item 2/)).toBeVisible();
  await page.keyboard.press("j");
  await expect(page.getByText(/Item 1/)).toBeVisible();

  // Stage Accept with Enter. Queue mode auto-advances to the next undecided item.
  await page.getByRole("button", { name: "Accept" }).press("Enter");
  await expect(page.getByRole("button", { name: SUBMIT })).toHaveText(
    "Submit sitting (1)",
  );
  await expect(page.getByText(/Item 2/)).toBeVisible();

  // Stage Accept on the next item too, then submit with Enter.
  await page.getByRole("button", { name: "Accept" }).press("Enter");
  await expect(page.getByRole("button", { name: SUBMIT })).toHaveText(
    "Submit sitting (2)",
  );

  await page.getByRole("button", { name: SUBMIT }).press("Enter");
  await expect(page.getByRole("button", { name: SUBMIT })).toBeDisabled();
  await expect(page.getByText(/of 3/)).toBeVisible();
});

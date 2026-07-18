import { expect, test } from "@playwright/test";

import {
  COWORK_FIRST_PROPOSAL,
  COWORK_SECOND_PROPOSAL,
  openCowork,
} from "./cowork-helpers";

/**
 * Browser-side review loop over the Co-work surface. The surface is demo-backed in
 * this tree, so this drives the in-memory review provider through a full sitting:
 * select a proposal, stage a no-input verb, stage an inline-input verb with typed
 * text, submit, and confirm the decided proposals leave the open set. The server
 * half of the loop (register, propose, marks, materialize onto disk) is proven by
 * the Flask-test-client gate at tests/unit/cowork/test_review_loop.py, and the two
 * halves join once the live transports wire in behind the provider seams.
 */

const SUBMIT = /Submit sitting/;

test("walks a sitting end to end in the browser", async ({ page }) => {
  await openCowork(page);

  // Accept the first proposal.
  await page.getByText(COWORK_FIRST_PROPOSAL).click();
  await expect(page.getByRole("region", { name: "Decide" })).toBeVisible();
  await page.getByRole("button", { name: "Accept" }).click();
  await expect(page.getByRole("button", { name: SUBMIT })).toHaveText(
    "Submit sitting (1)",
  );
  await expect(page.getByText("Staged: Accept")).toBeVisible();

  // Reject the second proposal as a preference, recording verbatim phrasing.
  await page.getByText(COWORK_SECOND_PROPOSAL).click();
  await page.getByRole("button", { name: "Reject as preference" }).click();
  await page
    .getByLabel("Your preferred phrasing, recorded as a preference")
    .fill("Keep the original wording here.");
  await page.getByRole("button", { name: "Stage" }).click();
  await expect(page.getByRole("button", { name: SUBMIT })).toHaveText(
    "Submit sitting (2)",
  );

  // Submit. The accepted and rejected proposals leave the open set and the submit
  // button disarms.
  await page.getByRole("button", { name: SUBMIT }).click();
  await expect(page.getByText(COWORK_FIRST_PROPOSAL)).toHaveCount(0);
  await expect(page.getByText(COWORK_SECOND_PROPOSAL)).toHaveCount(0);
  await expect(page.getByRole("button", { name: SUBMIT })).toBeDisabled();
});

test("exposes the three regions and one main landmark", async ({ page }) => {
  await openCowork(page);

  await expect(page.getByRole("main")).toHaveCount(1);
  await expect(page.getByRole("tab", { name: "Review" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByRole("tab", { name: /Chat/ })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Document editor" })).toBeVisible();

  // The Chat tab mounts the house conversation panel seeded by the document agent.
  await page.getByRole("tab", { name: /Chat/ }).click();
  await expect(
    page.getByText(/I proposed a few tracked edits/),
  ).toBeVisible();
});

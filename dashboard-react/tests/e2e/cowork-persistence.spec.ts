import { expect, test } from "@playwright/test";

import {
  openCowork,
  openCoworkScratch,
  resetCoworkStorage,
  waitForCoworkEditorDurable,
} from "./cowork-helpers";

/**
 * Persistence proofs for a launcher-created local Co-work document and the dev-only fixture.
 * Local document text uses the device-local IndexedDB Yjs transport. The fixture supplies a
 * deterministic editor-plus-Review scene for independent scroll-position continuity.
 */

const EDITOR_MARKER = "persist-marker-4b91c";

test.describe("Co-work persistence across visits", () => {
  test.beforeEach(async ({ page }) => {
    // Reset both stores for determinism, so the round-trip starts from a pristine empty document.
    await resetCoworkStorage(page);
  });

  test("a launcher-created local document survives reload", async ({
    page,
  }) => {
    const scratchId = await openCoworkScratch(page);

    // The Co-work view chrome renders its title, and no fabricated demo wording appears on the
    // honest empty route (Ruling 1 scrapped demo mode as a product surface).
    await expect(
      page.getByRole("heading", { level: 1, name: "Co-work" }),
    ).toBeVisible();
    await expect(page.getByText(/Demo data/i)).toHaveCount(0);

    // Type a unique marker into the editor. It rides the live editor state and is pushed to the
    // local IndexedDB transport per keystroke.
    const editor = page.getByRole("textbox", { name: "Document editor" });
    await editor.click();
    await page.keyboard.press("Control+End");
    await page.keyboard.type(` ${EDITOR_MARKER}`);
    await expect(editor).toContainText(EDITOR_MARKER);

    // Reload only once the editor content has reached the durable compacted snapshot, the form a
    // reload rehydrates from. This keeps the round-trip deterministic instead of racing the
    // compaction debounce.
    await waitForCoworkEditorDurable(page, scratchId);

    await page.reload({ waitUntil: "domcontentloaded" });

    // The editor rehydrates its typed marker from the local transport.
    await expect(
      page.getByRole("textbox", { name: "Document editor" }),
    ).toContainText(EDITOR_MARKER, { timeout: 60_000 });
  });

  test("the dev-only demo fixture route still renders the seeded scene", async ({
    page,
  }) => {
    // openCowork targets ?cowork_fixture=demo and waits for the seeded review rail. On the dev
    // server import.meta.env.DEV is true, so the DEV-gated fixture entry composes the scene the
    // review-loop suites depend on, and this case guards that gate against a regression.
    await openCowork(page);

    // The seeded document renders beside its review rail, so the fixture is the full scene rather
    // than the honest empty default.
    await expect(
      page.getByRole("textbox", { name: "Document editor" }),
    ).toContainText("Context bundle cache");
  });

  test("keeps the side-panel tabs pinned while a long Chat transcript scrolls", async ({
    page,
  }) => {
    await openCowork(page);
    await page.getByRole("tab", { name: /Chat/u }).click();

    const composer = page.getByRole("textbox", { name: "Message" });
    const send = page.getByRole("button", { name: "Send" });
    for (let index = 0; index < 10; index += 1) {
      await composer.fill(
        `Chat scroll containment ${index}: keep the side-panel tabs available while this transcript grows.`,
      );
      await send.click();
    }

    const metrics = await page.evaluate(() => {
      const railWrapper = document.querySelector<HTMLElement>(
        ".wb-cowork__rail-panel",
      );
      const tabs = document.querySelector<HTMLElement>(
        ".wb-cowork-rail__tabs",
      );
      const transcript = document.querySelector<HTMLElement>(
        ".wb-chat-list__scroll",
      );
      if (railWrapper === null || tabs === null || transcript === null) {
        throw new Error("The Chat rail did not render its scroll boundaries.");
      }
      return {
        railScrollTop: railWrapper.scrollTop,
        railOverflowY: getComputedStyle(railWrapper).overflowY,
        tabsHeight: tabs.getBoundingClientRect().height,
        tabsVisible: tabs.getBoundingClientRect().bottom > 0,
        transcriptClientHeight: transcript.clientHeight,
        transcriptScrollHeight: transcript.scrollHeight,
        transcriptScrollTop: transcript.scrollTop,
      };
    });

    expect(metrics.railScrollTop).toBe(0);
    expect(metrics.railOverflowY).toBe("clip");
    expect(metrics.tabsHeight).toBeGreaterThan(40);
    expect(metrics.tabsVisible).toBe(true);
    expect(metrics.transcriptScrollHeight).toBeGreaterThan(
      metrics.transcriptClientHeight,
    );
    expect(metrics.transcriptScrollTop).toBeGreaterThan(0);
  });

  test("editor and Review positions survive leaving the workspace", async ({ page }) => {
    // A shorter viewport guarantees both the seeded document and its review list have a
    // meaningful scroll range. The positions are deliberately different so accidentally
    // sharing one persistence key cannot pass the round trip.
    await page.setViewportSize({ width: 1280, height: 500 });
    await openCowork(page);

    const editorRegion = page.locator(".wb-cowork__editor-region");
    const reviewBody = page.locator(".wb-cowork-rail__body");
    const editorPosition = await editorRegion.evaluate((element) => {
      const max = element.scrollHeight - element.clientHeight;
      element.scrollTop = Math.floor(max * 0.61);
      return { max, top: element.scrollTop };
    });
    const reviewPosition = await reviewBody.evaluate((element) => {
      const max = element.scrollHeight - element.clientHeight;
      element.scrollTop = Math.floor(max * 0.79);
      return { max, top: element.scrollTop };
    });

    expect(editorPosition.max).toBeGreaterThan(40);
    expect(reviewPosition.max).toBeGreaterThan(40);
    expect(editorPosition.top).toBeGreaterThan(0);
    expect(reviewPosition.top).toBeGreaterThan(0);

    // Let the throttled writer settle, then leave and return to the same fixture route.
    await page.waitForTimeout(350);
    await page.goto("/app/", { waitUntil: "domcontentloaded" });
    await openCowork(page);

    await expect
      .poll(() => editorRegion.evaluate((element) => element.scrollTop))
      .toBeCloseTo(editorPosition.top, 0);
    await expect
      .poll(() => reviewBody.evaluate((element) => element.scrollTop))
      .toBeCloseTo(reviewPosition.top, 0);

    // Keeping Review hidden beyond the restoration deadline must not let its display:none
    // geometry overwrite the saved offset with zero.
    const restoredReviewTop = await reviewBody.evaluate((element) => element.scrollTop);
    await page.getByRole("tab", { name: /Chat/ }).click();
    await page.waitForTimeout(16_000);
    await page.getByRole("tab", { name: "Review" }).click();
    await expect
      .poll(() => reviewBody.evaluate((element) => element.scrollTop))
      .toBeCloseTo(restoredReviewTop, 0);
  });

  test("filtered and Queue views do not replace the canonical Review position", async ({
    page,
  }) => {
    await openCowork(page);
    const reviewBody = page.locator(".wb-cowork-rail__body");
    const canonicalTop = await reviewBody.evaluate((element) => {
      const max = element.scrollHeight - element.clientHeight;
      element.scrollTop = Math.floor(max * 0.72);
      return element.scrollTop;
    });
    expect(canonicalTop).toBeGreaterThan(40);
    await page.waitForTimeout(350);

    await page.getByRole("button", { name: /Flags/ }).click();
    await page.getByRole("button", { name: /All/ }).click();
    await expect
      .poll(() => reviewBody.evaluate((element) => element.scrollTop))
      .toBeCloseTo(canonicalTop, 0);

    await page.getByRole("button", { name: "Queue" }).click();
    await page.getByRole("button", { name: "Stream" }).click();
    await expect
      .poll(() => reviewBody.evaluate((element) => element.scrollTop))
      .toBeCloseTo(canonicalTop, 0);
  });
});

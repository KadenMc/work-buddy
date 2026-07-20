import { expect, test } from "@playwright/test";

import {
  openCowork,
  openCoworkEmpty,
  resetCoworkStorage,
  waitForCoworkEditorDurable,
} from "./cowork-helpers";

/**
 * The reload-persistence proof for the honest empty Co-work workspace. The empty default route
 * runs the editor on a local IndexedDB Yjs transport and the chat composer on a localStorage
 * draft, so a typed document and an unsent message must both survive a full page reload with no
 * server and no demo fixture. A second case guards the dev-only demo fixture entry, which the
 * review-loop suites still target, against a regression in its DEV gate.
 */

// The empty workspace persists its editor under this document id (surface EMPTY_DOCUMENT_ID).
const EMPTY_DOCUMENT_ID = "cowork-empty";

const EDITOR_MARKER = "persist-marker-4b91c";
const CHAT_DRAFT = "unsent draft 4b91c, kept across reload";

test.describe("Co-work persistence across reload", () => {
  test.beforeEach(async ({ page }) => {
    // Reset both stores for determinism, so the round-trip starts from a pristine empty document.
    await resetCoworkStorage(page);
  });

  test("editor content and chat draft both survive a reload on the empty default route", async ({
    page,
  }) => {
    await openCoworkEmpty(page);

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

    // Switch to Chat and type a distinct draft without sending. The composer mirrors it to
    // localStorage on every edit.
    await page.getByRole("tab", { name: /Chat/ }).click();
    const composer = page.getByRole("textbox", { name: "Message" });
    await composer.click();
    await page.keyboard.type(CHAT_DRAFT);
    await expect(composer).toHaveValue(CHAT_DRAFT);

    // Reload only once the editor content has reached the durable compacted snapshot, the form a
    // reload rehydrates from. This keeps the round-trip deterministic instead of racing the
    // compaction debounce.
    await waitForCoworkEditorDurable(page, EMPTY_DOCUMENT_ID);

    await page.reload({ waitUntil: "domcontentloaded" });

    // The editor rehydrates its typed marker from the local transport.
    await expect(
      page.getByRole("textbox", { name: "Document editor" }),
    ).toContainText(EDITOR_MARKER, { timeout: 60_000 });

    // The chat composer rehydrates its retained draft. The rail opens on Review, so switch back
    // to Chat to read the seeded composer.
    await page.getByRole("tab", { name: /Chat/ }).click();
    await expect(page.getByRole("textbox", { name: "Message" })).toHaveValue(
      CHAT_DRAFT,
    );
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
});

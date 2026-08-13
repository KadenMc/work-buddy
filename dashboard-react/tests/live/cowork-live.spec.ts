import { createHash } from "node:crypto";
import {
  access,
  lstat,
  mkdir,
  readFile,
  readdir,
  writeFile,
} from "node:fs/promises";
import path from "node:path";

import { expect, test, type Page, type Route } from "@playwright/test";
import * as Y from "yjs";

import { parseFrames } from "../../src/apps/cowork/persistence/framing";

interface Fixture {
  readonly format: string;
  readonly root: string;
  readonly host_root: string;
  readonly ordinary: {
    readonly name: string;
    readonly path: string;
    readonly root_gitignore_sha256: string;
    readonly unrelated_manifest_sha256: string;
    readonly unrelated_manifest_base64: string;
    readonly sibling_state_sha256: string;
  };
  readonly initialized: {
    readonly name: string;
    readonly path: string;
    readonly store_id: string;
  };
  readonly source: {
    readonly relative_path: string;
    readonly path: string;
    readonly sha256: string;
    readonly byte_length: number;
    readonly base64: string;
  };
  readonly sentinel: { readonly path: string; readonly sha256: string };
  readonly scratch: {
    readonly id: string;
    readonly marker: string;
    readonly snapshot_base64: string;
    readonly snapshot_sha256: string;
  };
  readonly harness: {
    readonly backend_port: number;
    readonly frontend_port: number;
    readonly normal_dashboard_port: number;
    readonly nonce: string;
  };
}

const fixturePath = process.env.COWORK_LIVE_FIXTURE_FILE;
if (fixturePath === undefined) throw new Error("COWORK_LIVE_FIXTURE_FILE is required");
const fixture = JSON.parse(await readFile(fixturePath, "utf-8")) as Fixture;
const expectedHarnessNonce = process.env.COWORK_LIVE_HARNESS_NONCE;
if (expectedHarnessNonce === undefined) {
  throw new Error("COWORK_LIVE_HARNESS_NONCE is required");
}
const backendBaseURL = process.env.COWORK_LIVE_BACKEND_URL;
if (backendBaseURL === undefined) {
  throw new Error("COWORK_LIVE_BACKEND_URL is required");
}

const digest = (bytes: Uint8Array): string =>
  createHash("sha256").update(bytes).digest("hex");

const fileDigest = async (file: string): Promise<string> => digest(await readFile(file));

const exists = async (target: string): Promise<boolean> => {
  try {
    await access(target);
    return true;
  } catch {
    return false;
  }
};

const treeDigest = async (root: string): Promise<string> => {
  const entries: string[] = [];
  const visit = async (directory: string): Promise<void> => {
    const names = (await readdir(directory)).sort((left, right) => left.localeCompare(right));
    for (const name of names) {
      const absolute = path.join(directory, name);
      const relative = path.relative(root, absolute).split(path.sep).join("/");
      const info = await lstat(absolute);
      if (info.isDirectory()) {
        entries.push(`d:${relative}`);
        await visit(absolute);
      } else if (info.isFile()) {
        const bytes = await readFile(absolute);
        entries.push(`f:${relative}:${bytes.length}:${digest(bytes)}`);
      } else {
        entries.push(`o:${relative}`);
      }
    }
  };
  await visit(root);
  return digest(Buffer.from(entries.join("\n"), "utf-8"));
};

const gotoCowork = async (page: Page, search = "?mode=launcher"): Promise<void> => {
  await page.goto(`/app/cowork${search}`, { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { level: 1, name: "Co-work" })).toBeVisible({
    timeout: 60_000,
  });
};

const mintBrowserIdentityBootstrap = async (page: Page): Promise<string> => {
  // The mint route exists only in the isolated live server. Calling it from the
  // directly served throwaway browser page supplies the exact Origin that will
  // redeem it. Human authority deliberately rejects Vite's proxy-marked requests.
  await page.goto(`${backendBaseURL}/app/`, { waitUntil: "domcontentloaded" });
  const result = await page.evaluate(async ({ nonce }) => {
    const denied = await fetch("/api/_cowork-live/identity-bootstrap", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ origin: window.location.origin }),
    });
    const mismatched = await fetch("/api/_cowork-live/identity-bootstrap", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-WB-Cowork-Live-Control": nonce,
      },
      body: JSON.stringify({ origin: "http://127.0.0.1:1" }),
    });
    const allowed = await fetch("/api/_cowork-live/identity-bootstrap", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-WB-Cowork-Live-Control": nonce,
      },
      body: JSON.stringify({ origin: window.location.origin }),
    });
    return {
      deniedStatus: denied.status,
      mismatchedStatus: mismatched.status,
      allowedStatus: allowed.status,
      payload: (await allowed.json()) as {
        ok?: boolean;
        token?: string;
        origin?: string;
        error?: string;
      },
      origin: window.location.origin,
    };
  }, { nonce: expectedHarnessNonce });
  expect(result.deniedStatus).toBe(403);
  expect(result.mismatchedStatus).toBe(403);
  expect(result.allowedStatus, JSON.stringify(result.payload)).toBe(200);
  expect(result.payload).toMatchObject({ ok: true, origin: result.origin });
  expect(result.payload.token).toMatch(/^wbb_/);
  return result.payload.token!;
};

const chooseFolder = async (
  page: Page,
  folder: { readonly name: string; readonly path: string },
  buttonName: string,
): Promise<void> => {
  await page.route(
    "**/api/truth/cowork/folders/choose",
    async (route) => {
      expect(route.request().headers()["x-work-buddy-intent"]).toBe(
        "cowork-folder-picker",
      );
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          cancelled: false,
          folder_name: folder.name,
          folder_path: folder.path,
          selection_token: null,
        }),
      });
    },
    { times: 1 },
  );
  await page.getByRole("button", { name: buttonName, exact: true }).click();
};

const chooseImportFile = async (
  page: Page,
  relativePath: string,
): Promise<void> => {
  await page.route(
    "**/api/truth/cowork/files/choose-import",
    async (route) => {
      expect(route.request().headers()["x-work-buddy-intent"]).toBe(
        "cowork-import-picker",
      );
      expect(route.request().postDataJSON()).toEqual({ store_id: ordinaryStoreId });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          cancelled: false,
          path: relativePath,
          importer_id: "markdown/v1",
          media_type: "text/markdown",
          source_sha256: fixture.source.sha256,
          importer: {
            importer_id: "markdown/v1",
            display_name: "Markdown",
            source_format: "markdown",
            media_type: "text/markdown",
            suffixes: [".md", ".markdown"],
            max_source_bytes: 16 * 1024 * 1024,
          },
        }),
      });
    },
    { times: 1 },
  );
  await page.getByRole("button", { name: "From file", exact: true }).click();
  await page.getByRole("button", { name: "Import", exact: true }).click();
};

const chooseLocation = async (
  page: Page,
  relativePath: string,
): Promise<void> => {
  await page.route(
    "**/api/truth/cowork/folders/choose-location",
    async (route) => {
      expect(route.request().headers()["x-work-buddy-intent"]).toBe(
        "cowork-location-picker",
      );
      expect(route.request().postDataJSON()).toEqual({ store_id: ordinaryStoreId });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          cancelled: false,
          path: relativePath,
        }),
      });
    },
    { times: 1 },
  );
  await page.getByRole("button", { name: "Change", exact: true }).click();
};

const waitForEditor = async (page: Page): Promise<ReturnType<Page["getByRole"]>> => {
  const editor = page.getByRole("textbox", { name: "Document editor" });
  await expect(editor).toBeVisible({ timeout: 60_000 });
  return editor;
};

const currentRouteIds = (page: Page): { storeId: string; documentId: string } => {
  const query = new URL(page.url()).searchParams;
  const storeId = query.get("store_id");
  const documentId = query.get("document_id");
  if (storeId === null || documentId === null) {
    throw new Error(`expected a registered Co-work route, received ${page.url()}`);
  }
  return { storeId, documentId };
};

interface BrowserOutboxRecord {
  readonly key: string;
  readonly nextId: number;
  readonly entries: readonly {
    readonly id: number;
    readonly acknowledged: boolean;
    readonly byteLength: number;
  }[];
}

const readBrowserOutbox = async (
  page: Page,
  key: string,
): Promise<BrowserOutboxRecord | undefined> =>
  page.evaluate(
    async ({ databaseName, storeName, recordKey }) =>
      new Promise<BrowserOutboxRecord | undefined>((resolve, reject) => {
        const request = indexedDB.open(databaseName, 1);
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
          const database = request.result;
          const transaction = database.transaction(storeName, "readonly");
          const get = transaction.objectStore(storeName).get(recordKey);
          get.onerror = () => reject(get.error);
          get.onsuccess = () => {
            const record = get.result as
              | {
                  key: string;
                  nextId: number;
                  entries: readonly {
                    id: number;
                    acknowledged: boolean;
                    batch: Uint8Array;
                  }[];
                }
              | undefined;
            resolve(
              record === undefined
                ? undefined
                : {
                    key: record.key,
                    nextId: record.nextId,
                    entries: record.entries.map((entry) => ({
                      id: entry.id,
                      acknowledged: entry.acknowledged,
                      byteLength: entry.batch.byteLength,
                    })),
                  },
            );
          };
          transaction.oncomplete = () => database.close();
          transaction.onerror = () => reject(transaction.error);
          transaction.onabort = () => reject(transaction.error);
        };
      }),
    {
      databaseName: "work-buddy-cowork-outbox",
      storeName: "ydoc-outbox",
      recordKey: key,
    },
  );

const seedLegacyScratch = async (page: Page): Promise<void> => {
  await page.goto("/app/", { waitUntil: "domcontentloaded" });
  await page.evaluate(
    async ({ snapshotBase64, snapshotSha256, scratchId }) => {
      const binary = atob(snapshotBase64);
      const snapshot = Uint8Array.from(binary, (character) => character.charCodeAt(0));
      await new Promise<void>((resolve, reject) => {
        const request = indexedDB.open("work-buddy-cowork", 1);
        request.onupgradeneeded = () => {
          if (!request.result.objectStoreNames.contains("cowork-ydoc")) {
            request.result.createObjectStore("cowork-ydoc", { keyPath: "key" });
          }
        };
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
          const database = request.result;
          const transaction = database.transaction("cowork-ydoc", "readwrite");
          transaction.objectStore("cowork-ydoc").put({
            key: `wb.cowork.ydoc.${scratchId}`,
            snapshot,
            snapshotSha256,
            log: [],
            baseOffset: 0,
          });
          transaction.oncomplete = () => {
            database.close();
            resolve();
          };
          transaction.onerror = () => reject(transaction.error);
          transaction.onabort = () => reject(transaction.error);
        };
      });
    },
    {
      snapshotBase64: fixture.scratch.snapshot_base64,
      snapshotSha256: fixture.scratch.snapshot_sha256,
      scratchId: fixture.scratch.id,
    },
  );
};

let ordinaryStoreId = "";
let firstDocumentId = "";
let importedDocumentId = "";

test.describe.serial("Co-work live lifecycle", () => {
  test.beforeEach(async ({ page }) => {
    // A browser runner cannot interact with a host-native modal. Keep the real Folder list
    // response and advertise the picker so each test can replace only the one choose call
    // with its explicit throwaway fixture path.
    await page.route(
      /\/api\/truth\/cowork\/folders(?:\?.*)?$/,
      async (route) => {
        const response = await route.fetch();
        const payload = (await response.json()) as Record<string, unknown>;
        await route.fulfill({
          response,
          json: {
            ...payload,
            chooser: { available: true, kind: "host_native" },
          },
        });
      },
    );
  });

  test("AC-ENV AC-01: production preview is isolated and first launch is honest", async ({
    page,
    request,
  }) => {
    expect(fixture.format).toBe("cowork-live-fixture/v1");
    expect(fixture.harness.backend_port).not.toBe(fixture.harness.normal_dashboard_port);
    expect(fixture.harness.frontend_port).not.toBe(fixture.harness.normal_dashboard_port);
    expect(await fileDigest(fixture.sentinel.path)).toBe(fixture.sentinel.sha256);

    const response = await request.get("/api/truth/cowork/folders");
    expect(response.ok()).toBe(true);
    expect(response.headers()["x-wb-cowork-live-harness"]).toBe(expectedHarnessNonce);
    const payload = (await response.json()) as {
      folders: readonly { store_id: string; folder_path: string }[];
    };
    expect(payload.folders).toEqual([
      expect.objectContaining({
        store_id: fixture.initialized.store_id,
        folder_path: fixture.initialized.path,
      }),
    ]);

    await gotoCowork(page);
    const lifecycle = page.locator(".wb-cowork-lifecycle");
    await expect(lifecycle.getByRole("button", { name: "Open folder", exact: true })).toBeVisible();
    await expect(lifecycle.getByRole("button", { name: "New", exact: true })).toBeVisible();
    await expect(
      lifecycle.getByRole("button", { name: "Open document", exact: true }),
    ).toBeDisabled();
    await expect(lifecycle.getByText("Choose a Folder for Co-work")).toHaveCount(0);
    await expect(lifecycle.getByRole("textbox", { name: /Folder path/i })).toHaveCount(0);
    await expect(lifecycle.getByRole("button", { name: "Inspect Folder" })).toHaveCount(0);
    await expect(lifecycle.locator(".ProseMirror")).toHaveCount(0);
    await expect(lifecycle.getByRole("tab")).toHaveCount(0);
    await expect(lifecycle.getByRole("separator")).toHaveCount(0);
    await expect(lifecycle).not.toContainText("No document open");
    for (const internalOrFalseStatus of [
      "Scope",
      "Truth store",
      "Document type",
      "Live",
      "In sync",
    ]) {
      await expect(lifecycle.getByText(internalOrFalseStatus, { exact: true })).toHaveCount(0);
    }
  });

  test("AC-02 AC-03A: inspect without mutation, set up an ordinary Folder, create, open, and reload", async ({
    page,
    request,
  }) => {
    const ordinaryBefore = await treeDigest(fixture.ordinary.path);
    await gotoCowork(page);

    let releaseInitialize!: () => void;
    let observeInitialize!: () => void;
    const initializeReached = new Promise<void>((resolve) => {
      observeInitialize = resolve;
    });
    const initializeGate = new Promise<void>((resolve) => {
      releaseInitialize = resolve;
    });
    await page.route(
      "**/api/truth/cowork/folders/initialize",
      async (route) => {
        observeInitialize();
        await initializeGate;
        await route.continue();
      },
      { times: 1 },
    );
    await chooseFolder(page, fixture.ordinary, "Open folder");
    expect(await treeDigest(fixture.ordinary.path)).toBe(ordinaryBefore);
    await expect(
      page.getByRole("heading", {
        name: `Set up Co-work in “${fixture.ordinary.name}”?`,
      }),
    ).toBeVisible();
    await expect(page.getByText(fixture.ordinary.path, { exact: true })).toBeVisible();
    await expect(page.getByText(/support data under/)).toContainText(".wbuddy");
    await page.getByRole("button", { name: "Set up Co-work" }).click();
    await initializeReached;
    releaseInitialize();

    await expect(
      page.getByRole("button", { name: fixture.ordinary.name, exact: true }).first(),
    ).toBeVisible({ timeout: 30_000 });
    const storeId = new URL(page.url()).searchParams.get("store_id");
    expect(storeId).toMatch(/^[0-9a-f]{32}$/);
    ordinaryStoreId = storeId ?? "";
    await expect(
      page.getByRole("button", { name: fixture.ordinary.name, exact: true }).first(),
    ).toBeVisible();

    expect(await fileDigest(path.join(fixture.ordinary.path, ".gitignore"))).toBe(
      fixture.ordinary.root_gitignore_sha256,
    );
    expect(await exists(path.join(fixture.ordinary.path, ".wbuddy", "truth"))).toBe(false);
    for (const required of ["store.yaml", "store.db", "blobs", "export", "runtime"] as const) {
      expect(
        await exists(path.join(fixture.ordinary.path, ".wbuddy", "cowork", required)),
      ).toBe(true);
    }
    const componentIgnore = await readFile(
      path.join(fixture.ordinary.path, ".wbuddy", "cowork", ".gitignore"),
      "utf-8",
    );
    for (const entry of ["/store.db", "/store.db-*", "/runtime/", "/blobs/"]) {
      expect(componentIgnore).toContain(entry);
    }
    const publishedManifest = await readFile(
      path.join(fixture.ordinary.path, ".wbuddy", "manifest.yaml"),
    );
    const originalManifest = Buffer.from(fixture.ordinary.unrelated_manifest_base64, "base64");
    for (const line of originalManifest.toString("utf-8").split(/\r?\n/).filter(Boolean)) {
      expect(publishedManifest.includes(Buffer.from(line, "utf-8"))).toBe(true);
    }
    expect(publishedManifest.toString("utf-8")).toMatch(/cowork:\s*\r?\n\s+path: cowork/);
    expect(
      await fileDigest(path.join(fixture.ordinary.path, ".wbuddy", "search", "state.bin")),
    ).toBe(fixture.ordinary.sibling_state_sha256);

    await mkdir(path.join(fixture.ordinary.path, "drafts"));
    await page.getByRole("button", { name: "New", exact: true }).click();
    await expect(page.getByRole("heading", { name: "New document" })).toBeVisible();
    await expect(page.getByText(/Document type/i)).toHaveCount(0);
    await page.getByLabel("Title").fill("First Working Note");
    await expect(page.getByLabel("File name")).toHaveValue(
      "first-working-note.md",
    );
    await chooseLocation(page, "drafts");
    await expect(
      page.getByText(`${fixture.ordinary.name} / drafts`, { exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Create document" }).click();
    const editor = await waitForEditor(page);
    await expect(editor).toHaveText("");
    await expect(editor).not.toContainText("Context bundle cache");
    ({ documentId: firstDocumentId } = currentRouteIds(page));
    expect(currentRouteIds(page).storeId).toBe(ordinaryStoreId);
    expect(
      await readFile(path.join(fixture.ordinary.path, "drafts", "first-working-note.md")),
    ).toEqual(Buffer.alloc(0));

    const documentResponse = await request.get(
      `/api/truth/doc/${firstDocumentId}?store_id=${ordinaryStoreId}`,
    );
    expect(documentResponse.ok()).toBe(true);
    const document = (await documentResponse.json()) as {
      initialization_state: string;
      structured_head_sha256: string;
      hashes: { ydoc_snapshot_sha256: string; last_materialized_sha256: string };
    };
    expect(document.initialization_state).toBe("ready");
    expect(document.structured_head_sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(document.hashes.ydoc_snapshot_sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(document.hashes.last_materialized_sha256).toBe(digest(Buffer.alloc(0)));

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(await waitForEditor(page)).toHaveText("");
    expect(currentRouteIds(page)).toEqual({
      storeId: ordinaryStoreId,
      documentId: firstDocumentId,
    });
  });

  test("AC-04: register existing Markdown without changing one source byte", async ({ page }) => {
    const before = await readFile(fixture.source.path);
    expect(digest(before)).toBe(fixture.source.sha256);
    await gotoCowork(page, `?store_id=${ordinaryStoreId}`);
    await expect(
      page.getByRole("button", { name: fixture.ordinary.name, exact: true }).first(),
    ).toBeVisible();
    await chooseImportFile(page, fixture.source.relative_path);
    const editor = await waitForEditor(page);
    await expect(editor).toContainText("Imported note");
    await expect(editor).toContainText("A line preserved exactly.");
    ({ documentId: importedDocumentId } = currentRouteIds(page));
    expect(await readFile(fixture.source.path)).toEqual(before);

    await page.reload({ waitUntil: "domcontentloaded" });
    const reloaded = await waitForEditor(page);
    await expect(reloaded).toContainText("Imported note");
    expect(await readFile(fixture.source.path)).toEqual(before);
  });

  test("AC-05: selected feedback opens and restores the real document conversation", async ({
    page,
    request,
  }) => {
    const quote = "A line preserved exactly.";
    const feedback = "Please make this line more concrete.";
    const reply = "I’ve got it. I’ll suggest a more concrete version for review.";
    const bindingUrl =
      `/api/truth/doc/${importedDocumentId}/conversation?store_id=${ordinaryStoreId}`;
    const resetAgent = await request.post(
      "/api/_cowork-live/agent-control",
      {
        headers: { "X-WB-Cowork-Live-Control": expectedHarnessNonce },
        data: { mode: "running", reset: true },
      },
    );
    expect(resetAgent.ok(), await resetAgent.text()).toBe(true);

    const before = await request.get(bindingUrl);
    expect(before.ok()).toBe(true);
    expect(await before.json()).toEqual(
      expect.objectContaining({
        ok: true,
        conversation_id: null,
        agent: expect.objectContaining({
          status: "not_started",
          started: false,
        }),
      }),
    );

    const conversationRequests: string[] = [];
    const conversationStarts: string[] = [];
    page.on("request", (observed) => {
      const parsed = new URL(observed.url());
      if (parsed.pathname.startsWith("/api/conversations/")) {
        conversationRequests.push(parsed.pathname);
      }
      if (
        parsed.pathname === `/api/truth/doc/${importedDocumentId}/conversation` &&
        observed.method() === "POST"
      ) {
        conversationStarts.push(parsed.pathname);
      }
    });

    await gotoCowork(
      page,
      `?store_id=${ordinaryStoreId}&document_id=${importedDocumentId}`,
    );
    await waitForEditor(page);
    await page.getByText(quote, { exact: true }).selectText();
    await page.getByRole("button", { name: "Give feedback", exact: true }).click();
    await page
      .getByRole("textbox", {
        name: "Feedback on the selected passage",
        exact: true,
      })
      .fill(feedback);

    const feedbackResponsePromise = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname ===
          `/api/truth/doc/${importedDocumentId}/feedback` &&
        response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Send feedback", exact: true }).click();
    const feedbackResponse = await feedbackResponsePromise;
    expect(feedbackResponse.ok()).toBe(true);
    const feedbackPayload = (await feedbackResponse.json()) as {
      ok: boolean;
      conversation_id: string;
      message_id: string;
      agent: {
        status: string;
        alive: boolean | null;
        started: boolean;
        error: string | null;
      };
    };
    expect(feedbackPayload.ok).toBe(true);
    expect(feedbackPayload.conversation_id).toMatch(/^[0-9a-f]{12}$/);
    expect(feedbackPayload.conversation_id).not.toContain("cowork-doc-");
    expect(feedbackPayload.message_id).not.toBe("");
    expect(feedbackPayload.agent).toEqual(
      expect.objectContaining({
        status: "running",
        alive: true,
        started: true,
        error: null,
      }),
    );

    await expect(page.getByText(feedback, { exact: true })).toBeVisible({
      timeout: 30_000,
    });
    const feedbackMessage = page
      .locator(".wb-chat-msg")
      .filter({ hasText: feedback });
    await expect(
      feedbackMessage.getByRole("button", {
        name: `Jump to passage: "${quote}"`,
      }),
    ).toBeVisible();
    const expectedConversationPath =
      `/api/conversations/${feedbackPayload.conversation_id}`;
    await expect
      .poll(() => conversationRequests.includes(expectedConversationPath))
      .toBe(true);
    expect(
      conversationRequests.some((pathname) => pathname.includes("cowork-doc-")),
    ).toBe(false);
    const spawnedAfterFeedback = await request.get(
      "/api/_cowork-live/agent-state",
      {
        headers: { "X-WB-Cowork-Live-Control": expectedHarnessNonce },
      },
    );
    expect(spawnedAfterFeedback.ok(), await spawnedAfterFeedback.text()).toBe(
      true,
    );
    expect(await spawnedAfterFeedback.json()).toEqual(
      expect.objectContaining({
        spawn_calls: 1,
        mode: "running",
        conversation_ids: [feedbackPayload.conversation_id],
      }),
    );

    // One mounted provider means one request on the house 3s poll cadence.
    const requestsBeforePoll = conversationRequests.filter(
      (pathname) => pathname === expectedConversationPath,
    ).length;
    await expect
      .poll(
        () =>
          conversationRequests.filter(
            (pathname) => pathname === expectedConversationPath,
          ).length,
        { timeout: 5_000 },
      )
      .toBeGreaterThan(requestsBeforePoll);
    await page.waitForTimeout(500);
    expect(
      conversationRequests.filter(
        (pathname) => pathname === expectedConversationPath,
      ).length - requestsBeforePoll,
    ).toBe(1);

    const replyResponse = await request.post(
      "/api/_cowork-live/conversation-reply",
      {
        headers: { "X-WB-Cowork-Live-Control": expectedHarnessNonce },
        data: {
          conversation_id: feedbackPayload.conversation_id,
          message: reply,
        },
      },
    );
    expect(replyResponse.ok(), await replyResponse.text()).toBe(true);
    await expect(page.getByText(reply, { exact: true })).toBeVisible({
      timeout: 15_000,
    });

    const startsBeforeReload = conversationStarts.length;
    await page.reload({ waitUntil: "domcontentloaded" });
    await waitForEditor(page);
    await expect(page.getByText(feedback, { exact: true })).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByText(reply, { exact: true })).toBeVisible();
    await expect(
      page.getByRole("button", {
        name: `Jump to passage: "${quote}"`,
      }),
    ).toBeVisible();
    expect(conversationStarts).toHaveLength(startsBeforeReload);
    const spawnedAfterReload = await request.get(
      "/api/_cowork-live/agent-state",
      {
        headers: { "X-WB-Cowork-Live-Control": expectedHarnessNonce },
      },
    );
    expect(spawnedAfterReload.ok(), await spawnedAfterReload.text()).toBe(true);
    expect(await spawnedAfterReload.json()).toEqual(
      expect.objectContaining({
        spawn_calls: 1,
        conversation_ids: [feedbackPayload.conversation_id],
      }),
    );

    const restored = await request.get(bindingUrl);
    expect(restored.ok()).toBe(true);
    expect(await restored.json()).toEqual(
      expect.objectContaining({
        ok: true,
        conversation_id: feedbackPayload.conversation_id,
      }),
    );
  });

  test("AC-05A: Working on and sibling docks keep editor, Verify, Review, and Chat concerns separated", async ({
    page,
  }) => {
    const quote = "A line preserved exactly.";
    await gotoCowork(
      page,
      `?store_id=${ordinaryStoreId}&document_id=${importedDocumentId}`,
    );
    const editor = await waitForEditor(page);
    const verifyTrigger = page.getByRole("button", {
      name: "Verify",
      exact: true,
    });
    const cothinkTrigger = page.getByRole("button", {
      name: "Co-think",
      exact: true,
    });
    const verifyPanel = page.locator("#wb-cowork-dock-panel-verify");
    const cothinkPanel = page.locator("#wb-cowork-dock-panel-cothink");
    const reviewPanel = page.locator("#wb-cowork-rail-panel-review");

    await expect(verifyTrigger).toHaveAttribute("aria-expanded", "false");
    await expect(cothinkTrigger).toHaveAttribute("aria-expanded", "false");
    await expect(verifyPanel).not.toBeVisible();
    await expect(cothinkPanel).not.toBeVisible();
    await expect(
      page.locator(".wb-cowork > .wb-cowork-action-dock"),
    ).toHaveCount(1);
    await expect(
      page.locator(".wb-cowork__editor-panel .wb-cowork-action-dock"),
    ).toHaveCount(0);
    const workspaceBox = await page.locator(".wb-cowork").boundingBox();
    const dockBox = await page
      .locator(".wb-cowork > .wb-cowork-action-dock")
      .boundingBox();
    expect(workspaceBox).not.toBeNull();
    expect(dockBox).not.toBeNull();
    expect(Math.abs((dockBox?.x ?? 0) - (workspaceBox?.x ?? 0))).toBeLessThan(1);
    expect(
      Math.abs((dockBox?.width ?? 0) - (workspaceBox?.width ?? 0)),
    ).toBeLessThan(1);
    await expect(reviewPanel).not.toContainText("Verify setup");
    await expect(
      reviewPanel.getByRole("button", { name: "Run Verify", exact: true }),
    ).toHaveCount(0);

    await page.getByText(quote, { exact: true }).selectText();
    const setBySelection = page.getByRole("button", {
      name: "Set by selection",
      exact: true,
    });
    await expect(setBySelection).toBeEnabled();
    await setBySelection.click();
    await expect(
      editor.locator('[data-wb-working-target="true"]'),
    ).toHaveText(quote);
    await expect(
      editor.locator('[data-wb-working-target-boundary="start"]'),
    ).toHaveCount(1);
    await expect(
      editor.locator('[data-wb-working-target-boundary="end"]'),
    ).toHaveCount(1);
    await expect(
      page.getByRole("region", { name: "Working on" }),
    ).toContainText("4 words");

    await verifyTrigger.click();
    await expect(verifyTrigger).toHaveAttribute("aria-expanded", "true");
    await expect(verifyPanel).toBeVisible();
    await expect(
      verifyPanel.getByRole("button", { name: "Run Verify", exact: true }),
    ).toBeVisible({ timeout: 30_000 });
    await expect(
      verifyPanel.getByRole("region", {
        name: "Verification checks",
        exact: true,
      }),
    ).toBeVisible();
    await expect(verifyPanel).not.toContainText("Sessions ·");
    await expect(verifyPanel).not.toContainText(
      "What should Verify accomplish?",
    );
    await expect(
      verifyPanel.getByRole("button", { name: "Add check", exact: true }),
    ).toBeVisible();
    await verifyPanel.getByText("Checks", { exact: true }).click();
    await expect(
      verifyPanel.getByRole("checkbox").first(),
    ).toBeVisible();

    await verifyPanel
      .getByRole("button", { name: "Add check", exact: true })
      .click();
    await expect(
      verifyPanel.getByRole("region", {
        name: "Add verification check",
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      verifyPanel.getByRole("button", { name: "Run Verify", exact: true }),
    ).toHaveCount(0);
    const checkName = verifyPanel.getByRole("textbox", {
      name: "Name",
      exact: true,
    });
    await checkName.fill("Keep this draft while switching sibling docks.");
    await cothinkTrigger.click();
    await expect(verifyTrigger).toHaveAttribute("aria-expanded", "false");
    await expect(cothinkTrigger).toHaveAttribute("aria-expanded", "true");
    await expect(verifyPanel).not.toBeVisible();
    await expect(cothinkPanel).toBeVisible();
    await expect(cothinkPanel).toHaveText("Planned");
    await verifyTrigger.click();
    await expect(cothinkPanel).not.toBeVisible();
    await expect(checkName).toHaveValue(
      "Keep this draft while switching sibling docks.",
    );
    await verifyPanel
      .getByRole("button", { name: "Close add check", exact: true })
      .click();
    await expect(
      verifyPanel.getByRole("button", { name: "Run Verify", exact: true }),
    ).toBeVisible();
    await verifyTrigger.click();
    await expect(verifyPanel).not.toBeVisible();

    await page.getByRole("tab", { name: /^Chat/ }).click();
    const chatPanel = page.locator("#wb-cowork-rail-panel-chat");
    await expect(chatPanel).toBeVisible();
    await expect(
      chatPanel.locator('[aria-label^="About:"]'),
    ).toContainText("About:");
    await expect(chatPanel).not.toContainText(
      "Use Working on for this message",
    );

    await page.getByRole("button", { name: "Clear", exact: true }).click();
  });

  test("AC-05B: failed agent start keeps feedback visible until an explicit restart", async ({
    page,
    request,
  }) => {
    const quote = "A line preserved exactly.";
    const feedback = "Keep this note even if chat cannot start.";
    const control = await request.post("/api/_cowork-live/agent-control", {
      headers: { "X-WB-Cowork-Live-Control": expectedHarnessNonce },
      data: { mode: "spawn_failed", reset: true },
    });
    expect(control.ok(), await control.text()).toBe(true);

    await gotoCowork(
      page,
      `?store_id=${ordinaryStoreId}&document_id=${importedDocumentId}`,
    );
    await waitForEditor(page);
    await page.getByText(quote, { exact: true }).selectText();
    await page
      .getByRole("button", { name: "Give feedback", exact: true })
      .click();
    await page
      .getByRole("textbox", {
        name: "Feedback on the selected passage",
        exact: true,
      })
      .fill(feedback);

    const feedbackResponsePromise = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname ===
          `/api/truth/doc/${importedDocumentId}/feedback` &&
        response.request().method() === "POST",
    );
    await page
      .getByRole("button", { name: "Send feedback", exact: true })
      .click();
    const feedbackResponse = await feedbackResponsePromise;
    expect(feedbackResponse.ok()).toBe(true);
    const feedbackPayload = (await feedbackResponse.json()) as {
      conversation_id: string;
      message_id: string;
      agent: { status: string; alive: boolean | null };
    };
    expect(feedbackPayload.agent).toEqual(
      expect.objectContaining({
        status: "spawn_failed",
        alive: false,
      }),
    );

    await expect(page.getByText(feedback, { exact: true })).toBeVisible({
      timeout: 30_000,
    });
    const failedFeedbackMessage = page
      .locator(".wb-chat-msg")
      .filter({ hasText: feedback });
    await expect(
      failedFeedbackMessage.getByRole("button", {
        name: `Jump to passage: "${quote}"`,
      }),
    ).toBeVisible();
    await expect(
      page.getByText("Chat couldn’t start.", { exact: true }),
    ).toBeVisible();
    const retryStart = page.getByRole("button", {
      name: "Try again",
      exact: true,
    });
    await expect(retryStart).toBeVisible();

    const failedState = await request.get("/api/_cowork-live/agent-state", {
      headers: { "X-WB-Cowork-Live-Control": expectedHarnessNonce },
    });
    expect(failedState.ok(), await failedState.text()).toBe(true);
    expect(await failedState.json()).toEqual(
      expect.objectContaining({
        spawn_calls: 1,
        mode: "spawn_failed",
        conversation_ids: [feedbackPayload.conversation_id],
      }),
    );

    const recover = await request.post("/api/_cowork-live/agent-control", {
      headers: { "X-WB-Cowork-Live-Control": expectedHarnessNonce },
      data: { mode: "running" },
    });
    expect(recover.ok(), await recover.text()).toBe(true);
    const retryResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname ===
          `/api/truth/doc/${importedDocumentId}/conversation` &&
        response.request().method() === "POST",
    );
    await retryStart.click();
    expect((await retryResponse).ok()).toBe(true);

    await expect(page.getByText(feedback, { exact: true })).toBeVisible();
    await expect(
      page.getByRole("textbox", { name: "Message" }),
    ).toBeVisible();
    const recoveredState = await request.get(
      "/api/_cowork-live/agent-state",
      {
        headers: { "X-WB-Cowork-Live-Control": expectedHarnessNonce },
      },
    );
    expect(recoveredState.ok(), await recoveredState.text()).toBe(true);
    expect(await recoveredState.json()).toEqual(
      expect.objectContaining({
        spawn_calls: 2,
        mode: "running",
        conversation_ids: [
          feedbackPayload.conversation_id,
          feedbackPayload.conversation_id,
        ],
      }),
    );
  });

  test("AC-08 AC-12: save exact Markdown, switch documents and Folders, and honor browser history", async ({
    page,
    request,
  }) => {
    await gotoCowork(
      page,
      `?store_id=${ordinaryStoreId}&document_id=${firstDocumentId}`,
    );
    await waitForEditor(page);
    await page.getByRole("button", { name: "New", exact: true }).click();
    await expect(page.getByRole("heading", { name: "New document" })).toBeVisible();
    await page.getByLabel("Title").fill("Second Working Note");
    await expect(page.getByLabel("File name")).toHaveValue(
      "second-working-note.md",
    );
    await page.getByRole("button", { name: "Create document" }).click();
    await expect
      .poll(() => currentRouteIds(page).documentId, { timeout: 30_000 })
      .not.toBe(firstDocumentId);
    await waitForEditor(page);
    const second = currentRouteIds(page);
    const secondPath = path.join(fixture.ordinary.path, "second-working-note.md");

    // Reload makes the catalog-derived mutation permissions authoritative after bootstrap.
    await page.reload({ waitUntil: "domcontentloaded" });
    const editor = await waitForEditor(page);
    await expect(editor).toHaveAttribute("aria-readonly", "false");
    const firstMarker = "Exact live Save marker";
    await editor.click();
    await page.keyboard.type(firstMarker);
    const syncStatus = page.locator(".wb-cowork__sync-status");
    await expect(syncStatus).toContainText("Unsaved changes", { timeout: 30_000 });
    expect(await readFile(secondPath, "utf-8")).toBe("");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(syncStatus).toContainText("Saved", { timeout: 30_000 });
    expect(await readFile(secondPath, "utf-8")).toBe(firstMarker);

    const shortcutSuffix = " via Ctrl+S";
    await editor.click();
    await editor.press("End");
    await page.keyboard.insertText(shortcutSuffix);
    await expect(syncStatus).toContainText("Unsaved changes", {
      timeout: 30_000,
    });
    await page.keyboard.press(process.platform === "darwin" ? "Meta+S" : "Control+S");
    await expect(syncStatus).toContainText("Saved", {
      timeout: 30_000,
    });
    expect(await readFile(secondPath, "utf-8")).toBe(`${firstMarker}${shortcutSuffix}`);

    const saved = (await (
      await request.get(`/api/truth/doc/${second.documentId}?store_id=${ordinaryStoreId}`)
    ).json()) as {
      structured_head_sha256: string;
      hashes: { current_file_sha256: string; last_materialized_sha256: string };
    };
    const expectedSavedHash = digest(Buffer.from(`${firstMarker}${shortcutSuffix}`, "utf-8"));
    expect(saved.hashes.current_file_sha256).toBe(expectedSavedHash);
    expect(saved.hashes.last_materialized_sha256).toBe(expectedSavedHash);
    expect(saved.structured_head_sha256).toMatch(/^[0-9a-f]{64}$/);

    await page.getByRole("button", { name: /Second Working Note/ }).click();
    const firstOption = page.getByRole("option", { name: /First Working Note/ });
    await firstOption.click();
    await expect
      .poll(() => currentRouteIds(page).documentId, { timeout: 30_000 })
      .toBe(firstDocumentId);
    await expect(await waitForEditor(page)).toHaveText("");
    expect(currentRouteIds(page).documentId).toBe(firstDocumentId);

    await page.goBack({ waitUntil: "domcontentloaded" });
    await expect(await waitForEditor(page)).toContainText(firstMarker);
    expect(currentRouteIds(page).documentId).toBe(second.documentId);
    await page.goForward({ waitUntil: "domcontentloaded" });
    await expect(await waitForEditor(page)).toHaveText("");
    expect(currentRouteIds(page).documentId).toBe(firstDocumentId);

    await chooseFolder(page, fixture.initialized, fixture.ordinary.name);
    await expect(
      page.getByRole("button", { name: fixture.initialized.name, exact: true }).first(),
    ).toBeVisible();
    expect(new URL(page.url()).searchParams.get("store_id")).toBe(
      fixture.initialized.store_id,
    );
    await page.goBack({ waitUntil: "domcontentloaded" });
    await expect(await waitForEditor(page)).toHaveText("");
    expect(currentRouteIds(page).documentId).toBe(firstDocumentId);
  });

  test("AC-PROV: direct-entry provenance is durable and owns selection actions", async ({
    page,
    request,
  }) => {
    const bootstrap = await mintBrowserIdentityBootstrap(page);
    // Same-origin GET does not normally carry Origin, but bootstrap source reads
    // are human-authority actions. Mirror a trusted browser launch's exact
    // loopback boundary across every request in this isolated page.
    await page.setExtraHTTPHeaders({ Origin: new URL(backendBaseURL).origin });
    await page.goto(
      `${backendBaseURL}/app/cowork?store_id=${fixture.initialized.store_id}` +
        `#wb-bootstrap=${encodeURIComponent(bootstrap)}`,
      { waitUntil: "domcontentloaded" },
    );
    await expect(page.getByRole("heading", { level: 1, name: "Co-work" })).toBeVisible({
      timeout: 60_000,
    });
    expect(new URL(page.url()).hash).not.toContain("wb-bootstrap");
    await expect
      .poll(
        async () =>
          page.evaluate(async () =>
            (await fetch("/api/truth/cowork/current-actor")).status,
          ),
        { timeout: 30_000 },
      )
      .toBe(200);

    await page.getByRole("button", { name: "New", exact: true }).click();
    await expect(page.getByRole("heading", { name: "New document" })).toBeVisible();
    await page.getByLabel("Title").fill("Provenance Working Note");
    await page.getByRole("button", { name: "Create document" }).click();
    const editor = await waitForEditor(page);
    await expect(editor).toHaveAttribute("aria-readonly", "false");
    await expect(editor).toHaveText("");
    const { documentId } = currentRouteIds(page);

    await editor.click();
    await page.keyboard.type("Test", { delay: 20 });
    await expect(editor).toHaveText("Test");

    // Entering Provenance finalizes the active typing burst. The stable rail owns
    // inspection while the contextual editor action becomes provenance-specific.
    await page.getByRole("tab", { name: "Provenance", exact: true }).click();
    await expect(page.getByRole("button", { name: "Give feedback", exact: true })).toHaveCount(0);

    const projectionUrl =
      `${backendBaseURL}/api/truth/doc/${documentId}` +
      `?store_id=${fixture.initialized.store_id}`;
    await expect
      .poll(async () => {
        const response = await request.get(projectionUrl);
        if (!response.ok()) return [{ status: response.status() }];
        const payload = (await response.json()) as {
          provenance?: {
            spans?: readonly {
              span?: { exact?: string } | null;
              target?: { currentness?: string };
              effective_attestation?: {
                source?: { kind?: string };
                basis?: { kind?: string };
                authorship?: {
                  kind?: string;
                  contributors?: readonly {
                    kind?: string;
                    identity_status?: string;
                  }[];
                };
                human_review?: { status?: string };
              } | null;
            }[];
          };
        };
        return (payload.provenance?.spans ?? [])
          .filter((span) => span.target?.currentness === "current")
          .map((span) => ({
            exact: span.span?.exact,
            source: span.effective_attestation?.source?.kind,
            basis: span.effective_attestation?.basis?.kind,
            authorship: span.effective_attestation?.authorship?.kind,
            contributors:
              span.effective_attestation?.authorship?.contributors?.map(
                (contributor) => ({
                  kind: contributor.kind,
                  identity_status: contributor.identity_status,
                }),
              ),
            review: span.effective_attestation?.human_review?.status,
          }));
      }, { timeout: 30_000 })
      .toEqual([
        {
          exact: "Test",
          source: "direct_entry",
          basis: "automatic_direct_entry_attribution",
          authorship: "human",
          contributors: [
            {
              kind: "human",
              identity_status: "local_actor_ref",
            },
          ],
          review: "not_applicable",
        },
      ]);
    const decoratedPassage = editor.locator(
      "[data-wb-decoration='provenance-overlay']",
      { hasText: "Test" },
    );
    await expect(decoratedPassage).toBeVisible({ timeout: 30_000 });

    // Select after the Provenance lens and its overlay are already active. This
    // is the normal inspection gesture and exercises the selection-action and
    // passive-hover lifecycles together.
    const passageBox = await decoratedPassage.boundingBox();
    expect(passageBox).not.toBeNull();
    await page.mouse.move(
      passageBox!.x + passageBox!.width - 1,
      passageBox!.y + passageBox!.height / 2,
    );
    await page.mouse.down();
    await page.mouse.move(
      passageBox!.x + 1,
      passageBox!.y + passageBox!.height / 2,
      { steps: 8 },
    );
    await page.mouse.up();
    await expect
      .poll(() => page.evaluate(() => window.getSelection()?.toString() ?? ""))
      .toBe("Test");
    await expect(page.getByRole("tooltip")).toHaveCount(0);
    const provenanceAction = page.getByRole("button", {
      name: "View provenance",
      exact: true,
    });
    await expect(provenanceAction).toBeVisible({ timeout: 5_000 });
  });

  test("Close Folder returns to the Folder launcher and can reopen it without a native picker", async ({
    page,
  }) => {
    await gotoCowork(page, `?store_id=${ordinaryStoreId}`);
    await expect(
      page.getByRole("button", { name: fixture.ordinary.name, exact: true }).first(),
    ).toBeVisible();

    await page.getByRole("button", { name: "Close folder", exact: true }).click();

    await expect(page).toHaveURL(/[?&]mode=launcher(?:&|$)/);
    const openFolder = page.getByRole("button", { name: "Open folder", exact: true });
    await expect(openFolder).toBeVisible();
    await expect(openFolder).toBeFocused();
    await expect(page.getByRole("heading", { name: "Folders", exact: true })).toBeVisible();

    await page
      .getByRole("button", { name: fixture.ordinary.name, exact: true })
      .click();
    await expect(page).toHaveURL(new RegExp(`store_id=${ordinaryStoreId}`));
    await expect(
      page.getByRole("button", { name: "Close folder", exact: true }),
    ).toBeVisible();
  });

  test("AC-09: legacy local writing is recovered and removed only after saving opens", async ({
    page,
  }) => {
    await seedLegacyScratch(page);
    await gotoCowork(page, "?mode=launcher");
    const recovered = page.getByRole("button", {
      name: /Recovered document.*Recovered from an earlier session/,
    });
    await expect(recovered).toContainText("Recovered from an earlier session");
    await recovered.click();
    const editor = await waitForEditor(page);
    await expect(editor).toContainText(fixture.scratch.marker);
    await chooseFolder(page, fixture.ordinary, "Save document");
    await expect(page.getByRole("heading", { name: "New document" })).toBeVisible();
    await expect(page.getByLabel("File name")).toHaveValue("recovered-document.md");
    await page.getByRole("button", { name: "Create document" }).click();
    await expect
      .poll(() => new URL(page.url()).searchParams.get("document_id"), { timeout: 30_000 })
      .toMatch(/^[0-9a-f]{32}$/);
    await expect(await waitForEditor(page)).toContainText(fixture.scratch.marker);
    expect(await readFile(path.join(fixture.ordinary.path, "recovered-document.md"), "utf-8")).toBe(
      fixture.scratch.marker,
    );

    await page.getByRole("button", { name: "Close document" }).click();
    await expect(page.getByText("Recovered document")).toHaveCount(0);
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.getByText("Recovered document")).toHaveCount(0);
  });

  test("AC-10: offline edits survive reload and retry in order", async ({ page, request }) => {
    await gotoCowork(
      page,
      `?store_id=${ordinaryStoreId}&document_id=${firstDocumentId}`,
    );
    const editor = await waitForEditor(page);
    const file = path.join(
      fixture.ordinary.path,
      "drafts",
      "first-working-note.md",
    );
    const recoveryExport = path.join(
      fixture.ordinary.path,
      ".wbuddy",
      "cowork",
      "export",
      "claims.jsonl",
    );
    expect(await readFile(file, "utf-8")).toBe("");
    // Opening this blank document created a legitimate structural update tail. The
    // recovery export must stay absent until a later compaction can represent it.
    expect(await exists(recoveryExport)).toBe(false);

    let rejectedPushes = 0;
    const ydocRoute = new RegExp(
      `/api/truth/doc/${firstDocumentId}/ydoc(?:\\?|$)`,
    );
    const rejectPush = async (route: Route): Promise<void> => {
      if (route.request().method() === "POST") {
        rejectedPushes += 1;
        await route.abort("internetdisconnected");
        return;
      }
      await route.continue();
    };
    await page.route(ydocRoute, rejectPush);

    const firstOfflineEdit = "Offline recovery marker";
    const laterOfflineEdit = " plus later write";
    await editor.click();
    await page.keyboard.insertText(firstOfflineEdit);
    const syncStatus = page.locator(".wb-cowork__sync-status");
    await expect(syncStatus).toContainText("Saved in this browser", {
      timeout: 30_000,
    });
    const outboxKey = `${ordinaryStoreId}:${firstDocumentId}`;
    await expect
      .poll(() => readBrowserOutbox(page, outboxKey), { timeout: 30_000 })
      .toMatchObject({
        key: outboxKey,
        entries: [{ acknowledged: false }],
      });
    const firstOutbox = await readBrowserOutbox(page, outboxKey);
    expect(firstOutbox).toBeDefined();
    await editor.press("End");
    await page.keyboard.insertText(laterOfflineEdit);
    await expect
      .poll(async () => (await readBrowserOutbox(page, outboxKey))?.nextId, {
        timeout: 30_000,
      })
      .toBeGreaterThan(firstOutbox!.nextId);
    await expect
      .poll(() => rejectedPushes, { timeout: 30_000 })
      .toBeGreaterThan(0);
    expect(await readFile(file, "utf-8")).toBe("");
    expect(await exists(recoveryExport)).toBe(false);

    await page.reload({ waitUntil: "domcontentloaded" });
    const recoveredEditor = await waitForEditor(page);
    await expect(page.locator(".wb-cowork__sync-status")).toContainText(
      "Saved in this browser",
      { timeout: 30_000 },
    );
    const reopenedOutbox = await readBrowserOutbox(page, outboxKey);
    expect(reopenedOutbox).toMatchObject({
      key: outboxKey,
      nextId: expect.any(Number),
    });
    expect(reopenedOutbox?.entries.some((entry) => !entry.acknowledged)).toBe(true);
    await expect(recoveredEditor).toHaveText(`${firstOfflineEdit}${laterOfflineEdit}`);
    expect(await readFile(file, "utf-8")).toBe("");

    await page.unroute(ydocRoute, rejectPush);
    await page.getByRole("button", { name: "Sync now" }).click();
    await expect(page.locator(".wb-cowork__sync-status")).toContainText(
      "Unsaved changes",
      { timeout: 30_000 },
    );
    expect(await readFile(file, "utf-8")).toBe("");
    expect(await exists(recoveryExport)).toBe(false);

    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.locator(".wb-cowork__sync-status")).toContainText(
      "Saved",
      { timeout: 30_000 },
    );
    expect(await exists(recoveryExport)).toBe(true);
    const recoveredText = `${firstOfflineEdit}${laterOfflineEdit}`;
    expect(await readFile(file, "utf-8")).toBe(recoveredText);
    const documentResponse = await request.get(
      `/api/truth/doc/${firstDocumentId}?store_id=${ordinaryStoreId}`,
    );
    expect(documentResponse.ok()).toBe(true);
    const document = (await documentResponse.json()) as {
      hashes: { current_file_sha256: string; last_materialized_sha256: string };
    };
    const recoveredHash = digest(Buffer.from(recoveredText, "utf-8"));
    expect(document.hashes.current_file_sha256).toBe(recoveredHash);
    expect(document.hashes.last_materialized_sha256).toBe(recoveredHash);
  });

  test("AC-15: a real proposal is reviewed and applied through one sitting", async ({
    page,
    request,
  }) => {
    test.skip(
      process.env.COWORK_LIVE_SKIP_AC15 === "1",
      "diagnostic iteration for lifecycle cases after the known proposal-origin defect",
    );
    const quote = "A line preserved exactly.";
    const replacement = "A line accepted through live review.";
    const seeded = await request.post("/api/_cowork-live/seed-proposal", {
      headers: { "X-WB-Cowork-Live-Control": expectedHarnessNonce },
      data: {
        store_id: ordinaryStoreId,
        document_id: importedDocumentId,
        quote,
        replacement,
      },
    });
    const seededPayload = (await seeded.json()) as {
      ok?: boolean;
      error?: string;
      proposal_id: string;
      canonical_sha256: string;
    };
    expect(seeded.ok(), JSON.stringify(seededPayload)).toBe(true);
    expect(seededPayload.proposal_id).toMatch(/^[0-9a-f]{32}$/);
    expect(seededPayload.canonical_sha256).toMatch(/^[0-9a-f]{64}$/);

    const ydocPath = `/api/truth/doc/${importedDocumentId}/ydoc`;
    const ydocUrl = `${ydocPath}?store_id=${ordinaryStoreId}`;
    const canonicalResponse = await request.get(ydocUrl);
    expect(canonicalResponse.ok()).toBe(true);
    const canonicalFrames = parseFrames(new Uint8Array(await canonicalResponse.body()));
    const canonical = new Y.Doc();
    for (const frame of canonicalFrames) Y.applyUpdate(canonical, frame);
    const canonicalProjection = canonical.getXmlFragment("default").toJSON();
    const canonicalHeaders = canonicalResponse.headers();
    const beforeDocumentResponse = await request.get(
      `/api/truth/doc/${importedDocumentId}?store_id=${ordinaryStoreId}`,
    );
    expect(beforeDocumentResponse.ok()).toBe(true);
    const beforeDocument = (await beforeDocumentResponse.json()) as {
      structured_head_sha256: string;
    };

    const openedAt = Date.now();
    const unexpectedPushes: Array<Promise<Record<string, unknown>>> = [];
    page.on("request", (observed) => {
      const parsed = new URL(observed.url());
      if (observed.method() !== "POST" || parsed.pathname !== ydocPath) return;
      unexpectedPushes.push(
        Promise.resolve().then(() => {
          const headers = observed.headers();
          const bytes = new Uint8Array(observed.postDataBuffer() ?? new Uint8Array());
          const compacted = headers["x-wb-compacted-snapshot-sha256"] !== undefined;
          const segments = compacted ? parseFrames(bytes) : [bytes];
          const batch = segments[0] ?? new Uint8Array();
          const afterBatch = new Y.Doc();
          for (const frame of canonicalFrames) Y.applyUpdate(afterBatch, frame);
          if (batch.byteLength > 0) Y.applyUpdate(afterBatch, batch);
          const afterBatchProjection = afterBatch.getXmlFragment("default").toJSON();
          afterBatch.destroy();
          let compactedProjection: string | null = null;
          if (compacted && segments[1] !== undefined) {
            const compactedDocument = new Y.Doc();
            Y.applyUpdate(compactedDocument, segments[1]);
            compactedProjection = compactedDocument.getXmlFragment("default").toJSON();
            compactedDocument.destroy();
          }
          return {
            elapsed_ms: Date.now() - openedAt,
            body_byte_length: bytes.byteLength,
            body_sha256: digest(bytes),
            base_sha256: headers["x-wb-base-sha256"] ?? null,
            base_ydoc_sha256: headers["x-wb-base-ydoc-sha256"] ?? null,
            compacted_snapshot_sha256:
              headers["x-wb-compacted-snapshot-sha256"] ?? null,
            segment_byte_lengths: segments.map((segment) => segment.byteLength),
            segment_sha256: segments.map((segment) => digest(segment)),
            before_projection: canonicalProjection,
            after_batch_projection: afterBatchProjection,
            compacted_projection: compactedProjection,
          };
        }),
      );
    });

    await gotoCowork(
      page,
      `?store_id=${ordinaryStoreId}&document_id=${importedDocumentId}`,
    );
    const editor = await waitForEditor(page);
    const proposal = page.locator(".wb-cowork-rail__card").filter({
      hasText: "Use the reviewed wording.",
    });
    await expect(proposal).toBeVisible({ timeout: 30_000 });
    // Give proposal projection and persistence scheduling a bounded quiet window. Merely
    // displaying view-only decorations must not create a durable Y.Doc write or advance
    // the server head: only a human edit may enter the outbox before sitting commit.
    await page.waitForTimeout(1_500);
    const outboxKey = `${ordinaryStoreId}:${importedDocumentId}`;
    const proposalOpenOutbox = await readBrowserOutbox(page, outboxKey);
    const pushEvidence = await Promise.all(unexpectedPushes);
    const afterOpenDocumentResponse = await request.get(
      `/api/truth/doc/${importedDocumentId}?store_id=${ordinaryStoreId}`,
    );
    expect(afterOpenDocumentResponse.ok()).toBe(true);
    const afterOpenDocument = (await afterOpenDocumentResponse.json()) as {
      structured_head_sha256: string;
    };
    await test.info().attach("ac15-proposal-open-persistence-evidence", {
      body: JSON.stringify(
        {
          canonical_frame_byte_lengths: canonicalFrames.map((frame) => frame.byteLength),
          canonical_frame_sha256: canonicalFrames.map((frame) => digest(frame)),
          canonical_projection: canonicalProjection,
          canonical_headers: {
            snapshot_sha256: canonicalHeaders["x-wb-snapshot-sha256"] ?? null,
            structured_head_sha256:
              canonicalHeaders["x-wb-ydoc-head-sha256"] ??
              canonicalHeaders["x-wb-structured-head-sha256"] ??
              null,
            projection_sha256: canonicalHeaders["x-wb-projection-sha256"] ?? null,
          },
          server_structured_head_before: beforeDocument.structured_head_sha256,
          server_structured_head_after_open: afterOpenDocument.structured_head_sha256,
          browser_outbox_after_open: proposalOpenOutbox ?? null,
          observed_pushes: pushEvidence,
        },
        null,
        2,
      ),
      contentType: "application/json",
    });
    expect(pushEvidence, JSON.stringify(pushEvidence, null, 2)).toEqual([]);
    expect(afterOpenDocument.structured_head_sha256).toBe(
      beforeDocument.structured_head_sha256,
    );
    expect(proposalOpenOutbox?.entries.some((entry) => !entry.acknowledged) ?? false).toBe(
      false,
    );
    canonical.destroy();
    await proposal.locator(".wb-cowork-rail__card-select").click();
    await page.getByRole("button", { name: "Accept", exact: true }).click();
    await expect(proposal).toContainText("Decision: Accept");
    await page.getByRole("button", { name: "Apply decisions (1)" }).click();
    await expect
      .poll(
        async () => {
          const response = await request.get(
            `/api/truth/doc/${importedDocumentId}?store_id=${ordinaryStoreId}`,
          );
          if (!response.ok()) return -1;
          const payload = (await response.json()) as { open_proposals: readonly unknown[] };
          return payload.open_proposals.length;
        },
        { timeout: 30_000 },
      )
      .toBe(0);
    await expect(proposal).toHaveCount(0, { timeout: 30_000 });

    const expected = Buffer.from(
      "# Imported note\n\nA line accepted through live review.\n",
      "utf-8",
    );
    await expect(editor).toContainText(replacement);
    await expect
      .poll(() => readFile(fixture.source.path), { timeout: 30_000 })
      .toEqual(expected);
    const documentResponse = await request.get(
      `/api/truth/doc/${importedDocumentId}?store_id=${ordinaryStoreId}`,
    );
    expect(documentResponse.ok()).toBe(true);
    const document = (await documentResponse.json()) as {
      open_proposals: readonly unknown[];
      hashes: { current_file_sha256: string; last_materialized_sha256: string };
    };
    expect(document.open_proposals).toHaveLength(0);
    expect(document.hashes.current_file_sha256).toBe(digest(expected));
    expect(document.hashes.last_materialized_sha256).toBe(digest(expected));
  });

  test("AC-13 AC-17: external Markdown cannot be overwritten and can be explicitly re-imported", async ({
    page,
    request,
  }) => {
    await gotoCowork(
      page,
      `?store_id=${ordinaryStoreId}&document_id=${importedDocumentId}`,
    );
    await waitForEditor(page);
    const external = Buffer.from(
      "# Imported note\n\nExternal author changed this line.\n",
      "utf-8",
    );
    await writeFile(fixture.source.path, external);
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));

    const reviewExternal = page.getByRole("button", {
      name: "Review file changes",
    });
    await expect(reviewExternal).toBeVisible({ timeout: 30_000 });
    const save = page.getByRole("button", { name: "Save", exact: true });
    await expect(save).toBeDisabled();
    await page.keyboard.press("Control+s");
    expect(await readFile(fixture.source.path)).toEqual(external);

    await reviewExternal.click();
    await expect(
      page.getByRole("heading", { name: "Review external Markdown changes" }),
    ).toBeVisible();
    const changes = page.locator('pre[aria-label="Markdown changes"]');
    await expect(changes).toContainText("External author changed this line.");
    expect(await readFile(fixture.source.path)).toEqual(external);
    await page.getByRole("button", { name: "Continue to replacement" }).click();
    await expect(page.getByText("Replacement confirmation", { exact: true })).toBeVisible();
    expect(await readFile(fixture.source.path)).toEqual(external);
    const committed = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        new URL(response.url()).pathname.endsWith("/commit") &&
        new URL(response.url()).pathname.includes(`/reimport/`),
      { timeout: 30_000 },
    );
    await page.getByRole("button", { name: "Replace Co-work document" }).click();
    const commitResponse = await committed;
    const commitBody = await commitResponse.text();
    expect(commitResponse.ok(), commitBody).toBe(true);
    await expect(
      page.getByRole("heading", { name: "Review external Markdown changes" }),
    ).toHaveCount(0, { timeout: 30_000 });

    const reopened = await waitForEditor(page);
    await expect(reopened).toContainText("External author changed this line.");
    await expect(page.locator(".wb-cowork__sync-status")).toContainText(
      "Saved",
      { timeout: 30_000 },
    );
    expect(await readFile(fixture.source.path)).toEqual(external);
    const documentResponse = await request.get(
      `/api/truth/doc/${importedDocumentId}?store_id=${ordinaryStoreId}`,
    );
    expect(documentResponse.ok()).toBe(true);
    const document = (await documentResponse.json()) as {
      drift: { state: string };
      hashes: { current_file_sha256: string; last_materialized_sha256: string };
    };
    expect(document.drift.state).toBe("clean");
    expect(document.hashes.current_file_sha256).toBe(digest(external));
    expect(document.hashes.last_materialized_sha256).toBe(digest(external));
  });

  test("AC-18: removing a document from Co-work retains its Markdown and history", async ({
    page,
    request,
  }) => {
    await gotoCowork(
      page,
      `?store_id=${ordinaryStoreId}&document_id=${importedDocumentId}`,
    );
    await waitForEditor(page);
    const retained = await readFile(fixture.source.path);
    await page.getByRole("button", { name: "More document actions" }).click();
    const remove = page.getByRole("menuitem", { name: "Remove from Co-work", exact: true });
    await expect(remove).toBeEnabled({ timeout: 30_000 });
    await remove.click();
    const retirementDialog = page.getByRole("dialog", { name: "Remove from Co-work?" });
    await expect(retirementDialog).toBeVisible();
    await expect(page.getByText("Confirm the exact consequence", { exact: true })).toBeVisible();
    await expect(page.getByText("It is not a file deletion.", { exact: false })).toBeVisible();
    const retired = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname.endsWith(`/${importedDocumentId}/retire`) &&
        response
          .request()
          .postDataJSON()
          ?.intent_id !== undefined,
      { timeout: 30_000 },
    );
    await retirementDialog
      .getByRole("button", { name: "Remove from Co-work", exact: true })
      .click();
    const retirementResponse = await retired;
    expect(retirementResponse.ok(), await retirementResponse.text()).toBe(true);
    await expect(retirementDialog).toHaveCount(0, { timeout: 30_000 });

    await expect(
      page.getByRole("button", { name: fixture.ordinary.name, exact: true }).first(),
    ).toBeVisible({ timeout: 30_000 });
    expect(await readFile(fixture.source.path)).toEqual(retained);
    const active = await request.get(
      `/api/truth/doc/list?store_id=${ordinaryStoreId}`,
    );
    expect(active.ok()).toBe(true);
    const activePayload = (await active.json()) as {
      docs: readonly { document_id: string }[];
    };
    expect(activePayload.docs.some((document) => document.document_id === importedDocumentId)).toBe(
      false,
    );
    const recovery = await request.get(
      `/api/truth/doc/list?store_id=${ordinaryStoreId}&include_retired=1`,
    );
    expect(recovery.ok()).toBe(true);
    const recoveryPayload = (await recovery.json()) as {
      docs: readonly {
        document_id: string;
        lifecycle: string;
        path: string;
      }[];
    };
    expect(recoveryPayload.docs).toContainEqual(
      expect.objectContaining({
        document_id: importedDocumentId,
        lifecycle: "retired",
        path: fixture.source.relative_path,
      }),
    );
  });

  test("AC-19: the live workspace is accessible by axe, keyboard, and narrow peer panes", async ({
    page,
  }) => {
    await gotoCowork(
      page,
      `?store_id=${ordinaryStoreId}&document_id=${firstDocumentId}`,
    );
    await waitForEditor(page);
    await page.addScriptTag({ path: "node_modules/axe-core/axe.min.js" });
    const violations = await page.evaluate(async () => {
      type AxeViolation = {
        id: string;
        impact: string | null;
        help: string;
        nodes: readonly { target: readonly string[]; failureSummary?: string }[];
      };
      const axeWindow = window as typeof window & {
        axe: {
          run(
            context: Element,
            options: { resultTypes: readonly string[] },
          ): Promise<{ violations: readonly AxeViolation[] }>;
        };
      };
      const lifecycle = document.querySelector(".wb-cowork-lifecycle");
      if (lifecycle === null) throw new Error("Co-work lifecycle root is missing");
      const result = await axeWindow.axe.run(lifecycle, {
        resultTypes: ["violations"],
      });
      return result.violations
        .filter(
          (violation) =>
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

    await page
      .getByRole("button", { name: /First Working Note drafts\/first-working-note\.md/ })
      .click();
    const search = page.getByRole("textbox", { name: "Search documents" });
    await expect(search).toBeFocused();
    await search.fill("Second Working Note");
    const second = page.getByRole("option", {
      name: /Second Working Note.*second-working-note\.md/,
    });
    await second.focus();
    await page.keyboard.press("Enter");
    await expect
      .poll(() => new URL(page.url()).searchParams.get("document_id"), {
        timeout: 30_000,
      })
      .not.toBe(firstDocumentId);
    await waitForEditor(page);

    await page.setViewportSize({ width: 390, height: 844 });
    const tabs = page.getByRole("tablist", { name: "Co-work panes" });
    await expect(tabs).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Verify", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Co-think", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Verify", exact: true }),
    ).toHaveAttribute("aria-expanded", "false");
    const editorTab = tabs.getByRole("tab", { name: "Editor" });
    const reviewTab = tabs.getByRole("tab", { name: "Review" });
    const chatTab = tabs.getByRole("tab", { name: "Chat" });
    await expect(editorTab).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#wb-cowork-mobile-panel-editor")).toBeVisible();
    await expect(page.locator("#wb-cowork-rail-panel-review")).not.toBeVisible();
    await editorTab.focus();
    await page.keyboard.press("ArrowRight");
    await expect(reviewTab).toBeFocused();
    await expect(reviewTab).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#wb-cowork-mobile-panel-editor")).not.toBeVisible();
    await expect(page.locator("#wb-cowork-rail-panel-review")).toBeVisible();
    await chatTab.click();
    await expect(chatTab).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#wb-cowork-rail-panel-review")).not.toBeVisible();
    await expect(page.locator("#wb-cowork-rail-panel-chat")).toBeVisible();
  });

  test("AC-20 @firefox-smoke: production build ignores the demo query and store-only routes stay launchers", async ({
    page,
  }) => {
    await gotoCowork(page, "?cowork_fixture=demo&mode=launcher");
    await expect(page.getByRole("button", { name: "Open folder" })).toBeVisible();
    await expect(page.getByRole("button", { name: "New", exact: true })).toBeVisible();
    await expect(page.getByText("Context bundle cache")).toHaveCount(0);
    await expect(page.getByRole("textbox", { name: "Document editor" })).toHaveCount(0);

    await gotoCowork(page, `?store_id=${fixture.initialized.store_id}`);
    await expect(
      page.getByRole("button", { name: fixture.initialized.name, exact: true }).first(),
    ).toBeVisible();
    await expect(page.getByRole("textbox", { name: "Document editor" })).toHaveCount(0);
    await expect(
      page.locator(".wb-cowork-lifecycle").getByText("Live", { exact: true }),
    ).toHaveCount(0);
  });
});

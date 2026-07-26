import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  asViewId,
  asWidgetInstanceId,
  asWidgetTypeId,
  type DashboardIntent,
} from "../../../dashboard/contributions/contracts";
import { COWORK_INTENTS } from "../contracts";
import { bootstrapCoworkYdoc } from "../documents/bootstrapCoworkYdoc";
import { frameSegments } from "../persistence/framing";
import { CoworkScratchRegistry } from "../scratch/registry";
import {
  coworkSessionDurability,
  createCoworkSessionDurabilityController,
  registeredSessionDurabilityKey,
  scratchSessionDurabilityKey,
} from "../session/CoworkSessionDurability";
import { CoworkHttpClient } from "./CoworkHttpClient";
import {
  HttpCoworkProvider,
  type CoworkLocationAdapter,
} from "./HttpCoworkProvider";

class MemoryLocation implements CoworkLocationAdapter {
  search: string;
  readonly listeners = new Set<(search: string) => void>();

  constructor(search = "") {
    this.search = search;
  }

  getSearch = () => this.search;
  pushSearch = (search: string) => {
    this.search = search;
    for (const listener of this.listeners) listener(search);
  };
  replaceSearch = this.pushSearch;
  subscribe = (listener: (search: string) => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
}

const json = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const validYdocResponse = async (): Promise<Response> => {
  const initialized = await bootstrapCoworkYdoc(
    new TextEncoder().encode("# Ready document\n"),
  );
  if (!initialized.ok) throw new Error(initialized.message);
  return new Response(frameSegments([initialized.snapshot]) as BodyInit, {
    status: 200,
    headers: {
      "Content-Type": "application/octet-stream",
      "X-WB-Snapshot-Sha256": initialized.snapshotSha256,
      "X-WB-Ydoc-Head-Sha256": initialized.snapshotSha256,
      "X-WB-Ydoc-Generation": "test-generation",
      "X-WB-Next-Offset": "0",
    },
  });
};

const folder = {
  store_id: "store-1",
  folder_name: "work-buddy",
  folder_path: "C:/Projects/work-buddy",
  layout: "wbuddy_cowork_v1",
  reachable: true,
  eligibility: "eligible",
  ineligible_reason: null,
  document_surface: {
    enabled: true,
    allowed_document_classes: ["co_authored"],
    feedback_capture: true,
  },
  permissions: {
    read: true,
    create: true,
    import: true,
    materialize: true,
    retire: true,
  },
  document_count: 0,
};

const document = (id: string, initializationState = "ready") => ({
  document_id: id,
  path: `docs/${id}.md`,
  title: id === "old" ? "Current document" : "Needs repair",
  document_class: "co_authored",
  profile: "co_authored",
  lifecycle: "active",
  initialization_state: initializationState,
  drift_state: "clean",
  open_proposal_count: 0,
  open_flag_count: 0,
  permissions: {
    open: initializationState === "ready",
    edit: true,
    materialize: true,
    repair: initializationState !== "ready",
    retire: true,
  },
});

const intent = (intentType: string, payload: Record<string, unknown>): DashboardIntent => ({
  intent_type: intentType,
  schema_version: 1,
  intent_id: `intent-${intentType}`,
  view_id: asViewId("wb.cowork.workspace"),
  instance_id: asWidgetInstanceId("wb-cowork:workspace"),
  payload,
});

const widgetRequest = {
  viewId: asViewId("wb.cowork.workspace"),
  instanceId: asWidgetInstanceId("wb-cowork:workspace"),
};

describe("HttpCoworkProvider", () => {
  beforeEach(() => localStorage.clear());

  it("keeps inspection tokens private and creates a store id only after explicit setup", async () => {
    const location = new MemoryLocation();
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/truth/cowork/folders?")) {
        return json({ read_only: false, folders: [], diagnostics: [] });
      }
      if (url.endsWith("/folders/inspect")) {
        expect(JSON.parse(String(init?.body))).toEqual({
          folder_path: "C:/Projects/work-buddy",
        });
        return json({
          status: "uninitialized",
          folder_name: "work-buddy",
          folder_path: "C:/Projects/work-buddy",
          inspection_token: "secret-inspection-token",
          available_actions: ["initialize"],
        });
      }
      if (url.endsWith("/folders/initialize")) {
        expect(JSON.parse(String(init?.body))).toMatchObject({
          inspection_token: "secret-inspection-token",
        });
        return json({ folder });
      }
      if (url.startsWith("/api/truth/doc/list?")) return json({ docs: [] });
      throw new Error(`Unexpected request: ${url}`);
    });
    const provider = new HttpCoworkProvider({
      location,
      storage: localStorage,
      client: new CoworkHttpClient(fetchImpl as typeof fetch),
    });

    await provider.loadView();
    expect(
      await provider.dispatch(
        intent(COWORK_INTENTS.folderSelect, {
          action: "inspect",
          folderPath: "C:/Projects/work-buddy",
        }),
      ),
    ).toMatchObject({ status: "accepted" });
    const inspected = await provider.loadWidget(
      asWidgetTypeId("wb.cowork.workspace-card"),
      widgetRequest,
    );
    expect(inspected.input.folderSelection).toEqual({
      kind: "setup_available",
      candidate: {
        folderName: "work-buddy",
        folderPath: "C:/Projects/work-buddy",
      },
    });
    expect(inspected.input.activeFolderStoreId).toBeNull();
    expect(JSON.stringify(inspected.input)).not.toContain("secret-inspection-token");
    expect(location.search).not.toContain("token");

    await provider.dispatch(
      intent(COWORK_INTENTS.folderSelect, { action: "initialize" }),
    );
    const initialized = await provider.loadWidget(
      asWidgetTypeId("wb.cowork.workspace-card"),
      widgetRequest,
    );
    expect(initialized.input.activeFolderStoreId).toBe("store-1");
    expect(initialized.input.folderSelection).toMatchObject({
      kind: "initialized",
      folder: { folderName: "work-buddy" },
    });
    expect(location.search).toBe("?store_id=store-1");
  });

  it("keeps the current session and URL when a picker target is not ready", async () => {
    const location = new MemoryLocation("?store_id=store-1&document_id=old");
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/truth/cowork/folders?")) {
        return json({ read_only: false, folders: [folder], diagnostics: [] });
      }
      if (url.startsWith("/api/truth/doc/list?")) {
        return json({ docs: [document("old"), document("repair", "bootstrap_required")] });
      }
      if (url.includes("/api/truth/doc/old/ydoc?")) return validYdocResponse();
      throw new Error(`Unexpected request: ${url}`);
    });
    const provider = new HttpCoworkProvider({
      location,
      storage: localStorage,
      client: new CoworkHttpClient(fetchImpl as typeof fetch),
    });
    const initial = await provider.loadView();
    expect(initial.model.navigationError).toBeNull();
    expect(initial.model.activeSession).toMatchObject({ kind: "registered" });

    const result = await provider.dispatch(
      intent(COWORK_INTENTS.documentOpen, {
        storeId: "store-1",
        documentId: "repair",
      }),
    );
    expect(result.status).toBe("rejected");
    expect(location.search).toBe("?store_id=store-1&document_id=old");
    const snapshot = await provider.loadView();
    expect(snapshot.model.activeSession).toMatchObject({
      kind: "registered",
      document: { documentId: "old" },
    });
    expect(snapshot.model.routeTarget).toEqual({
      kind: "registered",
      storeId: "store-1",
      documentId: "old",
    });
  });

  it("refreshes drift metadata on the active session without replacing its identity", async () => {
    const location = new MemoryLocation("?store_id=store-1&document_id=old");
    let catalogReads = 0;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/truth/cowork/folders?")) {
        return json({ read_only: false, folders: [folder], diagnostics: [] });
      }
      if (url.startsWith("/api/truth/doc/list?")) {
        catalogReads += 1;
        return json({
          docs: [
            {
              ...document("old"),
              drift_state: catalogReads === 1 ? "clean" : "drifted",
            },
          ],
        });
      }
      if (url.includes("/api/truth/doc/old/ydoc?")) return validYdocResponse();
      throw new Error(`Unexpected request: ${url}`);
    });
    const provider = new HttpCoworkProvider({
      location,
      storage: localStorage,
      client: new CoworkHttpClient(fetchImpl as typeof fetch),
    });

    const initial = await provider.loadView();
    expect(initial.model.activeSession).toMatchObject({
      kind: "registered",
      storeId: "store-1",
      document: { documentId: "old", driftState: "clean" },
    });

    await provider.dispatch(intent(COWORK_INTENTS.catalogRefresh, {}));
    const refreshed = await provider.loadView();
    expect(refreshed.model.activeSession).toMatchObject({
      kind: "registered",
      storeId: "store-1",
      document: { documentId: "old", driftState: "drifted" },
    });
    expect(location.search).toBe("?store_id=store-1&document_id=old");
  });

  it("makes an externally retired document durable before revoking its active session", async () => {
    const location = new MemoryLocation("?store_id=store-1&document_id=old");
    let catalogReads = 0;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/truth/cowork/folders?")) {
        return json({ read_only: false, folders: [folder], diagnostics: [] });
      }
      if (url.startsWith("/api/truth/doc/list?")) {
        catalogReads += 1;
        return json({ docs: catalogReads === 1 ? [document("old")] : [] });
      }
      if (url.includes("/api/truth/doc/old/ydoc?")) return validYdocResponse();
      throw new Error(`Unexpected request: ${url}`);
    });
    const provider = new HttpCoworkProvider({
      location,
      storage: localStorage,
      client: new CoworkHttpClient(fetchImpl as typeof fetch),
    });
    await provider.loadView();

    let releaseDurability!: () => void;
    const durabilityGate = new Promise<void>((resolve) => {
      releaseDurability = resolve;
    });
    const commit = vi.fn();
    const cancel = vi.fn();
    const prepareToLeave = vi.fn(async () => {
      await durabilityGate;
      return { commit, cancel };
    });
    const unregister = coworkSessionDurability.register(
      registeredSessionDurabilityKey("store-1", "old"),
      { prepareToLeave },
    );
    try {
      const refresh = provider.dispatch(intent(COWORK_INTENTS.catalogRefresh, {}));
      await vi.waitFor(() => expect(prepareToLeave).toHaveBeenCalledTimes(1));
      expect((await provider.loadView()).model.activeSession).toMatchObject({
        kind: "registered",
        document: { documentId: "old" },
      });

      releaseDurability();
      await expect(refresh).resolves.toMatchObject({ status: "accepted" });

      const snapshot = await provider.loadView();
      expect(commit).toHaveBeenCalledTimes(1);
      expect(cancel).not.toHaveBeenCalled();
      expect(snapshot.model.activeSession).toEqual({ kind: "none" });
      expect(snapshot.model.document).toBeNull();
      expect(snapshot.model.routeTarget).toEqual({
        kind: "unavailable",
        storeId: "store-1",
        documentId: "old",
        reason: "document_no_longer_available",
      });
      expect(snapshot.model.navigationError).toMatchObject({
        code: "document_no_longer_available",
        retryable: false,
      });
      expect(location.search).toBe("?store_id=store-1&document_id=old");
    } finally {
      unregister();
    }
  });

  it("keeps the prior document active when picker preflight finds semantic corruption", async () => {
    const location = new MemoryLocation("?store_id=store-1&document_id=old");
    const bad = { ...document("bad"), title: "Broken structured state" };
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/truth/cowork/folders?")) {
        return json({ read_only: false, folders: [folder], diagnostics: [] });
      }
      if (url.startsWith("/api/truth/doc/list?")) {
        return json({ docs: [document("old"), bad] });
      }
      if (url.includes("/api/truth/doc/old/ydoc?")) return validYdocResponse();
      if (url.includes("/api/truth/doc/bad/ydoc?")) {
        return new Response(frameSegments([new Uint8Array([1, 2, 3])]) as BodyInit, {
          status: 200,
          headers: {
            "X-WB-Snapshot-Sha256": "bad-snapshot",
            "X-WB-Ydoc-Head-Sha256": "bad-head",
            "X-WB-Ydoc-Generation": "bad-generation",
            "X-WB-Next-Offset": "0",
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const provider = new HttpCoworkProvider({
      location,
      storage: localStorage,
      client: new CoworkHttpClient(fetchImpl as typeof fetch),
    });
    await provider.loadView();

    const result = await provider.dispatch(
      intent(COWORK_INTENTS.documentOpen, {
        storeId: "store-1",
        documentId: "bad",
      }),
    );

    expect(result).toMatchObject({ status: "rejected" });
    expect(result.message).toContain("Repair it from the Markdown file");
    expect(location.search).toBe("?store_id=store-1&document_id=old");
    const snapshot = await provider.loadView();
    expect(snapshot.model.activeSession).toMatchObject({
      kind: "registered",
      document: { documentId: "old" },
    });
    expect(snapshot.model.catalog.documents.find((entry) => entry.documentId === "bad"))
      .toMatchObject({
        initializationState: "semantic_corrupt",
        permissions: { open: false, repair: true },
      });
  });

  it("keeps a corrupt deep link recoverable without committing an active session", async () => {
    const location = new MemoryLocation("?store_id=store-1&document_id=bad");
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/truth/cowork/folders?")) {
        return json({ read_only: false, folders: [folder], diagnostics: [] });
      }
      if (url.startsWith("/api/truth/doc/list?")) {
        return json({ docs: [document("bad")] });
      }
      if (url.includes("/api/truth/doc/bad/ydoc?")) {
        return new Response(frameSegments([new Uint8Array([255, 0, 1])]) as BodyInit, {
          status: 200,
          headers: {
            "X-WB-Snapshot-Sha256": "bad-snapshot",
            "X-WB-Ydoc-Head-Sha256": "bad-head",
            "X-WB-Ydoc-Generation": "bad-generation",
            "X-WB-Next-Offset": "0",
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const provider = new HttpCoworkProvider({
      location,
      storage: localStorage,
      client: new CoworkHttpClient(fetchImpl as typeof fetch),
    });

    const snapshot = await provider.loadView();

    expect(snapshot.model.activeSession).toEqual({ kind: "none" });
    expect(snapshot.model.document).toBeNull();
    expect(snapshot.model.routeTarget).toEqual({
      kind: "unavailable",
      storeId: "store-1",
      documentId: "bad",
      reason: "semantic_corrupt",
    });
    expect(snapshot.model.navigationError).toMatchObject({
      code: "semantic_corrupt",
      retryable: true,
    });
    expect(snapshot.model.catalog.documents[0]).toMatchObject({
      initializationState: "semantic_corrupt",
      permissions: { open: false, repair: true },
    });
  });

  it("opens an inspected initialized Folder through the opaque inspection token", async () => {
    const location = new MemoryLocation();
    const requests: string[] = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      requests.push(url);
      if (url.startsWith("/api/truth/cowork/folders?")) {
        return json({ read_only: false, folders: [], diagnostics: [] });
      }
      if (url.endsWith("/folders/inspect")) {
        return json({
          status: "initialized",
          store_id: "store-1",
          folder_name: "work-buddy",
          folder_path: "C:/Copied/work-buddy",
          inspection_token: "opaque-open-token",
          available_actions: ["open"],
        });
      }
      if (url.endsWith("/folders/open")) {
        expect(JSON.parse(String(init?.body))).toEqual({
          inspection_token: "opaque-open-token",
        });
        return json({ folder: { ...folder, folder_path: "C:/Copied/work-buddy" } });
      }
      if (url.startsWith("/api/truth/doc/list?")) return json({ docs: [] });
      throw new Error(`Unexpected request: ${url}`);
    });
    const provider = new HttpCoworkProvider({
      location,
      storage: localStorage,
      client: new CoworkHttpClient(fetchImpl as typeof fetch),
    });
    await provider.loadView();

    const result = await provider.dispatch(
      intent(COWORK_INTENTS.folderSelect, {
        action: "inspect",
        folderPath: "C:/Copied/work-buddy",
      }),
    );

    expect(result.status).toBe("accepted");
    expect(requests.findIndex((url) => url.endsWith("/folders/open"))).toBeGreaterThan(
      requests.findIndex((url) => url.endsWith("/folders/inspect")),
    );
    const snapshot = await provider.loadView();
    expect(snapshot.model.activeFolderStoreId).toBe("store-1");
    expect(snapshot.model.folderSelection).toMatchObject({
      kind: "initialized",
      folder: { folderPath: "C:/Copied/work-buddy" },
    });
    expect(JSON.stringify(snapshot.model)).not.toContain("opaque-open-token");
    expect(location.search).toBe("?store_id=store-1");
  });

  it("retires promoted scratch metadata without closing the registered session", async () => {
    const scratch = new CoworkScratchRegistry(localStorage).create("Draft to promote");
    const location = new MemoryLocation("?store_id=store-1&document_id=old");
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/truth/cowork/folders?")) {
        return json({ read_only: false, folders: [folder], diagnostics: [] });
      }
      if (url.startsWith("/api/truth/doc/list?")) return json({ docs: [document("old")] });
      if (url.includes("/api/truth/doc/old/ydoc?")) return validYdocResponse();
      throw new Error(`Unexpected request: ${url}`);
    });
    const provider = new HttpCoworkProvider({
      location,
      storage: localStorage,
      client: new CoworkHttpClient(fetchImpl as typeof fetch),
    });
    await provider.loadView();

    const result = await provider.dispatch(
      intent(COWORK_INTENTS.scratchClose, {
        retire: true,
        scratchId: scratch.scratchId,
      }),
    );

    expect(result.status).toBe("accepted");
    const snapshot = await provider.loadView();
    expect(snapshot.model.scratches).toEqual([]);
    expect(snapshot.model.activeSession).toMatchObject({
      kind: "registered",
      document: { documentId: "old" },
    });
    expect(location.search).toBe("?store_id=store-1&document_id=old");
  });

  it("reuses a Folder mutation key after an ambiguous initialize response", async () => {
    const location = new MemoryLocation();
    const mutationKeys: string[] = [];
    let initializeCommitted = false;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/truth/cowork/folders?")) {
        return json({ read_only: false, folders: [], diagnostics: [] });
      }
      if (url.endsWith("/folders/inspect")) {
        return json({
          status: "uninitialized",
          folder_name: "work-buddy",
          folder_path: "C:/Projects/work-buddy",
          inspection_token: "inspection-for-retry",
          available_actions: ["initialize"],
        });
      }
      if (url.endsWith("/folders/initialize")) {
        const body = JSON.parse(String(init?.body)) as { idempotency_key: string };
        mutationKeys.push(body.idempotency_key);
        if (!initializeCommitted) {
          initializeCommitted = true;
          throw new TypeError("response lost after initialize");
        }
        return json({ folder });
      }
      if (url.startsWith("/api/truth/doc/list?")) return json({ docs: [] });
      throw new Error(`Unexpected request: ${url}`);
    });
    const provider = new HttpCoworkProvider({
      location,
      storage: localStorage,
      client: new CoworkHttpClient(fetchImpl as typeof fetch),
    });
    await provider.loadView();
    await provider.dispatch(
      intent(COWORK_INTENTS.folderSelect, {
        action: "inspect",
        folderPath: "C:/Projects/work-buddy",
      }),
    );

    expect(
      await provider.dispatch(
        intent(COWORK_INTENTS.folderSelect, { action: "initialize" }),
      ),
    ).toMatchObject({ status: "rejected" });
    expect(
      await provider.dispatch(
        intent(COWORK_INTENTS.folderSelect, { action: "initialize" }),
      ),
    ).toMatchObject({ status: "accepted" });

    expect(mutationKeys).toHaveLength(2);
    expect(mutationKeys[1]).toBe(mutationKeys[0]);
    expect((await provider.loadView()).model.activeFolderStoreId).toBe("store-1");
  });

  it("preserves the current session and URL when any navigation cannot reach device durability", async () => {
    const location = new MemoryLocation("?store_id=store-1&document_id=old");
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/truth/cowork/folders?")) {
        return json({ read_only: false, folders: [folder], diagnostics: [] });
      }
      if (url.startsWith("/api/truth/doc/list?")) {
        return json({ docs: [document("old"), document("next")] });
      }
      if (url.endsWith("/folders/inspect")) {
        return json({
          status: "initialized",
          folder_name: "other",
          folder_path: "C:/Projects/other",
          store_id: "store-2",
          inspection_token: "inspect-store-2",
        });
      }
      if (url.endsWith("/folders/open")) {
        throw new Error("Folder open must wait for device durability");
      }
      if (url.includes("/api/truth/doc/old/ydoc?")) return validYdocResponse();
      if (url.includes("/api/truth/doc/next/ydoc?")) return validYdocResponse();
      throw new Error(`Unexpected request: ${url}`);
    });
    const provider = new HttpCoworkProvider({
      location,
      storage: localStorage,
      client: new CoworkHttpClient(fetchImpl as typeof fetch),
    });
    await provider.loadView();
    let attempts = 0;
    const unregister = coworkSessionDurability.register(
      registeredSessionDurabilityKey("store-1", "old"),
      {
        prepareToLeave: async () => {
          attempts += 1;
          throw new Error("IndexedDB append failed");
        },
      },
    );
    try {
      const actions = [
        intent(COWORK_INTENTS.documentOpen, {
          storeId: "store-1",
          documentId: "next",
        }),
        intent(COWORK_INTENTS.documentReload, {
          storeId: "store-1",
          documentId: "old",
        }),
        intent(COWORK_INTENTS.documentClose, {}),
        intent(COWORK_INTENTS.scratchOpen, {}),
        intent(COWORK_INTENTS.folderSelect, { action: "open", storeId: "store-1" }),
        intent(COWORK_INTENTS.folderSelect, {
          action: "inspect",
          folderPath: "C:/Projects/other",
        }),
      ];
      for (const action of actions) {
        expect(await provider.dispatch(action)).toMatchObject({ status: "rejected" });
        expect(location.search).toBe("?store_id=store-1&document_id=old");
        expect((await provider.loadView()).model.activeSession).toMatchObject({
          kind: "registered",
          document: { documentId: "old" },
        });
      }

      // Back/Forward has already changed the address when the barrier runs; it is restored.
      location.pushSearch("?store_id=store-1&document_id=next");
      await vi.waitFor(() =>
        expect(location.search).toBe("?store_id=store-1&document_id=old"),
      );
      expect((await provider.loadView()).model.activeSession).toMatchObject({
        kind: "registered",
        document: { documentId: "old" },
      });
      expect(attempts).toBe(7);
    } finally {
      unregister();
    }
  });

  it("atomically reloads the active document after device durability without changing its URL", async () => {
    const location = new MemoryLocation("?store_id=store-1&document_id=old");
    const pushSearch = vi.spyOn(location, "pushSearch");
    let catalogReads = 0;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/truth/cowork/folders?")) {
        return json({ read_only: false, folders: [folder], diagnostics: [] });
      }
      if (url.startsWith("/api/truth/doc/list?")) {
        catalogReads += 1;
        return json({
          docs: [
            {
              ...document("old"),
              drift_state: catalogReads === 1 ? "drifted" : "clean",
              structured_head_sha256: catalogReads === 1 ? "old-head" : "new-head",
              snapshot_sha256: catalogReads === 1 ? "old-snapshot" : "new-snapshot",
              projection_sha256: catalogReads === 1 ? "old-file" : "new-file",
              current_file_sha256: catalogReads === 1 ? "external-file" : "new-file",
            },
          ],
        });
      }
      if (url.includes("/api/truth/doc/old/ydoc?")) return validYdocResponse();
      throw new Error(`Unexpected request: ${url}`);
    });
    const provider = new HttpCoworkProvider({
      location,
      storage: localStorage,
      client: new CoworkHttpClient(fetchImpl as typeof fetch),
    });
    await provider.loadView();
    const commit = vi.fn();
    const cancel = vi.fn();
    const prepareToLeave = vi.fn(async () => ({ commit, cancel }));
    const unregister = coworkSessionDurability.register(
      registeredSessionDurabilityKey("store-1", "old"),
      { prepareToLeave },
    );
    try {
      expect(
        await provider.dispatch(
          intent(COWORK_INTENTS.documentReload, {
            storeId: "store-1",
            documentId: "old",
          }),
        ),
      ).toMatchObject({ status: "accepted" });

      expect(prepareToLeave).toHaveBeenCalledTimes(1);
      expect(commit).toHaveBeenCalledTimes(1);
      expect(cancel).not.toHaveBeenCalled();
      expect(pushSearch).not.toHaveBeenCalled();
      expect(location.search).toBe("?store_id=store-1&document_id=old");
      expect((await provider.loadView()).model).toMatchObject({
        openingTarget: null,
        navigationError: null,
        activeSession: {
          kind: "registered",
          storeId: "store-1",
          document: {
            documentId: "old",
            driftState: "clean",
            structuredHeadSha256: "new-head",
            snapshotSha256: "new-snapshot",
            projectionSha256: "new-file",
            currentFileSha256: "new-file",
          },
        },
      });
    } finally {
      unregister();
    }
  });

  it("restores the active document and cancels its reload lease when catalog refresh fails", async () => {
    const location = new MemoryLocation("?store_id=store-1&document_id=old");
    let catalogReads = 0;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/truth/cowork/folders?")) {
        return json({ read_only: false, folders: [folder], diagnostics: [] });
      }
      if (url.startsWith("/api/truth/doc/list?")) {
        catalogReads += 1;
        if (catalogReads === 1) {
          return json({
            docs: [
              {
                ...document("old"),
                drift_state: "drifted",
                structured_head_sha256: "old-head",
              },
            ],
          });
        }
        return json(
          {
            error: {
              code: "catalog_temporarily_unavailable",
              message: "Catalog refresh failed.",
              retryable: true,
            },
          },
          503,
        );
      }
      if (url.includes("/api/truth/doc/old/ydoc?")) return validYdocResponse();
      throw new Error(`Unexpected request: ${url}`);
    });
    const provider = new HttpCoworkProvider({
      location,
      storage: localStorage,
      client: new CoworkHttpClient(fetchImpl as typeof fetch),
    });
    await provider.loadView();
    const commit = vi.fn();
    const cancel = vi.fn();
    const unregister = coworkSessionDurability.register(
      registeredSessionDurabilityKey("store-1", "old"),
      {
        prepareToLeave: async () => ({ commit, cancel }),
      },
    );
    try {
      expect(
        await provider.dispatch(
          intent(COWORK_INTENTS.documentReload, {
            storeId: "store-1",
            documentId: "old",
          }),
        ),
      ).toMatchObject({
        status: "rejected",
        message: "Catalog refresh failed.",
      });

      expect(commit).not.toHaveBeenCalled();
      expect(cancel).toHaveBeenCalledTimes(1);
      expect(location.search).toBe("?store_id=store-1&document_id=old");
      expect((await provider.loadView()).model).toMatchObject({
        openingTarget: null,
        activeSession: {
          kind: "registered",
          storeId: "store-1",
          document: {
            documentId: "old",
            driftState: "drifted",
            structuredHeadSha256: "old-head",
          },
        },
        navigationError: {
          code: "catalog_temporarily_unavailable",
          message: "Catalog refresh failed.",
        },
      });
    } finally {
      unregister();
    }
  });

  it("coalesces rapid navigation behind one durability write and lets only the latest target win", async () => {
    const location = new MemoryLocation("?store_id=store-1&document_id=old");
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/truth/cowork/folders?")) {
        return json({ read_only: false, folders: [folder], diagnostics: [] });
      }
      if (url.startsWith("/api/truth/doc/list?")) {
        return json({ docs: [document("old"), document("first"), document("second")] });
      }
      if (/\/api\/truth\/doc\/(old|first|second)\/ydoc\?/.test(url)) {
        return validYdocResponse();
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const provider = new HttpCoworkProvider({
      location,
      storage: localStorage,
      client: new CoworkHttpClient(fetchImpl as typeof fetch),
    });
    await provider.loadView();
    let releaseDurability!: () => void;
    const durabilityGate = new Promise<void>((resolve) => {
      releaseDurability = resolve;
    });
    const pause = vi.fn();
    const resume = vi.fn();
    const ensure = vi.fn(() => durabilityGate);
    const unregister = coworkSessionDurability.register(
      registeredSessionDurabilityKey("store-1", "old"),
      createCoworkSessionDurabilityController({
        pause,
        resume,
        ensureDeviceDurability: ensure,
      }),
    );
    try {
      const first = provider.dispatch(
        intent(COWORK_INTENTS.documentOpen, {
          storeId: "store-1",
          documentId: "first",
        }),
      );
      const second = provider.dispatch(
        intent(COWORK_INTENTS.documentOpen, {
          storeId: "store-1",
          documentId: "second",
        }),
      );
      await vi.waitFor(() => expect(pause).toHaveBeenCalledTimes(1));
      expect(ensure).toHaveBeenCalledTimes(1);
      expect(location.search).toBe("?store_id=store-1&document_id=old");
      releaseDurability();
      await Promise.all([first, second]);

      expect((await provider.loadView()).model.activeSession).toMatchObject({
        kind: "registered",
        document: { documentId: "second" },
      });
      expect(location.search).toBe("?store_id=store-1&document_id=second");
      expect(resume).not.toHaveBeenCalled();
    } finally {
      unregister();
    }
  });

  it("does not let a delayed same-document reload overwrite newer navigation", async () => {
    const location = new MemoryLocation("?store_id=store-1&document_id=old");
    let catalogReads = 0;
    let resolveReloadCatalog!: (response: Response) => void;
    const reloadCatalog = new Promise<Response>((resolve) => {
      resolveReloadCatalog = resolve;
    });
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/truth/cowork/folders?")) {
        return json({ read_only: false, folders: [folder], diagnostics: [] });
      }
      if (url.startsWith("/api/truth/doc/list?")) {
        catalogReads += 1;
        if (catalogReads === 1) {
          return json({ docs: [document("old"), document("next")] });
        }
        return reloadCatalog;
      }
      if (/\/api\/truth\/doc\/(old|next)\/ydoc\?/.test(url)) {
        return validYdocResponse();
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const provider = new HttpCoworkProvider({
      location,
      storage: localStorage,
      client: new CoworkHttpClient(fetchImpl as typeof fetch),
    });
    await provider.loadView();
    const reloadCommit = vi.fn();
    const reloadCancel = vi.fn();
    const navigationCommit = vi.fn();
    const navigationCancel = vi.fn();
    let durabilityCalls = 0;
    const unregister = coworkSessionDurability.register(
      registeredSessionDurabilityKey("store-1", "old"),
      {
        prepareToLeave: async () => {
          durabilityCalls += 1;
          return durabilityCalls === 1
            ? { commit: reloadCommit, cancel: reloadCancel }
            : { commit: navigationCommit, cancel: navigationCancel };
        },
      },
    );
    try {
      const reload = provider.dispatch(
        intent(COWORK_INTENTS.documentReload, {
          storeId: "store-1",
          documentId: "old",
        }),
      );
      await vi.waitFor(() => expect(catalogReads).toBe(2));

      expect(
        await provider.dispatch(
          intent(COWORK_INTENTS.documentOpen, {
            storeId: "store-1",
            documentId: "next",
          }),
        ),
      ).toMatchObject({ status: "accepted" });
      resolveReloadCatalog(
        json({
          docs: [
            {
              ...document("old"),
              drift_state: "clean",
              structured_head_sha256: "replacement-head",
            },
            document("next"),
          ],
        }),
      );
      expect(await reload).toMatchObject({ status: "accepted" });

      expect(reloadCommit).not.toHaveBeenCalled();
      expect(reloadCancel).toHaveBeenCalledTimes(1);
      expect(navigationCommit).toHaveBeenCalledTimes(1);
      expect(navigationCancel).not.toHaveBeenCalled();
      expect(location.search).toBe("?store_id=store-1&document_id=next");
      expect((await provider.loadView()).model.activeSession).toMatchObject({
        kind: "registered",
        storeId: "store-1",
        document: { documentId: "next" },
      });
    } finally {
      unregister();
    }
  });

  it("keeps a scratch open when its device-local save barrier fails", async () => {
    const scratch = new CoworkScratchRegistry(localStorage).create("Unsaved scratch");
    const location = new MemoryLocation(`?scratch_id=${scratch.scratchId}`);
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/truth/cowork/folders?")) {
        return json({ read_only: false, folders: [], diagnostics: [] });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const provider = new HttpCoworkProvider({
      location,
      storage: localStorage,
      client: new CoworkHttpClient(fetchImpl as typeof fetch),
    });
    await provider.loadView();
    const unregister = coworkSessionDurability.register(
      scratchSessionDurabilityKey(scratch.scratchId),
      {
        prepareToLeave: async () => {
          throw new Error("scratch IndexedDB write failed");
        },
      },
    );
    try {
      expect(
        await provider.dispatch(intent(COWORK_INTENTS.scratchClose, {})),
      ).toMatchObject({ status: "rejected" });
      expect(location.search).toBe(`?scratch_id=${scratch.scratchId}`);
      expect((await provider.loadView()).model.activeSession).toMatchObject({
        kind: "scratch",
        scratchId: scratch.scratchId,
      });
    } finally {
      unregister();
    }
  });

  it("ignores a stale catalog completion after switching Folders", async () => {
    const secondFolder = {
      ...folder,
      store_id: "store-2",
      folder_name: "second",
      folder_path: "C:/Projects/second",
    };
    const location = new MemoryLocation("?store_id=store-1&document_id=old");
    let storeOneCatalogReads = 0;
    let resolveStale!: (response: Response) => void;
    const staleCatalog = new Promise<Response>((resolve) => {
      resolveStale = resolve;
    });
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/truth/cowork/folders?")) {
        return json({
          read_only: false,
          folders: [folder, secondFolder],
          diagnostics: [],
        });
      }
      if (url.includes("/api/truth/doc/list?") && url.includes("store_id=store-1")) {
        storeOneCatalogReads += 1;
        if (storeOneCatalogReads === 1) return json({ docs: [document("old")] });
        return staleCatalog;
      }
      if (url.includes("/api/truth/doc/list?") && url.includes("store_id=store-2")) {
        return json({ docs: [document("second-doc")] });
      }
      if (url.includes("/api/truth/doc/old/ydoc?")) return validYdocResponse();
      throw new Error(`Unexpected request: ${url}`);
    });
    const provider = new HttpCoworkProvider({
      location,
      storage: localStorage,
      client: new CoworkHttpClient(fetchImpl as typeof fetch),
    });
    await provider.loadView();
    const refresh = provider.dispatch(intent(COWORK_INTENTS.catalogRefresh, {}));
    await vi.waitFor(() => expect(storeOneCatalogReads).toBe(2));
    await provider.dispatch(
      intent(COWORK_INTENTS.folderSelect, { action: "open", storeId: "store-2" }),
    );
    resolveStale(
      json({ docs: [{ ...document("old"), drift_state: "drifted" }] }),
    );
    await refresh;

    const snapshot = await provider.loadView();
    expect(snapshot.model.activeFolderStoreId).toBe("store-2");
    expect(snapshot.model.catalog.documents.map((entry) => entry.documentId)).toEqual([
      "second-doc",
    ]);
    expect(snapshot.model.activeSession).toEqual({ kind: "none" });
    expect(location.search).toBe("?store_id=store-2");
  });
});

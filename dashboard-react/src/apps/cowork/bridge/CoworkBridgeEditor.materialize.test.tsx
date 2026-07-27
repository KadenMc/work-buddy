import { act, render, screen, waitFor } from "@testing-library/react";
import { ySyncPluginKey } from "@tiptap/y-tiptap";
import { describe, expect, it, vi } from "vitest";
import * as Y from "yjs";

import { bootstrapCoworkYdoc } from "../documents/bootstrapCoworkYdoc";
import { HttpCoworkMaterializationClient } from "../materialization/HttpCoworkMaterializationClient";
import type {
  CoworkMaterializationController,
  CoworkMaterializationState,
} from "../materialization/contracts";
import { InMemoryCoworkYdocTransport } from "../persistence/InMemoryCoworkYdocTransport";
import { sha256Hex } from "../persistence/hashing";
import type { CoworkYdocTransport } from "../persistence/transport";
import { createWbTrackedChangesAdapter } from "../suggestions/adapter";
import { editProposal } from "../suggestions/__tests__/support";
import { resolveQuoteAnchor } from "../suggestions/anchor";
import { CoworkBridgeEditor } from "./CoworkBridgeEditor";
import type { CoworkSittingWorkspace } from "./sittingWorkspace";

describe("CoworkBridgeEditor explicit Markdown Save", () => {
  it("never persists proposal projection while still persisting the next human edit", async () => {
    const initialized = await bootstrapCoworkYdoc(
      new TextEncoder().encode("The quick brown fox"),
    );
    if (!initialized.ok) throw new Error(initialized.message);
    const server = new InMemoryCoworkYdocTransport();
    const empty = await server.pull({});
    await server.push({
      batch: initialized.snapshot,
      baseSha256: empty.docSha256,
      baseStructuredHeadSha256: empty.structuredHeadSha256,
      baseYdocGeneration: empty.ydocGeneration,
      compaction: {
        snapshot: initialized.snapshot,
        snapshotSha256: initialized.snapshotSha256,
      },
    });
    const initialHead = (await server.pull({})).structuredHeadSha256;
    const pushes: unknown[] = [];
    const transport: CoworkYdocTransport = {
      pull: (request) => server.pull(request),
      push: async (request) => {
        pushes.push(request);
        return server.push(request);
      },
    };
    const document = new Y.Doc();
    const adapter = createWbTrackedChangesAdapter({ doc: document });
    const workspaceRef: { current: CoworkSittingWorkspace | null } = { current: null };

    render(
      <CoworkBridgeEditor
        document={document}
        adapter={adapter}
        transport={transport}
        seedMarkdown=""
        documentId={`proposal-origin-${Date.now()}`}
        storeId="proposal-origin-store"
        currentFileSha256={initialized.sourceSha256}
        initialDriftState="clean"
        canMaterialize
        onSittingWorkspace={(workspace) => {
          workspaceRef.current = workspace;
        }}
        onReady={() => {
          adapter.ingestProposal(
            editProposal("proposal-origin", "quick", "slow", {
              prefix: "The ",
              suffix: " brown",
            }),
          );
        }}
      />,
    );
    const textbox = await screen.findByRole(
      "textbox",
      { name: "Document editor" },
      { timeout: 10_000 },
    );
    await waitFor(() => expect(textbox).toHaveTextContent("slow"));

    await new Promise((resolve) => window.setTimeout(resolve, 400));
    expect(pushes).toHaveLength(0);
    expect((await server.pull({})).structuredHeadSha256).toBe(initialHead);

    const workspace = workspaceRef.current;
    if (workspace === null) throw new Error("sitting workspace was not ready");
    const preflight = await workspace.synchronize();
    expect(preflight.expectedStructuredHeadSha256).toBe(initialHead);
    expect(pushes).toHaveLength(0);
    expect((await server.pull({})).structuredHeadSha256).toBe(initialHead);

    act(() => {
      document.transact(() => {
        const paragraph = document.getXmlFragment("default").get(0);
        if (!(paragraph instanceof Y.XmlElement)) throw new Error("missing paragraph");
        paragraph.insert(0, [new Y.XmlText("Human ")]);
      }, ySyncPluginKey);
    });
    await waitFor(() => expect(pushes).toHaveLength(1));
    expect((await server.pull({})).structuredHeadSha256).not.toBe(initialHead);
  }, 25_000);

  it("applies live read-only and feedback policy flips without a writable frame", async () => {
    const initialized = await bootstrapCoworkYdoc(
      new TextEncoder().encode("The quick brown fox"),
    );
    if (!initialized.ok) throw new Error(initialized.message);
    const server = new InMemoryCoworkYdocTransport();
    const empty = await server.pull({});
    await server.push({
      batch: initialized.snapshot,
      baseSha256: empty.docSha256,
      baseStructuredHeadSha256: empty.structuredHeadSha256,
      baseYdocGeneration: empty.ydocGeneration,
      compaction: {
        snapshot: initialized.snapshot,
        snapshotSha256: initialized.snapshotSha256,
      },
    });
    const pushes: unknown[] = [];
    const transport: CoworkYdocTransport = {
      pull: (request) => server.pull(request),
      push: async (request) => {
        pushes.push(request);
        return server.push(request);
      },
    };
    const document = new Y.Doc();
    const adapter = createWbTrackedChangesAdapter({ doc: document });
    const policyDocumentId = `policy-${Date.now()}`;
    const editorRef: { current: import("@tiptap/core").Editor | null } = {
      current: null,
    };
    const renderEditor = (readOnly: boolean, feedback: boolean) => (
      <CoworkBridgeEditor
        document={document}
        adapter={adapter}
        transport={transport}
        seedMarkdown=""
        documentId={policyDocumentId}
        storeId="policy-store"
        currentFileSha256={initialized.sourceSha256}
        initialDriftState="clean"
        canMaterialize
        readOnly={readOnly}
        {...(feedback ? { onFeedbackCaptured: vi.fn() } : {})}
        onReady={(context) => {
          editorRef.current = context.editor;
        }}
      />
    );
    const view = render(renderEditor(false, true));
    const textbox = await screen.findByRole(
      "textbox",
      { name: "Document editor" },
      { timeout: 10_000 },
    );
    await waitFor(() => expect(editorRef.current).not.toBeNull());
    const mountedEditor = editorRef.current;
    if (mountedEditor === null) throw new Error("editor not mounted");
    const range = resolveQuoteAnchor(mountedEditor.state.doc, {
      exact: "quick",
      prefix: "The ",
      suffix: " brown",
    });
    if (range === null) throw new Error("selection anchor unavailable");
    act(() => mountedEditor.commands.setTextSelection(range));
    expect(await screen.findByRole("button", { name: "Give feedback" })).toBeVisible();

    view.rerender(renderEditor(true, false));
    expect(mountedEditor.isEditable).toBe(false);
    expect(textbox).toHaveAttribute("contenteditable", "false");
    expect(textbox).toHaveAttribute("aria-readonly", "true");
    expect(screen.queryByRole("button", { name: "Give feedback" })).toBeNull();
    act(() => {
      document.transact(() => {
        const paragraph = document.getXmlFragment("default").get(0);
        if (!(paragraph instanceof Y.XmlElement)) throw new Error("missing paragraph");
        paragraph.insert(0, [new Y.XmlText("Blocked ")]);
      }, ySyncPluginKey);
    });
    await new Promise((resolve) => window.setTimeout(resolve, 400));
    expect(pushes).toHaveLength(0);

    view.rerender(renderEditor(false, true));
    expect(mountedEditor.isEditable).toBe(true);
    expect(textbox).toHaveAttribute("contenteditable", "true");
    expect(textbox).toHaveAttribute("aria-readonly", "false");
    act(() => mountedEditor.commands.setTextSelection(range));
    expect(await screen.findByRole("button", { name: "Give feedback" })).toBeVisible();
    act(() => {
      document.transact(() => {
        const paragraph = document.getXmlFragment("default").get(0);
        if (!(paragraph instanceof Y.XmlElement)) throw new Error("missing paragraph");
        paragraph.insert(0, [new Y.XmlText("Writable ")]);
      }, ySyncPluginKey);
    });
    await waitFor(() => expect(pushes).toHaveLength(1));
  }, 25_000);

  it("flushes and compacts, renders the live editor, and publishes both CAS heads", async () => {
    const initialBytes = new TextEncoder().encode("Initial text");
    const initialized = await bootstrapCoworkYdoc(initialBytes);
    if (!initialized.ok) throw new Error(initialized.message);
    const transport = new InMemoryCoworkYdocTransport();
    const empty = await transport.pull({});
    await transport.push({
      batch: initialized.snapshot,
      baseSha256: empty.docSha256,
      baseStructuredHeadSha256: empty.structuredHeadSha256,
      baseYdocGeneration: empty.ydocGeneration,
      compaction: {
        snapshot: initialized.snapshot,
        snapshotSha256: initialized.snapshotSha256,
      },
    });

    const requests: Record<string, unknown>[] = [];
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      requests.push(body);
      if (requests.length === 1) {
        return new Response(
          JSON.stringify({
            ok: false,
            error: {
              code: "stale_structured_head",
              message: "structured document changed before Save",
              retryable: false,
            },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          ok: true,
          new_file_sha256: body.rendered_sha256,
          structured_head_sha256: body.expected_ydoc_head_sha256,
          document_version_id: "version-1",
          materialized_at: "2026-07-22T12:00:00.000Z",
          drift_state: "clean",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    const document = new Y.Doc();
    let injectMidSaveEdit = true;
    let compactionCount = 0;
    const raceTransport: CoworkYdocTransport = {
      pull: (request) => transport.pull(request),
      push: (request) => {
        if (request.compaction !== undefined && injectMidSaveEdit) {
          compactionCount += 1;
          injectMidSaveEdit = false;
          document.transact(() => {
            const paragraph = document.getXmlFragment("default").get(0);
            if (!(paragraph instanceof Y.XmlElement)) throw new Error("missing paragraph");
            paragraph.insert(0, [new Y.XmlText("Concurrent ")]);
          }, ySyncPluginKey);
        } else if (request.compaction !== undefined) compactionCount += 1;
        return transport.push(request);
      },
    };
    const adapter = createWbTrackedChangesAdapter({ doc: document });
    const controllerRef: { current: CoworkMaterializationController | null } = {
      current: null,
    };
    const states: CoworkMaterializationState[] = [];

    render(
      <CoworkBridgeEditor
        document={document}
        adapter={adapter}
        transport={raceTransport}
        seedMarkdown=""
        documentId="doc-1"
        storeId="store-1"
        currentFileSha256={await sha256Hex(initialBytes)}
        initialDriftState="clean"
        canMaterialize
        materializationClient={
          new HttpCoworkMaterializationClient(fetchImpl as typeof fetch)
        }
        onMaterializationController={(controller) => {
          controllerRef.current = controller;
        }}
        onMaterializationState={(state) => states.push(state)}
      />,
    );
    const textbox = await screen.findByRole(
      "textbox",
      { name: "Document editor" },
      { timeout: 10_000 },
    );
    await waitFor(() =>
      expect(states[states.length - 1]).toMatchObject({ kind: "up_to_date" }),
    );

    act(() => {
      document.transact(() => {
        const paragraph = document.getXmlFragment("default").get(0);
        if (!(paragraph instanceof Y.XmlElement)) throw new Error("missing paragraph");
        paragraph.insert(0, [new Y.XmlText("Edited ")]);
      }, ySyncPluginKey);
    });
    await waitFor(() => expect(textbox.textContent).toContain("Edited Initial text"));
    await waitFor(() =>
      expect(states[states.length - 1]).toMatchObject({ kind: "unsaved" }),
    );
    const controller = controllerRef.current;
    if (controller === null) throw new Error("materialization controller was not ready");

    await act(async () => controller.save());

    await waitFor(() =>
      expect(states[states.length - 1]).toMatchObject({
        kind: "conflict",
        canRetry: true,
      }),
    );
    await act(async () => controller.save());

    await waitFor(() =>
      expect(states[states.length - 1]).toMatchObject({ kind: "up_to_date" }),
    );
    expect(requests).toHaveLength(2);
    expect(requests[1]).toMatchObject({
      rendered_markdown: "Concurrent Edited Initial text",
      expected_file_sha256: await sha256Hex(initialBytes),
      snapshot_sha256: expect.stringMatching(/^[a-f0-9]{64}$/),
      expected_ydoc_head_sha256: expect.stringMatching(/^[a-f0-9]{64}$/),
      rendered_sha256: expect.stringMatching(/^[a-f0-9]{64}$/),
    });
    expect(requests[1].idempotency_key).not.toBe(requests[0].idempotency_key);
    expect(compactionCount).toBeGreaterThanOrEqual(3);
    expect(transport.hasSnapshot).toBe(true);
    expect(transport.pendingBatchCount).toBe(0);
    const persisted = await transport.pull({});
    const persistedDocument = new Y.Doc();
    if (persisted.snapshot !== null) Y.applyUpdate(persistedDocument, persisted.snapshot);
    for (const batch of persisted.batches) Y.applyUpdate(persistedDocument, batch);
    expect(persistedDocument.getXmlFragment("default").toString()).toContain(
      "Concurrent Edited Initial text",
    );
    persistedDocument.destroy();
  }, 25_000);

  it("mounts with exact durable offline edits after a registered document reload", async () => {
    const initialized = await bootstrapCoworkYdoc(new Uint8Array());
    if (!initialized.ok) throw new Error(initialized.message);
    const server = new InMemoryCoworkYdocTransport();
    const empty = await server.pull({});
    await server.push({
      batch: initialized.snapshot,
      baseSha256: empty.docSha256,
      baseStructuredHeadSha256: empty.structuredHeadSha256,
      baseYdocGeneration: empty.ydocGeneration,
      compaction: {
        snapshot: initialized.snapshot,
        snapshotSha256: initialized.snapshotSha256,
      },
    });
    const offlineTransport: CoworkYdocTransport = {
      pull: (request) => server.pull(request),
      push: async () => {
        throw new TypeError("offline");
      },
    };
    const marker = "Exact offline reload marker";
    const firstDocument = new Y.Doc();
    const firstController: { current: CoworkMaterializationController | null } = {
      current: null,
    };
    const firstStatuses: string[] = [];
    const firstRender = render(
      <CoworkBridgeEditor
        document={firstDocument}
        adapter={createWbTrackedChangesAdapter({ doc: firstDocument })}
        transport={offlineTransport}
        seedMarkdown=""
        documentId="reload-offline-doc"
        storeId="reload-offline-store"
        currentFileSha256={initialized.sourceSha256}
        initialDriftState="clean"
        canMaterialize
        onSyncStatus={(status) => firstStatuses.push(status)}
        onMaterializationController={(controller) => {
          firstController.current = controller;
        }}
      />,
    );
    const firstTextbox = await screen.findByRole(
      "textbox",
      { name: "Document editor" },
      { timeout: 10_000 },
    );

    act(() => {
      firstDocument.transact(() => {
        const paragraph = firstDocument.getXmlFragment("default").get(0);
        if (!(paragraph instanceof Y.XmlElement)) throw new Error("missing paragraph");
        paragraph.insert(paragraph.length, [new Y.XmlText(marker)]);
      }, ySyncPluginKey);
    });
    await waitFor(() => expect(firstTextbox.textContent).toContain(marker));
    await waitFor(() => expect(firstController.current).not.toBeNull());
    await act(async () => firstController.current?.retrySync());
    await waitFor(() => expect(firstStatuses).toContain("offline"));
    firstRender.unmount();

    const recoveredDocument = new Y.Doc();
    const recoveredStatuses: string[] = [];
    render(
      <CoworkBridgeEditor
        document={recoveredDocument}
        adapter={createWbTrackedChangesAdapter({ doc: recoveredDocument })}
        transport={offlineTransport}
        seedMarkdown=""
        documentId="reload-offline-doc"
        storeId="reload-offline-store"
        currentFileSha256={initialized.sourceSha256}
        initialDriftState="clean"
        canMaterialize
        onSyncStatus={(status) => recoveredStatuses.push(status)}
      />,
    );
    const recoveredTextbox = await screen.findByRole(
      "textbox",
      { name: "Document editor" },
      { timeout: 10_000 },
    );

    expect(recoveredTextbox).toHaveTextContent(marker);
    expect(recoveredDocument.getXmlFragment("default").toString()).toContain(marker);
    expect(
      recoveredStatuses.some(
        (status) => status === "saved_on_device" || status === "offline",
      ),
    ).toBe(true);
  }, 25_000);
});

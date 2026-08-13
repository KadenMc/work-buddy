import { act, render, screen, waitFor } from "@testing-library/react";
import { Editor } from "@tiptap/core";
import { ySyncPluginKey } from "@tiptap/y-tiptap";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as Y from "yjs";

import { resetLocalIdentityForTests } from "../../../security/localIdentity";

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
import type { SittingResponse } from "../suggestions/types";
import { resolveQuoteAnchor } from "../suggestions/anchor";
import { buildEditorExtensions } from "../editor/extensions";
import { serializeCoworkEditorMarkdown } from "../editor/serializeCoworkMarkdown";
import { projectCoworkLedgerDecorations } from "../editor/ledgerDecorations";
import { authenticatedHumanAuthorityFetch } from "../testSupport/authenticatedHumanAuthorityFetch";
import {
  assertCanonicalCoworkEditorState,
  CoworkBridgeEditor,
} from "./CoworkBridgeEditor";
import type { CoworkSittingWorkspace } from "./sittingWorkspace";

describe("CoworkBridgeEditor explicit Markdown Save", () => {
  beforeEach(() => resetLocalIdentityForTests());

  it("refuses to compact a legacy tracked-suggestion projection", async () => {
    const initialized = await bootstrapCoworkYdoc(
      new TextEncoder().encode("The quick brown fox"),
    );
    if (!initialized.ok) throw new Error(initialized.message);
    const document = new Y.Doc();
    Y.applyUpdate(document, initialized.snapshot);
    const editor = new Editor({
      extensions: buildEditorExtensions(document),
    });
    const adapter = createWbTrackedChangesAdapter({ doc: document });
    adapter.attach(editor);
    adapter.ingestProposal({
      proposal_id: "legacy-projection",
      kind: "edit",
      quoteAnchor: {
        exact: "quick",
        prefix: "The ",
        suffix: " brown",
      },
      replacement: "slow",
      attrs: {
        proposal_id: "legacy-projection",
        producer: "test-agent",
        epistemic: "ai_proposed",
      },
      base_doc_sha256: "base",
      canonical_sha256: "canonical",
    });

    expect(() => assertCanonicalCoworkEditorState(editor)).toThrow(
      /refused to save noncanonical proposal projection/u,
    );
    adapter.detach();
    editor.destroy();
    document.destroy();
  });

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
    const editorRef: { current: Editor | null } = { current: null };
    const workspaceRef: { current: CoworkSittingWorkspace | null } = { current: null };

    render(
      <CoworkBridgeEditor
        document={document}
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
        onReady={({ editor }) => {
          editorRef.current = editor;
          projectCoworkLedgerDecorations(editor, {
            edits: [
              {
                proposalId: "proposal-origin",
                quoteAnchor: {
                  exact: "quick",
                  prefix: "The ",
                  suffix: " brown",
                },
                replacement: "slow",
                changeType: "modification",
              },
            ],
            flags: [],
            expressions: [],
            claims: [],
            provenance: [],
          });
        }}
      />,
    );
    const textbox = await screen.findByRole(
      "textbox",
      { name: "Document editor" },
      { timeout: 10_000 },
    );
    await waitFor(() => expect(textbox).toHaveTextContent("slow"));
    expect(editorRef.current?.getText()).toBe("The quick brown fox");

    await new Promise((resolve) => window.setTimeout(resolve, 400));
    expect(pushes).toHaveLength(0);
    expect((await server.pull({})).structuredHeadSha256).toBe(initialHead);

    const workspace = workspaceRef.current;
    if (workspace === null) throw new Error("sitting workspace was not ready");
    const preflight = await workspace.synchronize();
    expect(preflight.expectedStructuredHeadSha256).toBe(initialHead);
    expect(pushes).toHaveLength(1);
    expect(
      (pushes[0] as { compaction?: { projectionMarkdown?: string } }).compaction
        ?.projectionMarkdown,
    ).toBe("The quick brown fox");
    expect((await server.pull({})).structuredHeadSha256).toBe(initialHead);

    const mountedEditor = editorRef.current;
    if (mountedEditor === null) throw new Error("editor was not ready");
    const quick = resolveQuoteAnchor(mountedEditor.state.doc, {
      exact: "quick",
      prefix: "The ",
      suffix: " brown",
    });
    if (quick === null) throw new Error("human edit anchor was not ready");
    act(() => {
      mountedEditor.view.dispatch(
        mountedEditor.state.tr.insertText("ly", quick.to),
      );
    });
    await waitFor(() => expect(pushes).toHaveLength(2));
    const persisted = await server.pull({});
    expect(persisted.structuredHeadSha256).not.toBe(initialHead);
    const reopened = new Y.Doc();
    if (persisted.snapshot !== null) Y.applyUpdate(reopened, persisted.snapshot);
    for (const batch of persisted.batches) Y.applyUpdate(reopened, batch);
    const reopenedEditor = new Editor({
      extensions: buildEditorExtensions(reopened),
      editable: false,
    });
    expect(serializeCoworkEditorMarkdown(reopenedEditor, reopened)).toBe(
      "The quickly brown fox",
    );
    expect(reopened.store.pendingStructs).toBeNull();
    reopenedEditor.destroy();
    reopened.destroy();
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
    const policyDocumentId = `policy-${Date.now()}`;
    const editorRef: { current: import("@tiptap/core").Editor | null } = {
      current: null,
    };
    const renderEditor = (readOnly: boolean, feedback: boolean) => (
      <CoworkBridgeEditor
        document={document}
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
    const fetchImpl = authenticatedHumanAuthorityFetch(async (
      _input: RequestInfo | URL,
      init?: RequestInit,
    ) => {
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
    const controllerRef: { current: CoworkMaterializationController | null } = {
      current: null,
    };
    const states: CoworkMaterializationState[] = [];

    render(
      <CoworkBridgeEditor
        document={document}
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
        onReady={({ editor }) => {
          projectCoworkLedgerDecorations(editor, {
            edits: [
              {
                proposalId: "open-during-save",
                quoteAnchor: {
                  exact: "Initial text",
                  prefix: "",
                  suffix: "",
                },
                replacement: "Proposed text",
                changeType: "modification",
              },
            ],
            flags: [],
            expressions: [],
            claims: [],
            provenance: [],
          });
        }}
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
    expect(textbox).toHaveTextContent("Proposed text");

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
    expect(String(requests[1].rendered_markdown)).not.toContain("Proposed text");
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
    expect(persistedDocument.getXmlFragment("default").toString()).not.toContain(
      "Proposed text",
    );
    expect(persistedDocument.store.pendingStructs).toBeNull();
    persistedDocument.destroy();
  }, 25_000);

  it("advances file and structured heads after a sitting before the next Save", async () => {
    const initialBytes = new TextEncoder().encode("The quick brown fox");
    const initialized = await bootstrapCoworkYdoc(initialBytes);
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
    const initialServerHead = (await server.pull({})).structuredHeadSha256;
    const saveRequests: Record<string, unknown>[] = [];
    const saveFetch = authenticatedHumanAuthorityFetch(async (
      _input: RequestInfo | URL,
      init?: RequestInit,
    ) => {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      saveRequests.push(body);
      return new Response(
        JSON.stringify({
          ok: true,
          new_file_sha256: body.rendered_sha256,
          structured_head_sha256: body.expected_ydoc_head_sha256,
          document_version_id: "version-after-sitting",
          materialized_at: "2026-07-28T12:00:00.000Z",
          drift_state: "clean",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    const document = new Y.Doc();
    const editorRef: { current: Editor | null } = { current: null };
    const workspaceRef: { current: CoworkSittingWorkspace | null } = { current: null };
    const controllerRef: { current: CoworkMaterializationController | null } = {
      current: null,
    };
    const states: CoworkMaterializationState[] = [];
    const proposal = editProposal("sitting-edit", "quick", "slow", {
      prefix: "The ",
      suffix: " brown",
    });

    render(
      <CoworkBridgeEditor
        document={document}
        transport={server}
        seedMarkdown=""
        documentId={`sitting-heads-${Date.now()}`}
        storeId="sitting-heads-store"
        currentFileSha256={initialized.sourceSha256}
        initialDriftState="clean"
        canMaterialize
        materializationClient={
          new HttpCoworkMaterializationClient(saveFetch as typeof fetch)
        }
        getProposalCatalog={() => [proposal]}
        onReady={({ editor }) => {
          editorRef.current = editor;
        }}
        onSittingWorkspace={(workspace) => {
          workspaceRef.current = workspace;
        }}
        onMaterializationController={(controller) => {
          controllerRef.current = controller;
        }}
        onMaterializationState={(state) => states.push(state)}
      />,
    );
    await screen.findByRole(
      "textbox",
      { name: "Document editor" },
      { timeout: 10_000 },
    );
    await waitFor(() => expect(workspaceRef.current).not.toBeNull());
    const workspace = workspaceRef.current;
    if (workspace === null) throw new Error("sitting workspace was not ready");
    const preflight = await workspace.synchronize();
    const prepared = await workspace.prepare(
      [
        {
          proposal_id: proposal.proposal_id,
          verb: "confirm",
          canonical_sha256: proposal.canonical_sha256,
        },
      ],
      preflight.generation,
    );
    const beforeCommit = await server.pull({});
    const serverCommit = await server.push({
      batch: prepared.commit.snapshot,
      baseSha256: beforeCommit.docSha256,
      baseStructuredHeadSha256: beforeCommit.structuredHeadSha256,
      baseYdocGeneration: beforeCommit.ydocGeneration,
      compaction: {
        snapshot: prepared.commit.snapshot,
        snapshotSha256: prepared.commit.snapshot_sha256,
      },
    });
    if (!serverCommit.ok) throw new Error("test sitting commit was stale");
    const committedHead =
      serverCommit.structuredHeadSha256 ?? serverCommit.docSha256;
    const response: SittingResponse = {
      ok: true,
      intent_id: "intent-head-advance",
      partial: false,
      results: [],
      materialize: {
        new_file_sha256: prepared.commit.rendered_sha256,
        document_version_id: "version-sitting",
      },
      structured_head_sha256: committedHead,
      snapshot_sha256: prepared.commit.snapshot_sha256,
    };

    await act(async () => {
      await workspace.refreshFromServer(response, preflight.generation);
    });
    prepared.dispose();
    await waitFor(() => expect(editorRef.current?.getText()).toBe("The slow brown fox"));
    expect(committedHead).not.toBe(initialServerHead);
    expect(states[states.length - 1]).toMatchObject({
      kind: "up_to_date",
      fileSha256: prepared.commit.rendered_sha256,
    });

    const editor = editorRef.current;
    if (editor === null) throw new Error("editor was not ready");
    const fox = resolveQuoteAnchor(editor.state.doc, {
      exact: "fox",
      prefix: "brown ",
      suffix: "",
    });
    if (fox === null) throw new Error("post-sitting edit anchor was not ready");
    act(() => editor.view.dispatch(editor.state.tr.insertText("!", fox.to)));
    await waitFor(() =>
      expect(states[states.length - 1]).toMatchObject({
        kind: "unsaved",
        fileSha256: prepared.commit.rendered_sha256,
      }),
    );
    const controller = controllerRef.current;
    if (controller === null) throw new Error("materialization controller was not ready");
    await act(async () => controller.save());

    expect(saveRequests).toHaveLength(1);
    expect(saveRequests[0]).toMatchObject({
      expected_file_sha256: prepared.commit.rendered_sha256,
      rendered_markdown: "The slow brown fox!",
    });
    expect(saveRequests[0].expected_ydoc_head_sha256).not.toBe(initialServerHead);
  }, 25_000);

  it("settles and compacts a detached document without writing its source file", async () => {
    const initialized = await bootstrapCoworkYdoc(
      new TextEncoder().encode("Initial text"),
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
    let failNextUpdate = true;
    let compactionCount = 0;
    const transport: CoworkYdocTransport = {
      pull: (request) => server.pull(request),
      push: async (request) => {
        if (request.compaction !== undefined) {
          compactionCount += 1;
          return server.push(request);
        }
        if (failNextUpdate) {
          failNextUpdate = false;
          throw new TypeError("offline");
        }
        return server.push(request);
      },
    };
    const materializationFetch = vi.fn(async () => {
      throw new Error("Detached lifecycle settlement must not materialize");
    });
    const controllerRef: { current: CoworkMaterializationController | null } = {
      current: null,
    };
    const statuses: string[] = [];
    const document = new Y.Doc();

    render(
      <CoworkBridgeEditor
        document={document}
        transport={transport}
        seedMarkdown=""
        documentId={`detached-settle-${Date.now()}`}
        storeId="detached-settle-store"
        currentFileSha256={initialized.sourceSha256}
        initialDriftState="clean"
        canMaterialize={false}
        materializationClient={
          new HttpCoworkMaterializationClient(
            materializationFetch as typeof fetch,
          )
        }
        onSyncStatus={(status) => statuses.push(status)}
        onMaterializationController={(controller) => {
          controllerRef.current = controller;
        }}
      />,
    );
    const textbox = await screen.findByRole(
      "textbox",
      { name: "Document editor" },
      { timeout: 10_000 },
    );
    await waitFor(() => expect(controllerRef.current).not.toBeNull());

    act(() => {
      document.transact(() => {
        const paragraph = document.getXmlFragment("default").get(0);
        if (!(paragraph instanceof Y.XmlElement)) throw new Error("missing paragraph");
        paragraph.insert(paragraph.length, [new Y.XmlText(" lifecycle edit")]);
      }, ySyncPluginKey);
    });
    await waitFor(() =>
      expect(textbox.textContent).toContain("Initial text lifecycle edit"),
    );
    await waitFor(() => expect(statuses).toContain("offline"));
    const controller = controllerRef.current;
    if (controller === null) throw new Error("materialization controller was not ready");

    await act(async () => controller.settleForLifecycle());

    expect(materializationFetch).not.toHaveBeenCalled();
    expect(compactionCount).toBe(1);
    expect(server.pendingBatchCount).toBe(0);
    expect(server.hasSnapshot).toBe(true);
    const persisted = await server.pull({});
    const persistedDocument = new Y.Doc();
    if (persisted.snapshot !== null) {
      Y.applyUpdate(persistedDocument, persisted.snapshot);
    }
    for (const batch of persisted.batches) {
      Y.applyUpdate(persistedDocument, batch);
    }
    expect(persistedDocument.getXmlFragment("default").toString()).toContain(
      "Initial text lifecycle edit",
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

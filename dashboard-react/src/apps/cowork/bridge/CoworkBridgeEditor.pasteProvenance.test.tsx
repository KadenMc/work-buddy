import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Editor } from "@tiptap/core";
import type { CoworkEditorLens } from "../editor/ledgerDecorations";
import { DOMParser as ProseMirrorDOMParser } from "@tiptap/pm/model";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as Y from "yjs";

import {
  refreshLocalIdentity,
  resetLocalIdentityForTests,
} from "../../../security/localIdentity";

import { bootstrapCoworkYdoc } from "../documents/bootstrapCoworkYdoc";
import { InMemoryCoworkYdocTransport } from "../persistence/InMemoryCoworkYdocTransport";
import type {
  CoworkYdocPushRequest,
  CoworkYdocTransport,
} from "../persistence/transport";
import {
  COWORK_PROVENANCE_EXACT_MAX_CHARS,
  DurableCoworkPasteProvenanceOutbox,
  InMemoryCoworkPasteProvenanceIntentStage,
  InMemoryCoworkPasteProvenanceOutboxBackingStore,
  resolveCoworkPasteAnchor,
  unknownCoworkProvenanceDetermination,
  type CoworkPasteProvenanceCapture,
  type CoworkPasteProvenanceIntentStage,
  type CoworkPasteProvenanceOutbox,
  type CoworkPasteProvenanceRecorder,
  type CoworkPasteProvenanceRequest,
  type CoworkProvenanceActorIdentity,
  type ProvenanceData,
  type ProvenanceProvider,
} from "../provenance";
import { CoworkHttpError } from "../providers/errors";
import {
  CoworkBridgeEditor,
  coworkPasteProvenanceOutboxKey,
} from "./CoworkBridgeEditor";

interface MountedPasteEditor {
  readonly editor: Editor;
  readonly document: Y.Doc;
  readonly server: InMemoryCoworkYdocTransport;
  readonly events: string[];
  readonly unmount: () => void;
  readonly setActiveLens: (lens: CoworkEditorLens) => void;
}

let nextDocument = 0;
const ACTOR = {
  kind: "human",
  ref: "dashboard-user",
  identity_status: "local_actor_ref",
} as const;
const LOCAL_PRINCIPAL = {
  actor: {
    schema: "wb.actor-ref/v1" as const,
    issuer_authority_id: "wia_test",
    subject: "wactor_test",
    kind: "human" as const,
    tenant_scope_id: "wts_test",
  },
  origin: window.location.origin,
  audience: "work-buddy-dashboard",
  session_expires_at: 99,
  rotation_due_at: 50,
  assurance: "enrolled_local_session" as const,
};

beforeEach(() => resetLocalIdentityForTests());

const mountPasteEditor = async (
  recorder: CoworkPasteProvenanceRecorder,
  options: {
    readonly outbox?: CoworkPasteProvenanceOutbox;
    readonly documentId?: string;
    readonly provenanceActor?: CoworkProvenanceActorIdentity;
    readonly resolveActorFromServer?: boolean;
    readonly onPushStart?: () => void;
    readonly beforePush?: () => Promise<void>;
    readonly activeLens?: CoworkEditorLens;
    readonly provenanceProvider?: ProvenanceProvider;
    readonly document?: Y.Doc;
    readonly server?: InMemoryCoworkYdocTransport;
  } = {},
): Promise<MountedPasteEditor> => {
  const server = options.server ?? new InMemoryCoworkYdocTransport();
  if (options.server === undefined) {
    const initialized = await bootstrapCoworkYdoc(
      new TextEncoder().encode("Before after"),
    );
    if (!initialized.ok) throw new Error(initialized.message);
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
  }

  const events: string[] = [];
  const transport: CoworkYdocTransport = {
    pull: (request) => server.pull(request),
    push: async (request: CoworkYdocPushRequest) => {
      events.push("push");
      options.onPushStart?.();
      await options.beforePush?.();
      return server.push(request);
    },
  };
  const editorRef: { current: Editor | null } = { current: null };
  const document = options.document ?? new Y.Doc();
  nextDocument += 1;
  const documentId =
    options.documentId ??
    `paste-provenance-${String(nextDocument)}-${String(Date.now())}`;
  const view = (activeLens: CoworkEditorLens | undefined) => (
    <CoworkBridgeEditor
      document={document}
      transport={transport}
      seedMarkdown=""
      documentId={documentId}
      storeId="paste-provenance-store"
      activeLens={activeLens}
      provenanceProvider={options.provenanceProvider}
      provenanceSelectionActionsActive
      onProvenanceSelectionAction={() => undefined}
      provenanceActor={
        options.resolveActorFromServer
          ? undefined
          : (options.provenanceActor ?? ACTOR)
      }
      pasteProvenanceOutbox={options.outbox}
      onRecordPasteProvenance={async (request) => {
        events.push("record");
        await recorder(request);
      }}
      onReady={({ editor }) => {
        editorRef.current = editor;
      }}
    />
  );
  const rendered = render(view(options.activeLens));
  await screen.findByRole(
    "textbox",
    { name: "Document editor" },
    { timeout: 10_000 },
  );
  await waitFor(() => expect(editorRef.current).not.toBeNull());
  return {
    editor: editorRef.current!,
    document,
    server,
    events,
    unmount: rendered.unmount,
    setActiveLens: (lens) => rendered.rerender(view(lens)),
  };
};

const emptyProvenanceData = (): ProvenanceData => ({
  schema: "cowork-provenance-view/v1",
  currentStructuredHeadSha256: null,
  documentDefault: null,
  spans: [],
  history: [],
  summary: {
    totalTargets: 0,
    currentSpanCount: 0,
    aiUnreviewedCount: 0,
    reviewedCount: 0,
    conflictedCount: 0,
    staleCount: 0,
    unrecorded: true,
  },
});

const provenanceProvider = (): ProvenanceProvider => {
  const data = emptyProvenanceData();
  return {
    load: vi.fn().mockResolvedValue({ state: "ready", data }),
    refresh: vi.fn().mockResolvedValue({ state: "ready", data }),
    subscribe: () => () => undefined,
    markReviewed: vi.fn().mockResolvedValue(undefined),
  };
};

const dispatchSimplePaste = (editor: Editor): void => {
  act(() => {
    editor.view.dispatch(
      editor.state.tr.insertText("pasted ", 8).setMeta("uiEvent", "paste"),
    );
  });
};

const dispatchSubstantialPaste = (editor: Editor, position = 8): void => {
  const host = document.createElement("div");
  host.innerHTML =
    "<p>First pasted paragraph.</p><p>Second pasted paragraph.</p>";
  const slice = ProseMirrorDOMParser.fromSchema(editor.schema).parseSlice(host);
  act(() => {
    editor.view.dispatch(
      editor.state.tr
        .replaceRange(position, position, slice)
        .setMeta("uiEvent", "paste"),
    );
  });
};

describe("CoworkBridgeEditor paste provenance", () => {
  it("records ordinary typing when Provenance opens immediately", async () => {
    const requests: CoworkPasteProvenanceRequest[] = [];
    const provider = provenanceProvider();
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `direct-entry-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const mounted = await mountPasteEditor(
      async (request) => {
        requests.push(request);
      },
      { outbox, activeLens: "neutral", provenanceProvider: provider },
    );

    act(() => mounted.editor.commands.insertContentAt(1, "Test"));
    mounted.setActiveLens("provenance");

    await waitFor(() => expect(requests).toHaveLength(1), {
      timeout: 10_000,
    });
    expect(requests[0]).toMatchObject({
      sourceKind: "direct_entry",
      basisKind: "automatic_direct_entry_attribution",
      expectedActorRef: ACTOR.ref,
      expectedActorIdentityStatus: ACTOR.identity_status,
      anchor: { exact: "Test", prefix: "", suffix: "Before after" },
      attestation: {
        authorship: {
          kind: "human",
          contributors: [{ ref: ACTOR.ref }],
        },
      },
    });
    expect(provider.refresh).toHaveBeenCalledOnce();
    await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
    expect(screen.queryByText(/where did this text come from/i)).toBeNull();
  }, 30_000);

  it("keeps corrections inside one honest direct-entry span", async () => {
    const requests: CoworkPasteProvenanceRequest[] = [];
    const mounted = await mountPasteEditor(
      async (request) => {
        requests.push(request);
      },
      { activeLens: "neutral", provenanceProvider: provenanceProvider() },
    );

    act(() => {
      mounted.editor.commands.setTextSelection(1);
      mounted.editor.commands.insertContent("Test");
      mounted.editor.commands.deleteRange({ from: 4, to: 5 });
      mounted.editor.commands.insertContentAt(4, "ting");
    });
    mounted.setActiveLens("provenance");

    await waitFor(() => expect(requests).toHaveLength(1), {
      timeout: 10_000,
    });
    expect(requests[0]?.anchor.exact).toBe("Testing");
  }, 30_000);

  it("maps displaced nearby bursts to one final head before freezing them", async () => {
    const requests: CoworkPasteProvenanceRequest[] = [];
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `displaced-direct-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const mounted = await mountPasteEditor(
      async (request) => {
        requests.push(request);
      },
      {
        outbox,
        activeLens: "neutral",
        provenanceProvider: provenanceProvider(),
      },
    );

    act(() => {
      mounted.editor.commands.insertContentAt(8, "A");
      // Insert before the first burst. This changes both its absolute position
      // and its bounded prefix, so retaining the pre-transaction selector
      // would not resolve against the final compacted document.
      mounted.editor.commands.insertContentAt(1, "B");
    });
    mounted.setActiveLens("provenance");

    await waitFor(() => expect(requests).toHaveLength(2), {
      timeout: 10_000,
    });
    expect(
      new Set(requests.map((request) => request.expectedStructuredHeadSha256))
        .size,
    ).toBe(1);
    expect(requests.map((request) => request.anchor.exact).sort()).toEqual([
      "A",
      "B",
    ]);
    for (const request of requests) {
      const resolution = resolveCoworkPasteAnchor(
        mounted.editor.state.doc,
        request.anchor,
      );
      expect(resolution.kind).toBe("unique");
      if (resolution.kind === "unique") {
        expect(
          mounted.editor.state.doc.textBetween(
            resolution.from,
            resolution.to,
            "\n",
          ),
        ).toBe(request.anchor.exact);
      }
    }
    await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
  }, 30_000);

  it("keeps a closing burst mapped when another edit lands during compaction", async () => {
    let blockPush = false;
    let releasePush!: () => void;
    let enteredBlockedPush!: () => void;
    const pushGate = new Promise<void>((resolve) => {
      releasePush = resolve;
    });
    const blockedPushEntered = new Promise<void>((resolve) => {
      enteredBlockedPush = resolve;
    });
    const requests: CoworkPasteProvenanceRequest[] = [];
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `inflight-map-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const mounted = await mountPasteEditor(
      async (request) => {
        requests.push(request);
      },
      {
        outbox,
        activeLens: "neutral",
        provenanceProvider: provenanceProvider(),
        beforePush: async () => {
          if (!blockPush) return;
          enteredBlockedPush();
          await pushGate;
        },
      },
    );

    act(() => mounted.editor.commands.insertContentAt(8, "A"));
    await waitFor(() => expect(mounted.events).toContain("push"));
    await waitFor(async () => {
      expect((await outbox.list())[0]).toMatchObject({
        status: "capturing",
        anchor: { exact: "A" },
      });
    });

    blockPush = true;
    mounted.setActiveLens("provenance");
    await blockedPushEntered;
    act(() => mounted.editor.commands.insertContentAt(1, "B"));
    releasePush();

    await waitFor(() => expect(requests).toHaveLength(2), {
      timeout: 10_000,
    });
    const original = requests.find((request) => request.anchor.exact === "A");
    expect(original).toBeDefined();
    const resolution = resolveCoworkPasteAnchor(
      mounted.editor.state.doc,
      original!.anchor,
    );
    expect(resolution.kind).toBe("unique");
    if (resolution.kind === "unique") {
      expect(
        mounted.editor.state.doc.textBetween(
          resolution.from,
          resolution.to,
          "\n",
        ),
      ).toBe("A");
    }
    await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
  }, 30_000);

  it("cancels provenance when the whole newly typed burst is deleted", async () => {
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>().mockResolvedValue();
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `deleted-burst-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const mounted = await mountPasteEditor(recorder, {
      outbox,
      activeLens: "neutral",
      provenanceProvider: provenanceProvider(),
    });

    act(() => {
      mounted.editor.commands.insertContentAt(1, "Test");
      mounted.editor.commands.deleteRange({ from: 1, to: 5 });
    });
    mounted.setActiveLens("provenance");

    await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
    expect(recorder).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).toBeNull();
  }, 30_000);

  it("leaves an open typed burst durable across unmount and records it on reopen", async () => {
    const requests: CoworkPasteProvenanceRequest[] = [];
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `reopen-direct-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const documentId = `reopen-direct-document-${String(Date.now())}`;
    const first = await mountPasteEditor(
      async (request) => {
        requests.push(request);
      },
      { outbox, document: new Y.Doc(), documentId, activeLens: "neutral" },
    );
    act(() => first.editor.commands.insertContentAt(1, "Test"));
    await waitFor(async () => {
      expect((await outbox.list())[0]).toMatchObject({
        status: "capturing",
        anchor: { exact: "Test" },
      });
    });
    first.unmount();
    expect(requests).toEqual([]);

    const reopened = await mountPasteEditor(
      async (request) => {
        requests.push(request);
      },
      {
        outbox,
        documentId,
        server: first.server,
        document: new Y.Doc(),
        activeLens: "provenance",
        provenanceProvider: provenanceProvider(),
      },
    );
    await waitFor(() => expect(requests).toHaveLength(1), {
      timeout: 10_000,
    });
    expect(requests[0]?.anchor.exact).toBe("Test");
    reopened.unmount();
  }, 30_000);

  it("retires an ownerless legacy actor-change row after reload so selection can record again", async () => {
    const requests: CoworkPasteProvenanceRequest[] = [];
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `ownerless-legacy-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    await outbox.append({
      anchor: { exact: "Before", prefix: "", suffix: " after" },
      idempotencyKey: "prior-manual-key",
      substantial: false,
      capturedActor: ACTOR,
      sourceKind: "legacy",
      basisKind: "user_attestation",
      determination: unknownCoworkProvenanceDetermination(),
      capturedAt: new Date().toISOString(),
      passageExcerpt: "Before",
      status: "ready",
    });
    await outbox.resetAfterActorChange(
      "actor-recovery",
      unknownCoworkProvenanceDetermination(),
    );

    const mounted = await mountPasteEditor(
      async (request) => {
        requests.push(request);
      },
      {
        outbox,
        activeLens: "provenance",
        provenanceProvider: provenanceProvider(),
      },
    );
    await waitFor(() => expect(outbox.list()).resolves.toEqual([]));

    act(() => {
      mounted.editor.commands.setTextSelection({ from: 1, to: 7 });
    });
    await userEvent.click(
      await screen.findByRole("button", { name: "Record provenance" }),
    );
    await userEvent.click(
      within(
        screen.getByRole("dialog", { name: "Record provenance" }),
      ).getByRole("button", { name: "Record provenance" }),
    );

    await waitFor(() => expect(requests).toHaveLength(1), {
      timeout: 10_000,
    });
    expect(requests[0]).toMatchObject({
      sourceKind: "legacy",
      basisKind: "user_attestation",
      expectedActorRef: ACTOR.ref,
      anchor: { exact: "Before" },
    });
    await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
  }, 30_000);

  it("never silently changes a frozen manual determination on ambiguous retry", async () => {
    const recorder = vi
      .fn<CoworkPasteProvenanceRecorder>()
      .mockRejectedValueOnce(
        new CoworkHttpError({
          code: "network_error",
          message: "Connection interrupted after submission.",
          retryable: true,
        }),
      )
      .mockResolvedValueOnce();
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `frozen-manual-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const mounted = await mountPasteEditor(recorder, {
      outbox,
      activeLens: "provenance",
      provenanceProvider: provenanceProvider(),
    });
    const user = userEvent.setup();

    act(() => {
      mounted.editor.commands.setTextSelection({ from: 1, to: 7 });
    });
    await user.click(
      await screen.findByRole("button", { name: "Record provenance" }),
    );
    const dialog = screen.getByRole("dialog", { name: "Record provenance" });
    const confirm = within(dialog).getByRole("button", {
      name: "Record provenance",
    });
    await user.click(confirm);
    await waitFor(() => expect(recorder).toHaveBeenCalledOnce());
    await screen.findByText("Connection interrupted after submission.");

    await user.click(
      within(dialog).getByRole("button", { name: /Authorship/i }),
    );
    await user.click(screen.getByRole("option", { name: /AI-written/i }));
    await user.click(confirm);

    expect(recorder).toHaveBeenCalledOnce();
    expect(
      await screen.findByText(/pending request is already frozen/i),
    ).toBeVisible();
    expect(recorder.mock.calls[0]?.[0].attestation.authorship.kind).toBe(
      "human",
    );

    await user.click(
      within(dialog).getByRole("button", { name: /Authorship/i }),
    );
    await user.click(screen.getByRole("option", { name: /Human-written/i }));
    await user.click(confirm);
    await waitFor(() => expect(recorder).toHaveBeenCalledTimes(2));
    expect(recorder.mock.calls[1]?.[0]).toEqual(recorder.mock.calls[0]?.[0]);
    await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
  }, 30_000);

  it("journals provenance before the paste can enter durable Yjs state", async () => {
    const order: string[] = [];
    const intentStage = new InMemoryCoworkPasteProvenanceIntentStage();
    const observingStage: CoworkPasteProvenanceIntentStage = {
      list: (key) => intentStage.list(key),
      put: (key: string, capture: CoworkPasteProvenanceCapture) => {
        order.push("journal");
        intentStage.put(key, capture);
      },
      remove: (key, idempotencyKey) => intentStage.remove(key, idempotencyKey),
    };
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `ordering-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
      observingStage,
    );
    const mounted = await mountPasteEditor(async () => undefined, {
      outbox,
    });
    mounted.document.on("update", () => order.push("ydoc"));

    dispatchSimplePaste(mounted.editor);

    expect(order).toContain("ydoc");
    expect(order[0]).toBe("journal");
  }, 20_000);

  it("blocks an oversized paste before journaling or changing the Y.Doc", async () => {
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `oversized-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>().mockResolvedValue();
    const mounted = await mountPasteEditor(recorder, { outbox });
    const initialText = mounted.editor.getText();
    const initialEvents = mounted.events.length;

    act(() => {
      mounted.editor.view.dispatch(
        mounted.editor.state.tr
          .insertText("x".repeat(COWORK_PROVENANCE_EXACT_MAX_CHARS + 1), 8)
          .setMeta("uiEvent", "paste"),
      );
    });

    expect(mounted.editor.getText()).toBe(initialText);
    expect(mounted.document.getXmlFragment("default").toString()).not.toContain(
      "xxx",
    );
    expect(await outbox.list()).toEqual([]);
    expect(recorder).not.toHaveBeenCalled();
    expect(mounted.events).toHaveLength(initialEvents);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Nothing was inserted. Paste it in sections of 1,000,000 characters or fewer.",
    );
  }, 30_000);

  it("revalidates the pasted passage only after its Yjs head is flushed", async () => {
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `post-flush-anchor-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>().mockResolvedValue();
    let liveEditor: Editor | null = null;
    let removeDuringFirstPush = false;
    const mounted = await mountPasteEditor(recorder, {
      outbox,
      onPushStart: () => {
        if (!removeDuringFirstPush || liveEditor === null) return;
        removeDuringFirstPush = false;
        act(() => {
          liveEditor!.view.dispatch(liveEditor!.state.tr.delete(8, 15));
        });
      },
    });
    liveEditor = mounted.editor;
    removeDuringFirstPush = true;

    dispatchSimplePaste(mounted.editor);

    await waitFor(async () => {
      expect((await outbox.list())[0]?.status).toBe("stale_target");
    });
    expect(mounted.editor.getText()).toBe("Before after");
    expect(recorder).not.toHaveBeenCalled();
  }, 20_000);

  it("keeps a simple paste and automatically attributes its exact persisted span", async () => {
    const requests: CoworkPasteProvenanceRequest[] = [];
    let mounted: MountedPasteEditor;
    mounted = await mountPasteEditor(async (request) => {
      requests.push(request);
      expect(request.expectedStructuredHeadSha256).toBe(
        (await mounted.server.pull({})).structuredHeadSha256,
      );
    });

    dispatchSimplePaste(mounted.editor);

    // Recording is asynchronous; the user's text is present immediately.
    expect(mounted.editor.getText()).toBe("Before pasted after");
    await waitFor(() => expect(requests).toHaveLength(1));
    expect(mounted.events).toEqual(["push", "push", "record"]);
    expect(requests[0]).toMatchObject({
      storeId: "paste-provenance-store",
      basisKind: "automatic_short_text_attribution",
      anchor: {
        exact: "pasted ",
        prefix: "Before ",
        suffix: "after",
      },
      attestation: {
        authorship: {
          kind: "human",
          contributors: [
            {
              kind: "current_user",
              ref: "dashboard-user",
              identity_status: "local_actor_ref",
            },
          ],
        },
        human_review: {
          status: "not_applicable",
          reviewers: [],
        },
      },
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  }, 20_000);

  it("asks about a substantial paste and records the user determination", async () => {
    const user = userEvent.setup();
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>().mockResolvedValue();
    const mounted = await mountPasteEditor(recorder);

    dispatchSubstantialPaste(mounted.editor);

    expect(mounted.editor.getText()).toContain("First pasted paragraph.");
    expect(mounted.editor.getText()).toContain("Second pasted paragraph.");
    expect(recorder).not.toHaveBeenCalled();
    expect(
      await screen.findByRole("dialog", {
        name: "Where did this text come from?",
      }),
    ).toBeVisible();

    await user.click(screen.getByRole("button", { name: /Authorship/i }));
    await user.click(screen.getByRole("option", { name: /AI-written/i }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(recorder).toHaveBeenCalledOnce());
    const request = recorder.mock.calls[0]?.[0];
    expect(request).toMatchObject({
      basisKind: "user_attestation",
      attestation: {
        authorship: { kind: "ai", contributors: [] },
        human_review: { status: "not_reviewed", reviewers: [] },
      },
    });
    expect(request?.anchor.exact).toContain("First pasted paragraph.");
    expect(request?.anchor.exact).toContain("Second pasted paragraph.");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  }, 20_000);

  it("records explicit unknown provenance when the user decides later", async () => {
    const user = userEvent.setup();
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>().mockResolvedValue();
    const mounted = await mountPasteEditor(recorder);
    dispatchSubstantialPaste(mounted.editor);

    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "Decide later" }));

    await waitFor(() => expect(recorder).toHaveBeenCalledOnce());
    expect(recorder.mock.calls[0]?.[0]).toMatchObject({
      basisKind: "user_attestation",
      attestation: {
        authorship: { kind: "unknown", contributors: [] },
        human_review: { status: "not_applicable", reviewers: [] },
      },
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  }, 20_000);

  it("records explicit unknown provenance when the modal is dismissed", async () => {
    const user = userEvent.setup();
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>().mockResolvedValue();
    const mounted = await mountPasteEditor(recorder);
    dispatchSubstantialPaste(mounted.editor);

    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");

    await waitFor(() => expect(recorder).toHaveBeenCalledOnce());
    expect(recorder.mock.calls[0]?.[0]).toMatchObject({
      basisKind: "user_attestation",
      attestation: {
        authorship: { kind: "unknown", contributors: [] },
      },
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  }, 20_000);

  it("settles Save and a same-turn Escape exactly once", async () => {
    const user = userEvent.setup();
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>().mockResolvedValue();
    const mounted = await mountPasteEditor(recorder);
    dispatchSubstantialPaste(mounted.editor);

    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: /Authorship/i }));
    await user.click(screen.getByRole("option", { name: /AI-written/i }));
    const save = screen.getByRole("button", { name: "Save" });
    act(() => {
      save.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      document.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Escape",
          code: "Escape",
          bubbles: true,
        }),
      );
    });

    await waitFor(() => expect(recorder).toHaveBeenCalledOnce());
    expect(recorder.mock.calls[0]?.[0]).toMatchObject({
      attestation: {
        authorship: { kind: "ai", contributors: [] },
        human_review: { status: "not_reviewed", reviewers: [] },
      },
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  }, 20_000);

  it("keeps a deferred determination recoverable until unknown provenance persists", async () => {
    const user = userEvent.setup();
    const recorder = vi
      .fn<CoworkPasteProvenanceRecorder>()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce();
    const mounted = await mountPasteEditor(recorder);
    dispatchSubstantialPaste(mounted.editor);

    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "Decide later" }));
    expect(
      await screen.findByText(
        "Co-work couldn’t record where this pasted text came from. Try again.",
      ),
    ).toBeVisible();
    expect(screen.getByRole("dialog")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Decide later" }));
    await waitFor(() => expect(recorder).toHaveBeenCalledTimes(2));
    expect(recorder.mock.calls[1]?.[0].idempotencyKey).toBe(
      recorder.mock.calls[0]?.[0].idempotencyKey,
    );
    expect(recorder.mock.calls[1]?.[0].expectedStructuredHeadSha256).toBe(
      recorder.mock.calls[0]?.[0].expectedStructuredHeadSha256,
    );
    expect(
      recorder.mock.calls.every(
        ([request]) => request.attestation.authorship.kind === "unknown",
      ),
    ).toBe(true);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  }, 20_000);

  it("rehydrates an unresolved determination after editor hydration", async () => {
    const user = userEvent.setup();
    const backing = new InMemoryCoworkPasteProvenanceOutboxBackingStore();
    const persisted = new DurableCoworkPasteProvenanceOutbox(
      "paste-provenance-store:reload-doc",
      backing,
    );
    await persisted.append({
      anchor: {
        exact: "Before",
        prefix: "",
        suffix: " after",
      },
      idempotencyKey: "persisted-paste-key",
      substantial: true,
      basisKind: "user_attestation",
      determination: unknownCoworkProvenanceDetermination(),
      status: "awaiting_determination",
    });
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>().mockResolvedValue();

    await mountPasteEditor(recorder, {
      outbox: new DurableCoworkPasteProvenanceOutbox(
        "paste-provenance-store:reload-doc",
        backing,
      ),
      documentId: "reload-doc",
    });

    expect(
      await screen.findByRole("dialog", {
        name: "Where did this text come from?",
      }),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Decide later" }));
    await waitFor(() => expect(recorder).toHaveBeenCalledOnce());
    expect(recorder.mock.calls[0]?.[0].idempotencyKey).toBe(
      "persisted-paste-key",
    );
    await waitFor(() => expect(persisted.list()).resolves.toEqual([]));
  }, 20_000);

  it("queues multiple substantial pastes instead of replacing the first", async () => {
    const user = userEvent.setup();
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `queue-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>().mockResolvedValue();
    const mounted = await mountPasteEditor(recorder, { outbox });

    dispatchSubstantialPaste(mounted.editor);
    dispatchSubstantialPaste(
      mounted.editor,
      mounted.editor.state.doc.content.size - 1,
    );

    await waitFor(() => expect(outbox.list()).resolves.toHaveLength(2));
    expect(
      await screen.findByText(/1 more pasted passage is waiting\./),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Decide later" }));
    await waitFor(() => expect(recorder).toHaveBeenCalledOnce());
    expect(screen.getByRole("dialog")).toBeVisible();
    expect(await outbox.list()).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Decide later" }));
    await waitFor(() => expect(recorder).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  }, 20_000);

  it("keeps one document queue across actor switches", () => {
    const firstKey = coworkPasteProvenanceOutboxKey("store", "document");
    const secondKey = coworkPasteProvenanceOutboxKey("store", "document");

    expect(secondKey).toBe(firstKey);
    expect(coworkPasteProvenanceOutboxKey("store", "other")).not.toBe(firstKey);
  });

  it("refetches a changed actor and requires explicit reconfirmation without retrying blindly", async () => {
    const user = userEvent.setup();
    const nextActor = {
      kind: "human",
      ref: "different-dashboard-user",
      identity_status: "local_actor_ref",
    } as const;
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `actor-change-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const recorder = vi
      .fn<CoworkPasteProvenanceRecorder>()
      .mockRejectedValueOnce(
        new CoworkHttpError({
          code: "provenance_actor_changed",
          message: "The acting user changed.",
          retryable: false,
          status: 409,
        }),
      )
      .mockResolvedValueOnce();
    const actorFetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/local-identity/session/csrf") {
        return new Response(
          JSON.stringify({
            ok: true,
            authenticated: true,
            principal: LOCAL_PRINCIPAL,
            csrf_token: "wbc_actor_change",
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      expect(String(input)).toBe("/api/truth/cowork/current-actor");
      return new Response(
        JSON.stringify({
          kind: nextActor.kind,
          ref: nextActor.ref,
          identity_status: nextActor.identity_status,
        }),
        { headers: { "Content-Type": "application/json" } },
      );
    });
    vi.stubGlobal("fetch", actorFetch);

    let mounted: MountedPasteEditor | undefined;
    try {
      mounted = await mountPasteEditor(recorder, {
        outbox,
        provenanceActor: ACTOR,
      });
      dispatchSimplePaste(mounted.editor);

      expect(
        await screen.findByText(
          "The active identity changed before this attribution was saved. Confirm it again for the current person.",
        ),
      ).toBeVisible();
      await waitFor(() =>
        expect(
          actorFetch.mock.calls.filter(
            ([input]) => String(input) === "/api/truth/cowork/current-actor",
          ),
        ).toHaveLength(1),
      );
      await new Promise((resolve) => window.setTimeout(resolve, 50));
      expect(recorder).toHaveBeenCalledOnce();
      expect(mounted.editor.getText()).toBe("Before pasted after");

      const pending = (await outbox.list())[0];
      expect(pending).toMatchObject({
        status: "awaiting_determination",
        basisKind: "user_attestation",
        requiresExplicitDetermination: true,
        determination: {
          authorship: { kind: "unknown", contributors: [] },
        },
        failure: { code: "provenance_actor_changed" },
      });
      expect(pending?.frozenRequest).toBeUndefined();
      expect(pending?.idempotencyKey).not.toBe(
        recorder.mock.calls[0]?.[0].idempotencyKey,
      );

      await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(recorder).toHaveBeenCalledOnce();
      await user.click(
        screen.getByRole("button", {
          name: "Review pending attribution",
        }),
      );
      await screen.findByRole("dialog");
      await user.click(screen.getByRole("button", { name: /Authorship/i }));
      await user.click(screen.getByRole("option", { name: /^Human-written/ }));
      await user.click(
        screen.getByRole("button", { name: "Confirm attribution" }),
      );

      await waitFor(() => expect(recorder).toHaveBeenCalledTimes(2));
      expect(recorder.mock.calls[1]?.[0]).toMatchObject({
        basisKind: "user_attestation",
        attestation: {
          authorship: {
            kind: "human",
            contributors: [
              {
                kind: "current_user",
                ref: nextActor.ref,
                identity_status: nextActor.identity_status,
              },
            ],
          },
        },
      });
      expect(recorder.mock.calls[1]?.[0].idempotencyKey).not.toBe(
        recorder.mock.calls[0]?.[0].idempotencyKey,
      );
      await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
    } finally {
      mounted?.unmount();
      vi.unstubAllGlobals();
    }
  }, 30_000);

  it("never sends a rehydrated entry whose passage is absent", async () => {
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `absent-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    await outbox.append({
      anchor: {
        exact: "This passage is not in the document.",
        prefix: "",
        suffix: "",
      },
      idempotencyKey: "absent-paste",
      substantial: false,
      basisKind: "automatic_short_text_attribution",
      determination: unknownCoworkProvenanceDetermination(),
      status: "ready",
    });
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>().mockResolvedValue();

    await mountPasteEditor(recorder, { outbox });

    expect(
      await screen.findByText(
        "This pasted passage no longer has one unique match in the current document. Restore or disambiguate it, then save again.",
      ),
    ).toBeVisible();
    expect(screen.getByLabelText("Pasted passage")).toHaveTextContent(
      "This passage is not in the document.",
    );
    expect(recorder).not.toHaveBeenCalled();
    expect((await outbox.list())[0]?.status).toBe("stale_target");
  }, 20_000);

  it("keeps a stale target visible until the user explicitly retargets it", async () => {
    const user = userEvent.setup();
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `stale-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const recorder = vi
      .fn<CoworkPasteProvenanceRecorder>()
      .mockRejectedValueOnce(
        new CoworkHttpError({
          code: "provenance_target_changed",
          message: "stale",
          retryable: true,
          status: 409,
        }),
      )
      .mockResolvedValueOnce();
    const mounted = await mountPasteEditor(recorder, { outbox });
    dispatchSubstantialPaste(mounted.editor);

    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "Decide later" }));
    expect(
      await screen.findByText(
        "This pasted passage no longer has one unique match in the current document. Restore or disambiguate it, then save again.",
      ),
    ).toBeVisible();
    const stale = (await outbox.list())[0];
    expect(stale?.status).toBe("stale_target");
    expect(stale?.frozenRequest?.idempotencyKey).toBe(
      recorder.mock.calls[0]?.[0].idempotencyKey,
    );

    await user.click(screen.getByRole("button", { name: "Keep for later" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const reopen = screen.getByRole("button", {
      name: "Review pending attribution",
    });
    expect(reopen).toBeVisible();
    await user.click(reopen);
    await screen.findByRole("dialog");

    await user.click(
      screen.getByRole("button", { name: "Save against current version" }),
    );
    await waitFor(() => expect(recorder).toHaveBeenCalledTimes(2));
    expect(recorder.mock.calls[1]?.[0].idempotencyKey).not.toBe(
      recorder.mock.calls[0]?.[0].idempotencyKey,
    );
    await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
    await waitFor(() =>
      expect(screen.queryByText(/paste attribution is waiting/i)).toBeNull(),
    );
  }, 20_000);

  it("holds a non-retryable rejection for explicit correction instead of looping", async () => {
    const user = userEvent.setup();
    const recorder = vi
      .fn<CoworkPasteProvenanceRecorder>()
      .mockRejectedValueOnce(
        new CoworkHttpError({
          code: "invalid_attestation",
          message: "invalid",
          retryable: false,
          status: 422,
        }),
      )
      .mockResolvedValueOnce();
    const mounted = await mountPasteEditor(recorder);
    dispatchSubstantialPaste(mounted.editor);

    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "Decide later" }));
    expect(
      await screen.findByText(
        "Co-work rejected this attribution. Review or correct it before starting a new save attempt.",
      ),
    ).toBeVisible();
    expect(recorder).toHaveBeenCalledOnce();

    await user.click(
      screen.getByRole("button", { name: "Start corrected save" }),
    );
    await waitFor(() => expect(recorder).toHaveBeenCalledTimes(2));
    expect(recorder.mock.calls[1]?.[0].idempotencyKey).not.toBe(
      recorder.mock.calls[0]?.[0].idempotencyKey,
    );
  }, 20_000);

  it("keeps editing available and durably defers paste attribution when actor lookup fails", async () => {
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>().mockResolvedValue();
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `missing-actor-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    let actorAttempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input) === "/api/local-identity/session/csrf") {
          return new Response(
            JSON.stringify({
              ok: true,
              authenticated: true,
              principal: LOCAL_PRINCIPAL,
              csrf_token: "wbc_test",
            }),
            { headers: { "Content-Type": "application/json" } },
          );
        }
        expect(String(input)).toBe("/api/truth/cowork/current-actor");
        actorAttempts += 1;
        if (actorAttempts === 1) {
          return new Response(
            JSON.stringify({
              error: {
                code: "identity_unavailable",
                message: "Identity service is unavailable.",
                retryable: true,
              },
            }),
            {
              status: 503,
              headers: { "Content-Type": "application/json" },
            },
          );
        }
        return new Response(
          JSON.stringify({
            kind: "human",
            ref: ACTOR.ref,
            identity_status: ACTOR.identity_status,
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }),
    );

    let mounted: MountedPasteEditor | undefined;
    try {
      mounted = await mountPasteEditor(recorder, {
        resolveActorFromServer: true,
        outbox,
      });
      const editorSurface = screen.getByRole("textbox", {
        name: "Document editor",
      });
      await waitFor(() => expect(actorAttempts).toBe(1));
      expect(editorSurface).toHaveAttribute("aria-readonly", "false");
      expect(screen.queryByText(/Reconnect Work Buddy to edit/i)).toBeNull();

      dispatchSimplePaste(mounted.editor);
      await waitFor(async () => {
        const entries = await outbox.list();
        expect(entries).toHaveLength(1);
        expect(entries[0]).toMatchObject({
          substantial: false,
          status: "awaiting_determination",
          requiresExplicitDetermination: true,
          determination: unknownCoworkProvenanceDetermination(),
        });
      });
      expect(recorder).not.toHaveBeenCalled();

      await act(async () => {
        await refreshLocalIdentity();
      });
      await waitFor(() => expect(actorAttempts).toBe(2));
      expect(
        await screen.findByRole("dialog", {
          name: "Where did this text come from?",
        }),
      ).toBeVisible();
      expect(editorSurface).toHaveAttribute("aria-readonly", "false");
      expect(screen.queryByText(/Reconnect Work Buddy to edit/i)).toBeNull();
    } finally {
      mounted?.unmount();
      vi.unstubAllGlobals();
    }
  }, 20_000);

  it("keeps editing available while a launcher identity is absent and reconnects silently", async () => {
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>().mockResolvedValue();
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `missing-session-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    let sessionAttempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/local-identity/session/csrf") {
          sessionAttempts += 1;
          if (sessionAttempts === 1) {
            return new Response(
              JSON.stringify({
                ok: true,
                authenticated: false,
                human_authority_available: false,
              }),
              { headers: { "Content-Type": "application/json" } },
            );
          }
          return new Response(
            JSON.stringify({
              ok: true,
              authenticated: true,
              principal: LOCAL_PRINCIPAL,
              csrf_token: "wbc_reconnected",
            }),
            { headers: { "Content-Type": "application/json" } },
          );
        }
        expect(url).toBe("/api/truth/cowork/current-actor");
        return new Response(
          JSON.stringify({
            kind: "human",
            ref: ACTOR.ref,
            identity_status: ACTOR.identity_status,
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }),
    );

    let mounted: MountedPasteEditor | undefined;
    try {
      mounted = await mountPasteEditor(recorder, {
        resolveActorFromServer: true,
        outbox,
      });
      const editorSurface = screen.getByRole("textbox", {
        name: "Document editor",
      });
      await waitFor(() => expect(sessionAttempts).toBe(1));
      expect(editorSurface).toHaveAttribute("aria-readonly", "false");
      expect(screen.queryByRole("alert")).toBeNull();

      dispatchSimplePaste(mounted.editor);
      await waitFor(async () =>
        expect((await outbox.list())[0]).toMatchObject({
          status: "awaiting_determination",
          requiresExplicitDetermination: true,
        }),
      );

      await act(async () => {
        await refreshLocalIdentity();
      });
      expect(
        await screen.findByRole("dialog", {
          name: "Where did this text come from?",
        }),
      ).toBeVisible();
      expect(editorSurface).toHaveAttribute("aria-readonly", "false");
      expect(screen.queryByRole("alert")).toBeNull();
      expect(sessionAttempts).toBeGreaterThanOrEqual(2);
    } finally {
      mounted?.unmount();
      vi.unstubAllGlobals();
    }
  }, 20_000);
});

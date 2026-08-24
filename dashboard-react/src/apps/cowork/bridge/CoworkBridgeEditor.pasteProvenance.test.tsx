import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Editor } from "@tiptap/core";
import {
  setCoworkEditorLens,
  type CoworkEditorLens,
} from "../editor/ledgerDecorations";
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
  type CoworkPasteProvenanceOutboxBackingStore,
  type CoworkPasteProvenanceReceipt,
  type CoworkPasteProvenanceRecorder as DurableCoworkPasteProvenanceRecorder,
  type CoworkPasteProvenanceRequest,
  type ProvenanceAttestation,
  type CoworkProvenanceActorIdentity,
  type ProvenanceData,
  type ProvenanceProvider,
} from "../provenance";
import { CoworkHttpError } from "../providers/errors";
import {
  CoworkBridgeEditor,
  coworkPasteProvenanceOutboxKey,
} from "./CoworkBridgeEditor";

type CoworkPasteProvenanceRecorder = (
  request: CoworkPasteProvenanceRequest,
) => Promise<CoworkPasteProvenanceReceipt | void>;

interface MountedPasteEditor {
  readonly editor: Editor;
  readonly document: Y.Doc;
  readonly server: InMemoryCoworkYdocTransport;
  readonly events: string[];
  readonly unmount: () => void;
  readonly setActiveLens: (lens: CoworkEditorLens) => void;
  readonly setProvenanceActor: (
    actor: CoworkProvenanceActorIdentity | undefined,
  ) => void;
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

const projectedReceipts = new WeakMap<
  ProvenanceProvider,
  Map<string, CoworkPasteProvenanceReceipt>
>();

const testProvenanceReceipt = (
  request: CoworkPasteProvenanceRequest,
): CoworkPasteProvenanceReceipt => ({
  attestationId: `attestation-${request.idempotencyKey}`,
  documentSpanId: `span-${request.idempotencyKey}`,
  targetStructuredHeadSha256: request.expectedStructuredHeadSha256,
});

const attestationForReceipt = (
  receipt: CoworkPasteProvenanceReceipt,
): ProvenanceAttestation => ({
  attestationId: receipt.attestationId,
  at: "2026-08-21T12:00:00.000Z",
  assertedBy: { kind: "human", ref: ACTOR.ref, meta: null },
  scope: {
    kind: "document_span",
    documentVersionId: null,
    documentSpanId: receipt.documentSpanId,
    structuredHeadSha256: receipt.targetStructuredHeadSha256,
  },
  authorship: { kind: "human", contributors: [] },
  humanReview: { status: "not_applicable", reviewers: [] },
  source: { kind: "direct_entry" },
  basis: { kind: "automatic_direct_entry_attribution", ref: null },
  supersedesId: null,
  canonicalSha256: "a".repeat(64),
});

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
    readonly onInputProvenancePendingChange?: (pending: boolean) => void;
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
  let renderedLens = options.activeLens;
  let renderedActor = options.resolveActorFromServer
    ? undefined
    : (options.provenanceActor ?? ACTOR);
  const durableRecorder: DurableCoworkPasteProvenanceRecorder = async (
    request,
  ) => {
    const receipt = (await recorder(request)) ?? testProvenanceReceipt(request);
    projectedReceipts
      .get(options.provenanceProvider as ProvenanceProvider)
      ?.set(receipt.attestationId, receipt);
    return receipt;
  };
  const view = () => (
    <CoworkBridgeEditor
      document={document}
      transport={transport}
      seedMarkdown=""
      documentId={documentId}
      storeId="paste-provenance-store"
      activeLens={renderedLens}
      provenanceProvider={options.provenanceProvider}
      provenanceSelectionActionsActive
      onProvenanceSelectionAction={() => undefined}
      onInputProvenancePendingChange={options.onInputProvenancePendingChange}
      provenanceActor={renderedActor}
      pasteProvenanceOutbox={options.outbox}
      onRecordPasteProvenance={async (request) => {
        events.push("record");
        return durableRecorder(request);
      }}
      onReady={({ editor }) => {
        editorRef.current = editor;
      }}
    />
  );
  const rendered = render(view());
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
    setActiveLens: (lens) => {
      renderedLens = lens;
      rendered.rerender(view());
    },
    setProvenanceActor: (actor) => {
      renderedActor = actor;
      rendered.rerender(view());
    },
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
  const receipts = new Map<string, CoworkPasteProvenanceReceipt>();
  const provider: ProvenanceProvider = {
    load: vi.fn().mockResolvedValue({ state: "ready", data }),
    refresh: vi.fn().mockImplementation(async () => ({
      state: "ready" as const,
      data: {
        ...data,
        history: [...receipts.values()].map(attestationForReceipt),
      },
    })),
    subscribe: () => () => undefined,
    markReviewed: vi.fn().mockResolvedValue(undefined),
  };
  projectedReceipts.set(provider, receipts);
  return provider;
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

  it("marks only delayed direct-entry text as pending until its receipt settles", async () => {
    let releaseRecorder!: () => void;
    let recorderEntered!: () => void;
    const recorderGate = new Promise<void>((resolve) => {
      releaseRecorder = resolve;
    });
    const entered = new Promise<void>((resolve) => {
      recorderEntered = resolve;
    });
    const pendingChanges: boolean[] = [];
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `pending-direct-entry-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const mounted = await mountPasteEditor(
      async () => {
        recorderEntered();
        await recorderGate;
      },
      {
        outbox,
        activeLens: "neutral",
        provenanceProvider: provenanceProvider(),
        onInputProvenancePendingChange: (pending) =>
          pendingChanges.push(pending),
      },
    );

    try {
      act(() => mounted.editor.commands.insertContentAt(1, "Pending text"));
      expect(pendingChanges[pendingChanges.length - 1]).toBe(true);
      act(() => {
        setCoworkEditorLens(mounted.editor, "provenance");
      });
      await act(async () => Promise.resolve());
      const pending = mounted.editor.view.dom.querySelector<HTMLElement>(
        '[data-wb-provenance-record-state="pending"]',
      );
      expect(pending).not.toBeNull();
      expect(pending).toHaveTextContent("Pending text");
      expect(pending).toHaveClass("wb-cowork-provenance--pending");
      expect(
        mounted.editor.view.dom.querySelector(
          '[data-wb-provenance-record-state="pending"]',
        )?.textContent,
      ).not.toContain("Before after");
      mounted.setActiveLens("provenance");
      await entered;
      await waitFor(() => expect(pendingChanges).toContain(true));

      const unrelated = resolveCoworkPasteAnchor(mounted.editor.state.doc, {
        exact: "after",
        prefix: "Before ",
        suffix: "",
      });
      expect(unrelated.kind).toBe("unique");
      if (unrelated.kind === "unique") {
        act(() => {
          mounted.editor.commands.setTextSelection({
            from: unrelated.from,
            to: unrelated.to,
          });
        });
      }
      expect(
        await screen.findByRole("button", { name: "Record provenance" }),
      ).toBeEnabled();
      expect(
        screen.queryByRole("button", { name: /Recording recent typing/u }),
      ).toBeNull();

      releaseRecorder();
      await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
      await waitFor(() => {
        expect(
          mounted.editor.view.dom.querySelector(
            '[data-wb-provenance-record-state="pending"]',
          ),
        ).toBeNull();
        expect(pendingChanges[pendingChanges.length - 1]).toBe(false);
      });
    } finally {
      releaseRecorder();
      mounted.unmount();
    }
  }, 30_000);

  it("blocks duplicate manual recording only for an overlapping volatile typing capture", async () => {
    const memoryBacking =
      new InMemoryCoworkPasteProvenanceOutboxBackingStore();
    let failWrites = true;
    const failingBacking: CoworkPasteProvenanceOutboxBackingStore = {
      durable: false,
      read: (key) => memoryBacking.read(key),
      mutate: (key, mutation) =>
        failWrites
          ? Promise.reject(new Error("injected outbox write failure"))
          : memoryBacking.mutate(key, mutation),
    };
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `volatile-direct-entry-${String(Date.now())}`,
      failingBacking,
    );
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>();
    const mounted = await mountPasteEditor(recorder, {
      outbox,
      activeLens: "neutral",
      provenanceProvider: provenanceProvider(),
    });

    try {
      act(() => mounted.editor.commands.insertContentAt(1, "Pending text"));
      mounted.setActiveLens("provenance");
      await screen.findByRole("button", { name: "Retry provenance storage" });
      failWrites = false;

      const pending = resolveCoworkPasteAnchor(mounted.editor.state.doc, {
        exact: "Pending text",
        prefix: "",
        suffix: "Before ",
      });
      expect(pending.kind).toBe("unique");
      if (pending.kind === "unique") {
        act(() => {
          mounted.editor.commands.setTextSelection({
            from: pending.from,
            to: pending.to,
          });
        });
      }
      await userEvent.click(
        await screen.findByRole("button", { name: "Record provenance" }),
      );
      const dialog = await screen.findByRole("dialog", {
        name: "Record provenance",
      });
      await userEvent.click(
        within(dialog).getByRole("button", { name: "Record provenance" }),
      );

      expect(
        await within(dialog).findByText(
          /Provenance delivery is already pending for this selection/u,
        ),
      ).toBeVisible();
      expect(recorder).toHaveBeenCalledOnce();
      expect(recorder.mock.calls[0]?.[0].sourceKind).toBe("direct_entry");
    } finally {
      mounted.unmount();
    }
  }, 30_000);

  it("finalizes a volatile typing capture when provenance storage is retried", async () => {
    const memoryBacking =
      new InMemoryCoworkPasteProvenanceOutboxBackingStore();
    let failWrites = true;
    const failingBacking: CoworkPasteProvenanceOutboxBackingStore = {
      durable: false,
      read: (key) => memoryBacking.read(key),
      mutate: (key, mutation) =>
        failWrites
          ? Promise.reject(new Error("injected outbox write failure"))
          : memoryBacking.mutate(key, mutation),
    };
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `retry-volatile-direct-entry-${String(Date.now())}`,
      failingBacking,
    );
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>();
    const pendingChanges: boolean[] = [];
    const mounted = await mountPasteEditor(recorder, {
      outbox,
      activeLens: "neutral",
      provenanceProvider: provenanceProvider(),
      onInputProvenancePendingChange: (pending) =>
        pendingChanges.push(pending),
    });

    try {
      act(() => mounted.editor.commands.insertContentAt(1, "Recovered typing"));
      mounted.setActiveLens("provenance");
      const retry = await screen.findByRole("button", {
        name: "Retry provenance storage",
      });

      failWrites = false;
      await userEvent.click(retry);

      await waitFor(() => expect(recorder).toHaveBeenCalledOnce());
      await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
      await waitFor(() =>
        expect(pendingChanges[pendingChanges.length - 1]).toBe(false),
      );
      expect(
        screen.queryByRole("button", { name: "Retry provenance storage" }),
      ).toBeNull();
    } finally {
      mounted.unmount();
    }
  }, 30_000);

  it("drains a recovered typing capture queued behind an in-flight finalizer", async () => {
    const memoryBacking =
      new InMemoryCoworkPasteProvenanceOutboxBackingStore();
    let failWrites = false;
    const backing: CoworkPasteProvenanceOutboxBackingStore = {
      durable: false,
      read: (key) => memoryBacking.read(key),
      mutate: (key, mutation) =>
        failWrites
          ? Promise.reject(new Error("injected outbox write failure"))
          : memoryBacking.mutate(key, mutation),
    };
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `racing-volatile-direct-entry-${String(Date.now())}`,
      backing,
    );
    let blockPush = false;
    let releasePush!: () => void;
    const pushGate = new Promise<void>((resolve) => {
      releasePush = resolve;
    });
    let signalBlockedPush!: () => void;
    const blockedPush = new Promise<void>((resolve) => {
      signalBlockedPush = resolve;
    });
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>();
    const mounted = await mountPasteEditor(recorder, {
      outbox,
      activeLens: "neutral",
      provenanceProvider: provenanceProvider(),
      beforePush: async () => {
        if (!blockPush) return;
        signalBlockedPush();
        await pushGate;
      },
    });

    try {
      act(() => mounted.editor.commands.insertContentAt(1, "First burst "));
      await waitFor(async () =>
        expect(await outbox.list()).toEqual([
          expect.objectContaining({
            sourceKind: "direct_entry",
            status: "capturing",
          }),
        ]),
      );

      blockPush = true;
      mounted.setActiveLens("provenance");
      await blockedPush;

      failWrites = true;
      act(() => mounted.editor.commands.insertContentAt(13, "Second burst "));
      const retry = await screen.findByRole("button", {
        name: "Retry provenance storage",
      });

      failWrites = false;
      await userEvent.click(retry);
      await waitFor(async () =>
        expect(
          (await outbox.list()).filter(
            (entry) => entry.status === "capturing",
          ),
        ).toHaveLength(2),
      );
      releasePush();

      await waitFor(() => expect(recorder).toHaveBeenCalledTimes(2), {
        timeout: 10_000,
      });
      await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
      expect(
        screen.queryByRole("button", { name: "Retry provenance storage" }),
      ).toBeNull();
    } finally {
      releasePush();
      mounted.unmount();
    }
  }, 30_000);

  it("retries independent ready provenance while an open capture stays stuck", async () => {
    const memoryBacking =
      new InMemoryCoworkPasteProvenanceOutboxBackingStore();
    let failWrites = false;
    const backing: CoworkPasteProvenanceOutboxBackingStore = {
      durable: false,
      read: (key) => memoryBacking.read(key),
      mutate: (key, mutation) =>
        failWrites
          ? Promise.reject(new Error("injected outbox write failure"))
          : memoryBacking.mutate(key, mutation),
    };
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `independent-ready-${String(Date.now())}`,
      backing,
    );
    await outbox.upsertCapture({
      anchor: { exact: "Missing direct entry", prefix: "", suffix: "" },
      idempotencyKey: "stuck-open-capture",
      substantial: false,
      sourceKind: "direct_entry",
      basisKind: "automatic_direct_entry_attribution",
      determination: unknownCoworkProvenanceDetermination(),
      capturedActor: ACTOR,
      capturedAt: new Date().toISOString(),
      passageExcerpt: "Missing direct entry",
      status: "capturing",
    });
    failWrites = true;
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>();
    const mounted = await mountPasteEditor(recorder, {
      outbox,
      activeLens: "neutral",
      provenanceProvider: provenanceProvider(),
    });

    try {
      act(() => mounted.editor.commands.insertContentAt(1, "Volatile typing "));
      const retry = await screen.findByRole("button", {
        name: "Retry provenance storage",
      });
      failWrites = false;
      await outbox.append({
        anchor: { exact: "Before", prefix: "typing ", suffix: " after" },
        idempotencyKey: "unrelated-ready-capture",
        substantial: false,
        sourceKind: "paste",
        basisKind: "automatic_short_text_attribution",
        determination: unknownCoworkProvenanceDetermination(),
        capturedActor: ACTOR,
        capturedAt: new Date().toISOString(),
        passageExcerpt: "Before",
        status: "ready",
      });

      await userEvent.click(retry);

      await waitFor(() =>
        expect(
          recorder.mock.calls.some(
            ([request]) =>
              request.idempotencyKey === "unrelated-ready-capture",
          ),
        ).toBe(true),
      );
      expect(
        await screen.findByRole("button", {
          name: "Retry provenance storage",
        }),
      ).toBeVisible();
      await expect(outbox.list()).resolves.toEqual([
        expect.objectContaining({
          idempotencyKey: "stuck-open-capture",
          status: "capturing",
        }),
      ]);
    } finally {
      mounted.unmount();
    }
  }, 30_000);

  it("does not strand the host pending signal when a staged burst is deleted immediately", async () => {
    const pendingChanges: boolean[] = [];
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>();
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `deleted-staged-direct-entry-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const mounted = await mountPasteEditor(recorder, {
      outbox,
      activeLens: "neutral",
      provenanceProvider: provenanceProvider(),
      onInputProvenancePendingChange: (pending) =>
        pendingChanges.push(pending),
    });

    try {
      act(() => {
        mounted.editor.commands.insertContentAt(1, "X");
        mounted.editor.commands.deleteRange({ from: 1, to: 2 });
      });

      await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
      await act(async () => Promise.resolve());
      expect(pendingChanges[pendingChanges.length - 1]).toBe(false);
      expect(recorder).not.toHaveBeenCalled();
    } finally {
      mounted.unmount();
    }
  });

  it("keeps the frozen capture pending until the refreshed projection matches the complete receipt", async () => {
    const data = emptyProvenanceData();
    const requests: CoworkPasteProvenanceRequest[] = [];
    let receipt: CoworkPasteProvenanceReceipt | undefined;
    let projectionState: "missing" | "misbound" | "exact" = "missing";
    const provider: ProvenanceProvider = {
      load: vi.fn().mockResolvedValue({ state: "ready", data }),
      refresh: vi.fn().mockImplementation(async () => {
        const projected =
          receipt === undefined || projectionState === "missing"
            ? []
            : [
                attestationForReceipt(
                  projectionState === "exact"
                    ? receipt
                    : {
                        ...receipt,
                        documentSpanId: `wrong-${receipt.documentSpanId}`,
                        targetStructuredHeadSha256: "f".repeat(64),
                      },
                ),
              ];
        return {
          state: "ready" as const,
          data: { ...data, history: projected },
        };
      }),
      subscribe: () => () => undefined,
      markReviewed: vi.fn().mockResolvedValue(undefined),
    };
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `pending-receipt-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const mounted = await mountPasteEditor(
      async (request) => {
        requests.push(request);
        receipt ??= testProvenanceReceipt(request);
        return receipt;
      },
      { outbox, activeLens: "neutral", provenanceProvider: provider },
    );

    try {
      act(() => mounted.editor.commands.insertContentAt(1, "Pending receipt"));
      act(() => {
        setCoworkEditorLens(mounted.editor, "provenance");
      });
      mounted.setActiveLens("provenance");

      await screen.findByRole("button", { name: "Retry provenance storage" });
      expect(requests).toHaveLength(1);
      await expect(outbox.list()).resolves.toEqual([
        expect.objectContaining({
          status: "retryable_failure",
          frozenRequest: expect.objectContaining({
            idempotencyKey: requests[0].idempotencyKey,
          }),
        }),
      ]);
      expect(
        mounted.editor.view.dom.querySelector(
          '[data-wb-provenance-record-state="pending"]',
        ),
      ).toHaveTextContent("Pending receipt");

      projectionState = "misbound";
      await userEvent.click(
        screen.getByRole("button", { name: "Retry provenance storage" }),
      );
      await waitFor(() => expect(requests).toHaveLength(2));
      await expect(outbox.list()).resolves.toEqual([
        expect.objectContaining({ status: "retryable_failure" }),
      ]);

      projectionState = "exact";
      await userEvent.click(
        screen.getByRole("button", { name: "Retry provenance storage" }),
      );
      await waitFor(() => expect(requests).toHaveLength(3));
      expect(requests[1]).toEqual(requests[0]);
      expect(requests[2]).toEqual(requests[0]);
      await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
      await waitFor(() =>
        expect(
          mounted.editor.view.dom.querySelector(
            '[data-wb-provenance-record-state="pending"]',
          ),
        ).toBeNull(),
      );
    } finally {
      mounted.unmount();
    }
  }, 30_000);

  it("reconciles a frozen direct-entry receipt when a later provenance snapshot publishes it", async () => {
    const data = emptyProvenanceData();
    const requests: CoworkPasteProvenanceRequest[] = [];
    const snapshotListeners = new Set<() => void>();
    const pendingChanges: boolean[] = [];
    let receipt: CoworkPasteProvenanceReceipt | undefined;
    let receiptProjected = false;
    const provider: ProvenanceProvider = {
      load: vi.fn().mockImplementation(async () => ({
        state: "ready" as const,
        data: {
          ...data,
          history:
            receipt === undefined || !receiptProjected
              ? []
              : [attestationForReceipt(receipt)],
        },
      })),
      refresh: vi.fn().mockImplementation(async () => ({
        state: "ready" as const,
        data: {
          ...data,
          history:
            receipt === undefined || !receiptProjected
              ? []
              : [attestationForReceipt(receipt)],
        },
      })),
      subscribe: (listener) => {
        snapshotListeners.add(listener);
        return () => snapshotListeners.delete(listener);
      },
      markReviewed: vi.fn().mockResolvedValue(undefined),
    };
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `published-receipt-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const mounted = await mountPasteEditor(
      async (request) => {
        requests.push(request);
        receipt ??= testProvenanceReceipt(request);
        return receipt;
      },
      {
        outbox,
        activeLens: "neutral",
        provenanceProvider: provider,
        onInputProvenancePendingChange: (pending) =>
          pendingChanges.push(pending),
      },
    );

    try {
      act(() => mounted.editor.commands.insertContentAt(1, "Published receipt"));
      mounted.setActiveLens("provenance");

      await screen.findByRole("button", { name: "Retry provenance storage" });
      expect(requests).toHaveLength(1);
      await expect(outbox.list()).resolves.toEqual([
        expect.objectContaining({
          sourceKind: "direct_entry",
          status: "retryable_failure",
          frozenRequest: expect.objectContaining({
            idempotencyKey: requests[0]!.idempotencyKey,
          }),
        }),
      ]);

      receiptProjected = true;
      act(() => {
        for (const listener of snapshotListeners) listener();
      });

      await waitFor(() => expect(requests).toHaveLength(2));
      expect(requests[1]).toEqual(requests[0]);
      await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
      await waitFor(() =>
        expect(pendingChanges[pendingChanges.length - 1]).toBe(false),
      );
      expect(
        screen.queryByRole("button", { name: "Retry provenance storage" }),
      ).toBeNull();
    } finally {
      mounted.unmount();
    }
  }, 30_000);

  it("retains pending provenance when the authoritative refresh fails", async () => {
    const data = emptyProvenanceData();
    let receipt: CoworkPasteProvenanceReceipt | undefined;
    let refreshFails = true;
    const provider: ProvenanceProvider = {
      load: vi.fn().mockResolvedValue({ state: "ready", data }),
      refresh: vi.fn().mockImplementation(async () => {
        if (refreshFails) throw new Error("projection unavailable");
        return {
          state: "ready" as const,
          data: {
            ...data,
            history:
              receipt === undefined ? [] : [attestationForReceipt(receipt)],
          },
        };
      }),
      subscribe: () => () => undefined,
      markReviewed: vi.fn().mockResolvedValue(undefined),
    };
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `pending-refresh-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const mounted = await mountPasteEditor(
      async (request) => {
        receipt ??= testProvenanceReceipt(request);
        return receipt;
      },
      { outbox, activeLens: "neutral", provenanceProvider: provider },
    );

    try {
      act(() => mounted.editor.commands.insertContentAt(1, "Refresh pending"));
      act(() => {
        setCoworkEditorLens(mounted.editor, "provenance");
      });
      mounted.setActiveLens("provenance");
      await screen.findByRole("button", { name: "Retry provenance storage" });
      await expect(outbox.list()).resolves.toEqual([
        expect.objectContaining({
          status: "retryable_failure",
          frozenRequest: expect.any(Object),
        }),
      ]);
      expect(
        mounted.editor.view.dom.querySelector(
          '[data-wb-provenance-record-state="pending"]',
        ),
      ).toHaveTextContent("Refresh pending");

      refreshFails = false;
      await userEvent.click(
        screen.getByRole("button", { name: "Retry provenance storage" }),
      );
      await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
    } finally {
      mounted.unmount();
    }
  }, 30_000);

  it("waits for the capture actor, then keeps top-block typing whole across a persistence settlement", async () => {
    const user = userEvent.setup({ delay: 5 });
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `direct-entry-actor-loading-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const requests: CoworkPasteProvenanceRequest[] = [];
    let actorRequests = 0;
    let releaseActor!: () => void;
    const actorGate = new Promise<void>((resolve) => {
      releaseActor = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/local-identity/session/csrf") {
          return new Response(
            JSON.stringify({
              ok: true,
              authenticated: true,
              principal: LOCAL_PRINCIPAL,
              csrf_token: "wbc_delayed_direct_entry_actor",
            }),
            { headers: { "Content-Type": "application/json" } },
          );
        }
        expect(url).toBe("/api/truth/cowork/current-actor");
        actorRequests += 1;
        await actorGate;
        return new Response(JSON.stringify(ACTOR), {
          headers: { "Content-Type": "application/json" },
        });
      }),
    );

    let mounted: MountedPasteEditor | undefined;
    try {
      mounted = await mountPasteEditor(
        async (request) => {
          requests.push(request);
        },
        {
          outbox,
          resolveActorFromServer: true,
          activeLens: "neutral",
          provenanceProvider: provenanceProvider(),
        },
      );
      await waitFor(() => expect(actorRequests).toBe(1));

      // Build the live shape from the reported regression: one empty top
      // paragraph followed by existing text. A structural split introduces no
      // authored text and therefore no provenance capture of its own.
      act(() => {
        mounted!.editor.commands.setTextSelection(1);
        mounted!.editor.commands.splitBlock();
      });
      expect(mounted.editor.state.doc.childCount).toBe(2);

      const editorSurface = screen.getByRole("textbox", {
        name: "Document editor",
      });
      expect(editorSurface).toHaveAttribute("aria-readonly", "true");
      expect(editorSurface).toHaveAttribute("contenteditable", "false");
      releaseActor();
      await waitFor(() => {
        expect(editorSurface).toHaveAttribute("aria-readonly", "false");
        expect(editorSurface).toHaveAttribute("contenteditable", "true");
      });
      await waitFor(() => expect(mounted!.events).toContain("push"), {
        timeout: 5_000,
      });
      const pushesBeforeTyping = mounted.events.filter(
        (event) => event === "push",
      ).length;

      mounted.editor.view.setProps({
        handleScrollToSelection: () => true,
      });
      act(() => {
        mounted!.editor.commands.setTextSelection(1);
      });
      editorSurface.focus();
      await user.type(editorSurface, "The party's r", {
        skipClick: true,
      });
      // Cross a real idle persistence push while focus and selection stay in
      // the same text block. Network settlement must not split an authorship
      // burst.
      await waitFor(
        () =>
          expect(
            mounted!.events.filter((event) => event === "push").length,
          ).toBeGreaterThan(pushesBeforeTyping),
        { timeout: 5_000 },
      );
      await user.type(editorSurface, "ocking, yes", {
        skipClick: true,
      });
      mounted.setActiveLens("provenance");

      await waitFor(() => expect(requests).toHaveLength(1), {
        timeout: 10_000,
      });
      expect(requests[0]).toMatchObject({
        sourceKind: "direct_entry",
        basisKind: "automatic_direct_entry_attribution",
        expectedActorRef: ACTOR.ref,
        expectedActorIdentityStatus: ACTOR.identity_status,
        anchor: {
          exact: "The party's rocking, yes",
          prefix: "",
          suffix: "\nBefore after",
        },
      });
      expect(await outbox.list()).toEqual([]);
    } finally {
      releaseActor();
      mounted?.unmount();
      vi.unstubAllGlobals();
    }
  }, 30_000);

  it("retains a direct-entry request after a non-target 409 and retries the exact frozen request", async () => {
    const user = userEvent.setup();
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `direct-entry-binding-mismatch-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const recorder = vi
      .fn<CoworkPasteProvenanceRecorder>()
      .mockRejectedValueOnce(
        new CoworkHttpError({
          code: "gesture_binding_mismatch",
          message: "The gesture did not bind to this request.",
          retryable: false,
          status: 409,
        }),
      )
      .mockResolvedValueOnce();
    const mounted = await mountPasteEditor(recorder, {
      outbox,
      activeLens: "neutral",
      provenanceProvider: provenanceProvider(),
    });

    act(() => mounted.editor.commands.insertContentAt(1, "Test"));
    mounted.setActiveLens("provenance");

    await waitFor(() => expect(recorder).toHaveBeenCalledOnce(), {
      timeout: 10_000,
    });
    const firstRequest = recorder.mock.calls[0]![0];
    await waitFor(async () => {
      const entries = await outbox.list();
      expect(entries).toHaveLength(1);
      expect(entries[0]).toMatchObject({
        sourceKind: "direct_entry",
        status: "retryable_failure",
        failure: {
          code: "gesture_binding_mismatch",
          kind: "retryable",
        },
      });
      expect(entries[0]?.frozenRequest).toEqual(firstRequest);
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Co-work couldn’t record provenance for recent typing. Your capture is safe; retry provenance storage.",
    );

    await user.click(
      screen.getByRole("button", { name: "Retry provenance storage" }),
    );

    await waitFor(() => expect(recorder).toHaveBeenCalledTimes(2), {
      timeout: 10_000,
    });
    expect(recorder.mock.calls[1]![0]).toEqual(firstRequest);
    await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  }, 30_000);

  it("retains a direct-entry target conflict until an explicit retry re-resolves it", async () => {
    const user = userEvent.setup();
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `direct-entry-target-changed-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const recorder = vi
      .fn<CoworkPasteProvenanceRecorder>()
      .mockRejectedValueOnce(
        new CoworkHttpError({
          code: "provenance_target_changed",
          message: "The document target changed.",
          retryable: true,
          status: 409,
        }),
      )
      .mockResolvedValueOnce();
    const mounted = await mountPasteEditor(recorder, {
      outbox,
      activeLens: "neutral",
      provenanceProvider: provenanceProvider(),
    });

    act(() => mounted.editor.commands.insertContentAt(1, "Test"));
    mounted.setActiveLens("provenance");

    await waitFor(() => expect(recorder).toHaveBeenCalledOnce(), {
      timeout: 10_000,
    });
    const rejectedRequest = recorder.mock.calls[0]![0];
    await waitFor(async () => {
      const entries = await outbox.list();
      expect(entries).toHaveLength(1);
      expect(entries[0]).toMatchObject({
        sourceKind: "direct_entry",
        status: "stale_target",
        failure: {
          code: "provenance_target_changed",
          kind: "stale_target",
        },
      });
      expect(entries[0]?.frozenRequest).toEqual(rejectedRequest);
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Co-work couldn’t record provenance for recent typing. Your capture is safe; retry provenance storage.",
    );

    await user.click(
      screen.getByRole("button", { name: "Retry provenance storage" }),
    );

    await waitFor(() => expect(recorder).toHaveBeenCalledTimes(2), {
      timeout: 10_000,
    });
    const retriedRequest = recorder.mock.calls[1]![0];
    expect(retriedRequest.idempotencyKey).not.toBe(
      rejectedRequest.idempotencyKey,
    );
    expect(retriedRequest).toMatchObject({
      sourceKind: rejectedRequest.sourceKind,
      basisKind: rejectedRequest.basisKind,
      expectedActorRef: rejectedRequest.expectedActorRef,
      expectedActorIdentityStatus: rejectedRequest.expectedActorIdentityStatus,
      anchor: rejectedRequest.anchor,
      attestation: rejectedRequest.attestation,
    });
    await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  }, 30_000);

  it("keeps actorless typing durable and records it explicitly after identity recovery", async () => {
    const user = userEvent.setup();
    const requests: CoworkPasteProvenanceRequest[] = [];
    const pendingChanges: boolean[] = [];
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `actorless-direct-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const documentId = `actorless-direct-document-${String(Date.now())}`;
    let sessionAttempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/truth/cowork/current-actor") {
          return new Response(JSON.stringify(ACTOR), {
            headers: { "Content-Type": "application/json" },
          });
        }
        expect(url).toBe("/api/local-identity/session/csrf");
        sessionAttempts += 1;
        if (sessionAttempts > 1) {
          return new Response(
            JSON.stringify({
              ok: true,
              authenticated: true,
              principal: LOCAL_PRINCIPAL,
              csrf_token: "wbc_recovered_direct_entry",
            }),
            { headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({
            ok: true,
            authenticated: false,
            human_authority_available: false,
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }),
    );

    let mounted: MountedPasteEditor | undefined;
    try {
      mounted = await mountPasteEditor(
        async (request) => {
          requests.push(request);
        },
        {
          outbox,
          documentId,
          resolveActorFromServer: true,
          activeLens: "neutral",
          provenanceProvider: provenanceProvider(),
          onInputProvenancePendingChange: (pending) =>
            pendingChanges.push(pending),
        },
      );
      await waitFor(() => expect(sessionAttempts).toBe(1));

      act(() => mounted!.editor.commands.insertContentAt(1, "Test"));
      mounted.setActiveLens("provenance");

      let pendingKey = "";
      await waitFor(async () => {
        const pending = (await outbox.list())[0];
        expect(pending).toMatchObject({
          anchor: { exact: "Test", prefix: "", suffix: "Before after" },
          sourceKind: "direct_entry",
          basisKind: "automatic_direct_entry_attribution",
          status: "capturing",
        });
        pendingKey = pending!.idempotencyKey;
      });
      expect(pendingChanges[pendingChanges.length - 1]).toBe(true);
      expect(requests).toEqual([]);
      await act(async () => {
        await refreshLocalIdentity();
      });
      await waitFor(() => expect(sessionAttempts).toBeGreaterThanOrEqual(2));
      await waitFor(async () => {
        expect((await outbox.list())[0]).toMatchObject({
          anchor: { exact: "Test", prefix: "", suffix: "Before after" },
          sourceKind: "legacy",
          basisKind: "user_attestation",
          status: "awaiting_determination",
          requiresExplicitDetermination: true,
          failure: { code: "provenance_actor_unavailable_at_capture" },
        });
      });
      await screen.findByRole("dialog", {
        name: "Recent typing needs attribution",
      });
      expect(screen.getByLabelText("Recent passage")).toHaveTextContent(
        "Test",
      );
      expect(pendingChanges[pendingChanges.length - 1]).toBe(false);
      expect(requests).toEqual([]);
      expect(await outbox.list()).toHaveLength(1);

      await user.click(screen.getByRole("button", { name: /Authorship/i }));
      await user.click(screen.getByRole("option", { name: /^Human-written/ }));
      await user.click(
        screen.getByRole("button", { name: "Confirm attribution" }),
      );

      await waitFor(() => expect(requests).toHaveLength(1), {
        timeout: 10_000,
      });
      expect(requests[0]).toMatchObject({
        sourceKind: "legacy",
        basisKind: "user_attestation",
        expectedActorRef: ACTOR.ref,
        expectedActorIdentityStatus: ACTOR.identity_status,
        anchor: { exact: "Test" },
      });
      expect(requests[0]!.idempotencyKey).toBe(pendingKey);
      await waitFor(() => expect(outbox.list()).resolves.toEqual([]));
    } finally {
      mounted?.unmount();
      vi.unstubAllGlobals();
    }
  }, 30_000);

  it("keeps dismissed actorless typing recoverable without creating another outbox row", async () => {
    const user = userEvent.setup();
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>().mockResolvedValue();
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `actorless-direct-dismiss-${String(Date.now())}`,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    let sessionAttempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/truth/cowork/current-actor") {
          return new Response(JSON.stringify(ACTOR), {
            headers: { "Content-Type": "application/json" },
          });
        }
        expect(url).toBe("/api/local-identity/session/csrf");
        sessionAttempts += 1;
        return new Response(
          JSON.stringify(
            sessionAttempts === 1
              ? {
                  ok: true,
                  authenticated: false,
                  human_authority_available: false,
                }
              : {
                  ok: true,
                  authenticated: true,
                  principal: LOCAL_PRINCIPAL,
                  csrf_token: "wbc_recovered_dismissed_direct_entry",
                },
          ),
          { headers: { "Content-Type": "application/json" } },
        );
      }),
    );

    let mounted: MountedPasteEditor | undefined;
    try {
      mounted = await mountPasteEditor(recorder, {
        outbox,
        resolveActorFromServer: true,
        activeLens: "neutral",
        provenanceProvider: provenanceProvider(),
      });
      await waitFor(() => expect(sessionAttempts).toBe(1));
      act(() => mounted!.editor.commands.insertContentAt(1, "Recover me"));
      mounted.setActiveLens("provenance");
      await waitFor(async () =>
        expect((await outbox.list())[0]).toMatchObject({
          sourceKind: "direct_entry",
          status: "capturing",
        }),
      );
      const initial = (await outbox.list())[0]!;

      await act(async () => {
        await refreshLocalIdentity();
      });
      await screen.findByRole("dialog", {
        name: "Recent typing needs attribution",
      });
      await user.click(screen.getByRole("button", { name: "Keep for later" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

      const deferred = await outbox.list();
      expect(deferred).toHaveLength(1);
      expect(deferred[0]).toMatchObject({
        id: initial.id,
        idempotencyKey: initial.idempotencyKey,
        sourceKind: "legacy",
        status: "awaiting_determination",
      });
      expect(recorder).not.toHaveBeenCalled();

      await user.click(
        screen.getByRole("button", { name: "Review pending attribution" }),
      );
      await screen.findByRole("dialog", {
        name: "Recent typing needs attribution",
      });
      expect(await outbox.list()).toHaveLength(1);
      expect(recorder).not.toHaveBeenCalled();
    } finally {
      mounted?.unmount();
      vi.unstubAllGlobals();
    }
  }, 30_000);

  it("reconstructs actorless typing recovery from the durable row after a fresh mount", async () => {
    const user = userEvent.setup();
    const backing = new InMemoryCoworkPasteProvenanceOutboxBackingStore();
    const outboxKey = `actorless-direct-reload-${String(Date.now())}`;
    const beforeReload = new DurableCoworkPasteProvenanceOutbox(
      outboxKey,
      backing,
    );
    const capturing = await beforeReload.upsertCapture({
      anchor: { exact: "Before", prefix: "", suffix: " after" },
      idempotencyKey: "actorless-direct-reload-key",
      substantial: false,
      sourceKind: "direct_entry",
      basisKind: "automatic_direct_entry_attribution",
      determination: unknownCoworkProvenanceDetermination(),
      capturedAt: "2026-08-21T12:00:00.000Z",
      passageExcerpt: "Before",
      status: "capturing",
    });
    const deferred = await beforeReload.deferDirectEntry(
      capturing.id,
      capturing.idempotencyKey,
      unknownCoworkProvenanceDetermination(),
      {
        code: "provenance_actor_unavailable_at_capture",
        message: "No enrolled local actor was available when this text was entered.",
        kind: "terminal",
      },
    );

    // Ctrl+R constructs both the component and the outbox adapter again. Only
    // the persisted row/failure marker crosses this seam.
    const afterReload = new DurableCoworkPasteProvenanceOutbox(
      outboxKey,
      backing,
    );
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>().mockResolvedValue();
    let mounted: MountedPasteEditor | undefined = await mountPasteEditor(
      recorder,
      {
        outbox: afterReload,
        activeLens: "provenance",
        provenanceProvider: provenanceProvider(),
      },
    );
    let remounted: MountedPasteEditor | undefined;

    try {
      await screen.findByRole("dialog", {
        name: "Recent typing needs attribution",
      });
      expect(screen.getByLabelText("Recent passage")).toHaveTextContent(
        "Before",
      );
      const recovered = await afterReload.list();
      expect(recovered).toHaveLength(1);
      expect(recovered[0]).toMatchObject({
        id: deferred.id,
        idempotencyKey: deferred.idempotencyKey,
        sourceKind: "legacy",
        status: "awaiting_determination",
      });
      expect(recorder).not.toHaveBeenCalled();

      // The controlled form persists as the user chooses. Reload in this
      // intermediate state, before Confirm, to prove the direct-entry lineage
      // marker survives independently of component-local refs.
      await user.click(screen.getByRole("button", { name: /Authorship/i }));
      await user.click(screen.getByRole("option", { name: /^Human-written/ }));
      await waitFor(async () =>
        expect((await afterReload.list())[0]).toMatchObject({
          id: deferred.id,
          idempotencyKey: deferred.idempotencyKey,
          determination: { authorship: { kind: "human" } },
          failure: { code: "provenance_actor_unavailable_at_capture" },
        }),
      );

      const server = mounted.server;
      mounted.unmount();
      mounted.document.destroy();
      mounted = undefined;
      const afterFormReload = new DurableCoworkPasteProvenanceOutbox(
        outboxKey,
        backing,
      );
      remounted = await mountPasteEditor(recorder, {
        outbox: afterFormReload,
        server,
        document: new Y.Doc(),
        activeLens: "provenance",
        provenanceProvider: provenanceProvider(),
      });

      await screen.findByRole("dialog", {
        name: "Recent typing needs attribution",
      });
      expect(
        screen.getByRole("button", { name: /Authorship/i }),
      ).toHaveTextContent("Human-written");
      expect(await afterFormReload.list()).toHaveLength(1);
      await user.click(
        screen.getByRole("button", { name: "Confirm attribution" }),
      );

      await waitFor(() => expect(recorder).toHaveBeenCalledOnce(), {
        timeout: 10_000,
      });
      expect(recorder.mock.calls[0]?.[0]).toMatchObject({
        idempotencyKey: deferred.idempotencyKey,
        sourceKind: "legacy",
        basisKind: "user_attestation",
        expectedActorRef: ACTOR.ref,
        anchor: deferred.anchor,
      });
      await waitFor(() => expect(afterFormReload.list()).resolves.toEqual([]));
      expect(recorder).toHaveBeenCalledOnce();
    } finally {
      remounted?.unmount();
      remounted?.document.destroy();
      mounted?.unmount();
      mounted?.document.destroy();
    }
  }, 30_000);

  it("does not resurrect actorless automatic work when recovered typing races demotion", async () => {
    const requests: CoworkPasteProvenanceRequest[] = [];
    const memoryBacking = new InMemoryCoworkPasteProvenanceOutboxBackingStore();
    const intentStage = new InMemoryCoworkPasteProvenanceIntentStage();
    const outboxKey = `actorless-demotion-race-${String(Date.now())}`;
    let releaseDemotion!: () => void;
    let enteredDemotion!: () => void;
    const demotionGate = new Promise<void>((resolve) => {
      releaseDemotion = resolve;
    });
    const demotionEntered = new Promise<void>((resolve) => {
      enteredDemotion = resolve;
    });
    let demotionPaused = false;
    const blockingBacking: CoworkPasteProvenanceOutboxBackingStore = {
      durable: false,
      read: (key) => memoryBacking.read(key),
      async mutate(key, mutation) {
        let demoted = false;
        const result = await memoryBacking.mutate(key, (current) => {
          const next = mutation(current);
          demoted =
            !demotionPaused &&
            current.entries.some(
              (entry) =>
                entry.sourceKind === "direct_entry" &&
                entry.status === "capturing" &&
                next.record.entries.some(
                  (candidate) =>
                    candidate.id === entry.id &&
                    candidate.sourceKind === "legacy" &&
                    candidate.status === "awaiting_determination",
                ),
            );
          return next;
        });
        if (demoted) {
          // Pause after the demotion replacement has passed its guards and
          // committed, but before the outbox operation settles. A subsequent
          // transaction can synchronously journal and enqueue an upsert now.
          demotionPaused = true;
          enteredDemotion();
          await demotionGate;
        }
        return result;
      },
    };
    const durableOutbox = new DurableCoworkPasteProvenanceOutbox(
      outboxKey,
      blockingBacking,
      intentStage,
    );
    let sessionAttempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/truth/cowork/current-actor") {
          return new Response(JSON.stringify(ACTOR), {
            headers: { "Content-Type": "application/json" },
          });
        }
        expect(url).toBe("/api/local-identity/session/csrf");
        sessionAttempts += 1;
        return new Response(
          JSON.stringify(
            sessionAttempts === 1
              ? {
                  ok: true,
                  authenticated: false,
                  human_authority_available: false,
                }
              : {
                  ok: true,
                  authenticated: true,
                  principal: LOCAL_PRINCIPAL,
                  csrf_token: "wbc_actorless_demotion_race",
                },
          ),
          { headers: { "Content-Type": "application/json" } },
        );
      }),
    );

    let mounted: MountedPasteEditor | undefined;
    let reopened: MountedPasteEditor | undefined;
    try {
      mounted = await mountPasteEditor(
        async (request) => {
          requests.push(request);
        },
        {
          outbox: durableOutbox,
          resolveActorFromServer: true,
          activeLens: "neutral",
          provenanceProvider: provenanceProvider(),
        },
      );
      await waitFor(() => expect(sessionAttempts).toBe(1));
      act(() => mounted!.editor.commands.insertContentAt(8, "A"));
      await waitFor(async () => {
        expect((await durableOutbox.list())[0]).toMatchObject({
          anchor: { exact: "A" },
          sourceKind: "direct_entry",
          status: "capturing",
        });
      });

      await act(async () => {
        await refreshLocalIdentity();
      });
      await demotionEntered;

      // The first burst has passed the actor-mismatch pre-check and is now
      // yielding inside durable demotion. This disjoint edit is actor-bound
      // and maps the older burst once more before staging its own key.
      act(() => mounted!.editor.commands.insertContentAt(1, "B"));
      releaseDemotion();

      await waitFor(() => expect(requests).toHaveLength(1), {
        timeout: 10_000,
      });
      expect(requests[0]).toMatchObject({
        sourceKind: "direct_entry",
        basisKind: "automatic_direct_entry_attribution",
        expectedActorRef: ACTOR.ref,
        anchor: { exact: "B" },
      });
      let pending = await durableOutbox.list();
      expect(pending).toHaveLength(1);
      expect(pending[0]).toMatchObject({
        anchor: { exact: "A" },
        sourceKind: "legacy",
        basisKind: "user_attestation",
        status: "awaiting_determination",
        requiresExplicitDetermination: true,
      });
      expect(
        pending.filter(
          (entry) =>
            entry.sourceKind === "direct_entry" || entry.status === "capturing",
        ),
      ).toEqual([]);
      const legacyId = pending[0]!.id;
      const server = mounted.server;
      mounted.unmount();
      mounted = undefined;

      const reopenedOutbox = new DurableCoworkPasteProvenanceOutbox(
        outboxKey,
        memoryBacking,
        intentStage,
      );
      reopened = await mountPasteEditor(
        async (request) => {
          requests.push(request);
        },
        {
          outbox: reopenedOutbox,
          server,
          document: new Y.Doc(),
          activeLens: "provenance",
          provenanceProvider: provenanceProvider(),
        },
      );
      await waitFor(async () => {
        pending = await reopenedOutbox.list();
        expect(pending).toHaveLength(1);
        expect(pending[0]).toMatchObject({
          id: legacyId,
          anchor: { exact: "A" },
          sourceKind: "legacy",
          status: "awaiting_determination",
        });
        expect(
          pending.filter(
            (entry) =>
              entry.sourceKind === "direct_entry" ||
              entry.status === "capturing",
          ),
        ).toEqual([]);
      });
      expect(requests).toHaveLength(1);
    } finally {
      releaseDemotion();
      reopened?.unmount();
      mounted?.unmount();
      vi.unstubAllGlobals();
    }
  }, 30_000);

  it("keeps adjacent direct-entry bursts disjoint when the capture actor changes", async () => {
    const nextActor = {
      kind: "human",
      ref: "next-dashboard-user",
      identity_status: "local_actor_ref",
    } as const;
    const memoryBacking = new InMemoryCoworkPasteProvenanceOutboxBackingStore();
    let blockNextMutation = false;
    let releaseActorRecovery!: () => void;
    let actorRecoveryEntered!: () => void;
    const actorRecoveryGate = new Promise<void>((resolve) => {
      releaseActorRecovery = resolve;
    });
    const enteredActorRecovery = new Promise<void>((resolve) => {
      actorRecoveryEntered = resolve;
    });
    const gatedBacking: CoworkPasteProvenanceOutboxBackingStore = {
      durable: false,
      read: (key) => memoryBacking.read(key),
      async mutate(key, mutation) {
        if (blockNextMutation) {
          blockNextMutation = false;
          actorRecoveryEntered();
          await actorRecoveryGate;
        }
        return memoryBacking.mutate(key, mutation);
      },
    };
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      `adjacent-actor-transition-${String(Date.now())}`,
      gatedBacking,
    );
    const requests: CoworkPasteProvenanceRequest[] = [];
    const mounted = await mountPasteEditor(
      async (request) => {
        requests.push(request);
      },
      {
        outbox,
        provenanceActor: ACTOR,
        activeLens: "neutral",
        provenanceProvider: provenanceProvider(),
      },
    );

    try {
      act(() => mounted.editor.commands.insertContentAt(1, "A"));
      await waitFor(async () =>
        expect((await outbox.list())[0]).toMatchObject({
          sourceKind: "direct_entry",
          capturedActor: ACTOR,
          anchor: { exact: "A" },
          status: "capturing",
        }),
      );

      // Hold the new-actor recovery read before it can demote the old burst.
      // The adjacent transaction now deterministically exercises the mapping
      // boundary between two capture-time actors.
      blockNextMutation = true;
      mounted.setProvenanceActor(nextActor);
      await enteredActorRecovery;
      act(() => mounted.editor.commands.insertContentAt(2, "B"));
      releaseActorRecovery();
      mounted.setActiveLens("provenance");

      await waitFor(() => expect(requests).toHaveLength(1), {
        timeout: 10_000,
      });
      expect(requests[0]).toMatchObject({
        sourceKind: "direct_entry",
        expectedActorRef: nextActor.ref,
        expectedActorIdentityStatus: nextActor.identity_status,
        anchor: { exact: "B" },
      });
      await waitFor(async () => {
        const pending = await outbox.list();
        expect(pending).toHaveLength(1);
        expect(pending[0]).toMatchObject({
          sourceKind: "legacy",
          anchor: { exact: "A" },
          status: "awaiting_determination",
          requiresExplicitDetermination: true,
        });
      });
    } finally {
      releaseActorRecovery();
      mounted.unmount();
    }
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

  it("reassociates an ownerless legacy actor-change row after reload", async () => {
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
    await waitFor(async () => {
      expect((await outbox.list())[0]).toMatchObject({
        sourceKind: "legacy",
        basisKind: "user_attestation",
        status: "awaiting_determination",
        requiresExplicitDetermination: true,
      });
    });

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
    let releaseRecoveredActor!: () => void;
    const recoveredActorGate = new Promise<void>((resolve) => {
      releaseRecoveredActor = resolve;
    });
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
        await recoveredActorGate;
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
      expect(editorSurface).toHaveAttribute("aria-readonly", "true");
      expect(editorSurface).toHaveAttribute("contenteditable", "false");
      releaseRecoveredActor();
      expect(
        await screen.findByRole("dialog", {
          name: "Where did this text come from?",
        }),
      ).toBeVisible();
      expect(editorSurface).toHaveAttribute("aria-readonly", "false");
      expect(screen.queryByText(/Reconnect Work Buddy to edit/i)).toBeNull();
    } finally {
      releaseRecoveredActor();
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

  it("clears an expired actor and re-resolves it after trusted session recovery", async () => {
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>().mockResolvedValue();
    const recoveredActor = {
      kind: "human",
      ref: "recovered-dashboard-user",
      identity_status: "local_actor_ref",
    } as const;
    let sessionAttempts = 0;
    let actorAttempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/local-identity/session/csrf") {
          sessionAttempts += 1;
          if (sessionAttempts === 2) {
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
              principal: {
                ...LOCAL_PRINCIPAL,
                session_expires_at: 199,
              },
              csrf_token:
                sessionAttempts === 1 ? "wbc_initial" : "wbc_recovered",
            }),
            { headers: { "Content-Type": "application/json" } },
          );
        }
        expect(url).toBe("/api/truth/cowork/current-actor");
        actorAttempts += 1;
        return new Response(
          JSON.stringify(actorAttempts === 1 ? ACTOR : recoveredActor),
          { headers: { "Content-Type": "application/json" } },
        );
      }),
    );

    let mounted: MountedPasteEditor | undefined;
    try {
      mounted = await mountPasteEditor(recorder, {
        resolveActorFromServer: true,
        activeLens: "provenance",
        provenanceProvider: provenanceProvider(),
      });
      await waitFor(() => expect(actorAttempts).toBe(1));
      act(() => {
        mounted!.editor.commands.setTextSelection({ from: 1, to: 7 });
      });
      expect(
        await screen.findByRole("button", { name: "Record provenance" }),
      ).toBeVisible();

      await act(async () => {
        await refreshLocalIdentity();
      });
      await waitFor(() =>
        expect(
          screen.queryByRole("button", { name: "Record provenance" }),
        ).toBeNull(),
      );

      await act(async () => {
        await refreshLocalIdentity();
      });
      await waitFor(() => expect(actorAttempts).toBe(2));
      expect(
        await screen.findByRole("button", { name: "Record provenance" }),
      ).toBeVisible();
      expect(sessionAttempts).toBeGreaterThanOrEqual(4);
    } finally {
      mounted?.unmount();
      vi.unstubAllGlobals();
    }
  }, 20_000);
});

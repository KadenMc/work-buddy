import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Editor } from "@tiptap/core";
import { DOMParser as ProseMirrorDOMParser } from "@tiptap/pm/model";
import { describe, expect, it, vi } from "vitest";
import * as Y from "yjs";

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
  unknownCoworkProvenanceDetermination,
  type CoworkPasteProvenanceCapture,
  type CoworkPasteProvenanceIntentStage,
  type CoworkPasteProvenanceOutbox,
  type CoworkPasteProvenanceRecorder,
  type CoworkPasteProvenanceRequest,
  type CoworkProvenanceActorIdentity,
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
}

let nextDocument = 0;
const ACTOR = {
  kind: "human",
  ref: "dashboard-user",
  identity_status: "local_actor_ref",
} as const;

const mountPasteEditor = async (
  recorder: CoworkPasteProvenanceRecorder,
  options: {
    readonly outbox?: CoworkPasteProvenanceOutbox;
    readonly documentId?: string;
    readonly provenanceActor?: CoworkProvenanceActorIdentity;
    readonly resolveActorFromServer?: boolean;
    readonly onPushStart?: () => void;
  } = {},
): Promise<MountedPasteEditor> => {
  const initialized = await bootstrapCoworkYdoc(
    new TextEncoder().encode("Before after"),
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

  const events: string[] = [];
  const transport: CoworkYdocTransport = {
    pull: (request) => server.pull(request),
    push: async (request: CoworkYdocPushRequest) => {
      events.push("push");
      options.onPushStart?.();
      return server.push(request);
    },
  };
  const editorRef: { current: Editor | null } = { current: null };
  const document = new Y.Doc();
  nextDocument += 1;
  const rendered = render(
    <CoworkBridgeEditor
      document={document}
      transport={transport}
      seedMarkdown=""
      documentId={
        options.documentId ??
        `paste-provenance-${String(nextDocument)}-${String(Date.now())}`
      }
      storeId="paste-provenance-store"
      provenanceActor={
        options.resolveActorFromServer
          ? undefined
          : options.provenanceActor ?? ACTOR
      }
      pasteProvenanceOutbox={options.outbox}
      onRecordPasteProvenance={async (request) => {
        events.push("record");
        await recorder(request);
      }}
      onReady={({ editor }) => {
        editorRef.current = editor;
      }}
    />,
  );
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
  };
};

const dispatchSimplePaste = (editor: Editor): void => {
  act(() => {
    editor.view.dispatch(
      editor.state.tr
        .insertText("pasted ", 8)
        .setMeta("uiEvent", "paste"),
    );
  });
};

const dispatchSubstantialPaste = (
  editor: Editor,
  position = 8,
): void => {
  const host = document.createElement("div");
  host.innerHTML = "<p>First pasted paragraph.</p><p>Second pasted paragraph.</p>";
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
  it("journals provenance before the paste can enter durable Yjs state", async () => {
    const order: string[] = [];
    const intentStage =
      new InMemoryCoworkPasteProvenanceIntentStage();
    const observingStage: CoworkPasteProvenanceIntentStage = {
      list: (key) => intentStage.list(key),
      put: (key: string, capture: CoworkPasteProvenanceCapture) => {
        order.push("journal");
        intentStage.put(key, capture);
      },
      remove: (key, idempotencyKey) =>
        intentStage.remove(key, idempotencyKey),
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
          .insertText(
            "x".repeat(COWORK_PROVENANCE_EXACT_MAX_CHARS + 1),
            8,
          )
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
          liveEditor!.view.dispatch(
            liveEditor!.state.tr.delete(8, 15),
          );
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
    expect(mounted.events).toEqual(["push", "record"]);
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
    const backing =
      new InMemoryCoworkPasteProvenanceOutboxBackingStore();
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
    const firstKey = coworkPasteProvenanceOutboxKey(
      "store",
      "document",
    );
    const secondKey = coworkPasteProvenanceOutboxKey(
      "store",
      "document",
    );

    expect(secondKey).toBe(firstKey);
    expect(coworkPasteProvenanceOutboxKey("store", "other")).not.toBe(
      firstKey,
    );
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
      await waitFor(() => expect(actorFetch).toHaveBeenCalledOnce());
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
      await user.click(
        screen.getByRole("option", { name: /^Human-written/ }),
      );
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
      expect(
        screen.queryByText(/paste attribution is waiting/i),
      ).toBeNull(),
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

  it("hydrates read-only when actor lookup fails and recovers on direct retry", async () => {
    const user = userEvent.setup();
    const recorder = vi.fn<CoworkPasteProvenanceRecorder>().mockResolvedValue();
    let actorAttempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
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
      });
      const editorSurface = screen.getByRole("textbox", {
        name: "Document editor",
      });
      expect(await screen.findByText("Editing is paused.")).toBeVisible();
      expect(editorSurface).toHaveAttribute("aria-readonly", "true");

      await user.click(
        screen.getByRole("button", { name: "Retry identity" }),
      );
      await waitFor(() =>
        expect(editorSurface).toHaveAttribute("aria-readonly", "false"),
      );
      expect(screen.queryByText("Editing is paused.")).toBeNull();

      dispatchSimplePaste(mounted.editor);
      await waitFor(() => expect(recorder).toHaveBeenCalledOnce());
      expect(actorAttempts).toBe(2);
    } finally {
      mounted?.unmount();
      vi.unstubAllGlobals();
    }
  }, 20_000);
});

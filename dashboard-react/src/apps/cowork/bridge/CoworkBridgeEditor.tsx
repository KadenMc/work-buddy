import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import type { Editor } from "@tiptap/core";
import * as Y from "yjs";

import {
  CoworkYdocPersistence,
  type CoworkSyncStatus,
} from "../persistence/CoworkYdocPersistence";
import { DurableCoworkYdocOutbox } from "../persistence/CoworkYdocOutbox";
import type { CoworkYdocTransport } from "../persistence/transport";
import {
  buildEditorExtensions,
  stopCapturingLoadTimeIds,
} from "../editor/extensions";
import { importCoworkMarkdown } from "../editor/markdownImport";
import type { WbTrackedChangesAdapter } from "../suggestions/types";
import type { CoworkApiError, CoworkDriftState } from "../contracts";
import { isLocalHumanOrigin } from "../editor/applyOrigin";
import { serializeCoworkEditorMarkdown } from "../editor/serializeCoworkMarkdown";
import { sha256Hex } from "../persistence/hashing";
import { asCoworkApiError } from "../providers/errors";
import {
  HttpCoworkMaterializationClient,
} from "../materialization/HttpCoworkMaterializationClient";
import type {
  CoworkMaterializationController,
  CoworkMaterializationState,
  CoworkMaterializeReceipt,
  CoworkMaterializeRequest,
} from "../materialization/contracts";
import type { FeedbackCapture } from "../chat";
import {
  CoworkFeedbackAffordance,
  type CoworkFeedbackTransport,
} from "../feedback";
import {
  prepareCoworkSittingDocument,
  type CoworkSittingWorkspace,
} from "./sittingWorkspace";
import "./styles.css";
import { Button, InlineAlert } from "../../../ui";
import {
  coworkSessionDurability,
  createCoworkSessionDurabilityController,
  registeredSessionDurabilityKey,
} from "../session/CoworkSessionDurability";

/** What the host reports up once the editor is mounted and the adapter attached. */
export interface CoworkEditorReadyContext {
  readonly editor: Editor;
  /** The ProseMirror DOM root, the coordinate source for the anchor-rect measurements. */
  readonly dom: HTMLElement;
}

export interface CoworkBridgeEditorProps {
  /** The shared local Y.Doc the adapter is bound to (apply-origin tagging, section 1.4). */
  readonly document: Y.Doc;
  /** The tracked-change adapter, attached to the editor here and shared with the bridge. */
  readonly adapter: WbTrackedChangesAdapter;
  /** The Yjs transport (HttpCoworkYdocTransport live, in-memory in tests). */
  readonly transport: CoworkYdocTransport;
  /** Markdown seeded into a brand-new document exactly once, on an empty fragment. */
  readonly seedMarkdown: string;
  /** Fired once the editor is mounted and the adapter attached. */
  readonly onReady?: (context: CoworkEditorReadyContext) => void;
  /** Fired when the editor is about to unmount, so the bridge can drop its editor refs. */
  readonly onTeardown?: () => void;
  /** The cowork doc id, for the R9 feedback affordance. */
  readonly documentId?: string;
  /** The scope store id the R9 feedback route takes. */
  readonly storeId?: string;
  /**
   * When supplied, the selection-triggered Give-feedback affordance mounts over
   * the editor and reports a successful R9 capture here. Omitted (demo, tests)
   * keeps the affordance off entirely.
   */
  readonly onFeedbackCaptured?: (capture: FeedbackCapture) => void;
  /** Injectable R9 transport for the affordance, else the same-origin HTTP one. */
  readonly feedbackTransport?: CoworkFeedbackTransport;
  readonly readOnly?: boolean;
  readonly onSyncStatus?: (status: CoworkSyncStatus) => void;
  readonly currentFileSha256?: string | null;
  readonly initialDriftState?: CoworkDriftState;
  readonly canMaterialize?: boolean;
  readonly materializationClient?: HttpCoworkMaterializationClient;
  readonly onMaterializationState?: (state: CoworkMaterializationState) => void;
  readonly onMaterializationController?: (
    controller: CoworkMaterializationController | null,
  ) => void;
  readonly onMaterialized?: (receipt: CoworkMaterializeReceipt) => void;
  readonly onSittingWorkspace?: (workspace: CoworkSittingWorkspace | null) => void;
  readonly onSittingProjectionAdopted?: () => void;
}

interface MountedProps extends CoworkBridgeEditorProps {
  readonly persistence: CoworkYdocPersistence;
  readonly seedWhenEmpty: boolean;
}

/**
 * The mounted live editor. It follows the same load-order the demo pane proved (SP-2): the
 * Y.Doc is hydrated from the transport before mount (the parent gates on that), the editor
 * binds to it, persistence starts pushing local human edits, a brand-new document is seeded
 * once, and the load-time id mint is fenced out of the undo stack. On top of the demo pane it
 * attaches the tracked-change adapter to the editor and reports the ready context up, so the
 * bridge can ingest proposals and measure anchor geometry.
 */
function MountedBridgeEditor({
  document,
  adapter,
  persistence,
  seedMarkdown,
  seedWhenEmpty,
  onReady,
  onTeardown,
  documentId,
  storeId,
  onFeedbackCaptured,
  feedbackTransport,
  readOnly = false,
}: MountedProps) {
  const extensions = useMemo(() => buildEditorExtensions(document), [document]);
  const seedContent = useMemo(
    () => importCoworkMarkdown(seedMarkdown).doc,
    [seedMarkdown],
  );
  const boundRef = useRef(false);
  const onReadyRef = useRef(onReady);
  const onTeardownRef = useRef(onTeardown);
  onReadyRef.current = onReady;
  onTeardownRef.current = onTeardown;

  const editor = useEditor(
    {
      extensions,
      immediatelyRender: false,
      editable: !readOnly,
      editorProps: {
        attributes: {
          class: "wb-cowork-editor__surface",
          "aria-label": "Document editor",
          role: "textbox",
          "aria-multiline": "true",
          "aria-readonly": readOnly ? "true" : "false",
        },
      },
    },
    [extensions],
  );

  // Catalog refreshes can revoke editing without changing the keyed document session.
  // Update the actual ProseMirror view during layout so no writable frame is painted.
  useLayoutEffect(() => {
    if (editor === null) return;
    editor.setEditable(!readOnly);
    editor.view.dom.setAttribute("aria-readonly", readOnly ? "true" : "false");
  }, [editor, readOnly]);

  useEffect(() => {
    if (editor === null || boundRef.current) return;
    boundRef.current = true;
    // Attach the adapter before seeding, so the tracked-change layer is ready the moment the
    // first proposal is ingested. Persistence starts before seeding so a brand-new document's
    // seed is pushed through R4 as its first human-origin update (SP-2 load-order).
    adapter.attach(editor);
    persistence.start();
    if (seedWhenEmpty) {
      editor.commands.setContent(seedContent);
    }
    stopCapturingLoadTimeIds(editor);
    onReadyRef.current?.({ editor, dom: editor.view.dom as HTMLElement });
  }, [editor, adapter, persistence, seedContent, seedWhenEmpty]);

  useEffect(() => {
    return () => {
      onTeardownRef.current?.();
      adapter.detach();
    };
  }, [adapter]);

  return (
    <>
      <EditorContent editor={editor} className="wb-cowork-editor__content" />
      {editor !== null && !readOnly && onFeedbackCaptured !== undefined && documentId !== undefined ? (
        <CoworkFeedbackAffordance
          editor={editor}
          documentId={documentId}
          storeId={storeId}
          onCaptured={onFeedbackCaptured}
          transport={feedbackTransport}
        />
      ) : null}
    </>
  );
}

/**
 * The live editor region of the Co-work surface. It owns its Yjs persistence controller,
 * hydrates the shared Y.Doc from the transport BEFORE mounting the editor, and gates the
 * mount by conditionally rendering (never useEditor(null), F5.4). The Y.Doc and the adapter
 * are passed in by the bridge so the review provider's submit path shares the same adapter.
 */
export function CoworkBridgeEditor(props: CoworkBridgeEditorProps) {
  const [persistence] = useState(
    () =>
      new CoworkYdocPersistence(props.document, props.transport, {
        ...(props.documentId === undefined || props.storeId === undefined
          ? {}
          : {
              outbox: new DurableCoworkYdocOutbox(
                `${props.storeId}:${props.documentId}`,
              ),
              requireSnapshot: true,
            }),
        readOnly: props.readOnly ?? false,
      }),
  );
  const [hydration, setHydration] = useState<{ readonly wasEmpty: boolean }>();
  const [hydrationError, setHydrationError] = useState<string>();
  const [attempt, setAttempt] = useState(0);
  const editorRef = useRef<Editor | null>(null);
  const editGeneration = useRef(0);
  const expectedFileSha256 = useRef(props.currentFileSha256 ?? null);
  const saveInFlight = useRef<Promise<void> | null>(null);
  const retryAttempt = useRef<{
    readonly generation: number;
    readonly request: CoworkMaterializeRequest;
  } | null>(null);
  const [materializationClient] = useState(
    () => props.materializationClient ?? new HttpCoworkMaterializationClient(),
  );
  const [materializationState, setMaterializationState] =
    useState<CoworkMaterializationState>({ kind: "checking" });
  const materializationStateRef = useRef(materializationState);
  const readOnlyRef = useRef(props.readOnly ?? false);
  readOnlyRef.current = props.readOnly ?? false;

  const durabilityController = useMemo(
    () =>
      createCoworkSessionDurabilityController({
        pause: () => {
          editorRef.current?.setEditable(false);
          persistence.stop();
        },
        resume: () => {
          if (readOnlyRef.current) return;
          persistence.start();
          editorRef.current?.setEditable(true);
        },
        ensureDeviceDurability: () => persistence.ensureDeviceDurability(),
      }),
    [persistence],
  );

  useEffect(() => {
    if (props.documentId === undefined || props.storeId === undefined) return;
    return coworkSessionDurability.register(
      registeredSessionDurabilityKey(props.storeId, props.documentId),
      durabilityController,
    );
  }, [durabilityController, props.documentId, props.storeId]);

  useLayoutEffect(() => {
    const readOnly = props.readOnly ?? false;
    if (readOnly) {
      editorRef.current?.setEditable(false);
      persistence.stop();
    }
    void persistence.setReadOnly(readOnly).then(
      () => {
        if (!readOnly && !readOnlyRef.current) editorRef.current?.setEditable(true);
      },
      () => undefined,
    );
  }, [persistence, props.readOnly]);

  const publishMaterializationState = useCallback(
    (state: CoworkMaterializationState): void => {
      materializationStateRef.current = state;
      setMaterializationState(state);
      props.onMaterializationState?.(state);
    },
    [props.onMaterializationState],
  );

  const initialProjectionConflict = useCallback((): CoworkApiError | null => {
    if (props.initialDriftState === "drifted") {
      return {
        code: "stale_file",
        message:
          "Markdown changed outside Co-work. Your Co-work edits are safe; review or re-import the file before saving.",
        retryable: false,
      };
    }
    if (props.initialDriftState === "missing") {
      return {
        code: "missing_file",
        message:
          "The Markdown file is missing. Your Co-work edits are safe; restore or re-import the file before saving.",
        retryable: false,
      };
    }
    return null;
  }, [props.initialDriftState]);

  const checkProjection = useCallback(
    async (editor: Editor): Promise<void> => {
      if (props.readOnly === true || props.canMaterialize === false) {
        publishMaterializationState({
          kind: "read_only",
          reason: "This document cannot publish Markdown from this session.",
        });
        return;
      }
      const conflict = initialProjectionConflict();
      if (conflict !== null) {
        publishMaterializationState({
          kind: "conflict",
          fileSha256: expectedFileSha256.current,
          error: conflict,
          canRetry: false,
        });
        return;
      }
      const fileSha256 = expectedFileSha256.current;
      if (fileSha256 === null || fileSha256.length === 0) {
        publishMaterializationState({
          kind: "error",
          fileSha256: null,
          error: {
            code: "file_hash_unavailable",
            message: "Co-work could not verify the current Markdown file, so Save is disabled.",
            retryable: false,
          },
          canRetry: false,
        });
        return;
      }
      const checkedGeneration = editGeneration.current;
      const rendered = serializeCoworkEditorMarkdown(editor, props.document);
      const renderedSha256 = await sha256Hex(new TextEncoder().encode(rendered));
      publishMaterializationState({
        kind:
          editGeneration.current === checkedGeneration && renderedSha256 === fileSha256
            ? "up_to_date"
            : "unsaved",
        fileSha256,
      });
    },
    [
      initialProjectionConflict,
      props.canMaterialize,
      props.document,
      props.readOnly,
      publishMaterializationState,
    ],
  );

  const retryableConflict = (error: CoworkApiError): boolean =>
    error.retryable ||
    error.code === "snapshot_mismatch" ||
    error.code === "stale_structured_head" ||
    error.code === "update_tail_present";

  const settleMaterialize = useCallback(
    async (
      request: CoworkMaterializeRequest,
      capturedGeneration: number,
    ): Promise<void> => {
      try {
        const receipt = await materializationClient.materialize(
          props.storeId ?? "",
          props.documentId ?? "",
          request,
        );
        retryAttempt.current = null;
        expectedFileSha256.current = receipt.newFileSha256;
        publishMaterializationState(
          editGeneration.current === capturedGeneration
            ? { kind: "up_to_date", fileSha256: receipt.newFileSha256 }
            : { kind: "unsaved", fileSha256: receipt.newFileSha256 },
        );
        props.onMaterialized?.(receipt);
      } catch (saveError) {
        const error = asCoworkApiError(saveError);
        const isConflict = error.status === 409 || [
          "snapshot_mismatch",
          "stale_structured_head",
          "update_tail_present",
          "stale_file",
          "missing_file",
          "open_flags_block_save",
          "external_write_race",
          "recovery_required",
        ].includes(error.code);
        const canRetry = isConflict ? retryableConflict(error) : error.retryable;
        const canReplayExactRequest =
          !isConflict &&
          editGeneration.current === capturedGeneration &&
          (error.code === "network_error" ||
            (error.status !== undefined && error.status >= 500 && error.retryable));
        if (!canReplayExactRequest) {
          retryAttempt.current = null;
        }
        publishMaterializationState({
          kind: isConflict ? "conflict" : "error",
          fileSha256: expectedFileSha256.current,
          error,
          canRetry,
        });
      }
    },
    [
      materializationClient,
      props.documentId,
      props.onMaterialized,
      props.storeId,
      publishMaterializationState,
    ],
  );

  const save = useCallback(async (): Promise<void> => {
    if (saveInFlight.current !== null) return saveInFlight.current;
    const editor = editorRef.current;
    const current = materializationStateRef.current;
    const fileSha256 = expectedFileSha256.current;
    if (
      editor === null ||
      fileSha256 === null ||
      current.kind === "up_to_date" ||
      current.kind === "checking" ||
      current.kind === "read_only" ||
      (current.kind === "conflict" && !current.canRetry) ||
      (current.kind === "error" && !current.canRetry)
    ) {
      return;
    }
    const run = (async () => {
      publishMaterializationState({ kind: "saving", fileSha256 });
      const reusable = retryAttempt.current;
      if (reusable !== null && reusable.generation === editGeneration.current) {
        await settleMaterialize(reusable.request, reusable.generation);
        return;
      }
      try {
        for (let stabilityAttempt = 0; stabilityAttempt < 2; stabilityAttempt += 1) {
          const generation = editGeneration.current;
          if (persistence.lastError !== null || persistence.pendingBatchCount > 0) {
            await persistence.retry();
          }
          await persistence.flush();
          const compacted = await persistence.compact();
          const renderedMarkdown = serializeCoworkEditorMarkdown(editor, props.document);
          const renderedSha256 = await sha256Hex(
            new TextEncoder().encode(renderedMarkdown),
          );
          // Never bind rendered bytes to a structured head captured before a concurrent edit.
          // Restart once from flush if the editor changed anywhere in that async window.
          if (editGeneration.current !== generation) continue;
          const request: CoworkMaterializeRequest = {
            renderedMarkdown,
            renderedSha256,
            expectedFileSha256: fileSha256,
            expectedStructuredHeadSha256: compacted.structuredHeadSha256,
            snapshotSha256: compacted.snapshotSha256,
            idempotencyKey:
              globalThis.crypto?.randomUUID?.() ??
              `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`,
          };
          retryAttempt.current = { generation, request };
          await settleMaterialize(request, generation);
          return;
        }
        publishMaterializationState({ kind: "unsaved", fileSha256 });
      } catch (saveError) {
        const error = asCoworkApiError(saveError);
        publishMaterializationState({
          kind: "error",
          fileSha256: expectedFileSha256.current,
          error,
          canRetry: error.retryable,
        });
      }
    })();
    saveInFlight.current = run;
    try {
      await run;
    } finally {
      saveInFlight.current = null;
    }
  }, [persistence, props.document, publishMaterializationState, settleMaterialize]);

  const retrySync = useCallback(async (): Promise<void> => {
    try {
      await persistence.retry();
      await persistence.flush();
    } catch {
      // Persistence publishes the typed sync state; edits remain in the durable outbox.
    }
  }, [persistence]);

  const materializationController = useMemo<CoworkMaterializationController>(
    () => ({ save, retrySync }),
    [retrySync, save],
  );

  const sittingWorkspace = useMemo<CoworkSittingWorkspace>(
    () => ({
      synchronize: async () => {
        if (persistence.lastError !== null || persistence.pendingBatchCount > 0) {
          await persistence.retry();
        }
        await persistence.flush();
        const fileSha256 = expectedFileSha256.current;
        if (fileSha256 === null || fileSha256.length === 0) {
          throw new Error("Co-work cannot verify the Markdown file for this sitting.");
        }
        return {
          expectedFileSha256: fileSha256,
          // The live Y.Doc also contains ephemeral proposal projection marks. A sitting
          // must never compact that view into canonical state merely to learn its head.
          // flush() has already persisted every human-origin update, so this is the exact
          // server head against which prepare should CAS. The isolated commit snapshot is
          // the only sitting-time compaction boundary and strips all remaining open marks.
          expectedStructuredHeadSha256: persistence.docSha256,
          generation: editGeneration.current,
        };
      },
      prepare: async (admittedItems, generation) => {
        if (editorRef.current === null) {
          throw new Error("The editor is not ready, so the sitting cannot be prepared.");
        }
        if (editGeneration.current !== generation) {
          throw new Error("The document changed while the sitting was being prepared.");
        }
        return prepareCoworkSittingDocument(
          props.document,
          admittedItems,
          generation,
          props.onSittingProjectionAdopted,
        );
      },
      isCurrent: (generation) => editGeneration.current === generation,
      refreshFromServer: async () => {
        await persistence.pullSince();
        props.onSittingProjectionAdopted?.();
      },
    }),
    [persistence, props.document, props.onSittingProjectionAdopted],
  );

  useEffect(() => {
    const onUpdate = (_update: Uint8Array, origin: unknown): void => {
      if (!isLocalHumanOrigin(origin)) return;
      editGeneration.current += 1;
      retryAttempt.current = null;
      const fileSha256 = expectedFileSha256.current;
      if (fileSha256 !== null) {
        publishMaterializationState({ kind: "unsaved", fileSha256 });
      }
    };
    props.document.on("update", onUpdate);
    return () => props.document.off("update", onUpdate);
  }, [props.document, publishMaterializationState]);

  useEffect(() => {
    props.onMaterializationController?.(materializationController);
    return () => props.onMaterializationController?.(null);
  }, [materializationController, props.onMaterializationController]);

  useEffect(() => {
    props.onSittingWorkspace?.(sittingWorkspace);
    return () => props.onSittingWorkspace?.(null);
  }, [props.onSittingWorkspace, sittingWorkspace]);

  useEffect(() => {
    const onOnline = (): void => {
      if (persistence.pendingBatchCount > 0 || persistence.lastError !== null) {
        void retrySync();
      }
    };
    window.addEventListener("online", onOnline);
    return () => window.removeEventListener("online", onOnline);
  }, [persistence, retrySync]);

  useEffect(() => {
    if (props.onSyncStatus === undefined) return;
    return persistence.subscribeStatus(props.onSyncStatus);
  }, [persistence, props.onSyncStatus]);

  useEffect(() => {
    let active = true;
    setHydration(undefined);
    setHydrationError(undefined);
    void persistence
      .hydrate()
      .then((result) => {
        if (!active) return;
        if (result.wasEmpty) {
          setHydrationError(
            "This registered document has no initialized structured snapshot. Repair it from unchanged Markdown before opening.",
          );
          return;
        }
        // Observe the hydrated Y.Doc before mounting Tiptap. A blank canonical document can
        // acquire its first paragraph while the collaboration binding mounts; that structural
        // base must reach the durable outbox before any typed updates that causally depend on
        // it. MountedBridgeEditor calls start() again, but start is deliberately idempotent.
        persistence.start();
        setHydration(result);
      })
      .catch((error: unknown) => {
        if (active) {
          setHydrationError(
            error instanceof Error ? error.message : "Co-work could not hydrate this document.",
          );
        }
      });
    return () => {
      active = false;
    };
  }, [persistence, attempt]);

  useEffect(
    () => () => {
      // In-app navigation awaits the explicit registry barrier. A direct host unmount has
      // no cancellable continuation, so dispose remains a best-effort fallback and must not
      // surface an unhandled rejection after React has removed the session.
      void persistence.dispose().catch(() => undefined);
    },
    [persistence],
  );

  return (
    <section className="wb-cowork-editor" aria-label="Editor">
      {hydrationError !== undefined ? (
        <InlineAlert tone="danger" role="alert" className="wb-cowork-editor__hydration-error">
          <strong>Structured document unavailable.</strong>
          <span>{hydrationError}</span>
          <Button size="small" onClick={() => setAttempt((current) => current + 1)}>
            Retry validation
          </Button>
        </InlineAlert>
      ) : hydration !== undefined ? (
        <MountedBridgeEditor
          {...props}
          onReady={(context) => {
            editorRef.current = context.editor;
            props.onReady?.(context);
            void checkProjection(context.editor);
          }}
          onTeardown={() => {
            editorRef.current = null;
            props.onTeardown?.();
          }}
          persistence={persistence}
          seedWhenEmpty={hydration.wasEmpty}
        />
      ) : (
        <p className="wb-cowork-editor__loading" role="status">
          Loading the document.
        </p>
      )}
    </section>
  );
}

export default CoworkBridgeEditor;

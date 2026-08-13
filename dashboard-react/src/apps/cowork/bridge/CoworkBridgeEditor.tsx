import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import { Extension, type Editor } from "@tiptap/core";
import {
  Plugin,
  type Transaction,
} from "@tiptap/pm/state";
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
import { assertCanonicalCoworkEditorState } from "../editor/canonicalState";
import { importCoworkMarkdown } from "../editor/markdownImport";
import type { ProposalInput } from "../suggestions/types";
import type { CoworkApiError, CoworkDriftState } from "../contracts";
import { isLocalHumanOrigin } from "../editor/applyOrigin";
import { serializeCoworkEditorMarkdown } from "../editor/serializeCoworkMarkdown";
import { sha256Hex } from "../persistence/hashing";
import { asCoworkApiError } from "../providers/errors";
import {
  HttpCoworkMaterializationClient,
} from "../materialization/HttpCoworkMaterializationClient";
import { CoworkHttpClient } from "../providers/CoworkHttpClient";
import type {
  CoworkMaterializationController,
  CoworkMaterializationState,
  CoworkMaterializeReceipt,
  CoworkMaterializeRequest,
} from "../materialization/contracts";
import type { ProvenanceMutationBarrier } from "../provenance/view/contracts";
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
import {
  DefaultCoworkActionSnapshotController,
  type CoworkActionSnapshotController,
} from "../targets";
import { CoworkWorkingTargetProjector } from "./CoworkWorkingTargetProjector";
import {
  CoworkProvenanceDeterminationDialog,
  COWORK_PROVENANCE_ACTOR_CHANGED,
  COWORK_PROVENANCE_EXACT_MAX_CHARS,
  COWORK_PROVENANCE_TARGET_CHANGED,
  CoworkPasteProvenanceExactLimitError,
  DurableCoworkPasteProvenanceOutbox,
  coworkPastePassageExcerpt,
  coworkPasteCaptureFromTransaction,
  coworkPasteTransactionExceedsProvenanceLimit,
  coworkProvenanceExactWithinLimit,
  defaultCoworkProvenanceDetermination,
  resolveCoworkPasteAnchor,
  unknownCoworkProvenanceDetermination,
  type CoworkPasteProvenanceCapture,
  type CoworkPasteProvenanceOutbox,
  type CoworkPasteProvenanceOutboxEntry,
  type CoworkPasteProvenanceRecorder,
  type CoworkProvenanceActorIdentity,
  type CoworkProvenanceDetermination,
} from "../provenance";
import {
  initializeLocalIdentity,
  refreshLocalIdentity,
  subscribeLocalIdentity,
} from "../../../security/localIdentity";

/** What the host reports up once the canonical editor is mounted. */
export interface CoworkEditorReadyContext {
  readonly editor: Editor;
  /** The ProseMirror DOM root used to resolve explicit Review passage navigation. */
  readonly dom: HTMLElement;
}

export interface CoworkBridgeEditorProps {
  /** The canonical collaborative document. Open proposals never mutate it. */
  readonly document: Y.Doc;
  /** The Yjs transport (HttpCoworkYdocTransport live, in-memory in tests). */
  readonly transport: CoworkYdocTransport;
  /** Markdown seeded into a brand-new document exactly once, on an empty fragment. */
  readonly seedMarkdown: string;
  /** Fired once the editor is mounted. */
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
  /**
   * Persists authorship and human-review provenance for the exact span inserted
   * by a paste. Omitted in non-registered/demo editors that have no durable
   * provenance ledger.
   */
  readonly onRecordPasteProvenance?: CoworkPasteProvenanceRecorder;
  /**
   * Injectable initial capture identity for tests/embedders. A server
   * actor-change rejection always overrides it and forces a fresh authoritative
   * lookup before any pending attribution can be reconfirmed.
   */
  readonly provenanceActor?: CoworkProvenanceActorIdentity;
  /** Injectable durable queue; production defaults to an IndexedDB-backed document queue. */
  readonly pasteProvenanceOutbox?: CoworkPasteProvenanceOutbox;
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
  readonly onProvenanceMutationBarrier?: (
    barrier: ProvenanceMutationBarrier | null,
  ) => void;
  /** Exact local-human edit signal; foreign hydration must not invalidate attribution. */
  readonly onLocalProvenanceEdit?: () => void;
  /**
   * A normal persistence cycle durably flushed and compacted one unchanged
   * editor generation. The consumer still verifies this head against a fresh
   * provenance projection before treating attribution as current.
   */
  readonly onProvenancePersistenceSettled?: (
    structuredHeadSha256: string,
  ) => void;
  /** Narrow editor-owned exact-action capture seam lifted to the keyed live session. */
  readonly onActionSnapshotController?: (
    controller: CoworkActionSnapshotController | null,
  ) => void;
  /** Latest authoritative R2 proposals, used only on the isolated sitting clone. */
  readonly getProposalCatalog?: () => readonly ProposalInput[];
  readonly onSittingServerRefreshed?: () => void;
}

interface MountedProps extends CoworkBridgeEditorProps {
  readonly persistence: CoworkYdocPersistence;
  readonly resolvedProvenanceActor?: CoworkProvenanceActorIdentity;
  readonly resolvedPasteProvenanceOutbox?: CoworkPasteProvenanceOutbox;
  readonly onProvenanceActorChanged?: () => void;
  readonly seedWhenEmpty: boolean;
}

export { assertCanonicalCoworkEditorState } from "../editor/canonicalState";

const pasteIdempotencyKey = (): string =>
  globalThis.crypto?.randomUUID?.() ??
  `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;

export const coworkPasteProvenanceOutboxKey = (
  storeId: string,
  documentId: string,
): string =>
  `cowork-paste-provenance/v2:${JSON.stringify([storeId, documentId])}`;

const retryablePasteProvenanceError = (): string =>
  "Co-work couldn’t record where this pasted text came from. Try again.";

const stalePasteProvenanceError = (): string =>
  "This pasted passage no longer has one unique match in the current document. Restore or disambiguate it, then save again.";

const terminalPasteProvenanceError = (): string =>
  "Co-work rejected this attribution. Review or correct it before starting a new save attempt.";

const outboxPasteProvenanceError = (): string =>
  "Co-work couldn’t safely store this paste attribution. The pasted text remains in the document; keep this page open and retry.";

const actorChangedPasteProvenanceError = (): string =>
  "The active identity changed before this attribution was saved. Confirm it again for the current person.";

const oversizedPasteProvenanceError = (): string =>
  `That paste is too large to attribute safely. Nothing was inserted. Paste it in sections of ${COWORK_PROVENANCE_EXACT_MAX_CHARS.toLocaleString("en-US")} characters or fewer.`;

/**
 * The mounted live editor. It follows the same load-order the demo pane proved (SP-2): the
 * Y.Doc is hydrated from the transport before mount (the parent gates on that), the editor
 * binds to it, persistence starts pushing local human edits, a brand-new document is seeded
 * once, and the load-time id mint is fenced out of the undo stack. It reports the ready
 * context so the bridge can project view-only review decorations and control Review focus.
 */
function MountedBridgeEditor({
  document,
  persistence,
  seedMarkdown,
  seedWhenEmpty,
  onReady,
  onTeardown,
  documentId,
  storeId,
  onFeedbackCaptured,
  feedbackTransport,
  onRecordPasteProvenance,
  resolvedProvenanceActor,
  resolvedPasteProvenanceOutbox,
  onProvenanceActorChanged,
  readOnly = false,
}: MountedProps) {
  const [oversizedPasteBlocked, setOversizedPasteBlocked] = useState(false);
  const pasteSizeGuard = useMemo(
    () =>
      Extension.create({
        name: "coworkPasteProvenanceSizeGuard",
        addProseMirrorPlugins() {
          return [
            new Plugin({
              filterTransaction: (transaction) => {
                if (
                  !coworkPasteTransactionExceedsProvenanceLimit(transaction)
                ) {
                  return true;
                }
                setOversizedPasteBlocked(true);
                return false;
              },
            }),
          ];
        },
      }),
    [],
  );
  const extensions = useMemo(
    () => [...buildEditorExtensions(document), pasteSizeGuard],
    [document, pasteSizeGuard],
  );
  const seedContent = useMemo(
    () => importCoworkMarkdown(seedMarkdown).doc,
    [seedMarkdown],
  );
  const boundRef = useRef(false);
  const onReadyRef = useRef(onReady);
  const onTeardownRef = useRef(onTeardown);
  const pasteRecorderRef = useRef(onRecordPasteProvenance);
  const pasteRecordTailRef = useRef<Promise<void>>(Promise.resolve());
  const pasteAttemptsRef = useRef(new Set<number>());
  const pasteSettlementsRef = useRef(new Set<number>());
  const pasteEditorRef = useRef<Editor | null>(null);
  const [pasteEntries, setPasteEntries] = useState<
    readonly CoworkPasteProvenanceOutboxEntry[]
  >([]);
  const [busyPasteIds, setBusyPasteIds] = useState<ReadonlySet<number>>(
    () => new Set(),
  );
  const [dismissedPasteIds, setDismissedPasteIds] = useState<
    ReadonlySet<number>
  >(() => new Set());
  const [outboxError, setOutboxError] = useState<string | null>(null);
  const [volatilePasteCaptures, setVolatilePasteCaptures] = useState<
    readonly CoworkPasteProvenanceCapture[]
  >([]);
  onReadyRef.current = onReady;
  onTeardownRef.current = onTeardown;
  pasteRecorderRef.current = onRecordPasteProvenance;

  const refreshPasteEntries = useCallback(async (): Promise<
    readonly CoworkPasteProvenanceOutboxEntry[] | null
  > => {
    if (resolvedPasteProvenanceOutbox === undefined) return null;
    try {
      const listed = await resolvedPasteProvenanceOutbox.list();
      const currentEditor = pasteEditorRef.current;
      if (currentEditor === null) return null;
      const validated: CoworkPasteProvenanceOutboxEntry[] = [];
      for (const entry of listed) {
        const resolution = resolveCoworkPasteAnchor(
          currentEditor.state.doc,
          entry.anchor,
        );
        if (
          resolution.kind === "unique" ||
          entry.status === "stale_target"
        ) {
          validated.push(entry);
          continue;
        }
        try {
          validated.push(
            await resolvedPasteProvenanceOutbox.markFailure(entry.id, {
              code:
                resolution.kind === "ambiguous"
                  ? "paste_anchor_ambiguous"
                  : "paste_anchor_absent",
              message:
                "The captured pasted passage no longer resolves uniquely.",
              kind: "stale_target",
            }),
          );
        } catch {
          setOutboxError(outboxPasteProvenanceError());
          return null;
        }
      }
      setPasteEntries(validated);
      setOutboxError(null);
      return validated;
    } catch {
      setOutboxError(outboxPasteProvenanceError());
      return null;
    }
  }, [resolvedPasteProvenanceOutbox]);

  const attemptPasteProvenance = useCallback(
    (entryId: number): Promise<void> => {
      if (
        resolvedPasteProvenanceOutbox === undefined ||
        resolvedProvenanceActor === undefined ||
        pasteAttemptsRef.current.has(entryId)
      ) {
        return Promise.resolve();
      }
      pasteAttemptsRef.current.add(entryId);
      setBusyPasteIds((current) => new Set(current).add(entryId));
      const run = async (): Promise<void> => {
        try {
          const stored = (
            await resolvedPasteProvenanceOutbox.list()
          ).find((entry) => entry.id === entryId);
          if (
            stored === undefined ||
            (stored.status !== "ready" &&
              stored.status !== "retryable_failure")
          ) {
            return;
          }

          // The paste transaction enters Yjs after the synchronous recovery
          // journal. Flush that update before resolving its quote anchor: the
          // passage and the structured head frozen below must describe the
          // same persisted document version.
          await Promise.resolve();
          await persistence.flush();
          const currentEditor = pasteEditorRef.current;
          const anchorResolution =
            currentEditor === null
              ? { kind: "absent" as const }
              : resolveCoworkPasteAnchor(
                  currentEditor.state.doc,
                  stored.anchor,
                );
          if (anchorResolution.kind !== "unique") {
            await resolvedPasteProvenanceOutbox.markFailure(entryId, {
              code:
                anchorResolution.kind === "ambiguous"
                  ? "paste_anchor_ambiguous"
                  : "paste_anchor_absent",
              message:
                "The captured pasted passage no longer resolves uniquely.",
              kind: "stale_target",
            });
            return;
          }
          const recorder = pasteRecorderRef.current;
          if (
            recorder === undefined ||
            documentId === undefined ||
            storeId === undefined
          ) {
            throw new Error(
              "Paste provenance is unavailable for this document.",
            );
          }

          let request = stored.frozenRequest;
          if (request === undefined) {
            const expectedStructuredHeadSha256 = persistence.docSha256;
            if (expectedStructuredHeadSha256.length === 0) {
              throw new Error(
                "The pasted text does not have a persisted structured head.",
              );
            }
            const frozen = await resolvedPasteProvenanceOutbox.freezeRequest(
              entryId,
              {
                storeId,
                documentId,
                expectedStructuredHeadSha256,
              },
            );
            request = frozen.frozenRequest;
          }
          if (request === undefined) {
            throw new Error(
              "Paste provenance is unavailable for this document.",
            );
          }

          // A resolved recorder call is the confirmed server receipt boundary.
          // Until then the complete frozen request remains replayable.
          await recorder(request);
          await resolvedPasteProvenanceOutbox.remove(entryId);
        } catch (error) {
          const apiError = asCoworkApiError(error);
          try {
            if (apiError.code === COWORK_PROVENANCE_ACTOR_CHANGED) {
              const recoveryPrefix = pasteIdempotencyKey();
              await resolvedPasteProvenanceOutbox.resetAfterActorChange(
                recoveryPrefix,
                unknownCoworkProvenanceDetermination(),
              );
              setVolatilePasteCaptures((current) =>
                current.map((capture, index) => ({
                  ...capture,
                  idempotencyKey: `${recoveryPrefix}:volatile:${String(index)}`,
                  basisKind: "user_attestation",
                  determination: unknownCoworkProvenanceDetermination(),
                  requiresExplicitDetermination: true,
                  status: "awaiting_determination",
                })),
              );
              onProvenanceActorChanged?.();
            } else {
              await resolvedPasteProvenanceOutbox.markFailure(entryId, {
                code: apiError.code,
                message: apiError.message,
                kind:
                  apiError.code === COWORK_PROVENANCE_TARGET_CHANGED
                    ? "stale_target"
                    : apiError.retryable
                      ? "retryable"
                      : "terminal",
              });
            }
          } catch {
            setOutboxError(outboxPasteProvenanceError());
          }
        } finally {
          pasteAttemptsRef.current.delete(entryId);
          setBusyPasteIds((current) => {
            const next = new Set(current);
            next.delete(entryId);
            return next;
          });
          await refreshPasteEntries();
        }
      };
      const result = pasteRecordTailRef.current.then(run, run);
      pasteRecordTailRef.current = result.catch(() => undefined);
      return result;
    },
    [
      documentId,
      persistence,
      refreshPasteEntries,
      resolvedProvenanceActor,
      resolvedPasteProvenanceOutbox,
      onProvenanceActorChanged,
      storeId,
    ],
  );

  const stagePasteProvenanceBeforeTransaction = useCallback(
    ({
      editor: transactionEditor,
      transaction,
    }: {
      readonly editor: Editor;
      readonly transaction: Transaction;
    }): void => {
      if (
        pasteRecorderRef.current === undefined ||
        documentId === undefined ||
        storeId === undefined ||
        resolvedPasteProvenanceOutbox === undefined
      ) {
        return;
      }
      pasteEditorRef.current = transactionEditor;
      const capture = coworkPasteCaptureFromTransaction(
        transaction,
        transaction.doc,
      );
      if (capture === null) return;
      if (!coworkProvenanceExactWithinLimit(capture.anchor.exact)) {
        setOversizedPasteBlocked(true);
        return;
      }
      setOversizedPasteBlocked(false);
      const captureRequest = {
        anchor: capture.anchor,
        idempotencyKey: pasteIdempotencyKey(),
        capturedAt: new Date().toISOString(),
        passageExcerpt: coworkPastePassageExcerpt(capture.anchor.exact),
        ...(persistence.docSha256.length === 0
          ? {}
          : {
              capturedBaseStructuredHeadSha256:
                persistence.docSha256,
            }),
      };
      if (capture.substantial || resolvedProvenanceActor === undefined) {
        const pendingCapture: CoworkPasteProvenanceCapture = {
          ...captureRequest,
          substantial: capture.substantial,
          basisKind: "user_attestation",
          determination: unknownCoworkProvenanceDetermination(),
          ...(resolvedProvenanceActor === undefined
            ? { requiresExplicitDetermination: true }
            : {}),
          status: "awaiting_determination",
        };
        void resolvedPasteProvenanceOutbox
          .append(pendingCapture)
          .then(refreshPasteEntries)
          .catch((error) => {
            if (error instanceof CoworkPasteProvenanceExactLimitError) {
              setOversizedPasteBlocked(true);
              return;
            }
            setVolatilePasteCaptures((current) => [
              ...current.filter(
                (candidate) =>
                  candidate.idempotencyKey !==
                  pendingCapture.idempotencyKey,
              ),
              pendingCapture,
            ]);
            setOutboxError(outboxPasteProvenanceError());
          });
        return;
      }
      const automatic = defaultCoworkProvenanceDetermination(
        resolvedProvenanceActor,
      );
      const automaticCapture: CoworkPasteProvenanceCapture = {
        ...captureRequest,
        substantial: false,
        basisKind: "automatic_short_text_attribution",
        determination: automatic,
        status: "ready",
      };
      void resolvedPasteProvenanceOutbox
        .append(automaticCapture)
        .then(async (entry) => {
          await refreshPasteEntries();
          await attemptPasteProvenance(entry.id);
        })
        .catch((error) => {
          if (error instanceof CoworkPasteProvenanceExactLimitError) {
            setOversizedPasteBlocked(true);
            return;
          }
          setVolatilePasteCaptures((current) => [
            ...current.filter(
              (candidate) =>
                candidate.idempotencyKey !==
                automaticCapture.idempotencyKey,
            ),
            automaticCapture,
          ]);
          setOutboxError(outboxPasteProvenanceError());
        });
    },
    [
      attemptPasteProvenance,
      documentId,
      refreshPasteEntries,
      resolvedPasteProvenanceOutbox,
      persistence,
      resolvedProvenanceActor,
      storeId,
    ],
  );

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

  useLayoutEffect(() => {
    if (editor === null) return;
    // Tiptap exposes beforeTransaction as an editor event (not a constructor
    // option). Attach before the first writable paint; it fires before
    // EditorView.updateState, so the synchronous recovery journal is durable
    // before y-prosemirror can publish the paste.
    editor.on(
      "beforeTransaction",
      stagePasteProvenanceBeforeTransaction,
    );
    return () => {
      editor.off(
        "beforeTransaction",
        stagePasteProvenanceBeforeTransaction,
      );
    };
  }, [editor, stagePasteProvenanceBeforeTransaction]);

  const dismissedPasteEntries = pasteEntries.filter((entry) =>
    dismissedPasteIds.has(entry.id),
  );
  const visiblePasteEntry =
    pasteEntries.find(
      (entry) =>
        !dismissedPasteIds.has(entry.id) &&
        (entry.substantial || entry.status !== "ready"),
    ) ?? null;
  const visiblePasteActorChanged =
    visiblePasteEntry?.failure?.code === COWORK_PROVENANCE_ACTOR_CHANGED;
  const visiblePasteRequiresExplicitDetermination =
    visiblePasteEntry?.requiresExplicitDetermination === true;

  useEffect(() => {
    const survivingIds = new Set(pasteEntries.map((entry) => entry.id));
    setDismissedPasteIds((current) => {
      const next = new Set(
        [...current].filter((entryId) => survivingIds.has(entryId)),
      );
      return next.size === current.size ? current : next;
    });
  }, [pasteEntries]);

  const updatePasteDetermination = useCallback(
    (
      entry: CoworkPasteProvenanceOutboxEntry,
      determination: CoworkProvenanceDetermination,
    ): void => {
      if (
        resolvedPasteProvenanceOutbox === undefined ||
        (entry.frozenRequest !== undefined &&
          entry.status !== "stale_target" &&
          entry.status !== "terminal_failure")
      ) {
        return;
      }
      setPasteEntries((current) =>
        current.map((candidate) =>
          candidate.id === entry.id
            ? { ...candidate, determination }
            : candidate,
        ),
      );
      void resolvedPasteProvenanceOutbox
        .updateDetermination(entry.id, determination)
        .then(refreshPasteEntries)
        .catch(() => {
          setOutboxError(outboxPasteProvenanceError());
        });
    },
    [refreshPasteEntries, resolvedPasteProvenanceOutbox],
  );

  const settlePasteEntry = useCallback(
    async (
      entry: CoworkPasteProvenanceOutboxEntry,
      determination: CoworkProvenanceDetermination,
    ): Promise<void> => {
      if (
        resolvedPasteProvenanceOutbox === undefined ||
        pasteSettlementsRef.current.has(entry.id)
      ) {
        return;
      }
      // Claim synchronously, before the first outbox await. React cannot paint
      // `busy` soon enough to stop an Escape/onOpenChange callback dispatched
      // in the same turn as Save or Decide later.
      pasteSettlementsRef.current.add(entry.id);
      setBusyPasteIds((current) => new Set(current).add(entry.id));
      try {
        if (
          entry.status === "stale_target" ||
          entry.status === "terminal_failure"
        ) {
          // Only this explicit user action replaces a rejected target/key.
          await resolvedPasteProvenanceOutbox.retarget(
            entry.id,
            pasteIdempotencyKey(),
            determination,
          );
        } else if (entry.frozenRequest === undefined) {
          await resolvedPasteProvenanceOutbox.markReady(
            entry.id,
            determination,
            entry.basisKind,
          );
        }
        await refreshPasteEntries();
        await attemptPasteProvenance(entry.id);
      } catch {
        setOutboxError(outboxPasteProvenanceError());
      } finally {
        pasteSettlementsRef.current.delete(entry.id);
        if (!pasteAttemptsRef.current.has(entry.id)) {
          setBusyPasteIds((current) => {
            const next = new Set(current);
            next.delete(entry.id);
            return next;
          });
        }
      }
    },
    [
      attemptPasteProvenance,
      refreshPasteEntries,
      resolvedPasteProvenanceOutbox,
    ],
  );

  useEffect(() => {
    let active = true;
    if (resolvedPasteProvenanceOutbox === undefined || editor === null) return;
    pasteEditorRef.current = editor;
    void refreshPasteEntries()
      .then((entries) => {
        if (!active || entries === null) return;
        // "ready" means a determination was already made but the browser
        // stopped before receipt. Attempt performs a second strict anchor
        // resolution immediately before network egress.
        for (const entry of entries) {
          if (entry.status === "ready") {
            void attemptPasteProvenance(entry.id);
          }
        }
      })
      .catch(() => {
        if (active) setOutboxError(outboxPasteProvenanceError());
      });
    return () => {
      active = false;
    };
  }, [
    attemptPasteProvenance,
    editor,
    refreshPasteEntries,
    resolvedPasteProvenanceOutbox,
  ]);

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
    pasteEditorRef.current = editor;
    // Persistence starts before seeding so a brand-new document's seed is
    // pushed through R4 as its first human-origin update (SP-2 load-order).
    persistence.start();
    if (seedWhenEmpty) {
      editor.commands.setContent(seedContent);
    }
    stopCapturingLoadTimeIds(editor);
    onReadyRef.current?.({ editor, dom: editor.view.dom as HTMLElement });
  }, [editor, persistence, seedContent, seedWhenEmpty]);

  useEffect(() => {
    return () => {
      pasteEditorRef.current = null;
      onTeardownRef.current?.();
    };
  }, []);

  const retryPasteOutbox = useCallback(async (): Promise<void> => {
    if (resolvedPasteProvenanceOutbox === undefined) return;
    const failed: CoworkPasteProvenanceCapture[] = [];
    for (const capture of volatilePasteCaptures) {
      if (!coworkProvenanceExactWithinLimit(capture.anchor.exact)) {
        setOversizedPasteBlocked(true);
        continue;
      }
      try {
        await resolvedPasteProvenanceOutbox.append(capture);
      } catch {
        failed.push(capture);
      }
    }
    setVolatilePasteCaptures(failed);
    const entries = await refreshPasteEntries();
    if (failed.length > 0 || entries === null) {
      setOutboxError(outboxPasteProvenanceError());
      return;
    }
    setOutboxError(null);
    for (const entry of entries) {
      if (entry.status === "ready") {
        await attemptPasteProvenance(entry.id);
      }
    }
  }, [
    attemptPasteProvenance,
    refreshPasteEntries,
    resolvedPasteProvenanceOutbox,
    volatilePasteCaptures,
  ]);

  return (
    <>
      <EditorContent editor={editor} className="wb-cowork-editor__content" />
      {oversizedPasteBlocked ? (
        <InlineAlert tone="warning" role="alert">
          <span>{oversizedPasteProvenanceError()}</span>
          <Button
            size="small"
            onClick={() => setOversizedPasteBlocked(false)}
          >
            Dismiss
          </Button>
        </InlineAlert>
      ) : null}
      {outboxError !== null ? (
        <InlineAlert tone="danger" role="alert">
          <span>{outboxError}</span>
          <Button
            size="small"
            onClick={() => void retryPasteOutbox().catch(() => {
              setOutboxError(outboxPasteProvenanceError());
            })}
          >
            Retry attribution storage
          </Button>
        </InlineAlert>
      ) : null}
      {dismissedPasteEntries.length > 0 && visiblePasteEntry === null ? (
        <InlineAlert tone="info" role="status">
          <span>
            {String(dismissedPasteEntries.length)} paste{" "}
            {dismissedPasteEntries.length === 1 ? "attribution is" : "attributions are"} waiting.
          </span>
          <Button
            size="small"
            onClick={() =>
              setDismissedPasteIds((current) => {
                const next = new Set(current);
                const first = dismissedPasteEntries[0]?.id;
                if (first !== undefined) next.delete(first);
                return next;
              })
            }
          >
            Review pending attribution
          </Button>
        </InlineAlert>
      ) : null}
      {editor !== null && !readOnly && onFeedbackCaptured !== undefined && documentId !== undefined ? (
        <CoworkFeedbackAffordance
          editor={editor}
          documentId={documentId}
          storeId={storeId}
          onCaptured={onFeedbackCaptured}
          transport={feedbackTransport}
        />
      ) : null}
      {visiblePasteEntry !== null &&
      resolvedProvenanceActor !== undefined ? (
        <CoworkProvenanceDeterminationDialog
          key={`${String(visiblePasteEntry.id)}:${resolvedProvenanceActor.identity_status}:${resolvedProvenanceActor.ref}`}
          value={visiblePasteEntry.determination}
          currentUserIdentity={resolvedProvenanceActor}
          passageExcerpt={visiblePasteEntry.passageExcerpt}
          busy={busyPasteIds.has(visiblePasteEntry.id)}
          formDisabled={
            visiblePasteEntry.frozenRequest !== undefined &&
            visiblePasteEntry.status !== "stale_target" &&
            visiblePasteEntry.status !== "terminal_failure"
          }
          error={
            visiblePasteActorChanged
              ? actorChangedPasteProvenanceError()
              : visiblePasteEntry.status === "stale_target"
                ? stalePasteProvenanceError()
                : visiblePasteEntry.status === "terminal_failure"
                  ? terminalPasteProvenanceError()
                  : visiblePasteEntry.status === "retryable_failure"
                    ? retryablePasteProvenanceError()
                    : null
          }
          description={
            pasteEntries.length > 1
              ? `Record its authorship and review status. ${String(pasteEntries.length - 1)} more pasted ${pasteEntries.length === 2 ? "passage is" : "passages are"} waiting.`
              : undefined
          }
          confirmLabel={
            visiblePasteActorChanged
              ? "Confirm attribution"
              : visiblePasteEntry.status === "stale_target"
                ? "Save against current version"
                : visiblePasteEntry.status === "terminal_failure"
                  ? "Start corrected save"
                  : visiblePasteEntry.status === "retryable_failure"
                    ? "Try again"
                    : undefined
          }
          cancelLabel={
            visiblePasteRequiresExplicitDetermination ||
            visiblePasteEntry.status === "stale_target" ||
            visiblePasteEntry.status === "terminal_failure"
              ? "Keep for later"
              : undefined
          }
          onChange={(value) =>
            updatePasteDetermination(visiblePasteEntry, value)
          }
          onConfirm={(value) =>
            settlePasteEntry(visiblePasteEntry, value)
          }
          onClose={() => {
            if (
              visiblePasteRequiresExplicitDetermination ||
              visiblePasteEntry.status === "stale_target" ||
              visiblePasteEntry.status === "terminal_failure"
            ) {
              setDismissedPasteIds((current) =>
                new Set(current).add(visiblePasteEntry.id),
              );
              return;
            }
            void settlePasteEntry(
              visiblePasteEntry,
              unknownCoworkProvenanceDetermination(),
            );
          }}
        />
      ) : null}
    </>
  );
}

/**
 * The live editor region of the Co-work surface. It owns its Yjs persistence controller,
 * hydrates the shared Y.Doc from the transport BEFORE mounting the editor, and gates the
 * mount by conditionally rendering (never useEditor(null), F5.4). Pending review state is
 * projected into ProseMirror decorations and never attached to this live Y.Doc.
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
  const provenanceEnabled =
    props.onRecordPasteProvenance !== undefined &&
    props.documentId !== undefined &&
    props.storeId !== undefined &&
    props.readOnly !== true;
  const [provenanceClient] = useState(() => new CoworkHttpClient());
  const [resolvedProvenanceActor, setResolvedProvenanceActor] = useState<
    CoworkProvenanceActorIdentity | undefined
  >(props.provenanceActor);
  const [provenanceActorState, setProvenanceActorState] = useState<
    "disabled" | "loading" | "ready" | "error"
  >(
    !provenanceEnabled
      ? "disabled"
      : props.provenanceActor === undefined
        ? "loading"
        : "ready",
  );
  const [provenanceActorAttempt, setProvenanceActorAttempt] = useState(0);
  const requestProvenanceActorRefresh = useCallback(() => {
    setResolvedProvenanceActor(undefined);
    setProvenanceActorState("loading");
    setProvenanceActorAttempt((current) => current + 1);
  }, []);
  const resolvedPasteProvenanceOutbox = useMemo<
    CoworkPasteProvenanceOutbox | undefined
  >(() => {
    if (props.pasteProvenanceOutbox !== undefined) {
      return props.pasteProvenanceOutbox;
    }
    if (
      !provenanceEnabled ||
      props.documentId === undefined ||
      props.storeId === undefined
    ) {
      return undefined;
    }
    return new DurableCoworkPasteProvenanceOutbox(
      coworkPasteProvenanceOutboxKey(
        props.storeId,
        props.documentId,
      ),
    );
  }, [
    props.documentId,
    props.pasteProvenanceOutbox,
    props.storeId,
    provenanceEnabled,
  ]);
  const [hydration, setHydration] = useState<{ readonly wasEmpty: boolean }>();
  const [hydrationError, setHydrationError] = useState<string>();
  const [attempt, setAttempt] = useState(0);
  const effectiveReadOnly = props.readOnly ?? false;
  const persistenceReadOnly = effectiveReadOnly || hydration === undefined;
  const editorRef = useRef<Editor | null>(null);
  const editGeneration = useRef(0);
  const lastProvenanceSettlementGeneration = useRef(0);
  const provenanceSettlementInFlight = useRef<Promise<void> | null>(null);
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
  const readOnlyRef = useRef(effectiveReadOnly);
  readOnlyRef.current = effectiveReadOnly;

  const actionSnapshotController = useMemo(
    () =>
      props.documentId === undefined || props.storeId === undefined
        ? null
        : new DefaultCoworkActionSnapshotController({
            document: props.document,
            documentId: props.documentId,
            storeId: props.storeId,
            persistence,
            getEditGeneration: () => editGeneration.current,
          }),
    [persistence, props.document, props.documentId, props.storeId],
  );
  const workingTargetProjector = useMemo(
    () =>
      actionSnapshotController === null
        ? null
        : new CoworkWorkingTargetProjector(actionSnapshotController),
    [actionSnapshotController],
  );

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
    const readOnly = persistenceReadOnly;
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
  }, [persistence, persistenceReadOnly]);

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
      if (effectiveReadOnly || props.canMaterialize === false) {
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
      effectiveReadOnly,
      props.canMaterialize,
      props.document,
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
          assertCanonicalCoworkEditorState(editor);
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

  const settleProvenancePersistence = useCallback((): void => {
    if (
      props.onProvenancePersistenceSettled === undefined ||
      provenanceSettlementInFlight.current !== null ||
      editGeneration.current <= lastProvenanceSettlementGeneration.current ||
      persistence.status !== "clean"
    ) {
      return;
    }
    const generation = editGeneration.current;
    let retryForNewerGeneration = false;
    const run = (async () => {
      try {
        await persistence.flush();
        const editor = editorRef.current;
        if (editor === null) return;
        assertCanonicalCoworkEditorState(editor);
        const receipt = await persistence.compact();
        if (generation !== editGeneration.current) {
          retryForNewerGeneration = true;
          return;
        }
        lastProvenanceSettlementGeneration.current = generation;
        props.onProvenancePersistenceSettled?.(
          receipt.structuredHeadSha256,
        );
      } catch {
        // Persistence publishes the actionable failure. Provenance remains
        // dirty until a later successful settlement and fresh R2 projection.
      } finally {
        provenanceSettlementInFlight.current = null;
        if (
          retryForNewerGeneration &&
          persistence.status === "clean" &&
          editGeneration.current > lastProvenanceSettlementGeneration.current
        ) {
          queueMicrotask(settleProvenancePersistence);
        }
      }
    })();
    provenanceSettlementInFlight.current = run;
  }, [persistence, props.onProvenancePersistenceSettled]);

  const settleForLifecycle = useCallback(async (): Promise<void> => {
    const currentEditor = editorRef.current;
    if (currentEditor === null) {
      throw new Error("The document is still loading. Try again in a moment.");
    }
    if (persistence.lastError !== null || persistence.pendingBatchCount > 0) {
      await persistence.retry();
    }
    await persistence.flush();
    assertCanonicalCoworkEditorState(currentEditor);
    await persistence.compact();
  }, [persistence]);

  const materializationController = useMemo<CoworkMaterializationController>(
    () => ({ save, retrySync, settleForLifecycle }),
    [retrySync, save, settleForLifecycle],
  );

  const sittingWorkspace = useMemo<CoworkSittingWorkspace>(
    () => ({
      synchronize: async () => {
        const fileSha256 = expectedFileSha256.current;
        if (fileSha256 === null || fileSha256.length === 0) {
          throw new Error(
            "Co-work cannot verify the Markdown file before applying these decisions.",
          );
        }
        // Publish the exact canonical Markdown and Y.Doc snapshot together.
        // A user edit racing the checkpoint causes a bounded recapture; Review
        // never pairs current prose with an older projection receipt.
        for (let attempt = 0; attempt < 2; attempt += 1) {
          if (persistence.lastError !== null || persistence.pendingBatchCount > 0) {
            await persistence.retry();
          }
          await persistence.flush();
          const editor = editorRef.current;
          if (editor === null) {
            throw new Error("The document is still loading. Try again in a moment.");
          }
          assertCanonicalCoworkEditorState(editor);
          const generation = editGeneration.current;
          const projectionMarkdown = serializeCoworkEditorMarkdown(
            editor,
            props.document,
          );
          const snapshot = Y.encodeStateAsUpdate(props.document);
          const [snapshotSha256, projectionSha256] = await Promise.all([
            sha256Hex(snapshot),
            sha256Hex(new TextEncoder().encode(projectionMarkdown)),
          ]);
          if (generation !== editGeneration.current) continue;
          const receipt = await persistence.compactProjection({
            snapshot,
            snapshotSha256,
            projectionMarkdown,
            projectionSha256,
          });
          if (receipt === null || generation !== editGeneration.current) continue;
          return {
            expectedFileSha256: fileSha256,
            expectedStructuredHeadSha256: receipt.structuredHeadSha256,
            generation,
          };
        }
        throw new Error(
          "The document kept changing while Review prepared it. Try again when the edit settles.",
        );
      },
      prepare: async (admittedItems, generation) => {
        if (editorRef.current === null) {
          throw new Error("The document is still loading. Try again in a moment.");
        }
        if (editGeneration.current !== generation) {
          throw new Error(
            "The document changed while your decisions were being prepared. Review it and try again.",
          );
        }
        return prepareCoworkSittingDocument(
          props.document,
          admittedItems,
          props.getProposalCatalog?.() ?? [],
          generation,
        );
      },
      isCurrent: (generation) => editGeneration.current === generation,
      refreshFromServer: async (response, generation) => {
        await persistence.pullSince();
        if (persistence.docSha256 !== response.structured_head_sha256) {
          throw new Error(
            "Co-work could not verify the structured document committed by this sitting.",
          );
        }
        if (response.materialize !== null) {
          expectedFileSha256.current = response.materialize.new_file_sha256;
          retryAttempt.current = null;
          publishMaterializationState(
            editGeneration.current === generation
              ? {
                  kind: "up_to_date",
                  fileSha256: response.materialize.new_file_sha256,
                }
              : {
                  kind: "unsaved",
                  fileSha256: response.materialize.new_file_sha256,
                },
          );
        }
        const editor = editorRef.current;
        if (editor !== null) assertCanonicalCoworkEditorState(editor);
        props.onProvenancePersistenceSettled?.(
          response.structured_head_sha256,
        );
        props.onSittingServerRefreshed?.();
      },
    }),
    [
      persistence,
      props.document,
      props.getProposalCatalog,
      props.onProvenancePersistenceSettled,
      props.onSittingServerRefreshed,
      publishMaterializationState,
    ],
  );

  const provenanceMutationBarrier = useMemo<ProvenanceMutationBarrier>(
    () => ({
      runWithSynchronizedDocument: async (operation) => {
        const liveEditor = editorRef.current;
        if (liveEditor === null) {
          throw new Error("The document is still loading. Try again in a moment.");
        }
        const wasEditable = liveEditor.isEditable;
        liveEditor.setEditable(false);
        try {
          if (persistence.lastError !== null || persistence.pendingBatchCount > 0) {
            await persistence.retry();
          }
          await persistence.flush();
          assertCanonicalCoworkEditorState(liveEditor);
          const receipt = await persistence.compact();
          return await operation({
            structuredHeadSha256: receipt.structuredHeadSha256,
          });
        } finally {
          if (wasEditable && !readOnlyRef.current && !liveEditor.isDestroyed) {
            liveEditor.setEditable(true);
          }
        }
      },
    }),
    [persistence],
  );

  useEffect(() => {
    props.onProvenanceMutationBarrier?.(provenanceMutationBarrier);
    return () => props.onProvenanceMutationBarrier?.(null);
  }, [props.onProvenanceMutationBarrier, provenanceMutationBarrier]);

  useEffect(() => {
    const onUpdate = (_update: Uint8Array, origin: unknown): void => {
      if (!isLocalHumanOrigin(origin)) return;
      editGeneration.current += 1;
      props.onLocalProvenanceEdit?.();
      retryAttempt.current = null;
      const fileSha256 = expectedFileSha256.current;
      if (fileSha256 !== null) {
        publishMaterializationState({ kind: "unsaved", fileSha256 });
      }
    };
    props.document.on("update", onUpdate);
    return () => props.document.off("update", onUpdate);
  }, [props.document, props.onLocalProvenanceEdit, publishMaterializationState]);

  useEffect(() => {
    props.onMaterializationController?.(materializationController);
    return () => props.onMaterializationController?.(null);
  }, [materializationController, props.onMaterializationController]);

  useEffect(() => {
    props.onSittingWorkspace?.(sittingWorkspace);
    return () => props.onSittingWorkspace?.(null);
  }, [props.onSittingWorkspace, sittingWorkspace]);

  useEffect(() => {
    props.onActionSnapshotController?.(actionSnapshotController);
    return () => {
      workingTargetProjector?.dispose();
      actionSnapshotController?.detach();
      props.onActionSnapshotController?.(null);
    };
  }, [
    actionSnapshotController,
    props.onActionSnapshotController,
    workingTargetProjector,
  ]);

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

  useEffect(
    () =>
      persistence.subscribeStatus((status) => {
        if (status === "clean") settleProvenancePersistence();
      }),
    [persistence, settleProvenancePersistence],
  );

  useEffect(() => {
    if (hydration === undefined || editorRef.current === null) return;
    void checkProjection(editorRef.current);
  }, [checkProjection, hydration]);

  useEffect(() => {
    let active = true;
    setHydration(undefined);
    setHydrationError(undefined);
    void (async () => {
      try {
        const result = await persistence.hydrate();
        if (!active) return;
        if (result.wasEmpty) {
          setHydrationError(
            "This document’s editing data is missing. Repair it from the Markdown file before opening.",
          );
          return;
        }
        // Observe the hydrated Y.Doc before mounting Tiptap. A blank canonical document can
        // acquire its first paragraph while the collaboration binding mounts; that structural
        // base must reach the durable outbox before any typed updates that causally depend on
        // it. MountedBridgeEditor calls start() again, but start is deliberately idempotent.
        persistence.start();
        setHydration(result);
      } catch {
        if (!active) return;
        setHydrationError(
          "Co-work couldn’t load this document. Try again or repair it from the Markdown file.",
        );
      }
    })();
    return () => {
      active = false;
    };
  }, [persistence, attempt]);

  useEffect(() => {
    let active = true;
    if (!provenanceEnabled) {
      setResolvedProvenanceActor(undefined);
      setProvenanceActorState("disabled");
      return () => {
        active = false;
      };
    }
    if (
      props.provenanceActor !== undefined &&
      provenanceActorAttempt === 0
    ) {
      setResolvedProvenanceActor(props.provenanceActor);
      setProvenanceActorState("ready");
      return () => {
        active = false;
      };
    }
    setResolvedProvenanceActor(undefined);
    setProvenanceActorState("loading");
    const resolveActor = async (): Promise<CoworkProvenanceActorIdentity> => {
      const identity = provenanceActorAttempt > 0
        ? await refreshLocalIdentity()
        : await initializeLocalIdentity();
      if (!identity.authenticated) {
        throw new Error(
          identity.reason ??
            "This browser does not have an authenticated local Work Buddy session.",
        );
      }
      return provenanceClient.currentActor();
    };
    void resolveActor().then(
      (actor) => {
        if (!active) return;
        setResolvedProvenanceActor(actor);
        setProvenanceActorState("ready");
      },
      () => {
        if (!active) return;
        setResolvedProvenanceActor(undefined);
        setProvenanceActorState("error");
      },
    );
    return () => {
      active = false;
    };
  }, [
    props.provenanceActor,
    provenanceActorAttempt,
    provenanceClient,
    provenanceEnabled,
  ]);

  useEffect(
    () =>
      subscribeLocalIdentity((identity) => {
        if (identity.authenticated && provenanceActorState === "error") {
          requestProvenanceActorRefresh();
        }
      }),
    [provenanceActorState, requestProvenanceActorRefresh],
  );

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
          <strong>Document couldn’t be opened.</strong>
          <span>{hydrationError}</span>
          <Button size="small" onClick={() => setAttempt((current) => current + 1)}>
            Try again
          </Button>
        </InlineAlert>
      ) : hydration !== undefined ? (
        <MountedBridgeEditor
          {...props}
          readOnly={effectiveReadOnly}
          onReady={(context) => {
            editorRef.current = context.editor;
            actionSnapshotController?.attach(context.editor);
            workingTargetProjector?.attach(context.editor);
            props.onReady?.(context);
            void checkProjection(context.editor);
          }}
          onTeardown={() => {
            workingTargetProjector?.detach();
            actionSnapshotController?.detach();
            editorRef.current = null;
            props.onTeardown?.();
          }}
          persistence={persistence}
          resolvedProvenanceActor={resolvedProvenanceActor}
          resolvedPasteProvenanceOutbox={resolvedPasteProvenanceOutbox}
          onProvenanceActorChanged={requestProvenanceActorRefresh}
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

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
import { Plugin, type Transaction } from "@tiptap/pm/state";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
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
import {
  setCoworkPendingProvenance,
  type CoworkEditorLens,
  type CoworkPendingProvenanceDecoration,
} from "../editor/ledgerDecorations";
import { serializeCoworkEditorMarkdown } from "../editor/serializeCoworkMarkdown";
import { sha256Hex } from "../persistence/hashing";
import { asCoworkApiError } from "../providers/errors";
import { HttpCoworkMaterializationClient } from "../materialization/HttpCoworkMaterializationClient";
import { CoworkHttpClient } from "../providers/CoworkHttpClient";
import type {
  CoworkMaterializationController,
  CoworkMaterializationState,
  CoworkMaterializeReceipt,
  CoworkMaterializeRequest,
} from "../materialization/contracts";
import type {
  ProvenanceMutationBarrier,
  ProvenanceProvider,
  ProvenanceSelectionAction,
} from "../provenance/view/contracts";
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
  CoworkProvenanceSelectionAffordance,
  COWORK_PROVENANCE_ACTOR_CHANGED,
  COWORK_PROVENANCE_EXACT_MAX_CHARS,
  COWORK_PROVENANCE_TARGET_CHANGED,
  CoworkPasteProvenanceExactLimitError,
  DurableCoworkPasteProvenanceOutbox,
  coworkPastePassageExcerpt,
  coworkPasteCaptureFromTransaction,
  coworkDirectEntryCaptureFromTransaction,
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
  quoteAnchorFromRange,
  type RangeQuoteAnchor,
} from "../feedback/feedbackAnchor";
import {
  initializeLocalIdentity,
  refreshLocalIdentity,
  subscribeLocalIdentity,
} from "../../../security/localIdentity";
import { parseCoworkLocalFileHref } from "../document-kernel/schema";
import {
  HttpCoworkLocalFileClient,
  linkedLocalFileWarning,
  type CoworkLocalFileClient,
} from "../localFiles";

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
  /** Injectable metadata-only local-file client; registered documents get HTTP by default. */
  readonly localFileClient?: CoworkLocalFileClient;
  /** Test/host seam for the explicit credential reveal warning. */
  readonly confirmCredentialReveal?: (warning: string) => boolean;
  /**
   * When supplied, the selection-triggered Give-feedback affordance mounts over
   * the editor and reports a successful R9 capture here. Omitted (demo, tests)
   * keeps the affordance off entirely.
   */
  readonly onFeedbackCaptured?: (capture: FeedbackCapture) => void;
  /** Injectable R9 transport for the affordance, else the same-origin HTTP one. */
  readonly feedbackTransport?: CoworkFeedbackTransport;
  /** The active view lens controls contextual selection actions. */
  readonly activeLens?: CoworkEditorLens;
  /** Authoritative projection used by Provenance selection actions. */
  readonly provenanceProvider?: ProvenanceProvider;
  /** Whether the stable Provenance rail can receive selection actions. */
  readonly provenanceSelectionActionsActive?: boolean;
  readonly onProvenanceSelectionAction?: (
    action: ProvenanceSelectionAction & {
      readonly intent: "review" | "view" | "inspect";
    },
  ) => void;
  /** Pending input attribution remains true through authoritative refresh. */
  readonly onInputProvenancePendingChange?: (pending: boolean) => void;
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
  readonly onSittingWorkspace?: (
    workspace: CoworkSittingWorkspace | null,
  ) => void;
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

interface DirectEntryBurst {
  readonly idempotencyKey: string;
  readonly from: number;
  readonly to: number;
  readonly capturedAt: string;
  readonly determination: CoworkProvenanceDetermination;
  readonly capturedActor?: CoworkProvenanceActorIdentity;
}

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
  "Co-work couldn’t safely store provenance for edited text. Your text remains in the document; keep this page open and retry.";

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
  localFileClient,
  confirmCredentialReveal,
  onFeedbackCaptured,
  feedbackTransport,
  activeLens,
  provenanceProvider,
  provenanceSelectionActionsActive,
  onProvenanceSelectionAction,
  onInputProvenancePendingChange,
  onRecordPasteProvenance,
  resolvedProvenanceActor,
  resolvedPasteProvenanceOutbox,
  onProvenanceActorChanged,
  readOnly = false,
}: MountedProps) {
  const [oversizedPasteBlocked, setOversizedPasteBlocked] = useState(false);
  const [localFileActionError, setLocalFileActionError] = useState<string | null>(
    null,
  );
  const resolvedLocalFileClient = useMemo<CoworkLocalFileClient | undefined>(
    () =>
      localFileClient ??
      (documentId === undefined || storeId === undefined
        ? undefined
        : new HttpCoworkLocalFileClient({ documentId, storeId })),
    [documentId, localFileClient, storeId],
  );
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
  const directEntryBurstRef = useRef<DirectEntryBurst | null>(null);
  const directEntryClosingBurstsRef = useRef(
    new Map<string, DirectEntryBurst>(),
  );
  const directEntryCaptureEnabledRef = useRef(false);
  const provenanceEditGenerationRef = useRef(0);
  const directEntryFinalizationRef = useRef<Promise<void> | null>(null);
  const finalizeDirectEntryBurstRef = useRef<() => Promise<void>>(
    async () => undefined,
  );
  const closeDirectEntryBurstRef = useRef<() => Promise<void>>(
    async () => undefined,
  );
  const manualRecordEntryRef = useRef(new Map<string, number>());
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
  const pendingDirectEntryDecorations = useMemo(() => {
    const pending = new Map<string, CoworkPendingProvenanceDecoration>();
    for (const capture of [...pasteEntries, ...volatilePasteCaptures]) {
      if (capture.sourceKind !== "direct_entry") continue;
      pending.set(capture.idempotencyKey, {
        captureId: capture.idempotencyKey,
        quoteAnchor: capture.anchor,
      });
    }
    return [...pending.values()];
  }, [pasteEntries, volatilePasteCaptures]);
  const pendingDirectEntryDecorationsRef = useRef(
    pendingDirectEntryDecorations,
  );
  pendingDirectEntryDecorationsRef.current = pendingDirectEntryDecorations;
  const inputProvenancePending =
    pasteEntries.some(
      (entry) =>
        entry.sourceKind === "direct_entry" ||
        (entry.sourceKind === "legacy" &&
          entry.status !== "awaiting_determination" &&
          entry.status !== "stale_target" &&
          entry.status !== "terminal_failure"),
    ) ||
    volatilePasteCaptures.some(
      (capture) =>
        capture.sourceKind !== undefined && capture.sourceKind !== "paste",
    );
  const directEntryFailurePending = pasteEntries.some(
    (entry) =>
      entry.sourceKind === "direct_entry" &&
      (entry.status === "retryable_failure" ||
        entry.status === "stale_target" ||
        entry.status === "terminal_failure"),
  );
  const visibleOutboxError =
    outboxError ??
    (directEntryFailurePending
      ? "Co-work couldn’t record provenance for recent typing. Your capture is safe; retry provenance storage."
      : null);
  onReadyRef.current = onReady;
  onTeardownRef.current = onTeardown;
  pasteRecorderRef.current = onRecordPasteProvenance;

  useEffect(() => {
    onInputProvenancePendingChange?.(inputProvenancePending);
    return () => onInputProvenancePendingChange?.(false);
  }, [inputProvenancePending, onInputProvenancePendingChange]);

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
        // An evolving direct-entry quote is not a target yet. A frozen request
        // is an immutable exactly-once retry and the server owns its CAS.
        if (entry.status === "capturing" || entry.frozenRequest !== undefined) {
          validated.push(entry);
          continue;
        }
        const resolution = resolveCoworkPasteAnchor(
          currentEditor.state.doc,
          entry.anchor,
        );
        if (resolution.kind === "unique" || entry.status === "stale_target") {
          validated.push(entry);
          continue;
        }
        try {
          validated.push(
            await resolvedPasteProvenanceOutbox.markFailure(entry.id, {
              code:
                resolution.kind === "ambiguous"
                  ? "provenance_anchor_ambiguous"
                  : "provenance_anchor_absent",
              message: "The captured text no longer resolves uniquely.",
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
        let stored: CoworkPasteProvenanceOutboxEntry | undefined;
        try {
          stored = (await resolvedPasteProvenanceOutbox.list()).find(
            (entry) => entry.id === entryId,
          );
          if (
            stored === undefined ||
            (stored.status !== "ready" && stored.status !== "retryable_failure")
          ) {
            return;
          }

          const recorder = pasteRecorderRef.current;
          if (
            recorder === undefined ||
            documentId === undefined ||
            storeId === undefined
          ) {
            throw new Error(
              "Text provenance is unavailable for this document.",
            );
          }

          let request = stored.frozenRequest;
          if (request === undefined) {
            // Only an unfrozen capture is revalidated. Once frozen, replay the
            // exact request/key first: the server may have committed it before
            // the browser lost the response.
            await Promise.resolve();
            await persistence.flush();
            const compacted = await persistence.compact();
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
                    ? "provenance_anchor_ambiguous"
                    : "provenance_anchor_absent",
                message: "The captured text no longer resolves uniquely.",
                kind: "stale_target",
              });
              return;
            }
            const expectedStructuredHeadSha256 = compacted.structuredHeadSha256;
            if (expectedStructuredHeadSha256.length === 0) {
              throw new Error(
                "The edited text does not have a persisted structured head.",
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
              "Text provenance is unavailable for this document.",
            );
          }

          // A resolved recorder call is the confirmed server receipt boundary.
          // Until then the complete frozen request remains replayable.
          const receipt = await recorder(request);
          // Keep the immutable request until the authoritative view includes
          // the receipt. Manual Record must not close into an empty/stale lens.
          if (provenanceProvider !== undefined) {
            const refreshed = await provenanceProvider.refresh();
            if (
              refreshed.state !== "ready" ||
              !refreshed.data.history.some(
                (record) =>
                  record.attestationId === receipt.attestationId &&
                  record.scope.documentSpanId === receipt.documentSpanId &&
                  record.scope.structuredHeadSha256 ===
                    receipt.targetStructuredHeadSha256,
              )
            ) {
              throw new Error(
                "Co-work saved the provenance record but could not confirm it in the current view yet.",
              );
            }
          }
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
                  sourceKind:
                    capture.sourceKind === "direct_entry"
                      ? "legacy"
                      : capture.sourceKind,
                  basisKind: "user_attestation",
                  capturedActor: undefined,
                  determination: unknownCoworkProvenanceDetermination(),
                  requiresExplicitDetermination: true,
                  status: "awaiting_determination",
                })),
              );
              onProvenanceActorChanged?.();
            } else if (stored !== undefined) {
              await resolvedPasteProvenanceOutbox.markFailure(entryId, {
                code: apiError.code,
                message: apiError.message,
                kind:
                  apiError.code === COWORK_PROVENANCE_TARGET_CHANGED
                    ? "stale_target"
                    : stored.sourceKind === "direct_entry" || apiError.retryable
                      ? "retryable"
                      : "terminal",
              });
              if (stored.sourceKind !== "paste") {
                setOutboxError(
                  "Co-work couldn’t record provenance for recent typing. Your capture is safe; retry provenance storage.",
                );
              }
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
      provenanceProvider,
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
        ...(resolvedProvenanceActor === undefined
          ? {}
          : { capturedActor: resolvedProvenanceActor }),
        capturedAt: new Date().toISOString(),
        passageExcerpt: coworkPastePassageExcerpt(capture.anchor.exact),
        ...(persistence.docSha256.length === 0
          ? {}
          : {
              capturedBaseStructuredHeadSha256: persistence.docSha256,
            }),
      };
      if (capture.substantial || resolvedProvenanceActor === undefined) {
        const pendingCapture: CoworkPasteProvenanceCapture = {
          ...captureRequest,
          substantial: capture.substantial,
          sourceKind: "paste",
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
                  candidate.idempotencyKey !== pendingCapture.idempotencyKey,
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
        sourceKind: "paste",
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
                candidate.idempotencyKey !== automaticCapture.idempotencyKey,
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

  const directCaptureForBurst = useCallback(
    (
      burst: DirectEntryBurst,
      editorDocument: ProseMirrorNode,
    ): CoworkPasteProvenanceCapture | null => {
      const anchor = quoteAnchorFromRange(editorDocument, burst.from, burst.to);
      if (anchor === null) return null;
      return {
        anchor,
        idempotencyKey: burst.idempotencyKey,
        substantial: false,
        sourceKind: "direct_entry",
        basisKind: "automatic_direct_entry_attribution",
        determination: burst.determination,
        ...(burst.capturedActor === undefined
          ? {}
          : { capturedActor: burst.capturedActor }),
        capturedAt: burst.capturedAt,
        passageExcerpt: coworkPastePassageExcerpt(anchor.exact),
        ...(persistence.docSha256.length === 0
          ? {}
          : {
              capturedBaseStructuredHeadSha256: persistence.docSha256,
            }),
        status: "capturing",
      };
    },
    [persistence],
  );

  const finalizeDirectEntryBurst = useCallback((): Promise<void> => {
    if (directEntryFinalizationRef.current !== null) {
      return directEntryFinalizationRef.current;
    }
    let retryForChangedGeneration = false;
    const run = (async (): Promise<void> => {
      if (resolvedPasteProvenanceOutbox === undefined) return;
      const listed = await resolvedPasteProvenanceOutbox.list();
      const closing = [...directEntryClosingBurstsRef.current.values()];
      const current = directEntryBurstRef.current;
      const bursts = [
        ...closing,
        ...(current === null ||
        closing.some((burst) => burst.idempotencyKey === current.idempotencyKey)
          ? []
          : [current]),
      ];
      if (bursts.length === 0) return;

      const generation = provenanceEditGenerationRef.current;
      await persistence.flush();
      const compacted = await persistence.compact();
      if (generation !== provenanceEditGenerationRef.current) {
        retryForChangedGeneration = true;
        return;
      }

      for (const burst of bursts) {
        let entry = listed.find(
          (candidate) => candidate.idempotencyKey === burst.idempotencyKey,
        );
        if (entry === undefined) continue;

        if (resolvedProvenanceActor === undefined) {
          // Identity may recover from a trusted launcher without changing the
          // editor. Preserve both actor-bound and actorless observations until
          // that boundary can decide whether automatic attribution is honest.
          continue;
        }
        const captureActorUnavailable = burst.capturedActor === undefined;
        const captureActorChanged =
          burst.capturedActor !== undefined &&
          (burst.capturedActor.ref !== resolvedProvenanceActor.ref ||
            burst.capturedActor.identity_status !==
              resolvedProvenanceActor.identity_status);
        if (captureActorUnavailable || captureActorChanged) {
          // Never let an unavailable/changed actor make the observation
          // disappear, and never let a later actor inherit it. Preserve the
          // exact selector as an explicit legacy determination the user can
          // resolve after a legitimate identity session is available.
          if (entry.status === "capturing") {
            const deferred =
              await resolvedPasteProvenanceOutbox.deferDirectEntry(
                entry.id,
                // This automatic request has never been frozen or submitted, so
                // preserving its key is both safe and important: a transaction
                // that mapped the burst while demotion was queued may already
                // have journalled one last capture under this key. Rotating it
                // here would let that queued upsert append an orphan automatic
                // row after the legacy replacement commits.
                burst.idempotencyKey,
                unknownCoworkProvenanceDetermination(),
                {
                  code: captureActorUnavailable
                    ? "provenance_actor_unavailable_at_capture"
                    : COWORK_PROVENANCE_ACTOR_CHANGED,
                  message: captureActorUnavailable
                    ? "No enrolled local actor was available when this text was entered."
                    : "The acting identity changed before this attribution was saved.",
                  kind: "terminal",
                },
              );
            if (
              generation !== provenanceEditGenerationRef.current ||
              deferred.idempotencyKey !== burst.idempotencyKey
            ) {
              // A disjoint edit can arrive while durable demotion yields. Keep
              // the burst refs intact and run the full close loop again so the
              // newly actor-bound burst is frozen and delivered at its current
              // structured head. The same-key replacement makes the queued
              // stale upsert reject instead of resurrecting automatic work.
              retryForChangedGeneration = true;
              return;
            }
          }
          directEntryClosingBurstsRef.current.delete(burst.idempotencyKey);
          if (
            directEntryBurstRef.current?.idempotencyKey === burst.idempotencyKey
          ) {
            directEntryBurstRef.current = null;
          }
          continue;
        }

        if (entry.status === "capturing") {
          entry = await resolvedPasteProvenanceOutbox.markReady(
            entry.id,
            entry.determination,
            "automatic_direct_entry_attribution",
          );
          entry = await resolvedPasteProvenanceOutbox.freezeRequest(entry.id, {
            storeId: storeId!,
            documentId: documentId!,
            expectedStructuredHeadSha256: compacted.structuredHeadSha256,
          });
          if (generation !== provenanceEditGenerationRef.current) {
            const currentEditor = pasteEditorRef.current;
            const latestBurst =
              directEntryClosingBurstsRef.current.get(burst.idempotencyKey) ??
              (directEntryBurstRef.current?.idempotencyKey ===
              burst.idempotencyKey
                ? directEntryBurstRef.current
                : burst);
            const latestCapture =
              currentEditor === null
                ? null
                : directCaptureForBurst(latestBurst, currentEditor.state.doc);
            if (latestCapture !== null) {
              await resolvedPasteProvenanceOutbox.reopenCapture(
                entry.id,
                latestCapture,
              );
              retryForChangedGeneration = true;
            }
            return;
          }
        }

        directEntryClosingBurstsRef.current.delete(burst.idempotencyKey);
        if (
          directEntryBurstRef.current?.idempotencyKey === burst.idempotencyKey
        ) {
          directEntryBurstRef.current = null;
        }
        await attemptPasteProvenance(entry.id);
      }
      await refreshPasteEntries();
    })()
      .catch(() => {
        // The mutable burst and synchronous intent remain available for a
        // same-mount retry; fire-and-forget lifecycle callers never leak an
        // unhandled rejection.
        setOutboxError(outboxPasteProvenanceError());
      })
      .finally(() => {
        directEntryFinalizationRef.current = null;
        if (
          retryForChangedGeneration &&
          (directEntryBurstRef.current !== null ||
            directEntryClosingBurstsRef.current.size > 0)
        ) {
          queueMicrotask(() => {
            void finalizeDirectEntryBurstRef.current();
          });
        }
      });
    directEntryFinalizationRef.current = run;
    return run;
  }, [
    attemptPasteProvenance,
    directCaptureForBurst,
    documentId,
    persistence,
    refreshPasteEntries,
    resolvedPasteProvenanceOutbox,
    resolvedProvenanceActor,
    storeId,
  ]);
  finalizeDirectEntryBurstRef.current = finalizeDirectEntryBurst;

  const closeDirectEntryBurst = useCallback((): Promise<void> => {
    const burst = directEntryBurstRef.current;
    if (burst !== null) {
      directEntryClosingBurstsRef.current.set(burst.idempotencyKey, burst);
      directEntryBurstRef.current = null;
    }
    return finalizeDirectEntryBurst();
  }, [finalizeDirectEntryBurst]);
  closeDirectEntryBurstRef.current = closeDirectEntryBurst;

  const stageDirectEntryProvenanceBeforeTransaction = useCallback(
    ({
      editor: transactionEditor,
      transaction,
    }: {
      readonly editor: Editor;
      readonly transaction: Transaction;
    }): void => {
      if (
        !directEntryCaptureEnabledRef.current ||
        pasteRecorderRef.current === undefined ||
        documentId === undefined ||
        storeId === undefined ||
        resolvedPasteProvenanceOutbox === undefined
      ) {
        return;
      }
      pasteEditorRef.current = transactionEditor;
      if (
        transaction.docChanged &&
        directEntryClosingBurstsRef.current.size > 0
      ) {
        for (const queued of directEntryClosingBurstsRef.current.values()) {
          const mapped: DirectEntryBurst = {
            ...queued,
            // This burst is already sealed. Insertions exactly at either
            // boundary belong to the new gesture, never to both gestures.
            from: transaction.mapping.map(queued.from, 1),
            to: transaction.mapping.map(queued.to, -1),
          };
          if (mapped.to <= mapped.from) {
            directEntryClosingBurstsRef.current.delete(queued.idempotencyKey);
            void resolvedPasteProvenanceOutbox
              .list()
              .then((entries) =>
                entries.find(
                  (entry) => entry.idempotencyKey === queued.idempotencyKey,
                ),
              )
              .then((entry) => {
                if (entry === undefined) return undefined;
                return entry.status === "capturing"
                  ? resolvedPasteProvenanceOutbox.cancelCapture(entry.id)
                  : resolvedPasteProvenanceOutbox.remove(entry.id);
              })
              .then(refreshPasteEntries)
              .catch(() => setOutboxError(outboxPasteProvenanceError()));
            continue;
          }
          const mappedCapture = directCaptureForBurst(mapped, transaction.doc);
          if (mappedCapture === null) {
            directEntryClosingBurstsRef.current.delete(queued.idempotencyKey);
            void resolvedPasteProvenanceOutbox
              .list()
              .then((entries) =>
                entries.find(
                  (entry) => entry.idempotencyKey === queued.idempotencyKey,
                ),
              )
              .then((entry) => {
                if (entry === undefined) return undefined;
                return entry.status === "capturing"
                  ? resolvedPasteProvenanceOutbox.cancelCapture(entry.id)
                  : resolvedPasteProvenanceOutbox.remove(entry.id);
              })
              .then(refreshPasteEntries)
              .catch(() => setOutboxError(outboxPasteProvenanceError()));
            continue;
          }
          directEntryClosingBurstsRef.current.set(
            mapped.idempotencyKey,
            mapped,
          );
          // upsertCapture stages the post-transaction quote synchronously. If
          // the row was frozen a moment earlier, its durable mutation rejects;
          // the generation guard reopens the same unsent key from this latest
          // queued burst rather than from an obsolete loop snapshot.
          void resolvedPasteProvenanceOutbox
            .upsertCapture(mappedCapture)
            .then(refreshPasteEntries)
            .catch(() => undefined);
        }
      }
      const capture = coworkDirectEntryCaptureFromTransaction(
        transaction,
        transaction.doc,
      );
      if (capture === null) {
        const prior = directEntryBurstRef.current;
        if (prior === null) return;
        if (transaction.docChanged) {
          const continuingMappedFrom = transaction.mapping.map(prior.from, -1);
          const continuingMappedTo = transaction.mapping.map(prior.to, 1);
          const previous = transactionEditor.state.selection;
          const touchesBurst =
            previous.from <= prior.to && previous.to >= prior.from;
          if (
            touchesBurst &&
            transaction.getMeta("uiEvent") !== "paste" &&
            transaction.getMeta("uiEvent") !== "drop"
          ) {
            if (continuingMappedTo <= continuingMappedFrom) {
              directEntryBurstRef.current = null;
              void resolvedPasteProvenanceOutbox
                .list()
                .then((entries) =>
                  entries.find(
                    (entry) => entry.idempotencyKey === prior.idempotencyKey,
                  ),
                )
                .then((entry) =>
                  entry === undefined
                    ? undefined
                    : resolvedPasteProvenanceOutbox.cancelCapture(entry.id),
                )
                .then(refreshPasteEntries)
                .catch(() => setOutboxError(outboxPasteProvenanceError()));
              return;
            }
            const nextBurst: DirectEntryBurst = {
              ...prior,
              from: continuingMappedFrom,
              to: continuingMappedTo,
            };
            const nextCapture = directCaptureForBurst(
              nextBurst,
              transaction.doc,
            );
            if (nextCapture !== null) {
              directEntryBurstRef.current = nextBurst;
              void resolvedPasteProvenanceOutbox
                .upsertCapture(nextCapture)
                .then(refreshPasteEntries)
                .catch(() => setOutboxError(outboxPasteProvenanceError()));
              return;
            }
          }
          // The transaction document is already the post-edit document. Keep
          // the closing burst's positions and quote aligned with it before a
          // second, disjoint burst can be staged and before compaction freezes
          // both rows against that same head.
          const sealedMappedFrom = transaction.mapping.map(prior.from, 1);
          const sealedMappedTo = transaction.mapping.map(prior.to, -1);
          if (sealedMappedTo <= sealedMappedFrom) {
            directEntryBurstRef.current = null;
            void resolvedPasteProvenanceOutbox
              .list()
              .then((entries) =>
                entries.find(
                  (entry) => entry.idempotencyKey === prior.idempotencyKey,
                ),
              )
              .then((entry) =>
                entry === undefined
                  ? undefined
                  : resolvedPasteProvenanceOutbox.cancelCapture(entry.id),
              )
              .then(refreshPasteEntries)
              .catch(() => setOutboxError(outboxPasteProvenanceError()));
            return;
          }
          const mappedBurst: DirectEntryBurst = {
            ...prior,
            from: sealedMappedFrom,
            to: sealedMappedTo,
          };
          const mappedCapture = directCaptureForBurst(
            mappedBurst,
            transaction.doc,
          );
          if (mappedCapture === null) {
            directEntryBurstRef.current = null;
            void resolvedPasteProvenanceOutbox
              .list()
              .then((entries) =>
                entries.find(
                  (entry) => entry.idempotencyKey === prior.idempotencyKey,
                ),
              )
              .then((entry) =>
                entry === undefined
                  ? undefined
                  : resolvedPasteProvenanceOutbox.cancelCapture(entry.id),
              )
              .then(refreshPasteEntries)
              .catch(() => setOutboxError(outboxPasteProvenanceError()));
            return;
          }
          directEntryBurstRef.current = mappedBurst;
          void resolvedPasteProvenanceOutbox
            .upsertCapture(mappedCapture)
            .then(refreshPasteEntries)
            .catch(() => setOutboxError(outboxPasteProvenanceError()));
          void closeDirectEntryBurstRef.current();
          return;
        }
        if (
          transaction.selectionSet &&
          (!transaction.selection.empty ||
            transaction.selection.head !== prior.to)
        ) {
          void closeDirectEntryBurstRef.current();
        }
        return;
      }

      const now = Date.now();
      const prior = directEntryBurstRef.current;
      const mappedPriorForExtension =
        prior === null
          ? null
          : {
              from: transaction.mapping.map(prior.from, -1),
              to: transaction.mapping.map(prior.to, 1),
            };
      const mappedPriorForSeal =
        prior === null
          ? null
          : {
              // Keep a displaced burst disjoint from text inserted exactly at
              // its boundaries. A valid same-actor continuation still merges
              // the outward mapping with `capture` below.
              from: transaction.mapping.map(prior.from, 1),
              to: transaction.mapping.map(prior.to, -1),
            };
      const previousSelection = transactionEditor.state.selection;
      const selectionContinuesBurst =
        prior !== null &&
        previousSelection.empty &&
        previousSelection.head === prior.to &&
        prior.capturedActor?.ref === resolvedProvenanceActor?.ref &&
        prior.capturedActor?.identity_status ===
          resolvedProvenanceActor?.identity_status;
      const touchesPrior =
        mappedPriorForExtension !== null &&
        capture.range.from <= mappedPriorForExtension.to &&
        capture.range.to >= mappedPriorForExtension.from;
      const mergedFrom =
        mappedPriorForExtension === null
          ? capture.range.from
          : Math.min(mappedPriorForExtension.from, capture.range.from);
      const mergedTo =
        mappedPriorForExtension === null
          ? capture.range.to
          : Math.max(mappedPriorForExtension.to, capture.range.to);
      const safeTo = Math.max(mergedFrom, mergedTo - 1);
      const sameTextblock = (() => {
        try {
          const start = transaction.doc.resolve(mergedFrom);
          const end = transaction.doc.resolve(safeTo);
          return start.sameParent(end) && start.parent.isTextblock;
        } catch {
          return false;
        }
      })();
      const canExtend =
        prior !== null &&
        selectionContinuesBurst &&
        touchesPrior &&
        sameTextblock;
      if (prior !== null && !canExtend) {
        const displacedBurst: DirectEntryBurst = {
          ...prior,
          from: mappedPriorForSeal!.from,
          to: mappedPriorForSeal!.to,
        };
        const displacedCapture = directCaptureForBurst(
          displacedBurst,
          transaction.doc,
        );
        if (displacedCapture !== null) {
          // upsertCapture journals synchronously. The finalizer may start now,
          // but list() will reconcile this post-transaction selector before it
          // freezes any queued burst.
          void resolvedPasteProvenanceOutbox
            .upsertCapture(displacedCapture)
            .then(refreshPasteEntries)
            .catch(() => setOutboxError(outboxPasteProvenanceError()));
          directEntryClosingBurstsRef.current.set(
            displacedBurst.idempotencyKey,
            displacedBurst,
          );
        } else {
          void resolvedPasteProvenanceOutbox
            .list()
            .then((entries) =>
              entries.find(
                (entry) => entry.idempotencyKey === prior.idempotencyKey,
              ),
            )
            .then((entry) =>
              entry === undefined
                ? undefined
                : resolvedPasteProvenanceOutbox.cancelCapture(entry.id),
            )
            .then(refreshPasteEntries)
            .catch(() => setOutboxError(outboxPasteProvenanceError()));
        }
        directEntryBurstRef.current = null;
        void finalizeDirectEntryBurst();
      }
      const range = canExtend
        ? { from: mergedFrom, to: mergedTo }
        : capture.range;
      const anchor = canExtend
        ? quoteAnchorFromRange(transaction.doc, range.from, range.to)
        : capture.anchor;
      if (anchor === null) return;
      const idempotencyKey = canExtend
        ? prior!.idempotencyKey
        : pasteIdempotencyKey();
      const nextBurst: DirectEntryBurst = {
        idempotencyKey,
        from: range.from,
        to: range.to,
        capturedAt: canExtend ? prior!.capturedAt : new Date(now).toISOString(),
        determination: canExtend
          ? prior!.determination
          : resolvedProvenanceActor === undefined
            ? unknownCoworkProvenanceDetermination()
            : defaultCoworkProvenanceDetermination(resolvedProvenanceActor),
        ...(canExtend
          ? prior!.capturedActor === undefined
            ? {}
            : { capturedActor: prior!.capturedActor }
          : resolvedProvenanceActor === undefined
            ? {}
            : { capturedActor: resolvedProvenanceActor }),
      };
      directEntryBurstRef.current = nextBurst;
      const directCapture: CoworkPasteProvenanceCapture = {
        anchor,
        idempotencyKey,
        substantial: false,
        sourceKind: "direct_entry",
        basisKind: "automatic_direct_entry_attribution",
        determination: nextBurst.determination,
        ...(nextBurst.capturedActor === undefined
          ? {}
          : { capturedActor: nextBurst.capturedActor }),
        capturedAt: nextBurst.capturedAt,
        passageExcerpt: coworkPastePassageExcerpt(anchor.exact),
        ...(persistence.docSha256.length === 0
          ? {}
          : {
              capturedBaseStructuredHeadSha256: persistence.docSha256,
            }),
        status: "capturing",
      };
      // upsertCapture synchronously replaces the recovery-journal record before
      // returning its promise. Focused contiguous typing stays mutable across
      // idle pauses; blur, navigation, selection movement, or recovery closes it.
      void resolvedPasteProvenanceOutbox
        .upsertCapture(directCapture)
        .then(refreshPasteEntries)
        .catch(() => {
          setVolatilePasteCaptures((current) => [
            ...current.filter(
              (candidate) => candidate.idempotencyKey !== idempotencyKey,
            ),
            directCapture,
          ]);
          setOutboxError(outboxPasteProvenanceError());
        });
    },
    [
      documentId,
      persistence,
      refreshPasteEntries,
      resolvedPasteProvenanceOutbox,
      resolvedProvenanceActor,
      storeId,
      directCaptureForBurst,
    ],
  );

  const stageProvenanceBeforeTransaction = useCallback(
    (context: {
      readonly editor: Editor;
      readonly transaction: Transaction;
    }) => {
      if (
        directEntryCaptureEnabledRef.current &&
        context.transaction.docChanged
      ) {
        provenanceEditGenerationRef.current += 1;
      }
      stageDirectEntryProvenanceBeforeTransaction(context);
      stagePasteProvenanceBeforeTransaction(context);
      if (context.transaction.docChanged) {
        const pending = new Map(
          pendingDirectEntryDecorationsRef.current.map((item) => [
            item.captureId,
            item,
          ]),
        );
        const stageBurst = (burst: DirectEntryBurst): void => {
          const capture = directCaptureForBurst(burst, context.transaction.doc);
          if (capture === null) {
            pending.delete(burst.idempotencyKey);
            return;
          }
          pending.set(burst.idempotencyKey, {
            captureId: burst.idempotencyKey,
            quoteAnchor: capture.anchor,
          });
        };
        for (const burst of directEntryClosingBurstsRef.current.values()) {
          stageBurst(burst);
        }
        if (directEntryBurstRef.current !== null) {
          stageBurst(directEntryBurstRef.current);
        }
        const staged = [...pending.values()];
        // `beforeTransaction` fires after ProseMirror has derived the next
        // state, so changing this transaction's metadata is too late. A
        // microtask dispatch lands before the browser can paint the edited
        // frame and avoids a re-entrant EditorView transaction.
        queueMicrotask(() => {
          if (
            pasteEditorRef.current === context.editor &&
            !context.editor.isDestroyed
          ) {
            setCoworkPendingProvenance(context.editor, staged);
          }
        });
        if (staged.length > 0) {
          onInputProvenancePendingChange?.(true);
        }
      }
    },
    [
      directCaptureForBurst,
      onInputProvenancePendingChange,
      stageDirectEntryProvenanceBeforeTransaction,
      stagePasteProvenanceBeforeTransaction,
    ],
  );

  const activateLocalFileLink = useCallback(
    async (linkId: string): Promise<void> => {
      if (resolvedLocalFileClient === undefined) return;
      setLocalFileActionError(null);
      try {
        const links = await resolvedLocalFileClient.list();
        const link = links.find((candidate) => candidate.linkId === linkId);
        if (link === undefined) {
          throw new Error("The local-file link is not registered for this document.");
        }
        const warning = linkedLocalFileWarning(link);
        if (warning) {
          const confirmReveal =
            confirmCredentialReveal ??
            ((message: string): boolean => globalThis.confirm(message));
          if (!confirmReveal(warning)) return;
        }
        await resolvedLocalFileClient.activate(link);
      } catch {
        setLocalFileActionError(
          "The linked local file could not be opened. Its bytes remain untouched.",
        );
      }
    },
    [confirmCredentialReveal, resolvedLocalFileClient],
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
        handleClick: (_view, _position, event) => {
          const target = event.target;
          if (!(target instanceof Element)) return false;
          const anchor = target.closest("a[href]");
          const linkId = parseCoworkLocalFileHref(
            anchor?.getAttribute("href") ?? "",
          );
          if (linkId === null) return false;
          // Local links never fall through to browser navigation. Scratch and
          // unregistered editors deliberately leave them inert.
          event.preventDefault();
          if (
            event.button !== 0 ||
            event.altKey ||
            event.ctrlKey ||
            event.metaKey ||
            event.shiftKey ||
            resolvedLocalFileClient === undefined
          ) {
            return true;
          }
          void activateLocalFileLink(linkId);
          return true;
        },
      },
    },
    [activateLocalFileLink, extensions, resolvedLocalFileClient],
  );

  useLayoutEffect(() => {
    if (editor === null) return;
    // Tiptap exposes beforeTransaction as an editor event (not a constructor
    // option). Attach before the first writable paint; it fires before
    // EditorView.updateState, so the synchronous recovery journal is durable
    // before y-prosemirror can publish the inserted text.
    editor.on("beforeTransaction", stageProvenanceBeforeTransaction);
    return () => {
      editor.off("beforeTransaction", stageProvenanceBeforeTransaction);
    };
  }, [editor, stageProvenanceBeforeTransaction]);

  useEffect(() => {
    if (editor === null) return;
    setCoworkPendingProvenance(editor, pendingDirectEntryDecorations);
  }, [editor, pendingDirectEntryDecorations]);

  const manualRecordEntryIds = new Set(manualRecordEntryRef.current.values());
  const isDirectEntryRecovery = (
    entry: CoworkPasteProvenanceOutboxEntry,
  ): boolean =>
    entry.sourceKind === "legacy" &&
    entry.status === "awaiting_determination" &&
    entry.requiresExplicitDetermination === true &&
    entry.failure?.code === "provenance_actor_unavailable_at_capture" &&
    !manualRecordEntryIds.has(entry.id);
  const dismissedDeterminationEntries = pasteEntries.filter(
    (entry) =>
      dismissedPasteIds.has(entry.id) &&
      (entry.sourceKind === "paste" || isDirectEntryRecovery(entry)),
  );
  const visiblePasteEntry =
    pasteEntries.find(
      (entry) =>
        !dismissedPasteIds.has(entry.id) &&
        entry.sourceKind === "paste" &&
        entry.status !== "capturing" &&
        (entry.substantial || entry.status !== "ready"),
    ) ??
    pasteEntries.find(
      (entry) =>
        !dismissedPasteIds.has(entry.id) && isDirectEntryRecovery(entry),
    ) ??
    null;
  const visibleDirectEntryRecovery =
    visiblePasteEntry !== null && isDirectEntryRecovery(visiblePasteEntry);
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
            resolvedProvenanceActor,
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
      resolvedProvenanceActor,
    ],
  );

  useEffect(() => {
    let active = true;
    if (resolvedPasteProvenanceOutbox === undefined || editor === null) return;
    pasteEditorRef.current = editor;
    void refreshPasteEntries()
      .then(async (entries) => {
        if (!active || entries === null) return;
        // Recover an open typing burst exactly as captured. Capture-time actor
        // and determination are immutable; a newly available identity never
        // gets substituted for the author observed before the restart.
        for (const entry of entries) {
          if (
            entry.status === "capturing" &&
            entry.sourceKind === "direct_entry"
          ) {
            const resolution = resolveCoworkPasteAnchor(
              editor.state.doc,
              entry.anchor,
            );
            if (resolution.kind !== "unique") continue;
            directEntryBurstRef.current = {
              idempotencyKey: entry.idempotencyKey,
              from: resolution.from,
              to: resolution.to,
              capturedAt: entry.capturedAt,
              determination: entry.determination,
              ...(entry.capturedActor === undefined
                ? {}
                : { capturedActor: entry.capturedActor }),
            };
            if (active) await finalizeDirectEntryBurstRef.current();
            if (active && resolvedProvenanceActor !== undefined) {
              // Actor recovery can race the actorless finalizer already in
              // flight. Re-check the durable row after that promise settles;
              // the ref now points at the callback bound to the recovered
              // actor, so one follow-up closes rather than stranding it.
              const stillCapturing = (
                await resolvedPasteProvenanceOutbox.list()
              ).some(
                (candidate) =>
                  candidate.id === entry.id && candidate.status === "capturing",
              );
              if (stillCapturing) {
                await finalizeDirectEntryBurstRef.current();
              }
            }
            continue;
          }
          if (
            entry.status === "ready" ||
            entry.status === "retryable_failure"
          ) {
            await attemptPasteProvenance(entry.id);
          }
        }
        if (active) await refreshPasteEntries();
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
    resolvedProvenanceActor,
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
    directEntryCaptureEnabledRef.current = false;
    pasteEditorRef.current = editor;
    // Persistence starts before seeding so a brand-new document's seed is
    // pushed through R4 as its first human-origin update (SP-2 load-order).
    persistence.start();
    if (seedWhenEmpty) {
      editor.commands.setContent(seedContent);
    }
    stopCapturingLoadTimeIds(editor);
    directEntryCaptureEnabledRef.current = true;
    onReadyRef.current?.({ editor, dom: editor.view.dom as HTMLElement });
  }, [editor, persistence, seedContent, seedWhenEmpty]);

  useEffect(() => {
    if (editor === null) return;
    const finishBurst = (): void => {
      void closeDirectEntryBurst();
    };
    editor.view.dom.addEventListener("blur", finishBurst);
    return () => {
      editor.view.dom.removeEventListener("blur", finishBurst);
    };
  }, [closeDirectEntryBurst, editor]);

  useEffect(() => {
    if (activeLens === "provenance") {
      void closeDirectEntryBurst();
    }
  }, [activeLens, closeDirectEntryBurst]);

  useEffect(() => {
    return () => {
      directEntryCaptureEnabledRef.current = false;
      // Do not start network work after the editor disappears. The latest
      // mutable burst is already in both the synchronous recovery journal and
      // durable outbox; the next mount closes it against a live editor/head.
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
        if (capture.status === "capturing") {
          await resolvedPasteProvenanceOutbox.upsertCapture(capture);
        } else {
          await resolvedPasteProvenanceOutbox.append(capture);
        }
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
      if (
        entry.sourceKind === "direct_entry" &&
        (entry.status === "stale_target" || entry.status === "terminal_failure")
      ) {
        // The server explicitly rejected the frozen target, so this visible
        // user retry starts a new attempt: resolve the captured quote against
        // the current document, compact a fresh head, and freeze a new key.
        await resolvedPasteProvenanceOutbox.retarget(
          entry.id,
          pasteIdempotencyKey(),
          entry.determination,
        );
        await attemptPasteProvenance(entry.id);
        continue;
      }
      if (entry.status === "ready" || entry.status === "retryable_failure") {
        await attemptPasteProvenance(entry.id);
      }
    }
  }, [
    attemptPasteProvenance,
    refreshPasteEntries,
    resolvedPasteProvenanceOutbox,
    volatilePasteCaptures,
  ]);

  const recordSelectedProvenance = useCallback(
    async (
      anchor: RangeQuoteAnchor,
      determination: CoworkProvenanceDetermination,
    ): Promise<void> => {
      if (
        resolvedPasteProvenanceOutbox === undefined ||
        resolvedProvenanceActor === undefined
      ) {
        throw new Error("Provenance recording is not ready yet.");
      }
      if (!coworkProvenanceExactWithinLimit(anchor.exact)) {
        throw new Error("The selected passage is too large to record at once.");
      }
      await closeDirectEntryBurst();
      const manualKey = JSON.stringify(anchor);
      let entries = await resolvedPasteProvenanceOutbox.list();
      const existingId = manualRecordEntryRef.current.get(manualKey);
      let entry =
        existingId === undefined
          ? undefined
          : entries.find((candidate) => candidate.id === existingId);
      if (existingId !== undefined && entry === undefined) {
        manualRecordEntryRef.current.delete(manualKey);
      }
      if (entry === undefined) {
        // Selection-dialog ownership is intentionally in-memory, while an
        // actorless typing capture is durable. Reassociate the same exact
        // selector after a reload/session recovery so explicit confirmation
        // completes that row instead of appending an overlapping duplicate.
        entry = entries.find(
          (candidate) =>
            candidate.sourceKind === "legacy" &&
            candidate.status === "awaiting_determination" &&
            candidate.frozenRequest === undefined &&
            JSON.stringify(candidate.anchor) === manualKey,
        );
        if (entry !== undefined) {
          manualRecordEntryRef.current.set(manualKey, entry.id);
        }
      }

      if (
        entry?.sourceKind === "legacy" &&
        entry.frozenRequest !== undefined &&
        JSON.stringify(entry.frozenRequest.attestation) !==
          JSON.stringify(determination)
      ) {
        throw new Error(
          "This pending request is already frozen. Retry using the provenance choice you previously confirmed; changing it requires a new selection after the pending request is resolved.",
        );
      }

      if (entry === undefined) {
        if (
          entries.some(
            (candidate) =>
              candidate.sourceKind === "direct_entry" ||
              (candidate.sourceKind === "legacy" &&
                candidate.status !== "awaiting_determination"),
          )
        ) {
          throw new Error(
            "Recording recent typing. Try again when provenance finishes refreshing.",
          );
        }
        const refreshed = await provenanceProvider?.refresh();
        const currentEditor = pasteEditorRef.current;
        if (refreshed?.state === "ready" && currentEditor !== null) {
          const selected = resolveCoworkPasteAnchor(
            currentEditor.state.doc,
            anchor,
          );
          const overlapsRecordedTarget =
            selected.kind === "unique" &&
            (refreshed.data.documentDefault?.target.currentness === "current" ||
              refreshed.data.spans.some((target) => {
                if (target.span === null) return false;
                const resolved = resolveCoworkPasteAnchor(
                  currentEditor.state.doc,
                  target.span,
                );
                return (
                  resolved.kind === "unique" &&
                  resolved.from < selected.to &&
                  resolved.to > selected.from
                );
              }));
          if (overlapsRecordedTarget) {
            throw new Error(
              "Provenance was already recorded for this selection. Refresh the selection to inspect it.",
            );
          }
        }
        entry = await resolvedPasteProvenanceOutbox.append({
          anchor,
          idempotencyKey: pasteIdempotencyKey(),
          substantial: false,
          capturedActor: resolvedProvenanceActor,
          sourceKind: "legacy",
          basisKind: "user_attestation",
          determination,
          capturedAt: new Date().toISOString(),
          passageExcerpt: coworkPastePassageExcerpt(anchor.exact),
          ...(persistence.docSha256.length === 0
            ? {}
            : {
                capturedBaseStructuredHeadSha256: persistence.docSha256,
              }),
          status: "ready",
        });
        manualRecordEntryRef.current.set(manualKey, entry.id);
      } else if (
        (entry.status === "stale_target" ||
          entry.status === "terminal_failure") &&
        entry.failure?.code === COWORK_PROVENANCE_TARGET_CHANGED
      ) {
        entry = await resolvedPasteProvenanceOutbox.retarget(
          entry.id,
          pasteIdempotencyKey(),
          determination,
        );
      } else if (
        entry.status === "awaiting_determination" &&
        entry.sourceKind === "legacy"
      ) {
        await resolvedPasteProvenanceOutbox.updateDetermination(
          entry.id,
          determination,
        );
        entry = await resolvedPasteProvenanceOutbox.markReady(
          entry.id,
          determination,
          "user_attestation",
          resolvedProvenanceActor,
        );
      }
      await refreshPasteEntries();
      await attemptPasteProvenance(entry.id);
      entries = await resolvedPasteProvenanceOutbox.list();
      const pending = entries.find((candidate) => candidate.id === entry.id);
      if (pending !== undefined) {
        throw new Error(
          pending.failure?.message ?? "Provenance could not be recorded.",
        );
      }
      manualRecordEntryRef.current.delete(manualKey);
    },
    [
      attemptPasteProvenance,
      closeDirectEntryBurst,
      persistence,
      provenanceProvider,
      refreshPasteEntries,
      resolvedPasteProvenanceOutbox,
      resolvedProvenanceActor,
    ],
  );

  return (
    <>
      <EditorContent editor={editor} className="wb-cowork-editor__content" />
      {localFileActionError !== null ? (
        <InlineAlert tone="warning" role="alert">
          <span>{localFileActionError}</span>
          <Button size="small" onClick={() => setLocalFileActionError(null)}>
            Dismiss
          </Button>
        </InlineAlert>
      ) : null}
      {oversizedPasteBlocked ? (
        <InlineAlert tone="warning" role="alert">
          <span>{oversizedPasteProvenanceError()}</span>
          <Button size="small" onClick={() => setOversizedPasteBlocked(false)}>
            Dismiss
          </Button>
        </InlineAlert>
      ) : null}
      {visibleOutboxError !== null ? (
        <InlineAlert tone="danger" role="alert">
          <span>{visibleOutboxError}</span>
          <Button
            size="small"
            onClick={() =>
              void retryPasteOutbox().catch(() => {
                setOutboxError(outboxPasteProvenanceError());
              })
            }
          >
            Retry provenance storage
          </Button>
        </InlineAlert>
      ) : null}
      {dismissedDeterminationEntries.length > 0 &&
      visiblePasteEntry === null ? (
        <InlineAlert tone="info" role="status">
          <span>
            {String(dismissedDeterminationEntries.length)} pending{" "}
            {dismissedDeterminationEntries.length === 1
              ? "attribution needs"
              : "attributions need"}{" "}
            your decision.
          </span>
          <Button
            size="small"
            onClick={() =>
              setDismissedPasteIds((current) => {
                const next = new Set(current);
                const first = dismissedDeterminationEntries[0]?.id;
                if (first !== undefined) next.delete(first);
                return next;
              })
            }
          >
            Review pending attribution
          </Button>
        </InlineAlert>
      ) : null}
      {editor !== null &&
      activeLens !== "provenance" &&
      !readOnly &&
      onFeedbackCaptured !== undefined &&
      documentId !== undefined ? (
        <CoworkFeedbackAffordance
          editor={editor}
          documentId={documentId}
          storeId={storeId}
          onCaptured={onFeedbackCaptured}
          transport={feedbackTransport}
        />
      ) : null}
      {editor !== null &&
      activeLens === "provenance" &&
      provenanceProvider !== undefined &&
      resolvedProvenanceActor !== undefined &&
      onProvenanceSelectionAction !== undefined ? (
        <CoworkProvenanceSelectionAffordance
          editor={editor}
          active={provenanceSelectionActionsActive ?? true}
          provider={provenanceProvider}
          currentUserIdentity={resolvedProvenanceActor}
          readOnly={readOnly}
          inputProvenancePending={inputProvenancePending}
          onRecord={recordSelectedProvenance}
          onAction={onProvenanceSelectionAction}
        />
      ) : null}
      {visiblePasteEntry !== null && resolvedProvenanceActor !== undefined ? (
        <CoworkProvenanceDeterminationDialog
          key={`${String(visiblePasteEntry.id)}:${resolvedProvenanceActor.identity_status}:${resolvedProvenanceActor.ref}`}
          value={visiblePasteEntry.determination}
          currentUserIdentity={resolvedProvenanceActor}
          title={
            visibleDirectEntryRecovery
              ? "Recent typing needs attribution"
              : undefined
          }
          passageExcerpt={visiblePasteEntry.passageExcerpt}
          passageLabel={
            visibleDirectEntryRecovery ? "Recent passage" : undefined
          }
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
            visibleDirectEntryRecovery
              ? "Choose who created this passage and, if AI contributed, whether a person reviewed it."
              : pasteEntries.length > 1
              ? `Record its authorship and review status. ${String(pasteEntries.length - 1)} more pasted ${pasteEntries.length === 2 ? "passage is" : "passages are"} waiting.`
              : undefined
          }
          confirmLabel={
            visibleDirectEntryRecovery
              ? "Confirm attribution"
              : visiblePasteActorChanged
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
            visibleDirectEntryRecovery ||
            visiblePasteRequiresExplicitDetermination ||
            visiblePasteEntry.status === "stale_target" ||
            visiblePasteEntry.status === "terminal_failure"
              ? "Keep for later"
              : undefined
          }
          onChange={(value) =>
            updatePasteDetermination(visiblePasteEntry, value)
          }
          onConfirm={(value) => settlePasteEntry(visiblePasteEntry, value)}
          onClose={() => {
            if (
              visibleDirectEntryRecovery ||
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
      coworkPasteProvenanceOutboxKey(props.storeId, props.documentId),
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
  // A writable frame cannot precede the capture-time actor lookup. Otherwise
  // normal typing can begin actorless and be split when that initial lookup
  // resolves. A genuine lookup failure still restores actorless editing so
  // the durable/manual recovery contract remains available.
  const editorReadOnly =
    effectiveReadOnly ||
    (provenanceEnabled && provenanceActorState === "loading");
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
  const readOnlyRef = useRef(editorReadOnly);
  readOnlyRef.current = editorReadOnly;

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
        if (!readOnly && !readOnlyRef.current)
          editorRef.current?.setEditable(true);
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
            message:
              "Co-work could not verify the current Markdown file, so Save is disabled.",
            retryable: false,
          },
          canRetry: false,
        });
        return;
      }
      const checkedGeneration = editGeneration.current;
      const rendered = serializeCoworkEditorMarkdown(editor, props.document);
      const renderedSha256 = await sha256Hex(
        new TextEncoder().encode(rendered),
      );
      publishMaterializationState({
        kind:
          editGeneration.current === checkedGeneration &&
          renderedSha256 === fileSha256
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
        const isConflict =
          error.status === 409 ||
          [
            "snapshot_mismatch",
            "stale_structured_head",
            "update_tail_present",
            "stale_file",
            "missing_file",
            "open_flags_block_save",
            "external_write_race",
            "recovery_required",
          ].includes(error.code);
        const canRetry = isConflict
          ? retryableConflict(error)
          : error.retryable;
        const canReplayExactRequest =
          !isConflict &&
          editGeneration.current === capturedGeneration &&
          (error.code === "network_error" ||
            (error.status !== undefined &&
              error.status >= 500 &&
              error.retryable));
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
        for (
          let stabilityAttempt = 0;
          stabilityAttempt < 2;
          stabilityAttempt += 1
        ) {
          const generation = editGeneration.current;
          if (
            persistence.lastError !== null ||
            persistence.pendingBatchCount > 0
          ) {
            await persistence.retry();
          }
          await persistence.flush();
          assertCanonicalCoworkEditorState(editor);
          const compacted = await persistence.compact();
          const renderedMarkdown = serializeCoworkEditorMarkdown(
            editor,
            props.document,
          );
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
  }, [
    persistence,
    props.document,
    publishMaterializationState,
    settleMaterialize,
  ]);

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
        props.onProvenancePersistenceSettled?.(receipt.structuredHeadSha256);
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
          if (
            persistence.lastError !== null ||
            persistence.pendingBatchCount > 0
          ) {
            await persistence.retry();
          }
          await persistence.flush();
          const editor = editorRef.current;
          if (editor === null) {
            throw new Error(
              "The document is still loading. Try again in a moment.",
            );
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
          if (receipt === null || generation !== editGeneration.current)
            continue;
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
          throw new Error(
            "The document is still loading. Try again in a moment.",
          );
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
        props.onProvenancePersistenceSettled?.(response.structured_head_sha256);
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
          throw new Error(
            "The document is still loading. Try again in a moment.",
          );
        }
        const wasEditable = liveEditor.isEditable;
        liveEditor.setEditable(false);
        try {
          if (
            persistence.lastError !== null ||
            persistence.pendingBatchCount > 0
          ) {
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
  }, [
    props.document,
    props.onLocalProvenanceEdit,
    publishMaterializationState,
  ]);

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
    if (props.provenanceActor !== undefined && provenanceActorAttempt === 0) {
      setResolvedProvenanceActor(props.provenanceActor);
      setProvenanceActorState("ready");
      return () => {
        active = false;
      };
    }
    setResolvedProvenanceActor(undefined);
    setProvenanceActorState("loading");
    const resolveActor = async (): Promise<CoworkProvenanceActorIdentity> => {
      const identity =
        provenanceActorAttempt > 0
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
        if (!provenanceEnabled) return;
        if (!identity.authenticated) {
          // A session can expire or be replaced while the editor remains
          // mounted. Never keep presenting its cached actor as current; direct
          // entry remains durable and actorless until trusted recovery.
          setResolvedProvenanceActor(undefined);
          setProvenanceActorState("error");
          return;
        }
        if (provenanceActorState !== "loading") {
          // Authenticated publications can represent a newly delivered cookie
          // (and potentially a different enrolled actor), so re-read the
          // canonical actor even when the prior actor looked healthy.
          requestProvenanceActorRefresh();
        }
      }),
    [provenanceActorState, provenanceEnabled, requestProvenanceActorRefresh],
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
        <InlineAlert
          tone="danger"
          role="alert"
          className="wb-cowork-editor__hydration-error"
        >
          <strong>Document couldn’t be opened.</strong>
          <span>{hydrationError}</span>
          <Button
            size="small"
            onClick={() => setAttempt((current) => current + 1)}
          >
            Try again
          </Button>
        </InlineAlert>
      ) : hydration !== undefined ? (
        <MountedBridgeEditor
          {...props}
          readOnly={editorReadOnly}
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

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  JsonValue,
  WidgetIntent,
  WidgetRendererProps,
} from "../../../dashboard/contributions/contracts";
import { createWidgetIntent } from "../../../widget-library/shared";
import { Button, InlineAlert } from "../../../ui";
import type {
  CoworkDocumentSummary,
  CoworkFolderSummary,
  CoworkViewModel,
  CoworkWorkspaceInput,
} from "../contracts";
import { COWORK_INTENTS } from "../contracts";
import {
  CoworkDocumentBar,
  coworkReimportLocalBlockedReason,
} from "../documents/CoworkDocumentBar";
import { CoworkDocumentLifecycleDialog } from "../documents/CoworkDocumentLifecycleDialog";
import { CoworkLocalDiscardDialog } from "../documents/CoworkLocalDiscardDialog";
import { CoworkDocumentPicker } from "../documents/CoworkDocumentPicker";
import { CoworkLauncher } from "../documents/CoworkLauncher";
import { CoworkReimportDialog } from "../documents/CoworkReimportDialog";
import { CoworkRetirementDialog } from "../documents/CoworkRetirementDialog";
import type {
  CoworkScratchPromotionContent,
  CoworkScratchPromotionHandle,
} from "../editor/CoworkEditorPane";
import {
  CoworkHttpClient,
  type CoworkReimportReceipt,
} from "../providers/CoworkHttpClient";
import { asCoworkApiError, coworkErrorMessage } from "../providers/errors";
import type { CoworkSyncStatus } from "../persistence/CoworkYdocPersistence";
import type {
  CoworkMaterializationController,
  CoworkMaterializationState,
} from "../materialization/contracts";
import { CoworkDocumentSession } from "../session/CoworkDocumentSession";
import { finishScratchPromotion } from "../scratch/promotion";
import {
  CoworkDemoWorkspace,
  CoworkScratchWorkspace,
  resolveFixtureMode,
} from "../surface/CoworkWorkspaceSurface";

const normalizeModel = (input: CoworkWorkspaceInput): CoworkViewModel => ({
  folders: input.folders ?? [],
  folderChooser: input.folderChooser ?? { available: true, kind: "host" },
  folderSelection: input.folderSelection ?? { kind: "none" },
  activeFolderStoreId: input.activeFolderStoreId ?? null,
  catalog: input.catalog ?? {
    status: "empty",
    documents: [],
    refreshedAt: null,
    error: null,
  },
  scratches: input.scratches ?? [],
  routeTarget: input.routeTarget ?? { kind: "launcher", storeId: null },
  activeSession: input.activeSession ?? { kind: "none" },
  openingTarget: input.openingTarget ?? null,
  navigationError: input.navigationError ?? null,
  readOnly: input.readOnly ?? false,
  document: input.document,
});

// External Markdown edits happen outside the React tree, so a quiet catalog refresh is the
// bridge that makes drift review discoverable while a document remains open. Focus and
// visibility events provide prompt feedback; the low-frequency poll covers a long-lived,
// visible tab without turning the catalog route into a hot loop.
const CATALOG_REFRESH_INTERVAL_MS = 60_000;

const activeFolder = (model: CoworkViewModel): CoworkFolderSummary | null =>
  model.folderSelection.kind === "initialized"
    ? model.folderSelection.folder
    : model.folders.find((folder) => folder.storeId === model.activeFolderStoreId) ?? null;

type LifecycleDialog = "create" | "register" | "repair" | null;

interface PendingFolderAction {
  readonly action: string;
  readonly storeId?: string;
}

interface PendingScratchPromotion {
  readonly scratchId: string;
  readonly title: string;
  readonly content: CoworkScratchPromotionContent;
}

interface ReimportContext {
  readonly storeId: string;
  readonly document: CoworkDocumentSummary;
}

interface PendingReimportReconciliation extends ReimportContext {
  readonly receipt: CoworkReimportReceipt;
}

export const reimportReceiptMatchesDocument = (
  receipt: CoworkReimportReceipt,
  document: CoworkDocumentSummary,
): boolean =>
  document.driftState === "clean" &&
  document.currentFileSha256 === receipt.sourceSha256 &&
  document.snapshotSha256 === receipt.snapshotSha256 &&
  document.structuredHeadSha256 === receipt.structuredHeadSha256;

export default function CoworkWorkspaceWidget({
  input,
  emit,
  presentation,
}: WidgetRendererProps<CoworkWorkspaceInput>) {
  const model = useMemo(() => normalizeModel(input), [input]);
  const client = useMemo(() => new CoworkHttpClient(), []);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [dialog, setDialog] = useState<LifecycleDialog>(null);
  const [repairDocument, setRepairDocument] = useState<CoworkDocumentSummary | null>(null);
  const [reimportOpen, setReimportOpen] = useState(false);
  const [reimportContext, setReimportContext] = useState<ReimportContext | null>(null);
  const [pendingReimport, setPendingReimport] =
    useState<PendingReimportReconciliation | null>(null);
  const [reimportRetryBusy, setReimportRetryBusy] = useState(false);
  const [retirementOpen, setRetirementOpen] = useState(false);
  const [localDiscardOpen, setLocalDiscardOpen] = useState(false);
  const [folderNotice, setFolderNotice] = useState<string | null>(null);
  const [pendingFolderAction, setPendingFolderAction] =
    useState<PendingFolderAction | null>(null);
  const folderActionBusy = useRef(false);
  const folderActionEpoch = useRef(0);
  const [localNotice, setLocalNotice] = useState<string | null>(null);
  const [syncStatus, setSyncStatus] = useState<CoworkSyncStatus>();
  const [documentSessionEpoch, setDocumentSessionEpoch] = useState(0);
  const [materializationState, setMaterializationState] =
    useState<CoworkMaterializationState>();
  const materializationController = useRef<CoworkMaterializationController | null>(null);
  const [promotionBusy, setPromotionBusy] = useState(false);
  const [pendingPromotion, setPendingPromotion] =
    useState<PendingScratchPromotion | null>(null);
  const promotionHandle = useRef<CoworkScratchPromotionHandle | null>(null);
  const scratchTouchCooldown = useRef<ReturnType<typeof globalThis.setTimeout> | null>(
    null,
  );
  const [promotionReady, setPromotionReady] = useState(false);
  const receivePromotionHandle = useCallback(
    (handle: CoworkScratchPromotionHandle | null): void => {
      promotionHandle.current = handle;
      setPromotionReady(handle !== null);
    },
    [],
  );
  const folder = activeFolder(model);

  const fixtureOverride =
    import.meta.env.DEV && typeof window !== "undefined"
      ? new URLSearchParams(window.location.search).get("cowork_fixture")
      : null;
  const fixtureMode = resolveFixtureMode(
    input.sessionQuality,
    input.document?.documentId,
    undefined,
    fixtureOverride,
  );
  const isDemo = import.meta.env.DEV && fixtureMode === "demo";

  const dispatch = useCallback(
    async (intentType: string, payload: JsonValue): Promise<void> => {
      const intent = createWidgetIntent(presentation, intentType, payload) as WidgetIntent;
      const result = await emit(intent);
      if (result.status !== "accepted") {
        throw new Error(result.message ?? "Co-work could not complete that action.");
      }
    },
    [emit, presentation],
  );

  const folderAction = useCallback(
    (action: string, payload: Record<string, JsonValue> = {}) =>
      dispatch(COWORK_INTENTS.folderSelect, { action, ...payload }),
    [dispatch],
  );
  const runFolderAction = useCallback(
    (action: string, payload: Record<string, JsonValue> = {}): void => {
      if (action === "cancel") {
        folderActionEpoch.current += 1;
        folderActionBusy.current = false;
        setPendingFolderAction(null);
        setFolderNotice(null);
        void folderAction(action, payload).catch((error: unknown) => {
          setFolderNotice(
            coworkErrorMessage(
              asCoworkApiError(error),
              "Co-work couldn’t return to the previous Folder.",
            ),
          );
        });
        return;
      }
      if (folderActionBusy.current) return;
      const epoch = ++folderActionEpoch.current;
      folderActionBusy.current = true;
      setPendingFolderAction({
        action,
        ...(typeof payload.storeId === "string" ? { storeId: payload.storeId } : {}),
      });
      setFolderNotice(null);
      void folderAction(action, payload)
        .catch((error: unknown) => {
          setFolderNotice(
            coworkErrorMessage(
              asCoworkApiError(error),
              "Co-work couldn’t open that Folder. Try again.",
            ),
          );
        })
        .finally(() => {
          if (folderActionEpoch.current !== epoch) return;
          folderActionBusy.current = false;
          setPendingFolderAction(null);
        });
    },
    [folderAction],
  );

  const openDocument = useCallback(
    async (document: CoworkDocumentSummary): Promise<void> => {
      const storeId = model.activeFolderStoreId;
      if (storeId === null) throw new Error("Choose a Folder before opening a document.");
      await dispatch(COWORK_INTENTS.documentOpen, {
        storeId,
        documentId: document.documentId,
      });
    },
    [dispatch, model.activeFolderStoreId],
  );

  const openLifecycleDialog = (next: Exclude<LifecycleDialog, null>): void => {
    if (folder === null) {
      runFolderAction("choose");
      return;
    }
    setPickerOpen(false);
    if (next !== "repair") setRepairDocument(null);
    setDialog(next);
  };

  const beginScratchPromotion = useCallback(async (): Promise<void> => {
    if (model.activeSession.kind !== "scratch" || promotionBusy) return;
    const handle = promotionHandle.current;
    if (handle === null) {
      setLocalNotice("The document is still loading. Try Save document again in a moment.");
      return;
    }
    setPromotionBusy(true);
    setLocalNotice("Preparing document…");
    try {
      const content = await handle.exportContent();
      setPendingPromotion({
        scratchId: model.activeSession.scratchId,
        title: model.activeSession.title,
        content,
      });
      if (folder === null) {
        setLocalNotice(null);
        setFolderNotice(null);
        try {
          await folderAction("choose");
        } catch (folderError) {
          setFolderNotice(
            coworkErrorMessage(
              asCoworkApiError(folderError),
              "Co-work couldn’t open that Folder. Try again.",
            ),
          );
          return;
        }
      } else {
        setLocalNotice(null);
        setPickerOpen(false);
        setDialog("create");
      }
    } catch (promotionError) {
      setLocalNotice(
        coworkErrorMessage(
          asCoworkApiError(promotionError),
          "Co-work couldn’t prepare this document.",
        ),
      );
    } finally {
      setPromotionBusy(false);
    }
  }, [folder, folderAction, model.activeSession, promotionBusy]);

  useEffect(() => {
    if (pendingPromotion === null || folder === null || dialog !== null) return;
    setLocalNotice(null);
    setPickerOpen(false);
    setDialog("create");
  }, [dialog, folder, pendingPromotion]);

  const openPromotedDocument = useCallback(
    async (document: CoworkDocumentSummary): Promise<void> => {
      const promotion = pendingPromotion;
      if (promotion === null) {
        await openDocument(document);
        return;
      }
      await finishScratchPromotion(
        document,
        promotion.scratchId,
        openDocument,
        async (scratchId) =>
          dispatch(COWORK_INTENTS.scratchClose, {
            retire: true,
            scratchId,
          }),
      );
      setPendingPromotion(null);
    },
    [dispatch, openDocument, pendingPromotion],
  );

  const reconcileCommittedReimport = useCallback(
    async (pending: PendingReimportReconciliation): Promise<void> => {
      await dispatch(COWORK_INTENTS.documentReload, {
        storeId: pending.storeId,
        documentId: pending.document.documentId,
      });
    },
    [dispatch],
  );

  const session = model.activeSession;
  const markLocalEdit = useCallback((): void => {
    if (session.kind !== "scratch" || scratchTouchCooldown.current !== null) return;
    const scratchId = session.scratchId;
    void dispatch(COWORK_INTENTS.scratchTouch, { scratchId }).catch(() => undefined);
    scratchTouchCooldown.current = globalThis.setTimeout(() => {
      scratchTouchCooldown.current = null;
    }, 5_000);
  }, [dispatch, session]);

  useEffect(() => {
    if (
      pendingReimport === null ||
      session.kind !== "registered" ||
      session.storeId !== pendingReimport.storeId ||
      session.document.documentId !== pendingReimport.document.documentId ||
      !reimportReceiptMatchesDocument(pendingReimport.receipt, session.document)
    ) {
      return;
    }
    // The provider keeps the same route and session identity during its atomic reload.
    // Remount only after the final snapshot proves it is the committed replacement;
    // otherwise the old drift prop can seed a fresh controller with a stale Markdown
    // conflict even though the server is already clean.
    setDocumentSessionEpoch((current) => current + 1);
    setPendingReimport(null);
    setReimportRetryBusy(false);
  }, [pendingReimport, session]);
  const sessionKey =
    session.kind === "registered"
      ? `${session.storeId}:${session.document.documentId}:${documentSessionEpoch}`
      : session.kind === "scratch"
        ? `scratch:${session.scratchId}`
        : session.kind;
  useEffect(() => {
    if (scratchTouchCooldown.current !== null) {
      globalThis.clearTimeout(scratchTouchCooldown.current);
      scratchTouchCooldown.current = null;
    }
    setSyncStatus(session.kind === "none" ? undefined : "hydrating");
    setMaterializationState(undefined);
    materializationController.current = null;
    promotionHandle.current = null;
    setPromotionReady(false);
    return () => {
      if (scratchTouchCooldown.current !== null) {
        globalThis.clearTimeout(scratchTouchCooldown.current);
        scratchTouchCooldown.current = null;
      }
    };
  }, [session.kind, sessionKey]);
  const documentRouteBlocksSession =
    model.openingTarget !== null || model.routeTarget.kind === "unavailable";
  const folderLifecycleActive =
    model.folderSelection.kind !== "none" &&
    model.folderSelection.kind !== "initialized" &&
    model.folderSelection.kind !== "choosing" &&
    model.folderSelection.kind !== "inspecting";
  const folderErrorNotice =
    folderNotice ??
    (session.kind === "none" || model.navigationError === null
      ? null
      : coworkErrorMessage(
          model.navigationError,
          "Co-work couldn’t open that Folder.",
        ));
  const sessionIsInert = documentRouteBlocksSession || folderLifecycleActive;
  const backgroundCatalogRefreshPaused =
    sessionIsInert ||
    pickerOpen ||
    dialog !== null ||
    reimportOpen ||
    retirementOpen ||
    localDiscardOpen;

  useEffect(() => {
    if (session.kind !== "registered" || backgroundCatalogRefreshPaused) return;

    let active = true;
    let inFlight = false;
    let queued = false;

    const refreshCatalog = (): void => {
      if (!active || document.visibilityState === "hidden") return;
      if (inFlight) {
        queued = true;
        return;
      }

      inFlight = true;
      void dispatch(COWORK_INTENTS.catalogRefresh, {})
        // This is background freshness, not a user-requested mutation. Keep the current
        // document usable on a transient failure; the next focus/poll will retry.
        .catch(() => undefined)
        .finally(() => {
          inFlight = false;
          if (!active || !queued) return;
          queued = false;
          refreshCatalog();
        });
    };
    const onFocus = (): void => refreshCatalog();
    const onVisibilityChange = (): void => {
      if (document.visibilityState === "visible") refreshCatalog();
    };

    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibilityChange);
    const interval = window.setInterval(
      refreshCatalog,
      CATALOG_REFRESH_INTERVAL_MS,
    );

    return () => {
      active = false;
      queued = false;
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.clearInterval(interval);
    };
  }, [backgroundCatalogRefreshPaused, dispatch, session.kind, sessionKey]);

  const saveMarkdown = useCallback(async (): Promise<void> => {
    const controller = materializationController.current;
    if (controller === null) {
      setLocalNotice("The document is still loading. Try Save again in a moment.");
      return;
    }
    setLocalNotice(null);
    await controller.save();
  }, []);

  const retrySync = useCallback(async (): Promise<void> => {
    if (session.kind === "scratch") {
      await promotionHandle.current?.retryDeviceSave();
      return;
    }
    await materializationController.current?.retrySync();
  }, [session.kind]);

  useEffect(() => {
    if (
      session.kind !== "registered" ||
      sessionIsInert ||
      pickerOpen ||
      dialog !== null
    ) {
      return;
    }
    const onKeyDown = (event: KeyboardEvent): void => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey || event.key.toLowerCase() !== "s") {
        return;
      }
      event.preventDefault();
      void saveMarkdown();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [dialog, pickerOpen, saveMarkdown, session.kind, sessionIsInert]);

  const launcher = (
    <CoworkLauncher
      model={model}
      pendingFolderAction={pendingFolderAction}
      onRetryInspection={() => {
        if (
          (model.folderSelection.kind === "none" ||
            model.folderSelection.kind === "initialized") &&
          model.activeFolderStoreId !== null &&
          model.catalog.error !== null
        ) {
          void dispatch(COWORK_INTENTS.catalogRefresh, {}).catch((error: unknown) => {
            setFolderNotice(
              coworkErrorMessage(
                asCoworkApiError(error),
                "Co-work couldn’t load the documents.",
              ),
            );
          });
        } else {
          runFolderAction("retry");
        }
      }}
      onCancelInspection={() => runFolderAction("cancel")}
      onInitialize={() => runFolderAction("initialize")}
      onOpenFolder={(storeId) => runFolderAction("open", { storeId })}
      onOpenDocument={(document) => void openDocument(document)}
      onCreate={() => openLifecycleDialog("create")}
      onRegister={() => openLifecycleDialog("register")}
      onOpenLocalDocument={(scratch) =>
        void dispatch(COWORK_INTENTS.scratchOpen, { scratchId: scratch.scratchId })
      }
      onNewDocument={() => void dispatch(COWORK_INTENTS.scratchOpen, {})}
    />
  );

  if (isDemo) {
    return <CoworkDemoWorkspace model={{ ...model, document: input.document }} />;
  }

  return (
    <div className="wb-cowork-lifecycle">
      <CoworkDocumentBar
        model={model}
        folderActionBusy={pendingFolderAction !== null}
        onChooseFolder={() => runFolderAction("choose")}
        onOpenPicker={() => {
          if (folder === null) runFolderAction("choose");
          else setPickerOpen(true);
        }}
        onCreate={() => openLifecycleDialog("create")}
        onCloseSession={() =>
          void dispatch(
            session.kind === "scratch"
              ? COWORK_INTENTS.scratchClose
              : COWORK_INTENTS.documentClose,
            {},
          )
        }
        onPromoteScratch={() => void beginScratchPromotion()}
        promotionBusy={promotionBusy}
        promotionReady={promotionReady}
        syncStatus={syncStatus}
        materializationState={materializationState}
        onSaveMarkdown={() => void saveMarkdown()}
        onRetrySync={() => void retrySync()}
        onReviewExternalChanges={() => {
          if (
            session.kind === "registered" &&
            coworkReimportLocalBlockedReason(syncStatus, materializationState) === null
          ) {
            setReimportContext({
              storeId: session.storeId,
              document: session.document,
            });
            setReimportOpen(true);
          }
        }}
        onRemoveDocument={() => setRetirementOpen(true)}
        onDiscardLocalDocument={() => setLocalDiscardOpen(true)}
      />

      {localNotice !== null ? (
        <InlineAlert tone="info" className="wb-cowork-lifecycle__notice">
          {localNotice}
        </InlineAlert>
      ) : null}
      {folderErrorNotice !== null &&
      !folderLifecycleActive &&
      (session.kind !== "none" || model.navigationError === null) ? (
        <InlineAlert tone="danger" className="wb-cowork-lifecycle__notice" role="alert">
          {folderErrorNotice}
        </InlineAlert>
      ) : null}
      {pendingReimport !== null && !reimportOpen ? (
        <InlineAlert tone="warning" className="wb-cowork-lifecycle__notice">
          <strong>The replacement is saved.</strong>
          <span>Co-work still needs to reopen it on this device.</span>
          <Button
            size="small"
            disabled={reimportRetryBusy}
            onClick={() => {
              setReimportRetryBusy(true);
              setLocalNotice(null);
              void reconcileCommittedReimport(pendingReimport)
                .catch((error: unknown) => {
                  setLocalNotice(
                    coworkErrorMessage(
                      asCoworkApiError(error),
                      "Co-work could not reopen the replacement.",
                    ),
                  );
                })
                .finally(() => setReimportRetryBusy(false));
            }}
          >
            {reimportRetryBusy ? "Reopening…" : "Retry reopen"}
          </Button>
        </InlineAlert>
      ) : null}

      <div className="wb-cowork-lifecycle__body">
        {pendingReimport !== null ? (
          <section className="wb-cowork-open-state" role="status">
            <p>Reopening document…</p>
          </section>
        ) : session.kind === "registered" ? (
          <div
            className={`wb-cowork-lifecycle__session${sessionIsInert ? " is-inert" : ""}`}
            aria-hidden={sessionIsInert || undefined}
            inert={sessionIsInert || undefined}
          >
            <CoworkDocumentSession
              key={`${session.storeId}:${session.document.documentId}:${documentSessionEpoch}`}
              storeId={session.storeId}
              document={session.document}
              feedbackCapture={folder?.documentSurface.feedbackCapture ?? false}
              onSyncStatus={setSyncStatus}
              onMaterializationState={setMaterializationState}
              onMaterializationController={(controller) => {
                materializationController.current = controller;
              }}
              onMaterialized={() => {
                void dispatch(COWORK_INTENTS.catalogRefresh, {});
              }}
            />
          </div>
        ) : session.kind === "scratch" ? (
          <div
            className={`wb-cowork-lifecycle__session${sessionIsInert ? " is-inert" : ""}`}
            aria-hidden={sessionIsInert || undefined}
            inert={sessionIsInert || undefined}
          >
            <CoworkScratchWorkspace
              key={session.scratchId}
              scratchId={session.scratchId}
              onPromotionHandle={receivePromotionHandle}
              onSyncStatus={setSyncStatus}
              onLocalEdit={markLocalEdit}
            />
          </div>
        ) : !folderLifecycleActive && model.routeTarget.kind === "launcher" ? launcher : null}

        {folderLifecycleActive ? (
          <div className="wb-cowork-open-state wb-cowork-open-state--folder">{launcher}</div>
        ) : null}

        {!folderLifecycleActive && documentRouteBlocksSession ? (
          <section className="wb-cowork-open-state" role={model.navigationError === null ? "status" : "alert"}>
            {model.navigationError === null ? (
              <p>Opening document…</p>
            ) : (
              <>
                <h2>This document could not be opened</h2>
                <p>
                  {coworkErrorMessage(
                    model.navigationError,
                    "This document couldn’t be opened.",
                  )}
                </p>
                <div className="wb-cowork-launcher__actions">
                  <Button onClick={() => {
                    if (model.routeTarget.kind === "unavailable") {
                      void dispatch(COWORK_INTENTS.documentOpen, {
                        storeId: model.routeTarget.storeId,
                        documentId: model.routeTarget.documentId,
                      });
                    }
                  }}>Retry</Button>
                  <Button onClick={() => void dispatch(COWORK_INTENTS.documentClose, {})}>
                    Return to Folder
                  </Button>
                </div>
              </>
            )}
          </section>
        ) : null}
      </div>

      {pickerOpen && folder !== null ? (
        <CoworkDocumentPicker
          folder={folder}
          documents={model.catalog.documents}
          currentDocumentId={session.kind === "registered" ? session.document.documentId : undefined}
          onClose={() => setPickerOpen(false)}
          onOpen={openDocument}
          onCreate={() => openLifecycleDialog("create")}
          onRegister={() => openLifecycleDialog("register")}
          onRepair={(document) => {
            setPickerOpen(false);
            setRepairDocument(document);
            setDialog("repair");
          }}
          onChangeFolder={() => {
            setPickerOpen(false);
            runFolderAction("choose");
          }}
        />
      ) : null}

      {dialog !== null && folder !== null ? (
        <CoworkDocumentLifecycleDialog
          mode={dialog}
          folder={folder}
          client={client}
          initialTitle={pendingPromotion?.title}
          initialContent={pendingPromotion?.content}
          repairDocument={repairDocument ?? undefined}
          onClose={() => {
            setDialog(null);
            setPendingPromotion(null);
            setRepairDocument(null);
          }}
          onOpened={pendingPromotion === null ? openDocument : openPromotedDocument}
        />
      ) : null}

      {reimportOpen && reimportContext !== null ? (
        <CoworkReimportDialog
          storeId={reimportContext.storeId}
          document={reimportContext.document}
          client={client}
          localBlockedReason={
            pendingReimport === null
              ? coworkReimportLocalBlockedReason(syncStatus, materializationState)
              : null
          }
          onClose={() => {
            setReimportOpen(false);
            setReimportContext(null);
          }}
          onReimported={async (receipt) => {
            const target: PendingReimportReconciliation = {
              ...reimportContext,
              receipt,
            };
            setPendingReimport(target);
            // A replacement snapshot was built in a fresh Y.Doc. Never merge it into the old
            // live CRDT. The provider's leave barrier first pauses/disposes the old session;
            // the receipt-matching effect above mounts the replacement only after refreshed
            // catalog metadata proves the final clean server pointer reached this widget.
            await reconcileCommittedReimport(target);
            setReimportOpen(false);
            setReimportContext(null);
          }}
        />
      ) : null}

      {retirementOpen && session.kind === "registered" ? (
        <CoworkRetirementDialog
          storeId={session.storeId}
          document={session.document}
          client={client}
          onClose={() => setRetirementOpen(false)}
          onRetired={async () => {
            setRetirementOpen(false);
            await dispatch(COWORK_INTENTS.documentClose, {});
            await dispatch(COWORK_INTENTS.catalogRefresh, {});
          }}
        />
      ) : null}

      {localDiscardOpen && session.kind === "scratch" ? (
        <CoworkLocalDiscardDialog
          title={session.title}
          onClose={() => setLocalDiscardOpen(false)}
          onDiscard={async () => {
            await dispatch(COWORK_INTENTS.scratchClose, {
              retire: true,
              scratchId: session.scratchId,
            });
            setLocalDiscardOpen(false);
          }}
        />
      ) : null}
    </div>
  );
}

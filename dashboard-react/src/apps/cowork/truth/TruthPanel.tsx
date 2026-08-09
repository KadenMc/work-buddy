import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  Button as AriaButton,
  Menu,
  MenuItem,
  MenuTrigger,
  Popover,
  type Key,
} from "react-aria-components";

import { HelpTarget, type HelpContent } from "../../../dashboard/help";
import type { ChatExecutionControl } from "../../../widget-library/chat";

import type {
  TruthClaimDecisionRequest,
  TruthAnalysisCandidate,
  TruthAnalysisCandidateDecisionRequest,
  TruthAnalysisProvider,
  TruthClaimFilter,
  TruthEditorIntegration,
  TruthMutationReceipt,
  TruthRailProvider,
  TruthSelectionCapture,
  TruthScrollIntegration,
  TruthViewScope,
} from "./contracts";
import { TruthClaimCard } from "./TruthClaimCard";
import { TruthClaimDetails } from "./TruthClaimDetails";
import { TruthAnalysisReview } from "./TruthAnalysisReview";
import { TruthSelectionComposer } from "./TruthSelectionComposer";
import {
  createPersistedTruthStore,
  TruthStore,
  useTruthState,
} from "./store";
import { useTruthClaimDetail, useTruthData } from "./useTruthData";
import { useTruthAnalysis } from "./useTruthAnalysis";
import "./styles.css";

const FILTERS: readonly {
  readonly value: TruthClaimFilter;
  readonly label: string;
  readonly count: "all" | "facts" | "proposed" | "needsReview" | "challenged" | "unconnected";
}[] = [
  { value: "all", label: "All", count: "all" },
  { value: "facts", label: "Facts", count: "facts" },
  { value: "proposed", label: "Proposed", count: "proposed" },
  { value: "needs_review", label: "Needs review", count: "needsReview" },
  { value: "challenged", label: "Challenged", count: "challenged" },
  { value: "unconnected", label: "Unconnected", count: "unconnected" },
];

const fallbackError = (cause: unknown): string =>
  cause instanceof Error && cause.message.trim().length > 0
    ? cause.message
    : "Truth could not complete that change.";

const MANUAL_HELP: HelpContent = {
  summary: "Add or connect a claim yourself.",
  details:
    "Use these manual actions when you already know the exact claim and how the selected passage expresses it.",
};

export interface TruthPanelProps {
  readonly provider: TruthRailProvider;
  readonly storeId: string;
  readonly documentId: string;
  /** Share this store with Review's TruthAttentionFeed to open exact claims. */
  readonly store?: TruthStore;
  readonly storage?: Storage;
  readonly editor?: TruthEditorIntegration;
  readonly analysis?: {
    readonly provider: TruthAnalysisProvider;
    readonly execution?: ChatExecutionControl;
  };
  readonly scroll?: TruthScrollIntegration;
  readonly readOnly?: boolean;
  /** False detaches the persisted scroll ref while the containing tab is hidden. */
  readonly active?: boolean;
}

export function TruthPanel({
  provider,
  storeId,
  documentId,
  store: injectedStore,
  storage,
  editor,
  analysis,
  scroll,
  readOnly: forcedReadOnly = false,
  active = true,
}: TruthPanelProps) {
  const [ownedStore] = useState(() => {
    if (injectedStore !== undefined) return injectedStore;
    const targetStorage =
      storage ?? (typeof window === "undefined" ? null : window.localStorage);
    return targetStorage === null
      ? new TruthStore()
      : createPersistedTruthStore(targetStorage, storeId, documentId);
  });
  const store = injectedStore ?? ownedStore;
  const scope = useTruthState(store, (state) => state.scope);
  const filter = useTruthState(store, (state) => state.filter);
  const selectedClaimId = useTruthState(store, (state) => state.selectedClaimId);
  const composer = useTruthState(store, (state) => state.composer);
  const { data, status, error, reload } = useTruthData(provider, { scope, filter });
  const analysisData = useTruthAnalysis(analysis?.provider ?? null);
  const claimDetail = useTruthClaimDetail(provider, selectedClaimId);
  const [decisionBusy, setDecisionBusy] = useState(false);
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [analysisStarting, setAnalysisStarting] = useState(false);
  const analysisStartingRef = useRef(false);
  const [analysisError, setAnalysisError] = useState<string | null>(null);
  const [busyCandidateId, setBusyCandidateId] = useState<string | null>(null);
  const [candidateErrorId, setCandidateErrorId] = useState<string | null>(null);
  const [candidateError, setCandidateError] = useState<string | null>(null);
  const [scrollAttachmentEpoch, setScrollAttachmentEpoch] = useState(0);
  const bodyElementRef = useRef<HTMLElement | null>(null);
  const panelElementRef = useRef<HTMLElement | null>(null);
  const moreButtonRef = useRef<HTMLButtonElement | null>(null);
  const scrollContainerRef = scroll?.scrollContainerRef;
  const detachScrollContainer = scroll?.onScrollContainerWillDetach;
  const listMode = composer === null && selectedClaimId === null;
  const attachScrollContainer = useCallback(
    (element: HTMLElement | null): void => {
      bodyElementRef.current = element;
      void scrollAttachmentEpoch;
      if (active && listMode) scrollContainerRef?.(element);
    },
    [active, listMode, scrollAttachmentEpoch, scrollContainerRef],
  );

  // Detail and composer views are transient drill-ins. They begin at the top
  // and never overwrite the persisted list position; returning to the list
  // reattaches its binding and restores the exact browsing context.
  useLayoutEffect(() => {
    if (!listMode && bodyElementRef.current !== null) {
      bodyElementRef.current.scrollTop = 0;
    }
  }, [composer, listMode, selectedClaimId]);

  useEffect(() => {
    // Own only claim focus established by this panel. A null selection can
    // coexist with an expression focus handed across a document navigation;
    // clearing that here would erase the destination context immediately.
    if (!active || selectedClaimId === null) return undefined;
    editor?.focusClaim?.(selectedClaimId);
    return () => editor?.focusClaim?.(null);
  }, [active, editor, selectedClaimId]);

  useEffect(() => {
    if (!active) editor?.focusAnalysisPassage?.(null);
  }, [active, editor]);

  const readOnly = forcedReadOnly || data?.readOnly === true;
  const canModify =
    !forcedReadOnly && data?.capabilities.canModify === true;
  const analysisExecution = analysis?.execution?.snapshot?.selection;
  const analysisProviderCapability =
    analysisExecution === undefined
      ? null
      : analysisData.capabilities?.providers.find(
          (item) => item.providerId === analysisExecution.providerId,
        ) ?? null;
  const requiredCostControl = analysisData.capabilities?.requiredCostControl ?? null;
  const analysisCostAttested =
    analysisProviderCapability?.analysisAvailable === true &&
    analysisProviderCapability.appliesToAllModels &&
    analysisProviderCapability.costControl.enforcementClass === "hard_ceiling" &&
    analysisProviderCapability.costControl.ceilingUsdPerWorkerSession !== null &&
    requiredCostControl?.enforcementClass === "hard_ceiling" &&
    analysisProviderCapability.costControl.ceilingUsdPerWorkerSession <=
      requiredCostControl.maximumUsdPerModelSession;
  const analysisRunActive =
    analysisData.run?.status === "queued" ||
    analysisData.run?.status === "running";
  const pendingAnalysisCandidateCount =
    analysisData.run?.candidates.filter(
      (candidate) => candidate.status === "pending",
    ).length ?? 0;
  const canAnalyze =
    canModify &&
    pendingAnalysisCandidateCount === 0 &&
    data?.capabilities.canObserve === true &&
    analysisData.status === "ready" &&
    analysisData.capabilitiesStatus === "ready" &&
    analysisCostAttested &&
    editor?.captureAnalysisTarget !== undefined &&
    analysis !== undefined &&
    analysisExecution !== undefined &&
    analysis.execution?.status === "ready" &&
    analysis.execution.currentAvailable;
  const counts = data?.counts ?? {
    all: 0,
    facts: 0,
    proposed: 0,
    needsReview: 0,
    challenged: 0,
    unconnected: 0,
  };
  const allowedClaimKinds = data?.capabilities.allowedClaimKinds ?? [];
  const composerCaptureRef = useRef<Promise<TruthSelectionCapture> | null>(null);
  const controlsLocked =
    composer !== null || selectedClaimId !== null || decisionBusy;
  const analyzeBlockedReason = controlsLocked
    ? "Return to the claims list first."
    : analysisStarting || analysisRunActive
      ? "The current analysis is still running."
      : analysisData.status === "loading"
        ? "Truth history is still loading."
        : analysisData.status === "error"
          ? "Reload Truth history before starting another analysis."
          : readOnly
            ? "Truth analysis is unavailable in read-only mode."
            : !canModify
              ? data?.capabilities.mutationUnavailableReason ??
                "Truth analysis is unavailable for this document."
              : pendingAnalysisCandidateCount > 0
                ? "Add, connect, or skip the prepared claims before analyzing another passage."
                : data?.capabilities.canObserve !== true
                  ? "Truth context is not available yet."
                  : editor?.captureAnalysisTarget === undefined
                    ? "The editor cannot capture a passage right now."
                    : analysisExecution === undefined ||
                        analysis?.execution?.status !== "ready" ||
                        !analysis.execution.currentAvailable
                      ? "Choose an available account model first."
                      : analysisData.capabilitiesStatus === "loading"
                        ? "Truth analysis availability is still loading."
                        : analysisData.capabilitiesStatus === "error"
                          ? "Truth analysis availability could not be verified. Try again."
                          : analysisProviderCapability === null
                            ? "Truth analysis is not available for this provider."
                            : !analysisProviderCapability.analysisAvailable
                              ? analysisProviderCapability.unavailableReason ??
                                "Truth analysis is not available for this provider."
                              : !analysisCostAttested
                                ? "Truth analysis requires a provider-enforced hard spending ceiling."
                      : null;
  const attestedWorkerCeiling = analysisCostAttested
    ? analysisProviderCapability?.costControl.ceilingUsdPerWorkerSession ?? null
    : null;
  const analyzeHelp: HelpContent = {
    summary: "Prepare claims from selected prose.",
    details: `${
      analysisExecution === undefined
        ? "Choose an available account model, then analyze one exact selected passage."
        : `The exact selected passage and bounded existing Truth context are sent to ${analysisExecution.providerLabel} · ${analysisExecution.modelLabel}. Analysis may run bounded web searches for factual grounding; results report what was actually searched.${
            attestedWorkerCeiling === null
                ? ""
              : ` A ${attestedWorkerCeiling.toLocaleString("en-US", {
                  style: "currency",
                  currency: "USD",
                  minimumFractionDigits: 2,
                  maximumFractionDigits: 2,
                })} limit is enforced on the selected account model for this analysis. Web research may incur separate provider charges that Co-work cannot cap yet.`
          }`
    }${analyzeBlockedReason === null ? "" : ` ${analyzeBlockedReason}`}`,
  };

  useEffect(() => {
    if (composer === null) composerCaptureRef.current = null;
  }, [composer]);

  const changeScope = (next: TruthViewScope): void => {
    if (next === scope) return;
    detachScrollContainer?.();
    setScrollAttachmentEpoch((value) => value + 1);
    store.setScope(next);
    window.requestAnimationFrame(() => {
      if (bodyElementRef.current !== null) bodyElementRef.current.scrollTop = 0;
    });
  };
  const changeFilter = (next: TruthClaimFilter): void => {
    if (next === filter) return;
    detachScrollContainer?.();
    setScrollAttachmentEpoch((value) => value + 1);
    store.setFilter(next);
    window.requestAnimationFrame(() => {
      if (bodyElementRef.current !== null) bodyElementRef.current.scrollTop = 0;
    });
  };
  const closeTransientView = (): void => {
    const closingClaimId = selectedClaimId;
    setDecisionError(null);
    store.selectClaim(null);
    if (closingClaimId !== null) {
      window.requestAnimationFrame(() => {
        const controls = panelElementRef.current?.querySelectorAll<HTMLElement>(
          "[data-truth-claim-id]",
        );
        const target = controls === undefined
          ? undefined
          : [...controls].find(
              (element) => element.dataset.truthClaimId === closingClaimId,
            );
        target?.focus();
      });
    }
  };

  const openComposer = (mode: "propose" | "connect"): void => {
    if (editor === undefined) return;
    const capture = editor.captureSelection();
    // The composer subscribes after this event update commits. Mark an
    // immediate planning rejection handled during that short handoff while
    // preserving the original promise for the composer's visible error state.
    void capture.catch(() => undefined);
    composerCaptureRef.current = capture;
    store.openComposer(mode);
  };

  const analyzePassage = (): void => {
    if (
      analysis === undefined ||
      editor?.captureAnalysisTarget === undefined ||
      analysisExecution === undefined ||
      !canAnalyze ||
      analyzeBlockedReason !== null ||
      analysisStartingRef.current ||
      analysisRunActive
    ) {
      return;
    }
    analysisStartingRef.current = true;
    setAnalysisStarting(true);
    setAnalysisError(null);
    const captureAnalysisTarget = editor.captureAnalysisTarget;
    void Promise.resolve()
      .then(() => captureAnalysisTarget("current_selection"))
      .then((frozen) =>
        analysis.provider.start({
          targetChoice: "current_selection",
          capture: frozen,
          execution: {
            providerId: analysisExecution.providerId,
            modelId: analysisExecution.modelId,
            providerLabel: analysisExecution.providerLabel,
            modelLabel: analysisExecution.modelLabel,
          },
        }),
      )
      .then((run) => {
        analysisData.adopt(run);
        setAnnouncement("Truth analysis started.");
      })
      .catch((cause: unknown) => setAnalysisError(fallbackError(cause)))
      .finally(() => {
        analysisStartingRef.current = false;
        setAnalysisStarting(false);
      });
  };

  const decideAnalysisCandidate = async (
    request: TruthAnalysisCandidateDecisionRequest,
  ): Promise<void> => {
    if (analysis === undefined || busyCandidateId !== null) return;
    setBusyCandidateId(request.candidateId);
    setCandidateErrorId(null);
    setCandidateError(null);
    try {
      const receipt = await analysis.provider.decideCandidate(request);
      const next = await analysis.provider.loadRun(request.analysisRunId);
      analysisData.adopt(next);
      if (receipt.candidateStatus === "saved") {
        setAnnouncement(
          request.decision === "connect_existing"
            ? "Passage connected to the existing claim."
            : "Claim added as proposed.",
        );
        reload();
      } else {
        setAnnouncement("Candidate claim skipped.");
      }
    } catch (cause: unknown) {
      setCandidateErrorId(request.candidateId);
      setCandidateError(fallbackError(cause));
      throw cause;
    } finally {
      setBusyCandidateId(null);
    }
  };

  const focusAnalysisCandidate = useCallback(
    (candidate: TruthAnalysisCandidate | null): void =>
      editor?.focusAnalysisPassage?.(
        candidate === null
          ? null
          : {
              candidateId: candidate.candidateId,
              selector: candidate.expression.selector,
            },
      ),
    [editor],
  );
  const revealAnalysisCandidate = useCallback(
    (candidate: TruthAnalysisCandidate): void =>
      editor?.revealAnalysisPassage?.({
        candidateId: candidate.candidateId,
        selector: candidate.expression.selector,
      }),
    [editor],
  );

  const cancelComposer = (): void => {
    composerCaptureRef.current = null;
    store.closeComposer();
    window.requestAnimationFrame(() => {
      moreButtonRef.current?.focus();
    });
  };

  const decide = async (request: TruthClaimDecisionRequest): Promise<void> => {
    setDecisionBusy(true);
    setDecisionError(null);
    try {
      await provider.decideClaim(request);
      setAnnouncement("Claim updated.");
      reload();
      claimDetail.reload();
    } catch (cause: unknown) {
      setDecisionError(fallbackError(cause));
      throw cause;
    } finally {
      setDecisionBusy(false);
    }
  };

  const completeComposer = (receipt: TruthMutationReceipt): void => {
    const message = !receipt.expressionCreated
      ? composer === "propose"
        ? "No change was needed: the selected passage was already connected to the matching claim."
        : "No change was needed: the selected passage was already connected to that claim."
      : composer === "propose"
        ? receipt.claimCreated
          ? "Claim proposed and connected to the selected passage."
          : "The matching claim was connected to the selected passage."
        : "Claim connected to the selected passage.";
    setAnnouncement(message);
    composerCaptureRef.current = null;
    store.closeComposer();
    reload();
    if (receipt.claimId !== null) store.selectClaim(receipt.claimId);
  };

  let content;
  if (composer !== null) {
    content = editor === undefined ? (
      <div className="wb-cowork-truth__state is-error" role="alert">
        <p>The editor cannot capture a selection right now.</p>
        <button type="button" onClick={() => store.closeComposer()}>Back to Truth</button>
      </div>
    ) : (
      <TruthSelectionComposer
        key={composer}
        mode={composer}
        provider={provider}
        editor={editor}
        initialCapture={composerCaptureRef.current ?? undefined}
        allowedClaimKinds={allowedClaimKinds}
        onCancel={cancelComposer}
        onComplete={completeComposer}
      />
    );
  } else if (selectedClaimId !== null) {
    content = claimDetail.status === "loading" ? (
      <p className="wb-cowork-truth__state" role="status">Loading claim…</p>
    ) : claimDetail.status === "error" || claimDetail.detail === null ? (
      <div className="wb-cowork-truth__state is-error" role="alert">
        <p>{claimDetail.error ?? "This claim is unavailable."}</p>
        <div>
          <button type="button" onClick={claimDetail.reload}>Try again</button>
          <button type="button" onClick={closeTransientView}>Back to claims</button>
        </div>
      </div>
    ) : (
      <TruthClaimDetails
        claim={claimDetail.detail}
        readOnly={readOnly}
        busy={decisionBusy}
        error={decisionError}
        refreshError={claimDetail.error}
        active={active}
        onClose={closeTransientView}
        onRetryRefresh={claimDetail.reload}
        onRevealPassage={editor?.revealPassage}
        onDecide={decide}
      />
    );
  } else if (status === "loading") {
    content = <p className="wb-cowork-truth__state" role="status">Loading Truth…</p>;
  } else if (status === "error") {
    content = (
      <div className="wb-cowork-truth__state is-error" role="alert">
        <p>{error ?? "Truth could not be loaded."}</p>
        <button type="button" onClick={reload}>Try again</button>
      </div>
    );
  } else if (data === null || data.claims.length === 0) {
    const filtered = filter !== "all";
    const unconnectedRequiresFolder =
      scope === "document" && filter === "unconnected";
    content = (
      <div className="wb-cowork-truth__empty">
        <h3>
          {unconnectedRequiresFolder
            ? "Unconnected claims are in Folder"
            : filtered
            ? `No ${FILTERS.find((item) => item.value === filter)?.label.toLocaleLowerCase() ?? "claims"}`
            : scope === "document"
              ? "No claims are connected to this document"
              : "No claims in this folder"}
        </h3>
        <p>
          {unconnectedRequiresFolder
            ? "This document view contains only claims connected to its prose."
            : filtered
            ? "No claims in this view match the selected filter."
            : scope === "document"
              ? "Select a passage and choose Analyze passage to prepare claims and source findings for review."
              : "Analyze a passage in a document to begin, or add one manually."}
        </p>
        {unconnectedRequiresFolder || filtered ? (
          <div className="wb-cowork-truth__empty-actions">
            {unconnectedRequiresFolder ? (
            <button type="button" onClick={() => changeScope("folder")}>
              Show Folder
            </button>
            ) : (
            <button type="button" onClick={() => changeFilter("all")}>
              Clear filter
            </button>
            )}
          </div>
        ) : null}
      </div>
    );
  } else {
    content = (
      <ul className="wb-cowork-truth__claim-list">
        {data.claims.map((claim) => (
          <TruthClaimCard
            key={claim.claimId}
            claim={claim}
            onSelect={() => store.selectClaim(claim.claimId)}
            onRevealPassage={editor?.revealPassage}
          />
        ))}
      </ul>
    );
  }

  const analysisContent =
    listMode && analysisData.run !== null ? (
      <TruthAnalysisReview
        run={analysisData.run}
        busyCandidateId={busyCandidateId}
        errorCandidateId={candidateErrorId}
        error={candidateError}
        canModify={canModify}
        allowedClaimKinds={allowedClaimKinds}
        onFocusCandidate={focusAnalysisCandidate}
        onRevealCandidate={revealAnalysisCandidate}
        onDecide={decideAnalysisCandidate}
      />
    ) : null;

  return (
    <section ref={panelElementRef} className="wb-cowork-truth" aria-label="Truth">
      <h2 className="wb-cowork-truth__visually-hidden">Truth</h2>
      <div className="wb-cowork-truth__controls">
        <div className="wb-cowork-truth__scope" role="group" aria-label="Truth view">
          <button type="button" disabled={controlsLocked} aria-pressed={scope === "document"} onClick={() => changeScope("document")}>This document</button>
          <button type="button" disabled={controlsLocked} aria-pressed={scope === "folder"} onClick={() => changeScope("folder")}>Folder</button>
        </div>
        {analysis !== undefined || canModify ? (
          <div className="wb-cowork-truth__actions">
            {analysis === undefined ? null : (
              <HelpTarget content={analyzeHelp} placement="bottom end">
                <button
                  type="button"
                  className="is-primary"
                  aria-disabled={analyzeBlockedReason === null ? undefined : true}
                  onClick={analyzePassage}
                >
                  {analysisStarting || analysisRunActive
                    ? "Analyzing…"
                    : "Analyze passage"}
                </button>
              </HelpTarget>
            )}
            {canModify ? <div className="wb-cowork-truth__more">
              <MenuTrigger>
                <HelpTarget
                  content={MANUAL_HELP}
                  placement="bottom end"
                  reactAriaComposite
                >
                  <AriaButton
                    ref={moreButtonRef}
                    isDisabled={controlsLocked || analysisStarting}
                  >
                    Add manually
                  </AriaButton>
                </HelpTarget>
                <Popover
                  className="wb-popover wb-cowork-truth__more-popover"
                  placement="bottom end"
                >
                  <Menu
                    className="wb-cowork-truth__more-menu"
                    aria-label="Manual Truth actions"
                    onAction={(key: Key) =>
                      openComposer(key === "connect" ? "connect" : "propose")
                    }
                  >
                    <MenuItem
                      id="propose"
                      className="wb-cowork-truth__more-item"
                      isDisabled={editor === undefined}
                    >
                      Add claim manually
                    </MenuItem>
                    <MenuItem
                      id="connect"
                      className="wb-cowork-truth__more-item"
                      isDisabled={editor === undefined}
                    >
                      Connect selection manually
                    </MenuItem>
                  </Menu>
                </Popover>
              </MenuTrigger>
            </div> : null}
          </div>
        ) : null}
      </div>
      <div className="wb-cowork-truth__filters" role="group" aria-label="Filter claims">
        {FILTERS.map((item) => (
          <button key={item.value} type="button" disabled={controlsLocked} aria-pressed={filter === item.value} onClick={() => changeFilter(item.value)}>
            <span>{item.label}</span><span className="wb-cowork-truth__count">{counts[item.count]}</span>
          </button>
        ))}
      </div>
      {readOnly ? <p className="wb-cowork-truth__read-only" role="status">Truth is read-only. Claims, evidence, and history remain available.</p> : null}
      {!readOnly && !canModify && data?.capabilities.mutationUnavailableReason !== null && data?.capabilities.mutationUnavailableReason !== undefined ? (
        <p className="wb-cowork-truth__read-only" role="status">
          {data.capabilities.mutationUnavailableReason} Claims, evidence, and history remain available.
        </p>
      ) : null}
      {status === "ready" && error !== null && data !== null ? (
        <div className="wb-cowork-truth__refresh-warning" role="status">
          <span>{error}</span>
          <button type="button" onClick={reload}>Try again</button>
        </div>
      ) : null}
      {analysisError === null ? null : (
        <p className="wb-cowork-truth__error" role="alert">
          {analysisError}
        </p>
      )}
      {analysisData.error !== null ? (
        <div className="wb-cowork-truth__refresh-warning" role="status">
          <span>{analysisData.error}</span>
          <button type="button" onClick={analysisData.reload}>Try again</button>
        </div>
      ) : null}
      <p
        className="wb-cowork-truth__visually-hidden"
        role="status"
        aria-live="polite"
      >
        {announcement}
      </p>
      <div
        className="wb-cowork-truth__body"
        ref={attachScrollContainer}
        data-truth-scroll-container="true"
      >
        {analysisContent}
        {content}
      </div>
    </section>
  );
}

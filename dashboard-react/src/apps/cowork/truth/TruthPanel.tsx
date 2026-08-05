import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import type {
  TruthClaimDecisionRequest,
  TruthClaimFilter,
  TruthEditorIntegration,
  TruthMutationReceipt,
  TruthRailProvider,
  TruthScrollIntegration,
  TruthViewScope,
} from "./contracts";
import { TruthClaimCard } from "./TruthClaimCard";
import { TruthClaimDetails } from "./TruthClaimDetails";
import { TruthSelectionComposer } from "./TruthSelectionComposer";
import {
  createPersistedTruthStore,
  TruthStore,
  useTruthState,
} from "./store";
import { useTruthClaimDetail, useTruthData } from "./useTruthData";
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

export interface TruthPanelProps {
  readonly provider: TruthRailProvider;
  readonly storeId: string;
  readonly documentId: string;
  /** Share this store with Review's TruthAttentionFeed to open exact claims. */
  readonly store?: TruthStore;
  readonly storage?: Storage;
  readonly editor?: TruthEditorIntegration;
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
  const claimDetail = useTruthClaimDetail(provider, selectedClaimId);
  const [decisionBusy, setDecisionBusy] = useState(false);
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [scrollAttachmentEpoch, setScrollAttachmentEpoch] = useState(0);
  const bodyElementRef = useRef<HTMLElement | null>(null);
  const panelElementRef = useRef<HTMLElement | null>(null);
  const proposeButtonRef = useRef<HTMLButtonElement | null>(null);
  const connectButtonRef = useRef<HTMLButtonElement | null>(null);
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

  const readOnly = forcedReadOnly || data?.readOnly === true;
  const canModify =
    !forcedReadOnly && data?.capabilities.canModify === true;
  const counts = data?.counts ?? {
    all: 0,
    facts: 0,
    proposed: 0,
    needsReview: 0,
    challenged: 0,
    unconnected: 0,
  };
  const allowedClaimKinds = data?.capabilities.allowedClaimKinds ?? [];
  const controlsLocked =
    composer !== null || selectedClaimId !== null || decisionBusy;

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
    store.openComposer(mode);
  };

  const cancelComposer = (): void => {
    const returningTo = composer;
    store.closeComposer();
    window.requestAnimationFrame(() => {
      (returningTo === "connect"
        ? connectButtonRef.current
        : proposeButtonRef.current)?.focus();
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
              ? "Connect selected prose to an existing claim, or propose a new one."
              : "Propose a claim from selected prose when you are ready."}
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

  return (
    <section ref={panelElementRef} className="wb-cowork-truth" aria-label="Truth">
      <h2 className="wb-cowork-truth__visually-hidden">Truth</h2>
      <div className="wb-cowork-truth__controls">
        <div className="wb-cowork-truth__scope" role="group" aria-label="Truth view">
          <button type="button" disabled={controlsLocked} aria-pressed={scope === "document"} onClick={() => changeScope("document")}>This document</button>
          <button type="button" disabled={controlsLocked} aria-pressed={scope === "folder"} onClick={() => changeScope("folder")}>Folder</button>
        </div>
        {canModify ? (
          <div className="wb-cowork-truth__actions">
            <button ref={proposeButtonRef} type="button" disabled={editor === undefined || controlsLocked} onClick={() => openComposer("propose")}>Propose from selection</button>
            <button ref={connectButtonRef} type="button" disabled={editor === undefined || controlsLocked} onClick={() => openComposer("connect")}>Connect selection</button>
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
        {content}
      </div>
    </section>
  );
}

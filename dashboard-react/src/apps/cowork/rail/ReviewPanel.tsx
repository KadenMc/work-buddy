/**
 * The Review tab. Composes the drift strip, the filter lens, the stream or queue
 * mode, the mark bar for the current target, the read-only inspector, and the
 * sitting submit. It reads its local state from the RailStore through selectors
 * (so a card re-renders only on its own slice) and its review data from the
 * provider seam. The staged sitting survives a reload through the draft
 * persistence, and a dirty sitting arms the route-change guard.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefCallback,
} from "react";

import { Inspector } from "./Inspector";
import { VerificationAttentionFeed } from "./VerificationAttentionFeed";
import { FilterLens } from "./FilterLens";
import { MarkBar, type MarkBarTarget } from "./MarkBar";
import { QueueView, type QueueBindings } from "./QueueView";
import { RailDriftStrip } from "./RailDriftStrip";
import { StreamView } from "./StreamView";
import type {
  CothinkItem,
  EvaluationResult,
  StagedClaimDecision,
  StagedDecision,
  VerificationRecheckIntent,
} from "./contracts";
import { useDraftPersistence, useUnsavedChangesGuard } from "./dirty";
import {
  claimRefMatchesId,
  filterCounts,
  isSelectedItem,
  visibleItems,
  type RailItem,
} from "./items";
import type { ReviewAnchorController, ReviewRailProvider } from "./provider";
import { isDirty, type RailSelectionKind, type RailStore } from "./store";
import { useReviewData } from "./useReviewData";
import { useRailState } from "./useRailState";
import { RecoverableDecisionApplyError } from "./applyRecovery";
import {
  asCoworkApiError,
  coworkErrorMessage,
} from "../providers/errors";

interface ReviewApplyBlocker {
  readonly proposalId: string;
  readonly message: string;
}

interface ReviewApplyAttempt {
  readonly proposalDecisions: readonly StagedDecision[];
  readonly claimDecisions: readonly StagedClaimDecision[];
  readonly retainedBlockers: readonly ReviewApplyBlocker[];
  /** The complete Review selection when this exact request was confirmed. */
  readonly selectionFingerprint: string;
}

type ReviewApplyNotice =
  | {
      readonly kind: "recovery";
      readonly availableProposalIds: readonly string[];
      readonly blockers: readonly ReviewApplyBlocker[];
      readonly decisionFingerprint: string;
    }
  | {
      readonly kind: "partial";
      readonly appliedCount: number;
      readonly blockers: readonly ReviewApplyBlocker[];
    }
  | {
      readonly kind: "error";
      readonly message: string;
      readonly attempt: ReviewApplyAttempt;
    };

const proposalDecisionTuple = (decision: StagedDecision) => [
  decision.proposalId,
  decision.verb,
  decision.canonicalSha256,
  decision.amendContent ?? null,
  decision.redirectNote ?? null,
  decision.negationText ?? null,
  decision.preferenceText ?? null,
] as const;

const claimDecisionTuple = (decision: StagedClaimDecision) => [
  decision.claimId,
  decision.verb,
  decision.canonicalSha256,
] as const;

const sameProposalDecision = (
  left: StagedDecision | undefined,
  right: StagedDecision,
): boolean =>
  left !== undefined &&
  JSON.stringify(proposalDecisionTuple(left)) ===
    JSON.stringify(proposalDecisionTuple(right));

const decisionsFingerprint = (
  decisions: Readonly<Record<string, StagedDecision>>,
  claimDecisions: Readonly<Record<string, StagedClaimDecision>>,
): string =>
  JSON.stringify({
    proposals: Object.values(decisions)
      .sort((left, right) => left.proposalId.localeCompare(right.proposalId))
      .map(proposalDecisionTuple),
    claims: Object.values(claimDecisions)
      .sort((left, right) => left.claimId.localeCompare(right.claimId))
      .map(claimDecisionTuple),
  });

const mergeBlockers = (
  ...groups: readonly (readonly ReviewApplyBlocker[])[]
): readonly ReviewApplyBlocker[] => {
  const byProposal = new Map<string, ReviewApplyBlocker>();
  for (const group of groups) {
    for (const blocker of group) byProposal.set(blocker.proposalId, blocker);
  }
  return [...byProposal.values()];
};

const quantity = (count: number, singular: string, plural = `${singular}s`) =>
  `${count} ${count === 1 ? singular : plural}`;

export interface ReviewPanelProps {
  readonly provider: ReviewRailProvider;
  readonly store: RailStore;
  readonly documentId: string;
  /** Injectable for tests, defaults to window.localStorage. */
  readonly storage?: Storage;
  readonly scrollContainerRef?: RefCallback<HTMLElement>;
  readonly onScrollContainerWillDetach?: () => void;
  readonly reviewAnchors?: ReviewAnchorController;
  readonly queueBindings?: QueueBindings;
  /** Whether Review is currently visible and may handle global shortcuts. */
  readonly active?: boolean;
  readonly onDiscussCothink?: (
    item: CothinkItem,
  ) => void | Promise<void>;
  readonly onRecheckIntent?: (
    intent: VerificationRecheckIntent,
  ) => void | Promise<void>;
  onSubmitted?(): void;
}

export function ReviewPanel(props: ReviewPanelProps) {
  const { store } = props;
  const storage = props.storage ?? window.localStorage;
  const { data, status, reload } = useReviewData(props.provider);
  const [submitting, setSubmitting] = useState(false);
  const [applyNotice, setApplyNotice] = useState<ReviewApplyNotice | null>(null);
  const [attentionError, setAttentionError] = useState<string | null>(null);
  const [busyAttentionId, setBusyAttentionId] = useState<string | null>(null);
  const pendingRevealRef = useRef<{
    readonly source: ReviewAnchorController;
    readonly id: string;
    readonly kind: RailSelectionKind;
  } | null>(null);

  const filter = useRailState(store, (state) => state.filter);
  const mode = useRailState(store, (state) => state.mode);
  const selectedId = useRailState(store, (state) => state.selectedId);
  const selectedKind = useRailState(store, (state) => state.selectedKind);
  const queueIndex = useRailState(store, (state) => state.queueIndex);
  const decisions = useRailState(store, (state) => state.decisions);
  const claimDecisions = useRailState(store, (state) => state.claimDecisions);
  const inspectorSpanId = useRailState(store, (state) => state.inspectorSpanId);
  const dirty = useRailState(store, isDirty);
  const decisionFingerprint = useMemo(
    () => decisionsFingerprint(decisions, claimDecisions),
    [decisions, claimDecisions],
  );
  const activeRecovery =
    applyNotice?.kind === "recovery" &&
    applyNotice.decisionFingerprint === decisionFingerprint
      ? applyNotice
      : null;
  const activeError =
    applyNotice?.kind === "error" &&
    applyNotice.attempt.selectionFingerprint === decisionFingerprint
      ? applyNotice
      : null;

  useDraftPersistence(store, props.documentId, storage);
  useUnsavedChangesGuard(store, dirty);

  const visible = useMemo(
    () => (data === null ? [] : visibleItems(data, filter)),
    [data, filter],
  );
  const counts = useMemo(
    () => (data === null ? { all: 0, suggestions: 0, flags: 0, claims: 0 } : filterCounts(data)),
    [data],
  );
  const spanByClaim = useMemo(() => {
    const map = new Map<string, string>();
    if (data === null) return map;
    for (const claim of data.claims) {
      const expression = data.expressions.find((candidate) =>
        claimRefMatchesId(candidate.claimRef, claim.claimId),
      );
      if (expression !== undefined) map.set(claim.claimId, expression.spanId);
    }
    return map;
  }, [data]);

  const clampedIndex = Math.min(queueIndex, Math.max(0, visible.length - 1));
  const selectedVisibleItem =
    selectedId === null || selectedKind === null
      ? undefined
      : visible.find((item) =>
          isSelectedItem(item, selectedId, selectedKind),
        );
  const targetItem: RailItem | undefined =
    mode === "queue" ? visible[clampedIndex] : selectedVisibleItem;
  const targetId = targetItem?.id ?? null;
  const targetKind = targetItem?.kind ?? null;

  /*
   * Selection is a kind-qualified rail concern, but the editor owns its visual
   * treatment. Keep the focused decoration in sync without ever hiding the
   * underlying annotations when the card lens changes.
   */
  useEffect(() => {
    const source = props.reviewAnchors;
    if (source === undefined) return;
    if (targetId === null || targetKind === null) {
      pendingRevealRef.current = null;
      source.clearFocusedAnchor();
      return;
    }
    const pending = pendingRevealRef.current;
    const flash =
      pending?.source === source &&
      pending.id === targetId &&
      pending.kind === targetKind;
    pendingRevealRef.current = null;
    source.focusAnchor(
      targetId,
      targetKind,
      flash ? { scroll: true, flash: true } : {},
    );
  }, [props.reviewAnchors, targetId, targetKind]);

  useEffect(
    () => () => {
      props.reviewAnchors?.clearFocusedAnchor();
    },
    [props.reviewAnchors],
  );

  /*
   * Queue focus is itself the selection. In stream mode, a filter or refresh
   * that removes the selected card clears the stale selection and MarkBar.
   */
  useEffect(() => {
    if (data === null) return;
    if (targetItem === undefined) {
      if (selectedId !== null || selectedKind !== null) store.clearSelection();
      return;
    }
    if (
      mode === "queue" &&
      !isSelectedItem(targetItem, selectedId, selectedKind)
    ) {
      store.select(targetItem.id, targetItem.kind);
    }
  }, [
    data,
    mode,
    selectedId,
    selectedKind,
    store,
    targetId,
    targetKind,
    targetItem,
  ]);

  const advanceToNextUndecided = useCallback(
    (fromIndex: number) => {
      for (let offset = 1; offset <= visible.length; offset += 1) {
        const next = fromIndex + offset;
        if (next >= visible.length) break;
        const item = visible[next];
        const decided =
          item.kind === "claim"
            ? claimDecisions[item.id] !== undefined
            : decisions[item.id] !== undefined;
        if (!decided) {
          store.setQueueIndex(next);
          store.select(item.id, item.kind);
          return;
        }
      }
    },
    [visible, decisions, claimDecisions, store],
  );

  const stageProposal = useCallback(
    (decision: StagedDecision) => {
      if (submitting) return;
      setApplyNotice(null);
      store.stageDecision(decision);
      if (mode === "queue") advanceToNextUndecided(clampedIndex);
    },
    [store, mode, advanceToNextUndecided, clampedIndex, submitting],
  );

  const stageClaim = useCallback(
    (decision: StagedClaimDecision) => {
      if (submitting) return;
      setApplyNotice(null);
      store.stageClaimDecision(decision);
      if (mode === "queue") advanceToNextUndecided(clampedIndex);
    },
    [store, mode, advanceToNextUndecided, clampedIndex, submitting],
  );

  const navigate = useCallback(
    (delta: number) => {
      const next = Math.min(
        Math.max(0, clampedIndex + delta),
        Math.max(0, visible.length - 1),
      );
      store.setQueueIndex(next);
      const item = visible[next];
      if (item !== undefined) store.select(item.id, item.kind);
    },
    [clampedIndex, visible, store],
  );

  const revealAnchor = useMemo(() => {
    const source = props.reviewAnchors;
    if (source === undefined) return undefined;
    return (id: string, kind: RailSelectionKind) =>
      source.focusAnchor(id, kind, { scroll: true, flash: true });
  }, [props.reviewAnchors]);

  const selectAndRevealAnchor = useMemo(() => {
    const source = props.reviewAnchors;
    if (source === undefined) return undefined;
    return (id: string, kind: RailSelectionKind) => {
      const current = store.getState();
      if (current.selectedId === id && current.selectedKind === kind) {
        source.focusAnchor(id, kind, { scroll: true, flash: true });
        return;
      }
      /*
       * Select first so the card, MarkBar, and editor target agree. The
       * selection effect consumes this marker and performs one flashing focus,
       * instead of immediately replacing the flash with its normal focus.
       */
      pendingRevealRef.current = { source, id, kind };
      store.select(id, kind);
    };
  }, [props.reviewAnchors, store]);

  const submit = useCallback(async (requestedAttempt?: ReviewApplyAttempt) => {
    if (data === null || submitting) return;
    const state = store.getState();
    const selectionFingerprint = decisionsFingerprint(
      state.decisions,
      state.claimDecisions,
    );
    const attempt: ReviewApplyAttempt =
      requestedAttempt ?? {
        proposalDecisions: Object.values(state.decisions).map((decision) => ({
          ...decision,
        })),
        claimDecisions: Object.values(state.claimDecisions).map((decision) => ({
          ...decision,
        })),
        retainedBlockers: [],
        selectionFingerprint,
      };
    if (
      attempt.selectionFingerprint !== selectionFingerprint ||
      (attempt.proposalDecisions.length === 0 &&
        attempt.claimDecisions.length === 0)
    ) {
      return;
    }
    setSubmitting(true);
    setApplyNotice(null);
    try {
      const result = await props.provider.submitSitting({
        baseDocSha256: data.drift.currentFileSha256 ?? "",
        proposalDecisions: attempt.proposalDecisions,
        claimDecisions: attempt.claimDecisions,
      });
      const failed = result.results.filter(
        (item) => item.result === "error" || item.result === "rejected_stale_view",
      );
      for (const item of result.results) {
        if (item.result !== "error" && item.result !== "rejected_stale_view") {
          const submitted = attempt.proposalDecisions.find(
            (decision) => decision.proposalId === item.proposalId,
          );
          if (
            submitted !== undefined &&
            sameProposalDecision(
              store.getState().decisions[item.proposalId],
              submitted,
            )
          ) {
            store.clearDecision(item.proposalId);
          }
        }
      }
      const appliedCount = result.results.length - failed.length;
      const blockers = [
        ...attempt.retainedBlockers,
        ...failed.map((item) => ({
          proposalId: item.proposalId,
          message:
            item.error?.trim() ||
            "This suggestion no longer matches the current document.",
        })),
      ];
      setApplyNotice(
        blockers.length === 0
          ? null
          : { kind: "partial", appliedCount, blockers },
      );
      // Failed prepare items remain staged and durable. Reload re-derives only committed
      // items from the ledger, never from the fact that a submit was attempted.
      reload();
      props.onSubmitted?.();
    } catch (error) {
      if (error instanceof RecoverableDecisionApplyError) {
        const submittedIds = new Set(
          attempt.proposalDecisions.map((decision) => decision.proposalId),
        );
        setApplyNotice({
          kind: "recovery",
          availableProposalIds: error.recovery.availableProposalIds.filter(
            (proposalId) => submittedIds.has(proposalId),
          ),
          blockers: mergeBlockers(
            attempt.retainedBlockers,
            error.recovery.blockers,
          ),
          decisionFingerprint: attempt.selectionFingerprint,
        });
      } else {
        const fallback =
          "Co-work couldn’t confirm that your decisions were applied.";
        setApplyNotice({
          kind: "error",
          message: coworkErrorMessage(asCoworkApiError(error), fallback),
          attempt,
        });
      }
    } finally {
      setSubmitting(false);
    }
  }, [data, props, reload, store, submitting]);

  const revealResult = useCallback(
    (result: EvaluationResult) => {
      if (result.quoteAnchor === null || props.reviewAnchors === undefined) return;
      props.reviewAnchors.focusAnchor(result.resultId, "evaluation_result", {
        scroll: true,
        flash: true,
      });
    },
    [props.reviewAnchors],
  );

  const openCorrection = useCallback(
    (proposalId: string) => {
      store.select(proposalId, "proposal");
      props.reviewAnchors?.focusAnchor(proposalId, "proposal", {
        scroll: true,
        flash: true,
      });
    },
    [props.reviewAnchors, store],
  );

  const actOnCothink = useCallback(
    async (item: CothinkItem, action: "park" | "dismiss") => {
      if (props.provider.actOnCothink === undefined || busyAttentionId !== null) {
        return;
      }
      setBusyAttentionId(item.itemId);
      setAttentionError(null);
      try {
        await props.provider.actOnCothink(
          item.itemId,
          action,
          item.canonicalSha256,
        );
        reload();
      } catch {
        setAttentionError("Co-think couldn’t save that choice.");
      } finally {
        setBusyAttentionId(null);
      }
    },
    [busyAttentionId, props.provider, reload],
  );

  const discussCothink = useCallback(
    async (item: CothinkItem) => {
      if (
        props.provider.discussCothink === undefined ||
        busyAttentionId !== null
      ) {
        return;
      }
      setBusyAttentionId(item.itemId);
      setAttentionError(null);
      try {
        await props.provider.discussCothink(
          item.itemId,
          item.canonicalSha256,
        );
        await props.onDiscussCothink?.(item);
        reload();
      } catch {
        setAttentionError(
          "Co-think couldn’t save that discussion in Chat.",
        );
      } finally {
        setBusyAttentionId(null);
      }
    },
    [
      busyAttentionId,
      props.onDiscussCothink,
      props.provider,
      reload,
    ],
  );

  if (status === "loading" || data === null) {
    return (
      <div className="wb-cowork-rail__panel" role="status">
        <p className="wb-cowork-rail__empty">Loading review…</p>
      </div>
    );
  }

  if (status === "error") {
    return (
      <div className="wb-cowork-rail__panel" role="alert">
        <p className="wb-cowork-rail__empty">Review couldn’t load.</p>
        <button
          type="button"
          className="wb-cowork-rail__verb wb-cowork-rail__verb--neutral"
          onClick={reload}
        >
          Retry
        </button>
      </div>
    );
  }

  const markTarget: MarkBarTarget | undefined =
    targetItem === undefined
      ? undefined
      : targetItem.kind === "claim"
        ? { kind: "claim", claim: targetItem.claim }
        : { kind: "proposal", proposal: targetItem.proposal };

  const pendingCount =
    Object.keys(decisions).length + Object.keys(claimDecisions).length;
  const proposalLabel = (proposalId: string) =>
    data.proposals.find((proposal) => proposal.proposalId === proposalId)?.tldr ??
    "Suggestion no longer shown";
  const reviewBlocker = (proposalId: string) => {
    store.select(proposalId, "proposal");
    props.reviewAnchors?.focusAnchor(proposalId, "proposal", {
      scroll: true,
      flash: true,
    });
  };
  const removeBlockedDecision = (proposalId: string) => {
    store.clearDecision(proposalId);
    // A blocker can depend on another selected edit (for example, an overlap).
    // Removing one therefore invalidates the whole diagnosis. Re-run preflight on
    // the remaining explicit choices instead of pretending the other blockers stand.
    setApplyNotice(null);
  };

  return (
    <div className="wb-cowork-rail__panel">
      <RailDriftStrip title={data.title} drift={data.drift} />

      <VerificationAttentionFeed
        results={data.evaluationResults}
        recheckIntents={data.verificationRecheckIntents}
        cothinkItems={data.cothinkItems}
        cothinkOutcomes={data.cothinkOutcomes}
        busyItemId={busyAttentionId}
        onRevealResult={revealResult}
        onOpenProposal={openCorrection}
        onDiscussCothink={
          props.provider.discussCothink === undefined
            ? undefined
            : (item) => {
                void discussCothink(item);
              }
        }
        onCothinkAction={
          props.provider.actOnCothink === undefined
            ? undefined
            : (item, action) => {
                void actOnCothink(item, action);
              }
        }
        onRecheckIntent={
          props.onRecheckIntent === undefined
            ? undefined
            : (intent) => {
                setBusyAttentionId(intent.intentId);
                setAttentionError(null);
                void Promise.resolve(props.onRecheckIntent?.(intent))
                  .catch((cause: unknown) => {
                    setAttentionError(
                      cause instanceof Error
                        ? cause.message
                        : "The correction recheck could not open in Verify.",
                    );
                  })
                  .finally(() => setBusyAttentionId(null));
              }
        }
      />
      {attentionError !== null ? (
        <p className="wb-cowork-rail__sitting-error" role="alert">
          {attentionError}
        </p>
      ) : null}

      <div className="wb-cowork-rail__toolbar">
        <div className="wb-cowork-rail__mode" role="group" aria-label="Review layout">
          <button
            type="button"
            className="wb-cowork-rail__mode-btn"
            aria-pressed={mode === "stream"}
            onClick={() => store.setMode("stream")}
          >
            Stream
          </button>
          <button
            type="button"
            className="wb-cowork-rail__mode-btn"
            aria-pressed={mode === "queue"}
            onClick={() => {
              if (mode === "stream" && filter === "all") {
                props.onScrollContainerWillDetach?.();
              }
              store.setMode("queue");
            }}
          >
            Queue
          </button>
        </div>
        <button
          type="button"
          className="wb-cowork-rail__submit"
          disabled={!dirty || submitting || activeRecovery !== null}
          onClick={() => {
            void submit(activeError?.attempt);
          }}
        >
          {submitting
            ? "Applying decisions…"
            : activeRecovery !== null
              ? "Decisions need review"
              : activeError !== null &&
                  activeError.attempt.retainedBlockers.length > 0
                ? activeError.attempt.proposalDecisions.length === 1
                  ? "Try the other decision again"
                  : `Try the other ${String(activeError.attempt.proposalDecisions.length)} again`
                : `${activeError !== null ? "Try again" : "Apply decisions"}${pendingCount > 0 ? ` (${pendingCount})` : ""}`}
        </button>
      </div>

      {activeRecovery !== null ? (
        <section
          className="wb-cowork-rail__apply-notice wb-cowork-rail__apply-notice--attention"
          role="alert"
          aria-labelledby="wb-cowork-apply-recovery-title"
        >
          <strong id="wb-cowork-apply-recovery-title">
            {quantity(activeRecovery.blockers.length, "decision")} {activeRecovery.blockers.length === 1 ? "needs" : "need"} review
          </strong>
          <p>
            Nothing was applied.
            {activeRecovery.availableProposalIds.length > 0
              ? activeRecovery.availableProposalIds.length === 1
                ? " The other decision is ready."
                : ` The other ${String(activeRecovery.availableProposalIds.length)} are ready.`
              : " Your decisions are still selected."}
          </p>
          <ul className="wb-cowork-rail__apply-blockers">
            {activeRecovery.blockers.map((blocker) => {
              const label = proposalLabel(blocker.proposalId);
              const isShown = data.proposals.some(
                (proposal) => proposal.proposalId === blocker.proposalId,
              );
              return (
                <li key={blocker.proposalId}>
                  {isShown ? (
                    <button
                      type="button"
                      className="wb-cowork-rail__apply-blocker"
                      onClick={() => reviewBlocker(blocker.proposalId)}
                    >
                      {label}
                    </button>
                  ) : (
                    <strong>{label}</strong>
                  )}
                  <span>{blocker.message}</span>
                  <button
                    type="button"
                    className="wb-cowork-rail__apply-remove"
                    aria-label={`Remove decision: ${label}`}
                    onClick={() => removeBlockedDecision(blocker.proposalId)}
                  >
                    Remove decision
                  </button>
                </li>
              );
            })}
          </ul>
          <div className="wb-cowork-rail__apply-actions">
            {activeRecovery.availableProposalIds.length > 0 ? (
              <button
                type="button"
                className="wb-cowork-rail__verb wb-cowork-rail__verb--neutral"
                disabled={submitting}
                onClick={() => {
                  const current = store.getState();
                  const available = new Set(
                    activeRecovery.availableProposalIds,
                  );
                  void submit({
                    proposalDecisions: Object.values(current.decisions)
                      .filter((decision) => available.has(decision.proposalId))
                      .map((decision) => ({ ...decision })),
                    claimDecisions: [],
                    retainedBlockers: activeRecovery.blockers,
                    selectionFingerprint: decisionsFingerprint(
                      current.decisions,
                      current.claimDecisions,
                    ),
                  });
                }}
              >
                {activeRecovery.availableProposalIds.length === 1
                  ? "Apply the other decision"
                  : `Apply the other ${String(activeRecovery.availableProposalIds.length)}`}
              </button>
            ) : null}
          </div>
        </section>
      ) : applyNotice?.kind === "partial" ? (
        <section
          className="wb-cowork-rail__apply-notice"
          role="status"
          aria-label="Decision apply result"
        >
          <strong>
            {applyNotice.appliedCount > 0
              ? `${quantity(applyNotice.appliedCount, "decision")} applied; `
              : "No decisions applied; "}
            {quantity(applyNotice.blockers.length, "decision")} {applyNotice.blockers.length === 1 ? "still needs" : "still need"} review
          </strong>
          <p>The blocked decisions remain selected.</p>
          <ul className="wb-cowork-rail__apply-blockers">
            {applyNotice.blockers.map((blocker) => {
              const label = proposalLabel(blocker.proposalId);
              const isShown = data.proposals.some(
                (proposal) => proposal.proposalId === blocker.proposalId,
              );
              return (
                <li key={blocker.proposalId}>
                  {isShown ? (
                    <button
                      type="button"
                      className="wb-cowork-rail__apply-blocker"
                      onClick={() => reviewBlocker(blocker.proposalId)}
                    >
                      {label}
                    </button>
                  ) : (
                    <strong>{label}</strong>
                  )}
                  <span>{blocker.message}</span>
                  <button
                    type="button"
                    className="wb-cowork-rail__apply-remove"
                    aria-label={`Remove decision: ${label}`}
                    onClick={() => removeBlockedDecision(blocker.proposalId)}
                  >
                    Remove decision
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      ) : activeError !== null ? (
        <p className="wb-cowork-rail__sitting-error" role="alert">
          {activeError.message}{" "}
          {activeError.attempt.retainedBlockers.length > 0
            ? "Your confirmed decisions and blocked items are still here, so it is safe to try again."
            : "Your decisions are still selected, so it is safe to try again."}
        </p>
      ) : null}

      <FilterLens
        filter={filter}
        counts={counts}
        onChange={(next) => {
          if (mode === "stream" && filter === "all" && next !== "all") {
            props.onScrollContainerWillDetach?.();
          }
          store.setFilter(next);
        }}
      />

      <div
        className="wb-cowork-rail__body"
        ref={
          mode === "stream" && filter === "all"
            ? props.scrollContainerRef
            : undefined
        }
      >
        {mode === "stream" ? (
          <StreamView
            items={visible}
            selectedId={selectedId}
            selectedKind={selectedKind}
            decisions={decisions}
            claimDecisions={claimDecisions}
            inspectSpanByClaim={spanByClaim}
            onSelect={(id, kind) => store.select(id, kind)}
            onScrollToAnchor={selectAndRevealAnchor}
            onInspect={(spanId) => store.openInspector(spanId)}
          />
        ) : (
          <QueueView
            items={visible}
            index={clampedIndex}
            decisions={decisions}
            claimDecisions={claimDecisions}
            inspectSpanByClaim={spanByClaim}
            bindings={props.queueBindings}
            keyboardNavigationEnabled={props.active ?? true}
            onNavigate={navigate}
            onSelect={(id, kind) => store.select(id, kind)}
            onScrollToAnchor={revealAnchor}
            onInspect={(spanId) => store.openInspector(spanId)}
          />
        )}
      </div>

      {inspectorSpanId !== null ? (
        <Inspector
          spanId={inspectorSpanId}
          data={data}
          onClose={() => store.closeInspector()}
        />
      ) : null}

      {markTarget !== undefined ? (
        <MarkBar
          disabled={submitting}
          target={markTarget}
          stagedProposal={
            markTarget.kind === "proposal"
              ? decisions[markTarget.proposal.proposalId]
              : undefined
          }
          stagedClaim={
            markTarget.kind === "claim"
              ? claimDecisions[markTarget.claim.claimId]
              : undefined
          }
          onStageProposal={stageProposal}
          onStageClaim={stageClaim}
          onClearProposal={(id) => {
            if (submitting) return;
            setApplyNotice(null);
            store.clearDecision(id);
          }}
          onClearClaim={(id) => {
            if (submitting) return;
            setApplyNotice(null);
            store.clearClaimDecision(id);
          }}
          showHotkeys={mode === "queue"}
        />
      ) : (
        <p className="wb-cowork-rail__markbar-hint">
          Select an item to decide on it.
        </p>
      )}
    </div>
  );
}

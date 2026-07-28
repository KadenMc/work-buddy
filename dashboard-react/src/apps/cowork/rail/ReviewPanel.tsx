/**
 * The Review tab. Composes the drift strip, the filter lens, the stream or queue
 * mode, the mark bar for the current target, the read-only inspector, and the
 * sitting submit. It reads its local state from the RailStore through selectors
 * (so a card re-renders only on its own slice) and its review data from the
 * provider seam. The staged sitting survives a reload through the draft
 * persistence, and a dirty sitting arms the route-change guard.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Inspector } from "./Inspector";
import { FilterLens } from "./FilterLens";
import { MarkBar, type MarkBarTarget } from "./MarkBar";
import { QueueView, type QueueBindings } from "./QueueView";
import { RailDriftStrip } from "./RailDriftStrip";
import { StreamView } from "./StreamView";
import type {
  StagedClaimDecision,
  StagedDecision,
} from "./contracts";
import { useDraftPersistence, useUnsavedChangesGuard } from "./dirty";
import {
  claimRefMatchesId,
  filterCounts,
  isSelectedItem,
  visibleItems,
  type RailItem,
} from "./items";
import type { AnchorRectSource, ReviewRailProvider } from "./provider";
import { isDirty, type RailSelectionKind, type RailStore } from "./store";
import { useIsNarrow } from "./useIsNarrow";
import { useReviewData } from "./useReviewData";
import { useRailState } from "./useRailState";

export interface ReviewPanelProps {
  readonly provider: ReviewRailProvider;
  readonly store: RailStore;
  readonly documentId: string;
  /** Injectable for tests, defaults to window.localStorage. */
  readonly storage?: Storage;
  readonly anchorRects?: AnchorRectSource;
  readonly queueBindings?: QueueBindings;
  /** Whether Review is currently visible and may handle global shortcuts. */
  readonly active?: boolean;
  /** Force the grouped narrow fallback. Otherwise a container query decides. */
  readonly narrow?: boolean;
  onSubmitted?(): void;
}

export function ReviewPanel(props: ReviewPanelProps) {
  const { store } = props;
  const storage = props.storage ?? window.localStorage;
  const { data, status, reload } = useReviewData(props.provider);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const pendingRevealRef = useRef<{
    readonly source: AnchorRectSource;
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

  const [measuredNarrow, narrowRef] = useIsNarrow();
  const narrow = props.narrow ?? measuredNarrow;

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
    const source = props.anchorRects;
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
      flash ? { scroll: true, flash: true } : { scroll: true },
    );
  }, [props.anchorRects, targetId, targetKind]);

  useEffect(
    () => () => {
      props.anchorRects?.clearFocusedAnchor();
    },
    [props.anchorRects],
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
      store.stageDecision(decision);
      if (mode === "queue") advanceToNextUndecided(clampedIndex);
    },
    [store, mode, advanceToNextUndecided, clampedIndex],
  );

  const stageClaim = useCallback(
    (decision: StagedClaimDecision) => {
      store.stageClaimDecision(decision);
      if (mode === "queue") advanceToNextUndecided(clampedIndex);
    },
    [store, mode, advanceToNextUndecided, clampedIndex],
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
    const source = props.anchorRects;
    if (source === undefined) return undefined;
    return (id: string, kind: RailSelectionKind) =>
      source.focusAnchor(id, kind, { scroll: true, flash: true });
  }, [props.anchorRects]);

  const selectAndRevealAnchor = useMemo(() => {
    const source = props.anchorRects;
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
  }, [props.anchorRects, store]);

  const submit = useCallback(async () => {
    if (data === null || submitting) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const result = await props.provider.submitSitting({
        baseDocSha256: data.drift.currentFileSha256 ?? "",
        proposalDecisions: Object.values(store.getState().decisions),
        claimDecisions: Object.values(store.getState().claimDecisions),
      });
      for (const item of result.results) {
        if (item.result !== "error" && item.result !== "rejected_stale_view") {
          store.clearDecision(item.proposalId);
        }
      }
      // Failed prepare items remain staged and durable. Reload re-derives only committed
      // items from the ledger, never from the fact that a submit was attempted.
      reload();
      props.onSubmitted?.();
    } catch {
      setSubmitError("Co-work couldn’t apply your decisions.");
    } finally {
      setSubmitting(false);
    }
  }, [data, props, reload, store, submitting]);

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

  return (
    <div
      ref={narrowRef}
      className="wb-cowork-rail__panel"
      data-narrow={narrow ? "true" : undefined}
    >
      <RailDriftStrip title={data.title} drift={data.drift} />

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
            onClick={() => store.setMode("queue")}
          >
            Queue
          </button>
        </div>
        <button
          type="button"
          className="wb-cowork-rail__submit"
          disabled={!dirty || submitting}
          onClick={() => {
            void submit();
          }}
        >
          {submitting
            ? "Applying decisions…"
            : `${submitError === null ? "Apply decisions" : "Try again"}${pendingCount > 0 ? ` (${pendingCount})` : ""}`}
        </button>
      </div>

      {submitError !== null ? (
        <p className="wb-cowork-rail__sitting-error" role="alert">
          {submitError} Your decisions are still here.
        </p>
      ) : null}

      <FilterLens
        filter={filter}
        counts={counts}
        onChange={(next) => store.setFilter(next)}
      />

      <div className="wb-cowork-rail__body">
        {mode === "stream" ? (
          <StreamView
            items={visible}
            selectedId={selectedId}
            selectedKind={selectedKind}
            decisions={decisions}
            claimDecisions={claimDecisions}
            inspectSpanByClaim={spanByClaim}
            grouped={narrow}
            anchorRects={props.anchorRects}
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
          onClearProposal={(id) => store.clearDecision(id)}
          onClearClaim={(id) => store.clearClaimDecision(id)}
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

/**
 * The Review tab. Composes the drift strip, the filter lens, the stream or queue
 * mode, the mark bar for the current target, the read-only inspector, and the
 * sitting submit. It reads its local state from the RailStore through selectors
 * (so a card re-renders only on its own slice) and its review data from the
 * provider seam. The staged sitting survives a reload through the draft
 * persistence, and a dirty sitting arms the route-change guard.
 */

import { useCallback, useMemo } from "react";

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
  filterCounts,
  orderedItems,
  visibleItems,
  type RailItem,
} from "./items";
import type { AnchorRectSource, ReviewRailProvider } from "./provider";
import { isDirty, type RailStore } from "./store";
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
  /** Force the grouped narrow fallback. Otherwise a container query decides. */
  readonly narrow?: boolean;
  onSubmitted?(): void;
}

export function ReviewPanel(props: ReviewPanelProps) {
  const { store } = props;
  const storage = props.storage ?? window.localStorage;
  const { data, status, reload } = useReviewData(props.provider);

  const filter = useRailState(store, (state) => state.filter);
  const mode = useRailState(store, (state) => state.mode);
  const selectedId = useRailState(store, (state) => state.selectedId);
  const queueIndex = useRailState(store, (state) => state.queueIndex);
  const decisions = useRailState(store, (state) => state.decisions);
  const claimDecisions = useRailState(store, (state) => state.claimDecisions);
  const inspectorSpanId = useRailState(store, (state) => state.inspectorSpanId);
  const dirty = useRailState(store, isDirty);

  const [measuredNarrow, narrowRef] = useIsNarrow();
  const narrow = props.narrow ?? measuredNarrow;

  useDraftPersistence(store, props.documentId, storage);
  useUnsavedChangesGuard(store, dirty);

  const allItems = useMemo(
    () => (data === null ? [] : orderedItems(data)),
    [data],
  );
  const visible = useMemo(
    () => (data === null ? [] : visibleItems(data, filter)),
    [data, filter],
  );
  const counts = useMemo(
    () => (data === null ? { all: 0, suggestions: 0, flags: 0, claims: 0 } : filterCounts(data)),
    [data],
  );
  const byId = useMemo(() => {
    const map = new Map<string, RailItem>();
    for (const item of allItems) map.set(item.id, item);
    return map;
  }, [allItems]);
  const spanByClaim = useMemo(() => {
    const map = new Map<string, string>();
    if (data === null) return map;
    for (const claim of data.claims) {
      const expression = data.expressions.find((candidate) =>
        candidate.claimRef.includes(claim.claimId),
      );
      if (expression !== undefined) map.set(claim.claimId, expression.spanId);
    }
    return map;
  }, [data]);

  const clampedIndex = Math.min(queueIndex, Math.max(0, visible.length - 1));

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

  const scrollToAnchor = useMemo(() => {
    const source = props.anchorRects;
    if (source === undefined) return undefined;
    return (id: string) => source.scrollToAnchor(id);
  }, [props.anchorRects]);

  const submit = useCallback(async () => {
    if (data === null) return;
    await props.provider.submitSitting({
      baseDocSha256: data.drift.currentFileSha256 ?? "",
      proposalDecisions: Object.values(store.getState().decisions),
      claimDecisions: Object.values(store.getState().claimDecisions),
    });
    store.clearAllDecisions();
    // The draft mirror clears on the resulting empty-sitting persist.
    reload();
    props.onSubmitted?.();
  }, [data, props, store, reload]);

  if (status === "loading" || data === null) {
    return (
      <div className="wb-cowork-rail__panel" role="status">
        <p className="wb-cowork-rail__empty">Loading the review layer.</p>
      </div>
    );
  }

  if (status === "error") {
    return (
      <div className="wb-cowork-rail__panel" role="alert">
        <p className="wb-cowork-rail__empty">The review layer could not load.</p>
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

  const targetItem: RailItem | undefined =
    mode === "queue"
      ? visible[clampedIndex]
      : selectedId !== null
        ? byId.get(selectedId)
        : undefined;

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
          disabled={!dirty}
          onClick={() => {
            void submit();
          }}
        >
          Submit sitting{pendingCount > 0 ? ` (${pendingCount})` : ""}
        </button>
      </div>

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
            decisions={decisions}
            claimDecisions={claimDecisions}
            inspectSpanByClaim={spanByClaim}
            grouped={narrow}
            anchorRects={props.anchorRects}
            onSelect={(id, kind) => store.select(id, kind)}
            onScrollToAnchor={scrollToAnchor}
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
            onNavigate={navigate}
            onSelect={(id, kind) => store.select(id, kind)}
            onScrollToAnchor={scrollToAnchor}
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

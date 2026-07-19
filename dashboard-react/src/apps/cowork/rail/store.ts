/**
 * The Review rail local state store. Section 5.2 keeps the staged sitting, the
 * keyboard focus, the filter lens, and the mode as widget-local state that never
 * rides a ViewProvider snapshot. This is a small external store read through
 * selectors (the useEditorState pattern), so a card re-renders only when its own
 * slice changes and a thirty-mark sitting does not thrash the tree.
 */

import type { StagedClaimDecision, StagedDecision } from "./contracts";

/** Which rail tab is shown (section 5.1, Review or Chat). */
export type RailTab = "review" | "chat";

/** The Review-tab layout, the aligned stream or the queue focus mode (SP-6). */
export type RailMode = "stream" | "queue";

/** The typed-group filter lens (SP-6 variant B as a lens over the stream). */
export type RailFilter = "all" | "suggestions" | "flags" | "claims";

/** What the mark bar and inspector are currently pointed at. */
export type RailSelectionKind = "proposal" | "claim";

export interface RailState {
  readonly tab: RailTab;
  readonly mode: RailMode;
  readonly filter: RailFilter;
  /** The selected proposal or claim id, or null for none. */
  readonly selectedId: string | null;
  readonly selectedKind: RailSelectionKind | null;
  /** The queue focus index, over the currently filtered item list. */
  readonly queueIndex: number;
  /** Staged proposal and flag decisions by proposal id. */
  readonly decisions: Readonly<Record<string, StagedDecision>>;
  /** Staged claim decisions by claim id. */
  readonly claimDecisions: Readonly<Record<string, StagedClaimDecision>>;
  /** The span the read-only inspector is open on, or null when closed. */
  readonly inspectorSpanId: string | null;
}

const INITIAL_STATE: RailState = {
  tab: "review",
  mode: "stream",
  filter: "all",
  selectedId: null,
  selectedKind: null,
  queueIndex: 0,
  decisions: {},
  claimDecisions: {},
  inspectorSpanId: null,
};

type Listener = () => void;

/** Whether the sitting holds any staged decision (drives the dirty guard). */
export function isDirty(state: RailState): boolean {
  return (
    Object.keys(state.decisions).length > 0 ||
    Object.keys(state.claimDecisions).length > 0
  );
}

export class RailStore {
  private state: RailState;
  private readonly listeners = new Set<Listener>();

  constructor(initial: Partial<RailState> = {}) {
    this.state = { ...INITIAL_STATE, ...initial };
  }

  getState = (): RailState => this.state;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  private set(next: RailState): void {
    if (next === this.state) return;
    this.state = next;
    for (const listener of this.listeners) listener();
  }

  setTab(tab: RailTab): void {
    this.set({ ...this.state, tab });
  }

  setMode(mode: RailMode): void {
    this.set({ ...this.state, mode });
  }

  setFilter(filter: RailFilter): void {
    // Reset the queue cursor when the filtered set changes underfoot.
    this.set({ ...this.state, filter, queueIndex: 0 });
  }

  select(id: string, kind: RailSelectionKind): void {
    this.set({ ...this.state, selectedId: id, selectedKind: kind });
  }

  clearSelection(): void {
    this.set({ ...this.state, selectedId: null, selectedKind: null });
  }

  setQueueIndex(index: number): void {
    this.set({ ...this.state, queueIndex: Math.max(0, index) });
  }

  stageDecision(decision: StagedDecision): void {
    this.set({
      ...this.state,
      decisions: { ...this.state.decisions, [decision.proposalId]: decision },
    });
  }

  clearDecision(proposalId: string): void {
    if (this.state.decisions[proposalId] === undefined) return;
    const next = { ...this.state.decisions };
    delete next[proposalId];
    this.set({ ...this.state, decisions: next });
  }

  stageClaimDecision(decision: StagedClaimDecision): void {
    this.set({
      ...this.state,
      claimDecisions: {
        ...this.state.claimDecisions,
        [decision.claimId]: decision,
      },
    });
  }

  clearClaimDecision(claimId: string): void {
    if (this.state.claimDecisions[claimId] === undefined) return;
    const next = { ...this.state.claimDecisions };
    delete next[claimId];
    this.set({ ...this.state, claimDecisions: next });
  }

  clearAllDecisions(): void {
    this.set({ ...this.state, decisions: {}, claimDecisions: {} });
  }

  /** Restore a persisted draft (dirty-state retention across a reload). */
  hydrateDecisions(
    decisions: Readonly<Record<string, StagedDecision>>,
    claimDecisions: Readonly<Record<string, StagedClaimDecision>>,
  ): void {
    this.set({ ...this.state, decisions, claimDecisions });
  }

  openInspector(spanId: string): void {
    this.set({ ...this.state, inspectorSpanId: spanId });
  }

  closeInspector(): void {
    if (this.state.inspectorSpanId === null) return;
    this.set({ ...this.state, inspectorSpanId: null });
  }
}

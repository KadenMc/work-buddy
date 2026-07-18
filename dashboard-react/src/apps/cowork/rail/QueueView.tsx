/**
 * The queue-focus Review mode (SP-6 variant C). One item at a time with a
 * progress indicator and a collapsed all-items list. Keyboard-driven: the
 * navigation binding defaults to Kaden's inverted pair (j previous or up, k next
 * or down) and is configurable. The purpose-built home for the PRD section 6
 * batch-sitting flow, mark then advance.
 */

import { useEffect } from "react";

import { ClaimCard } from "./ClaimCard";
import { ProposalCard } from "./ProposalCard";
import type { StagedClaimDecision, StagedDecision } from "./contracts";
import { groupOf, type RailItem } from "./items";
import type { RailSelectionKind } from "./store";

/** The queue navigation binding. Defaults to j previous, k next (inverted). */
export interface QueueBindings {
  readonly prev: string;
  readonly next: string;
}

export const DEFAULT_QUEUE_BINDINGS: QueueBindings = { prev: "j", next: "k" };

export interface QueueViewProps {
  readonly items: readonly RailItem[];
  readonly index: number;
  readonly decisions: Readonly<Record<string, StagedDecision>>;
  readonly claimDecisions: Readonly<Record<string, StagedClaimDecision>>;
  readonly inspectSpanByClaim: ReadonlyMap<string, string>;
  readonly bindings?: QueueBindings;
  onNavigate(delta: number): void;
  onSelect(id: string, kind: RailSelectionKind): void;
  onScrollToAnchor?(id: string): void;
  onInspect(spanId: string): void;
}

function isDecided(
  item: RailItem,
  decisions: Readonly<Record<string, StagedDecision>>,
  claimDecisions: Readonly<Record<string, StagedClaimDecision>>,
): boolean {
  return item.kind === "claim"
    ? claimDecisions[item.id] !== undefined
    : decisions[item.id] !== undefined;
}

function itemLabel(item: RailItem): string {
  if (item.kind === "claim") return `Claim, ${item.claim.proposition}`;
  const noun =
    item.proposal.kind === "flag"
      ? "Flag"
      : item.proposal.changeType === "deletion"
        ? "Deletion"
        : "Insertion";
  return `${noun}, ${item.proposal.tldr}`;
}

export function QueueView(props: QueueViewProps) {
  const bindings = props.bindings ?? DEFAULT_QUEUE_BINDINGS;
  const total = props.items.length;
  const clampedIndex = Math.min(props.index, Math.max(0, total - 1));
  const focused = props.items[clampedIndex];
  const undecided = props.items.filter(
    (item) => !isDecided(item, props.decisions, props.claimDecisions),
  ).length;

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || target?.isContentEditable) {
        return;
      }
      if (event.key === bindings.prev) {
        event.preventDefault();
        props.onNavigate(-1);
      } else if (event.key === bindings.next) {
        event.preventDefault();
        props.onNavigate(1);
      }
    };
    window.addEventListener("keydown", handler);
    return () => {
      window.removeEventListener("keydown", handler);
    };
  }, [bindings.prev, bindings.next, props]);

  if (focused === undefined) {
    return (
      <div className="wb-cowork-rail__queue" role="status">
        <p className="wb-cowork-rail__empty">Nothing to review here.</p>
      </div>
    );
  }

  const scrollTo =
    props.onScrollToAnchor === undefined
      ? undefined
      : () => props.onScrollToAnchor?.(focused.id);

  return (
    <div className="wb-cowork-rail__queue">
      <div className="wb-cowork-rail__progress">
        <p className="wb-cowork-rail__progress-top">
          <span className="wb-cowork-rail__progress-pos">
            Item {clampedIndex + 1}
          </span>
          <span className="wb-cowork-rail__progress-of"> of {total}</span>
          <span className="wb-cowork-rail__progress-remaining">
            {undecided} undecided
          </span>
        </p>
        <ol
          className="wb-cowork-rail__progress-bar"
          aria-label={`Item ${clampedIndex + 1} of ${total}, ${undecided} undecided`}
        >
          {props.items.map((item, position) => (
            <li
              key={item.id}
              className="wb-cowork-rail__progress-seg"
              data-state={
                position === clampedIndex
                  ? "current"
                  : isDecided(item, props.decisions, props.claimDecisions)
                    ? "done"
                    : "todo"
              }
            />
          ))}
        </ol>
      </div>

      <div className="wb-cowork-rail__queue-focus">
        <ul className="wb-cowork-rail__card-list">
          {focused.kind === "claim" ? (
            <ClaimCard
              claim={focused.claim}
              selected
              staged={props.claimDecisions[focused.id]}
              onSelect={() => props.onSelect(focused.id, "claim")}
              inspectSpanId={props.inspectSpanByClaim.get(focused.id)}
              onInspect={props.onInspect}
              onScrollToAnchor={scrollTo}
            />
          ) : (
            <ProposalCard
              proposal={focused.proposal}
              selected
              staged={props.decisions[focused.id]}
              onSelect={() => props.onSelect(focused.id, "proposal")}
              onScrollToAnchor={scrollTo}
            />
          )}
        </ul>
      </div>

      <div className="wb-cowork-rail__kbhints" aria-hidden="true">
        <span>
          <kbd>{bindings.prev}</kbd> previous
        </span>
        <span>
          <kbd>{bindings.next}</kbd> next
        </span>
      </div>

      <div className="wb-cowork-rail__allitems">
        <p className="wb-cowork-rail__allitems-head">
          All items
          <span className="wb-cowork-rail__allitems-count">
            {clampedIndex + 1} / {total}
          </span>
        </p>
        <ul className="wb-cowork-rail__allitems-list">
          {props.items.map((item, position) => {
            const decided = isDecided(
              item,
              props.decisions,
              props.claimDecisions,
            );
            return (
              <li key={item.id}>
                <button
                  type="button"
                  className="wb-cowork-rail__allitems-row"
                  data-current={position === clampedIndex ? "true" : undefined}
                  data-group={groupOf(item)}
                  onClick={() => props.onNavigate(position - clampedIndex)}
                >
                  <span
                    className="wb-cowork-rail__allitems-tick"
                    data-group={groupOf(item)}
                    aria-hidden="true"
                  />
                  <span className="wb-cowork-rail__allitems-label">
                    {itemLabel(item)}
                  </span>
                  <span className="wb-cowork-rail__allitems-status">
                    {position === clampedIndex
                      ? "now"
                      : decided
                        ? "decided"
                        : "undecided"}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

/**
 * The Review stream is a conventional document-order list. Cards stay in normal
 * flow so the browser owns scrolling and reading order. Selecting a card keeps
 * the editor highlight in sync; its passage button provides explicit navigation.
 */

import { ClaimCard } from "./ClaimCard";
import { ProposalCard } from "./ProposalCard";
import type { StagedClaimDecision, StagedDecision } from "./contracts";
import {
  isSelectedItem,
  railItemKey,
  type RailItem,
} from "./items";
import type { RailSelectionKind } from "./store";

export interface StreamViewProps {
  readonly items: readonly RailItem[];
  readonly selectedId: string | null;
  readonly selectedKind: RailSelectionKind | null;
  readonly decisions: Readonly<Record<string, StagedDecision>>;
  readonly claimDecisions: Readonly<Record<string, StagedClaimDecision>>;
  /** Claim id to inspector span id, for the claim inspect affordance. */
  readonly inspectSpanByClaim: ReadonlyMap<string, string>;
  onSelect(id: string, kind: RailSelectionKind): void;
  onScrollToAnchor?(id: string, kind: RailSelectionKind): void;
  onInspect(spanId: string): void;
}

export function StreamView(props: StreamViewProps) {
  const renderCard = (item: RailItem) => {
    const scrollTo =
      props.onScrollToAnchor === undefined
        ? undefined
        : () => props.onScrollToAnchor?.(item.id, item.kind);
    if (item.kind === "claim") {
      return (
        <ClaimCard
          key={railItemKey(item)}
          claim={item.claim}
          selected={isSelectedItem(
            item,
            props.selectedId,
            props.selectedKind,
          )}
          staged={props.claimDecisions[item.id]}
          onSelect={() => props.onSelect(item.id, "claim")}
          inspectSpanId={props.inspectSpanByClaim.get(item.id)}
          onInspect={props.onInspect}
          onScrollToAnchor={scrollTo}
        />
      );
    }
    return (
      <ProposalCard
        key={railItemKey(item)}
        proposal={item.proposal}
        selected={isSelectedItem(
          item,
          props.selectedId,
          props.selectedKind,
        )}
        staged={props.decisions[item.id]}
        onSelect={() => props.onSelect(item.id, "proposal")}
        onScrollToAnchor={scrollTo}
      />
    );
  };

  if (props.items.length === 0) {
    return (
      <div className="wb-cowork-rail__stream" role="status">
        <p className="wb-cowork-rail__empty">Nothing to review here.</p>
      </div>
    );
  }

  return (
    <div className="wb-cowork-rail__stream">
      <ul className="wb-cowork-rail__card-list">
        {props.items.map(renderCard)}
      </ul>
    </div>
  );
}

/**
 * The Review stream is a conventional document-order list. Cards stay in normal
 * flow so the browser owns scrolling and reading order. The selected props are
 * passive render state; activating a card is an explicit select-and-reveal
 * command. Its passage button uses the same command with stronger visual
 * emphasis.
 */

import { ProposalCard } from "./ProposalCard";
import type { StagedDecision } from "./contracts";
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
  onActivate(id: string, kind: RailSelectionKind): void;
  onScrollToAnchor?(id: string, kind: RailSelectionKind): void;
}

export function StreamView(props: StreamViewProps) {
  const renderCard = (item: RailItem) => {
    const scrollTo =
      props.onScrollToAnchor === undefined
        ? undefined
        : () => props.onScrollToAnchor?.(item.id, item.kind);
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
        onSelect={() => props.onActivate(item.id, "proposal")}
        onScrollToAnchor={scrollTo}
      />
    );
  };

  if (props.items.length === 0) {
    return (
      <div className="wb-cowork-rail__stream" role="status">
        <p className="wb-cowork-rail__empty">No suggestions or flags to review.</p>
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

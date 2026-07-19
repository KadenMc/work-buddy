/**
 * The aligned-stream Review layout (SP-6 variant A default). One document-order
 * column of margin cards, each tied to its anchor. When an AnchorRectSource is
 * wired the cards are positioned per-anchor outside the React render cycle
 * (useAlignedStream), and clustered anchors are pushed apart. With no source, or
 * on a narrow container, the stream degrades to a grouped document-order list
 * (SP-6 variant B fallback) plus scroll-to-and-highlight on select.
 */

import { ClaimCard } from "./ClaimCard";
import { ProposalCard } from "./ProposalCard";
import type { StagedClaimDecision, StagedDecision } from "./contracts";
import { groupOf, type RailGroup, type RailItem } from "./items";
import type { AnchorRectSource } from "./provider";
import type { RailSelectionKind } from "./store";
import { useAlignedStream } from "./useAlignedStream";

export interface StreamViewProps {
  readonly items: readonly RailItem[];
  readonly selectedId: string | null;
  readonly decisions: Readonly<Record<string, StagedDecision>>;
  readonly claimDecisions: Readonly<Record<string, StagedClaimDecision>>;
  /** Claim id to inspector span id, for the claim inspect affordance. */
  readonly inspectSpanByClaim: ReadonlyMap<string, string>;
  /** Force the grouped fallback (narrow viewport). */
  readonly grouped: boolean;
  readonly anchorRects?: AnchorRectSource;
  onSelect(id: string, kind: RailSelectionKind): void;
  onScrollToAnchor?(id: string): void;
  onInspect(spanId: string): void;
}

const GROUP_HEADING: Record<RailGroup, string> = {
  suggestions: "Suggestions",
  flags: "Flags",
  claims: "Claims",
};

const GROUP_ORDER: readonly RailGroup[] = ["suggestions", "flags", "claims"];

export function StreamView(props: StreamViewProps) {
  const controller = useAlignedStream({
    anchorRects: props.anchorRects,
    ids: props.items.map((item) => item.id),
  });

  const renderCard = (item: RailItem) => {
    const cardRef = controller.aligned
      ? controller.registerCard(item.id)
      : undefined;
    const scrollTo =
      props.onScrollToAnchor === undefined
        ? undefined
        : () => props.onScrollToAnchor?.(item.id);
    if (item.kind === "claim") {
      return (
        <ClaimCard
          key={item.id}
          claim={item.claim}
          selected={props.selectedId === item.id}
          staged={props.claimDecisions[item.id]}
          onSelect={() => props.onSelect(item.id, "claim")}
          inspectSpanId={props.inspectSpanByClaim.get(item.id)}
          onInspect={props.onInspect}
          onScrollToAnchor={scrollTo}
          cardRef={cardRef}
        />
      );
    }
    return (
      <ProposalCard
        key={item.id}
        proposal={item.proposal}
        selected={props.selectedId === item.id}
        staged={props.decisions[item.id]}
        onSelect={() => props.onSelect(item.id, "proposal")}
        onScrollToAnchor={scrollTo}
        cardRef={cardRef}
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

  if (props.grouped) {
    return (
      <div className="wb-cowork-rail__stream" data-grouped="true">
        {GROUP_ORDER.map((group) => {
          const groupItems = props.items.filter(
            (item) => groupOf(item) === group,
          );
          if (groupItems.length === 0) return null;
          return (
            <section
              key={group}
              className="wb-cowork-rail__group"
              aria-label={GROUP_HEADING[group]}
            >
              <h3 className="wb-cowork-rail__group-head">
                {GROUP_HEADING[group]}
                <span className="wb-cowork-rail__group-count">
                  {groupItems.length}
                </span>
              </h3>
              <ul className="wb-cowork-rail__card-list">
                {groupItems.map(renderCard)}
              </ul>
            </section>
          );
        })}
      </div>
    );
  }

  return (
    <div
      className="wb-cowork-rail__stream"
      data-aligned={controller.aligned ? "true" : undefined}
    >
      <ul
        className="wb-cowork-rail__card-list"
        ref={controller.aligned ? controller.registerContainer : undefined}
      >
        {props.items.map(renderCard)}
      </ul>
    </div>
  );
}

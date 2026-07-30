import type {
  CothinkItem,
  CothinkItemStatus,
  CothinkOutcome,
  EvaluationResult,
  VerificationRecheckIntent,
} from "./contracts";

const RESULT_LABEL: Readonly<Record<EvaluationResult["kind"], string>> = {
  conforming: "Requirement met",
  nonconforming: "Requirement not met",
  inconclusive: "Inconclusive",
  review_comment: "Review comment",
};

export type CothinkAction = "park" | "dismiss";

export interface VerificationAttentionFeedProps {
  readonly results: readonly EvaluationResult[];
  readonly cothinkItems: readonly CothinkItem[];
  readonly cothinkOutcomes: readonly CothinkOutcome[];
  readonly recheckIntents?: readonly VerificationRecheckIntent[];
  readonly busyItemId?: string | null;
  readonly onRevealResult?: (result: EvaluationResult) => void;
  readonly onOpenProposal?: (proposalId: string) => void;
  readonly onDiscussCothink?: (item: CothinkItem) => void;
  readonly onCothinkAction?: (
    item: CothinkItem,
    action: CothinkAction,
  ) => void;
  readonly onRecheckIntent?: (
    intent: VerificationRecheckIntent,
  ) => void;
}

function VerificationRecheckCard({
  intent,
  busy,
  onRecheck,
}: {
  readonly intent: VerificationRecheckIntent;
  readonly busy: boolean;
  readonly onRecheck?: (intent: VerificationRecheckIntent) => void;
}) {
  const targetLabel =
    intent.originalActionTarget.label ??
    (intent.originalActionTarget.kind === "document"
      ? "Whole document"
      : "Original document target");
  const needsTarget = intent.status === "user_action_required";
  return (
    <article
      className="wb-cowork-attention-card wb-cowork-attention-card--verify"
      aria-labelledby={`verify-recheck-${intent.intentId}`}
    >
      <header>
        <p className="wb-cowork-attention-card__eyebrow">Co-work Verify</p>
        <h3 id={`verify-recheck-${intent.intentId}`}>
          {needsTarget
            ? "Choose a target for the follow-up check"
            : "Correction ready to recheck"}
        </h3>
      </header>
      <p>
        {needsTarget
          ? "The earlier target predates durable target references. Set the intended passage with the Working on controls, then run Verify from its dock."
          : `Recheck ${targetLabel} against the committed document version with the original provider and model.`}
      </p>
      <p className="wb-cowork-attention-card__version">
        {intent.pendingProposalIds.length.toLocaleString()} applied{" "}
        {intent.pendingProposalIds.length === 1 ? "correction" : "corrections"}{" "}
        awaiting recheck
      </p>
      {onRecheck !== undefined ? (
        <div className="wb-cowork-attention-card__actions">
          <button
            type="button"
            disabled={busy}
            onClick={() => onRecheck(intent)}
          >
            {busy
              ? needsTarget
                ? "Opening target…"
                : "Opening Verify…"
              : needsTarget
                ? "Set target and recheck"
                : "Recheck in Verify"}
          </button>
        </div>
      ) : null}
    </article>
  );
}

function CothinkOutcomeCard({
  outcome,
}: {
  readonly outcome: CothinkOutcome;
}) {
  const title =
    outcome.status === "running"
      ? "Looking for another perspective"
      : outcome.status === "unavailable"
        ? "Perspective unavailable"
        : "No useful alternative found";
  return (
    <article
      className="wb-cowork-attention-card wb-cowork-attention-card--cothink"
      aria-labelledby={`cothink-outcome-${outcome.outcomeId}`}
    >
      <header>
        <p className="wb-cowork-attention-card__eyebrow">Co-think</p>
        <h3 id={`cothink-outcome-${outcome.outcomeId}`}>{title}</h3>
      </header>
      {outcome.rationale.length > 0 ? <p>{outcome.rationale}</p> : null}
      <p className="wb-cowork-attention-card__version">
        {outcome.targetLabel}
        {outcome.currentVersion ? "" : " · Earlier version"}
      </p>
    </article>
  );
}

const latestFirst = <T extends { readonly createdAt: string }>(
  values: readonly T[],
): readonly T[] =>
  [...values].sort((left, right) =>
    right.createdAt.localeCompare(left.createdAt),
  );

function EvaluationResultCard({
  result,
  onReveal,
  onOpenProposal,
}: {
  readonly result: EvaluationResult;
  readonly onReveal?: (result: EvaluationResult) => void;
  readonly onOpenProposal?: (proposalId: string) => void;
}) {
  const firstProposal = result.proposalIds[0];
  return (
    <article
      className="wb-cowork-attention-card wb-cowork-attention-card--verify"
      data-result-kind={result.kind}
      aria-labelledby={`evaluation-result-${result.resultId}`}
    >
      <header>
        <p className="wb-cowork-attention-card__eyebrow">Co-work Verify</p>
        <h3 id={`evaluation-result-${result.resultId}`}>
          {RESULT_LABEL[result.kind]}
        </h3>
      </header>
      <p className="wb-cowork-attention-card__criterion">
        {result.criterionLabel}
      </p>
      <p>{result.explanation}</p>
      {result.quoteAnchor !== null ? (
        <blockquote>{result.quoteAnchor.exact}</blockquote>
      ) : null}
      <dl className="wb-cowork-attention-card__facts">
        <div>
          <dt>Method</dt>
          <dd>{result.methodLabel}</dd>
        </div>
        <div>
          <dt>Coverage</dt>
          <dd>{result.coverageLabel}</dd>
        </div>
        <div>
          <dt>Version</dt>
          <dd>{result.currentVersion ? "Current" : "Earlier version"}</dd>
        </div>
      </dl>
      {result.limitations.length > 0 ? (
        <details>
          <summary>Limits</summary>
          <ul>
            {result.limitations.map((limitation) => (
              <li key={limitation}>{limitation}</li>
            ))}
          </ul>
        </details>
      ) : null}
      <div className="wb-cowork-attention-card__actions">
        {result.quoteAnchor !== null && onReveal !== undefined ? (
          <button type="button" onClick={() => onReveal(result)}>
            Show evidence
          </button>
        ) : null}
        {firstProposal !== undefined && onOpenProposal !== undefined ? (
          <button type="button" onClick={() => onOpenProposal(firstProposal)}>
            Review correction
          </button>
        ) : null}
      </div>
    </article>
  );
}

function CothinkCard({
  item,
  busy,
  onDiscuss,
  onAction,
}: {
  readonly item: CothinkItem;
  readonly busy: boolean;
  readonly onDiscuss?: (item: CothinkItem) => void;
  readonly onAction?: (item: CothinkItem, action: CothinkAction) => void;
}) {
  return (
    <article
      className="wb-cowork-attention-card wb-cowork-attention-card--cothink"
      aria-labelledby={`cothink-item-${item.itemId}`}
    >
      <header>
        <p className="wb-cowork-attention-card__eyebrow">
          Co-think · Alternative perspective
        </p>
        <h3 id={`cothink-item-${item.itemId}`}>{item.targetLabel}</h3>
      </header>
      <p>{item.content}</p>
      {item.rationale.length > 0 ? (
        <p className="wb-cowork-attention-card__rationale">{item.rationale}</p>
      ) : null}
      {!item.currentVersion ? (
        <p className="wb-cowork-attention-card__version">Earlier version</p>
      ) : null}
      <div className="wb-cowork-attention-card__actions">
        {onDiscuss !== undefined ? (
          <button type="button" onClick={() => onDiscuss(item)}>
            Discuss
          </button>
        ) : null}
        {onAction !== undefined ? (
          <>
            <button
              type="button"
              disabled={busy || item.status === "parked"}
              onClick={() => onAction(item, "park")}
            >
              {item.status === "parked" ? "Kept for later" : "Keep for later"}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onAction(item, "dismiss")}
            >
              Dismiss
            </button>
          </>
        ) : null}
      </div>
    </article>
  );
}

export function VerificationAttentionFeed(
  props: VerificationAttentionFeedProps,
) {
  const visibleResults = latestFirst(
    props.results.filter(
      (result) =>
        result.disposition === "surface_result" ||
        result.disposition === "surface_proposal" ||
        result.disposition === "escalate",
    ),
  );
  const visibleCothink = latestFirst(
    props.cothinkItems.filter(
      (item) => item.status === ("open" satisfies CothinkItemStatus) ||
        item.status === "parked",
    ),
  );
  const visibleCothinkOutcomes = latestFirst(
    props.cothinkOutcomes.filter(
      (outcome) => outcome.status !== "completed_with_item",
    ),
  );
  const visibleRechecks = [...(props.recheckIntents ?? [])]
    .filter((intent) => intent.status !== "fulfilled")
    .sort((left, right) =>
      right.committedAt.localeCompare(left.committedAt),
    );
  if (
    visibleResults.length === 0 &&
    visibleRechecks.length === 0 &&
    visibleCothink.length === 0 &&
    visibleCothinkOutcomes.length === 0
  ) {
    return null;
  }
  return (
    <section className="wb-cowork-attention" aria-label="Verify and Co-think">
      {visibleRechecks.map((intent) => (
        <VerificationRecheckCard
          key={intent.intentId}
          intent={intent}
          busy={props.busyItemId === intent.intentId}
          onRecheck={props.onRecheckIntent}
        />
      ))}
      {visibleResults.map((result) => (
        <EvaluationResultCard
          key={result.resultId}
          result={result}
          onReveal={props.onRevealResult}
          onOpenProposal={props.onOpenProposal}
        />
      ))}
      {visibleCothink.map((item) => (
        <CothinkCard
          key={item.itemId}
          item={item}
          busy={props.busyItemId === item.itemId}
          onDiscuss={props.onDiscussCothink}
          onAction={props.onCothinkAction}
        />
      ))}
      {visibleCothinkOutcomes.map((outcome) => (
        <CothinkOutcomeCard key={outcome.outcomeId} outcome={outcome} />
      ))}
    </section>
  );
}

import { useState } from "react";

import type {
  CothinkItem,
  CothinkItemStatus,
  CothinkOutcome,
  EvaluationResult,
  EvaluationRunSummary,
  VerificationRecheckIntent,
  VerifyRunInspection,
} from "./contracts";

const RESULT_LABEL: Readonly<Record<EvaluationResult["kind"], string>> = {
  conforming: "Requirement met",
  nonconforming: "Requirement not met",
  inconclusive: "Inconclusive",
  review_comment: "Review comment",
};

const RUN_LABEL: Readonly<Record<EvaluationRunSummary["status"], string>> = {
  prepared: "Preparing",
  queued: "Queued",
  running: "Checking",
  completed: "Complete",
  completed_with_failures: "Completed with limits",
  failed: "Couldn’t complete",
  cancelled: "Cancelled",
};

export type CothinkAction = "park" | "dismiss";

export interface VerificationAttentionFeedProps {
  readonly runs: readonly EvaluationRunSummary[];
  readonly results: readonly EvaluationResult[];
  readonly cothinkItems: readonly CothinkItem[];
  readonly cothinkOutcomes: readonly CothinkOutcome[];
  readonly recheckIntents?: readonly VerificationRecheckIntent[];
  readonly busyItemId?: string | null;
  readonly onRevealResult?: (result: EvaluationResult) => void;
  readonly onOpenProposal?: (proposalId: string) => void;
  readonly onInspectRun?: (runId: string) => Promise<VerifyRunInspection>;
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
          ? "The earlier scoped target predates durable target references. Choose the intended passage in the document action bar and run Verify explicitly."
          : `Recheck ${targetLabel} against the committed document version with the original provider and model.`}
      </p>
      <p className="wb-cowork-attention-card__version">
        {intent.pendingProposalIds.length.toLocaleString()} applied{" "}
        {intent.pendingProposalIds.length === 1 ? "correction" : "corrections"}{" "}
        awaiting recheck
      </p>
      {!needsTarget && onRecheck !== undefined ? (
        <div className="wb-cowork-attention-card__actions">
          <button
            type="button"
            disabled={busy}
            onClick={() => onRecheck(intent)}
          >
            {busy ? "Starting recheck…" : "Recheck now"}
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

function RunInspection({
  inspection,
}: {
  readonly inspection: VerifyRunInspection;
}) {
  return (
    <div className="wb-cowork-verify-history__inspection">
      <p>
        Frozen plan <code>{inspection.plan.planSnapshotId.slice(0, 12)}</code> ·
        document version{" "}
        <code>{inspection.action.structuredHeadSha256.slice(0, 12)}</code>
      </p>
      <ul>
        {inspection.checks.map((check) => (
          <li key={check.checkExecutionId}>
            {check.definition.title} v{check.definition.version.toString()} ·{" "}
            {check.mechanism} · {check.status}
          </li>
        ))}
      </ul>
      {inspection.coordination.length > 0 ? (
        <ul>
          {inspection.coordination.map((job) => (
            <li key={job.jobId}>
              {job.role.replace(/_/gu, " ")} · {job.provider} · {job.model} ·{" "}
              {job.status} · up to ${job.costCeilingUsd.toFixed(2)}
              {job.error === null ? "" : ` · ${job.error}`}
            </li>
          ))}
        </ul>
      ) : null}
      {inspection.results.map((result) => (
        <div key={result.evaluationResultId}>
          <strong>{result.kind.replace(/_/gu, " ")}</strong>
          <p>{result.message}</p>
          {result.dispositions.map((disposition, index) => (
            <p key={`${disposition.decision}:${index.toString()}`}>
              {disposition.decision.replace(/_/gu, " ")} ·{" "}
              {disposition.rationale}
            </p>
          ))}
          {result.lineage.length > 0 ? (
            <p>
              Lineage:{" "}
              {result.lineage
                .map(
                  (relation) =>
                    `${relation.relation} ${relation.targetKind} ${relation.targetRef.slice(0, 12)}`,
                )
                .join(" · ")}
            </p>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function RunHistory({
  runs,
  onInspectRun,
}: {
  readonly runs: readonly EvaluationRunSummary[];
  readonly onInspectRun?: (runId: string) => Promise<VerifyRunInspection>;
}) {
  const [inspection, setInspection] = useState<VerifyRunInspection | null>(
    null,
  );
  const [loadingRunId, setLoadingRunId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (runs.length === 0) return null;
  const latest = latestFirst(runs)[0];
  const active = latest.status === "queued" || latest.status === "running";
  return (
    <details
      className="wb-cowork-verify-history"
      open={active || inspection !== null}
    >
      <summary>
        <span>Verify runs</span>
        <span className="wb-cowork-verify-history__summary">
          {RUN_LABEL[latest.status]} · {latest.targetLabel}
        </span>
      </summary>
      <ol className="wb-cowork-verify-history__list">
        {latestFirst(runs).map((run) => (
          <li key={run.runId}>
            <span className="wb-cowork-verify-history__status">
              {RUN_LABEL[run.status]}
            </span>
            <span>{run.targetLabel}</span>
            <span>{run.coverageLabel}</span>
            {!run.currentVersion ? <span>Earlier version</span> : null}
            {run.coordinationStatus === "unavailable" ? (
              <span role="status">Coordination unavailable</span>
            ) : null}
            {onInspectRun !== undefined ? (
              <button
                type="button"
                disabled={loadingRunId !== null}
                onClick={() => {
                  setLoadingRunId(run.runId);
                  setError(null);
                  void onInspectRun(run.runId)
                    .then(
                      (value) => setInspection(value),
                      (cause: unknown) =>
                        setError(
                          cause instanceof Error
                            ? cause.message
                            : "Verify run details could not be loaded.",
                        ),
                    )
                    .finally(() => setLoadingRunId(null));
                }}
              >
                {loadingRunId === run.runId ? "Loading…" : "Inspect"}
              </button>
            ) : null}
          </li>
        ))}
      </ol>
      {inspection !== null ? <RunInspection inspection={inspection} /> : null}
      {error !== null ? <p role="alert">{error}</p> : null}
    </details>
  );
}

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
    props.runs.length === 0 &&
    visibleResults.length === 0 &&
    visibleRechecks.length === 0 &&
    visibleCothink.length === 0 &&
    visibleCothinkOutcomes.length === 0
  ) {
    return null;
  }
  return (
    <section className="wb-cowork-attention" aria-label="Verify and Co-think">
      <RunHistory runs={props.runs} onInspectRun={props.onInspectRun} />
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

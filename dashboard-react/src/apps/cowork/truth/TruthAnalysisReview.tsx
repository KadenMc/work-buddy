import { useEffect, useId, useRef, useState } from "react";

import type {
  TruthAnalysisCandidate,
  TruthAnalysisCandidateDecisionRequest,
  TruthAnalysisRun,
  TruthAnalysisSourceCoverage,
  TruthEvidenceRelationship,
  TruthExpressionRole,
} from "./contracts";

const pretty = (value: string): string =>
  value
    .split("_")
    .join(" ")
    .replace(/^./u, (first) => first.toLocaleUpperCase());

const safeSourceUrl = (value: string): string | null => {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:"
      ? url.toString()
      : null;
  } catch {
    return null;
  }
};

const relationshipLabel = (value: TruthEvidenceRelationship): string => {
  const labels: Record<TruthEvidenceRelationship, string> = {
    supports: "Supports",
    partially_supports: "Partly supports",
    contradicts: "Contradicts",
    mentions: "Mentions",
    does_not_address: "Does not address",
    inconclusive: "Inconclusive",
  };
  return labels[value];
};

const selectableSupport = (candidate: TruthAnalysisCandidate["evidence"][number]): boolean =>
  candidate.attachable &&
  ((candidate.sourceKind === "truth_span" &&
    candidate.integrityState === "recorded") ||
    (candidate.sourceKind === "web_fetch" &&
      candidate.integrityState === "captured_runtime")) &&
  (candidate.relationship === "supports" ||
    candidate.relationship === "partially_supports");

const runMessage = (run: TruthAnalysisRun): string => {
  const pendingCount = run.candidates.filter(
    (candidate) => candidate.status === "pending",
  ).length;
  switch (run.status) {
    case "queued":
      return "Analysis queued.";
    case "running":
      return "Analyzing passage…";
    case "completed_with_failures":
      return "Analysis finished with limitations.";
    case "failed":
      return run.error ?? "Analysis could not finish.";
    case "cancelled":
      return "Analysis cancelled.";
    case "completed":
      return run.candidates.length === 0
        ? "No claims were found to review."
        : pendingCount === 0
          ? "Review complete."
          : pendingCount === run.candidates.length
            ? `${pendingCount.toLocaleString()} ${pendingCount === 1 ? "claim" : "claims"} ready to review.`
            : `${pendingCount.toLocaleString()} left to review.`;
  }
};

const candidateStatusLabel = (candidate: TruthAnalysisCandidate): string => {
  if (candidate.status === "pending") return "";
  if (candidate.status === "dismissed") return "Skipped";
  return candidate.decision === "connect_existing"
    ? "Connected"
    : "Added as proposed";
};

const coverageStatusLabel = (
  status: TruthAnalysisSourceCoverage["status"],
): string => {
  switch (status) {
    case "supplied":
      return "Provided as context";
    case "searched":
      return "Checked";
    case "partial":
      return "Partly checked";
    case "not_searched":
      return "Not checked";
    case "unavailable":
      return "Unavailable";
    case "failed":
      return "Check failed";
  }
};

function CoverageList({
  coverage,
}: {
  readonly coverage: readonly TruthAnalysisSourceCoverage[];
}) {
  if (coverage.length === 0) return null;
  return (
    <section className="wb-cowork-truth-analysis__coverage">
      <h5>Analysis coverage</h5>
      <ul>
        {coverage.map((item, index) => (
          <li key={`${item.source}:${String(index)}`}>
            <strong>{pretty(item.source)}</strong>
            <span>{coverageStatusLabel(item.status)}</span>
            {item.externalEgress === true ? <span>External sharing</span> : null}
            {item.detail === null ? null : <p>{item.detail}</p>}
          </li>
        ))}
      </ul>
    </section>
  );
}

interface CandidateCardProps {
  readonly run: TruthAnalysisRun;
  readonly candidate: TruthAnalysisCandidate;
  readonly expanded: boolean;
  readonly busy: boolean;
  readonly decisionsLocked: boolean;
  readonly error: string | null;
  readonly canModify: boolean;
  readonly allowedClaimKinds: readonly string[];
  readonly restoreToSummary: boolean;
  onRestoreSummary(): void;
  onReveal(): void;
  onToggle(): void;
  onDecide(request: TruthAnalysisCandidateDecisionRequest): Promise<void>;
}

function CandidateCard({
  run,
  candidate,
  expanded,
  busy,
  decisionsLocked,
  error,
  canModify,
  allowedClaimKinds,
  restoreToSummary,
  onRestoreSummary,
  onReveal,
  onToggle,
  onDecide,
}: CandidateCardProps) {
  const [editing, setEditing] = useState(false);
  const [proposition, setProposition] = useState(candidate.proposition);
  const [claimKind, setClaimKind] = useState(candidate.claimKind);
  const [role, setRole] = useState<TruthExpressionRole>(
    candidate.expression.role,
  );
  const [selectedEvidence, setSelectedEvidence] = useState<readonly string[]>(
    [],
  );
  const propositionId = useId();
  const kindId = useId();
  const roleId = useId();
  const toggleRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    setEditing(false);
    setProposition(candidate.proposition);
    setClaimKind(candidate.claimKind);
    setRole(candidate.expression.role);
    setSelectedEvidence([]);
    // A new canonical revision is a new review subject. Ordinary polling of
    // the same revision must not erase a person's in-progress edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candidate.canonicalSha256]);

  const pending = candidate.status === "pending";
  const reportedCoverage =
    candidate.sourceCoverage.length > 0
      ? candidate.sourceCoverage
      : run.sourceCoverage;
  const evidenceWasSearched = reportedCoverage.some(
    (item) =>
      item.source !== "selected_passage" &&
      (item.status === "searched" || item.status === "partial"),
  );
  const claimIdentityEdited =
    proposition.trim() !== candidate.proposition ||
    claimKind.trim() !== candidate.claimKind;
  const matchCanConnect =
    candidate.existingClaimMatch?.relationship === "exact" ||
    candidate.existingClaimMatch?.relationship === "equivalent";
  const connectExisting = matchCanConnect && !claimIdentityEdited;
  const decideAndRestoreFocus = (
    request: TruthAnalysisCandidateDecisionRequest,
  ): void => {
    void onDecide(request).then(
      () => {
        if (restoreToSummary) onRestoreSummary();
        else toggleRef.current?.focus();
      },
      () => toggleRef.current?.focus(),
    );
  };
  const save = (): void => {
    if (!pending || decisionsLocked || proposition.trim().length === 0) return;
    decideAndRestoreFocus({
      analysisRunId: run.analysisRunId,
      candidateId: candidate.candidateId,
      decision: connectExisting ? "connect_existing" : "save_as_proposed",
      expectedCanonicalSha256: candidate.canonicalSha256,
      ...(connectExisting
        ? { existingClaimId: candidate.existingClaimMatch!.claimId }
        : {}),
      edits: {
        proposition: proposition.trim(),
        claimKind: claimKind.trim(),
        expressionRole: role,
        evidenceCandidateIds: selectedEvidence,
      },
    });
  };

  const dismiss = (): void => {
    if (!pending || decisionsLocked) return;
    decideAndRestoreFocus({
      analysisRunId: run.analysisRunId,
      candidateId: candidate.candidateId,
      decision: "dismiss",
      expectedCanonicalSha256: candidate.canonicalSha256,
    });
  };

  return (
    <li
      className="wb-cowork-truth-analysis__candidate"
      data-status={candidate.status}
    >
      <button
        ref={toggleRef}
        type="button"
        className="wb-cowork-truth-analysis__candidate-toggle"
        aria-expanded={expanded}
        onClick={onToggle}
      >
        <span>{candidate.proposition}</span>
        <small>
          {pretty(candidate.claimKind)} · {pretty(candidate.expression.role)}
          {candidate.status === "pending" ? "" : ` · ${candidateStatusLabel(candidate)}`}
        </small>
      </button>

      {expanded ? (
        <div className="wb-cowork-truth-analysis__candidate-body">
          <section>
            <h5>Passage</h5>
            <blockquote>{candidate.expression.quote}</blockquote>
            <button type="button" onClick={onReveal}>
              Show in document
            </button>
            {candidate.confidenceExtraction === null ? null : (
              <small>
                Expression match confidence: {Math.round(candidate.confidenceExtraction * 100)}%
              </small>
            )}
          </section>

          {candidate.existingClaimMatch === null ? (
            <p className="wb-cowork-truth-analysis__match">New claim</p>
          ) : (
            <section className="wb-cowork-truth-analysis__match">
              <h5>Existing claim match</h5>
              <p>{candidate.existingClaimMatch.proposition}</p>
              <small>
                {pretty(candidate.existingClaimMatch.relationship)}
                {candidate.existingClaimMatch.confidence === null
                  ? ""
                  : ` · ${Math.round(candidate.existingClaimMatch.confidence * 100)}% match`}
              </small>
              {candidate.existingClaimMatch.rationale === null ? null : (
                <p>{candidate.existingClaimMatch.rationale}</p>
              )}
            </section>
          )}

          <section className="wb-cowork-truth-analysis__evidence">
            <h5>Possible evidence</h5>
            {candidate.evidence.length === 0 ? (
              <p>
                {evidenceWasSearched
                  ? "No possible evidence was found in the sources checked."
                  : "Evidence sources were not searched for this claim."}
              </p>
            ) : (
              <ul>
                {candidate.evidence.map((item) => (
                  <li key={item.evidenceCandidateId}>
                    <div>
                      <strong>{relationshipLabel(item.relationship)}</strong>
                      {safeSourceUrl(item.sourceLocator) === null ? (
                        <span>{item.sourceTitle ?? item.sourceLocator}</span>
                      ) : (
                        <a
                          href={safeSourceUrl(item.sourceLocator) ?? undefined}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          {item.sourceTitle ?? item.sourceLocator}
                        </a>
                      )}
                      {item.quote === null ? null : <blockquote>{item.quote}</blockquote>}
                      {item.rationale === null ? null : <p>{item.rationale}</p>}
                      {item.capture?.textTruncated === true ? (
                        <p className="wb-cowork-truth-analysis__partial-capture">
                          <strong>Partially captured.</strong>{" "}
                          {item.capture.capturedTextBytes.toLocaleString()} of{" "}
                          {item.capture.extractedTextBytes.toLocaleString()} extracted bytes
                          were available for analysis.
                        </p>
                      ) : null}
                      {item.trustClass === null && item.integrityState === null ? null : (
                        <small>
                          {[
                            item.sourceKind === "passage_citation"
                              ? "citation cue"
                              : null,
                            item.trustClass,
                            item.integrityState,
                          ]
                            .filter((value): value is string => value !== null)
                            .map(pretty)
                            .join(" · ")}
                        </small>
                      )}
                    </div>
                    {selectableSupport(item) ? (
                      <label className="wb-cowork-truth-analysis__evidence-choice">
                        <input
                          type="checkbox"
                          aria-label={`Attach as support: ${item.sourceTitle ?? item.sourceLocator}`}
                          checked={selectedEvidence.includes(item.evidenceCandidateId)}
                          onChange={(event) => {
                            const checked = event.currentTarget.checked;
                            setSelectedEvidence((current) =>
                              checked
                                ? [...current, item.evidenceCandidateId]
                                : current.filter(
                                    (id) => id !== item.evidenceCandidateId,
                                  ),
                            );
                          }}
                        />
                        <span>Attach as support</span>
                      </label>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </section>

          {candidate.limitations.length === 0 ? null : (
            <section className="wb-cowork-truth-analysis__limitations">
              <h5>Limitations</h5>
              <ul>
                {candidate.limitations.map((limitation) => (
                  <li key={limitation}>{limitation}</li>
                ))}
              </ul>
            </section>
          )}

          {editing ? (
            <fieldset
              className="wb-cowork-truth-analysis__edit"
              disabled={decisionsLocked}
            >
              <legend>Edit details</legend>
              <label htmlFor={propositionId}>Claim</label>
              <textarea
                id={propositionId}
                rows={3}
                value={proposition}
                onChange={(event) => setProposition(event.currentTarget.value)}
              />
              <label htmlFor={kindId}>Kind</label>
              {allowedClaimKinds.length === 0 ? (
                <input id={kindId} value={claimKind} readOnly />
              ) : (
                <select
                  id={kindId}
                  value={claimKind}
                  onChange={(event) => setClaimKind(event.currentTarget.value)}
                >
                  {allowedClaimKinds.map((kind) => (
                    <option key={kind} value={kind}>{pretty(kind)}</option>
                  ))}
                </select>
              )}
              <label htmlFor={roleId}>How the passage expresses it</label>
              <select
                id={roleId}
                value={role}
                onChange={(event) =>
                  setRole(event.currentTarget.value as TruthExpressionRole)
                }
              >
                <option value="quote">Directly states it</option>
                <option value="paraphrase">Paraphrases it</option>
                <option value="summary">Summarizes it</option>
                <option value="instantiation">Gives a concrete instance</option>
              </select>
            </fieldset>
          ) : null}

          {error === null ? null : (
            <p className="wb-cowork-truth__error" role="alert">{error}</p>
          )}
          {pending && canModify ? (
            <div className="wb-cowork-truth-analysis__actions">
              <button
                type="button"
                onClick={() => setEditing((current) => !current)}
                disabled={decisionsLocked}
              >
                {editing ? "Done editing" : "Edit details"}
              </button>
              <button
                type="button"
                className="is-primary"
                onClick={save}
                disabled={decisionsLocked || proposition.trim().length === 0}
              >
                {busy
                  ? "Saving…"
                  : connectExisting
                    ? "Connect existing claim"
                    : "Add as proposed"}
              </button>
              <button type="button" onClick={dismiss} disabled={decisionsLocked}>
                Skip
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

export interface TruthAnalysisReviewProps {
  readonly run: TruthAnalysisRun;
  readonly busyCandidateId?: string | null;
  readonly errorCandidateId?: string | null;
  readonly error?: string | null;
  readonly canModify?: boolean;
  readonly allowedClaimKinds?: readonly string[];
  onFocusCandidate?(candidate: TruthAnalysisCandidate | null): void;
  onRevealCandidate?(candidate: TruthAnalysisCandidate): void;
  onDecide(request: TruthAnalysisCandidateDecisionRequest): Promise<void>;
}

/** Durable AI-prepared suggestions rendered without hiding ledger browsing. */
export function TruthAnalysisReview({
  run,
  busyCandidateId = null,
  errorCandidateId = null,
  error = null,
  canModify = true,
  allowedClaimKinds = [],
  onFocusCandidate,
  onRevealCandidate,
  onDecide,
}: TruthAnalysisReviewProps) {
  const [expandedCandidateId, setExpandedCandidateId] = useState<string | null>(
    null,
  );
  const contentId = useId();
  const summaryButtonRef = useRef<HTMLButtonElement | null>(null);
  // A restored or newly completed run stays a compact, stable row until the
  // person explicitly opens it; async completion never grows above the ledger.
  const [collapsed, setCollapsed] = useState(true);
  const visibleCandidates = run.candidates;
  const pendingCount = run.candidates.filter(
    (candidate) => candidate.status === "pending",
  ).length;

  useEffect(() => {
    if (run.candidates.length > 0 && pendingCount === 0) setCollapsed(true);
  }, [pendingCount, run.candidates.length]);

  useEffect(() => {
    const focused = collapsed
      ? null
      : run.candidates.find(
          (candidate) => candidate.candidateId === expandedCandidateId,
        ) ?? null;
    onFocusCandidate?.(focused);
    return () => onFocusCandidate?.(null);
  }, [collapsed, expandedCandidateId, onFocusCandidate, run.candidates]);

  return (
    <section className="wb-cowork-truth-analysis" aria-label="Truth analysis">
      <header className="wb-cowork-truth-analysis__head">
        <div>
          <strong role="status" aria-live="polite">{runMessage(run)}</strong>
          <small>
            {run.targetLabel}
            {run.execution.providerLabel.length > 0 || run.execution.modelLabel.length > 0
              ? ` · ${[run.execution.providerLabel, run.execution.modelLabel]
                  .filter((value) => value.length > 0)
                  .join(" · ")}`
              : ""}
          </small>
        </div>
        <button
          ref={summaryButtonRef}
          type="button"
          aria-expanded={!collapsed}
          aria-controls={contentId}
          onClick={() => setCollapsed((value) => !value)}
        >
          {collapsed
            ? pendingCount > 0
              ? "Review"
              : run.candidates.length > 0
                ? "View"
              : "Details"
            : "Hide"}
        </button>
      </header>

      <div
        id={contentId}
        className="wb-cowork-truth-analysis__content"
        hidden={collapsed}
      >
        {visibleCandidates.length === 0 ? null : (
          <ul className="wb-cowork-truth-analysis__candidates">
            {visibleCandidates.map((candidate) => (
              <CandidateCard
                key={candidate.candidateId}
                run={run}
                candidate={candidate}
                expanded={expandedCandidateId === candidate.candidateId}
                busy={busyCandidateId === candidate.candidateId}
                decisionsLocked={busyCandidateId !== null}
                error={errorCandidateId === candidate.candidateId ? error : null}
                canModify={canModify}
                allowedClaimKinds={allowedClaimKinds}
                restoreToSummary={pendingCount === 1 && candidate.status === "pending"}
                onRestoreSummary={() => summaryButtonRef.current?.focus()}
                onReveal={() => onRevealCandidate?.(candidate)}
                onToggle={() =>
                  setExpandedCandidateId((current) =>
                    current === candidate.candidateId ? null : candidate.candidateId,
                  )
                }
                onDecide={onDecide}
              />
            ))}
          </ul>
        )}

        <CoverageList coverage={run.sourceCoverage} />

        {run.limitations.length === 0 ? null : (
          <section className="wb-cowork-truth-analysis__run-limitations">
            <h5>Limitations</h5>
            <ul className="wb-cowork-truth-analysis__limitations">
              {run.limitations.map((limitation) => (
                <li key={limitation}>{limitation}</li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </section>
  );
}

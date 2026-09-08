import { useEffect, useId, useRef, useState } from "react";

import type {
  TruthClaimDecision,
  TruthClaimDecisionRequest,
  TruthClaimDetail,
  TruthPassageConnection,
  TruthRedactionReason,
  TruthEvidenceReceipt,
  TruthEditorIntegration,
} from "./contracts";
import { isTruthFact, orderTruthPassages } from "./contracts";
import type { TruthDecisionRequest } from "./store";

const pretty = (value: string): string =>
  value
    .split("_").join(" ")
    .replace(/^./u, (first) => first.toLocaleUpperCase());

const claimKindLabel = (kind: string): string =>
  kind === "fact" ? "Factual claim" : pretty(kind);

const displayTime = (value: string): string => {
  if (value.length === 0) return "Time not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
};

const healthReasonLabel = (value: string): string => {
  const known: Record<string, string> = {
    active_needs_review_overlay: "This claim needs human review.",
    single_confirmed_successor_race:
      "More than one successor was confirmed at the same time.",
    content_redacted_after_belief:
      "Readable content was redacted after this claim entered the record.",
    missing_base_status: "This claim has no usable lifecycle status.",
  };
  return value
    .split(",")
    .map((reason) => known[reason] ?? `${pretty(reason)}.`)
    .join(" ");
};

const actorLabel = (kind: string, ref: string | null): string => {
  const role =
    kind === "human"
      ? "Human"
      : kind === "agent_run"
        ? "Agent"
        : pretty(kind);
  return ref === null || ref.length === 0 ? role : `${role} · ${ref}`;
};

const stateSentence = (claim: TruthClaimDetail): string => {
  if (claim.redacted) return "This claim's readable content was redacted.";
  if (claim.voided) return "This claim is voided and is not a current fact.";
  if (claim.needsReview || claim.health === "needs_review") return "This claim needs human review before it can be treated as a current fact.";
  if (isTruthFact(claim)) return "This claim is a current fact.";
  if (claim.baseStatus === "proposed") return "This claim is proposed and has not been confirmed.";
  if (claim.baseStatus === "rejected") return "This proposed claim was rejected. Its history remains available.";
  if (claim.baseStatus === "challenged") return "This claim is challenged and needs a human decision.";
  return `This claim is ${pretty(claim.baseStatus).toLocaleLowerCase()} and is not a current fact.`;
};

const trustSentence = (receipt: TruthEvidenceReceipt): string => {
  if (receipt.trustClass.includes("quarantin")) return "This source is quarantined and cannot be used to confirm this claim here.";
  if (receipt.trustClass === "unattested" || receipt.trustClass === "unknown") return "This source's trust has not been established.";
  if (receipt.trustClass === "measurement") return "This receipt records a measurement; review its source and method before relying on it.";
  if (receipt.authorKind === "agent_run" || receipt.authorKind === "agent" || receipt.trustClass === "agent_authored") return "This support was written by an agent.";
  if (receipt.authorKind === "human" || receipt.trustClass === "human_authored") return "This support was written by a person.";
  return `The source is recorded as ${pretty(receipt.trustClass).toLocaleLowerCase()}.`;
};

const staleSentence = (connection: TruthPassageConnection): string | null => {
  if (connection.stale === "span_missing") return "This passage no longer matches the document version it was connected to. Review its location.";
  if (connection.stale === "claim_changed") return "The claim has changed since this passage was connected.";
  if (connection.stale === "claim_terminal") return "The connected claim is no longer active.";
  return null;
};

const passageRoleLabel = (role: TruthPassageConnection["role"]): string => ({
  quote: "Directly states the claim", paraphrase: "Paraphrases the claim",
  summary: "Summarizes the claim", instantiation: "Gives an instance of the claim",
})[role];

const structuredValue = (value: unknown): string => {
  if (value === null) return "None";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "Unprintable structured value";
  }
};

const availableDecisions = (
  claim: TruthClaimDetail,
): readonly TruthClaimDecision[] => {
  const supported = new Set<TruthClaimDecision>([
    "confirm",
    "reaffirm",
    "reject",
    "redact",
  ]);
  return claim.availableActions.filter(
    (action): action is TruthClaimDecision => supported.has(action as TruthClaimDecision),
  );
};

interface DecisionDraft {
  readonly decision: TruthClaimDecision;
  readonly reason: TruthRedactionReason;
}

const initialDraft = (decision: TruthClaimDecision): DecisionDraft => ({
  decision,
  reason: "privacy",
});

export interface TruthClaimDetailsProps {
  readonly claim: TruthClaimDetail;
  readonly readOnly?: boolean;
  readonly busy?: boolean;
  readonly error?: string | null;
  readonly refreshError?: string | null;
  readonly active?: boolean;
  readonly requestedDecision?: TruthDecisionRequest | null;
  readonly orderPassages?: TruthEditorIntegration["orderPassages"];
  onClose(): void;
  onRetryRefresh?(): void;
  onRevealPassage?(connection: TruthPassageConnection): void;
  onDecide?(request: TruthClaimDecisionRequest): void | Promise<void>;
  onAddCorrectedClaim?(claim: TruthClaimDetail): void;
}

export function TruthClaimDetails({
  claim,
  readOnly = false,
  busy = false,
  error = null,
  refreshError = null,
  active = true,
  requestedDecision = null,
  orderPassages,
  onClose,
  onRetryRefresh,
  onRevealPassage,
  onDecide,
  onAddCorrectedClaim,
}: TruthClaimDetailsProps) {
  const [draft, setDraft] = useState<DecisionDraft | null>(null);
  const [lastRevealedPassageId, setLastRevealedPassageId] = useState<string | null>(null);
  const reasonId = useId();
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const confirmationRef = useRef<HTMLElement | null>(null);
  const decisionTriggerRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => { setDraft(null); }, [claim.claimId, claim.canonicalSha256, claim.decisionBinding?.contextSha256]);
  useEffect(() => { setLastRevealedPassageId(null); }, [claim.claimId, claim.canonicalSha256]);
  useEffect(() => {
    if (requestedDecision?.claimId !== claim.claimId || readOnly || claim.decisionBinding === null) return;
    if (availableDecisions(claim).includes(requestedDecision.action)) {
      setDraft(initialDraft(requestedDecision.action));
    }
  }, [requestedDecision, claim.claimId, claim.canonicalSha256, readOnly]);
  useEffect(() => {
    if (!active) return;
    const focused = document.activeElement;
    const focusWasDisplaced =
      focused === null ||
      focused === document.body ||
      !focused.isConnected ||
      focused.closest("[hidden]") !== null;
    if (focusWasDisplaced) headingRef.current?.focus();
  }, [active, claim.claimId]);
  useEffect(() => {
    if (draft !== null) confirmationRef.current?.focus();
  }, [draft]);

  const decisions = availableDecisions(claim);
  const structuredEntries = Object.entries(claim.structured);
  const connections = orderPassages?.(claim.connections) ?? orderTruthPassages(claim.connections);
  const navigableConnections = connections;
  const submitDecision = (): void => {
    if (draft === null || onDecide === undefined || busy) return;
    if (claim.decisionBinding === null) return;
    void Promise.resolve(
      onDecide({
        claimId: claim.claimId,
        action: draft.decision,
        expectedCanonicalSha256: claim.decisionBinding.payloadSha256,
        expectedContextSha256: claim.decisionBinding.contextSha256,
        ...(draft.decision === "confirm" || draft.decision === "reaffirm"
          ? { gestureKind: draft.decision }
          : {}),
        ...(draft.decision === "redact" ? { reason: draft.reason } : {}),
      }),
    ).then(
      () => {
        setDraft(null);
        window.requestAnimationFrame(() => headingRef.current?.focus());
      },
      () => undefined,
    );
  };

  const openDecision = (
    decision: TruthClaimDecision,
    trigger: HTMLButtonElement,
  ): void => {
    decisionTriggerRef.current = trigger;
    setDraft(initialDraft(decision));
  };

  const cancelDecision = (): void => {
    setDraft(null);
    window.requestAnimationFrame(() => decisionTriggerRef.current?.focus());
  };

  return (
    <section className="wb-cowork-truth__details" aria-label="Claim details">
      <div className="wb-cowork-truth__details-head">
        <div>
          <span className="wb-cowork-truth__claim-kind">
            {claimKindLabel(claim.claimKind)}
          </span>
          <h3 ref={headingRef} tabIndex={-1}>{claim.proposition}</h3>
        </div>
        <button
          type="button"
          className="wb-cowork-truth__close"
          disabled={busy}
          onClick={onClose}
        >
          Close
        </button>
      </div>

      <p className="wb-cowork-truth__state-summary">{stateSentence(claim)}</p>
      {claim.provenance?.preparedBy ? (
        <p className="wb-cowork-truth__preparation">
          Prepared by an analysis ({claim.provenance.preparedBy.providerId} · {claim.provenance.preparedBy.modelId}).
          {claim.createdAt.length === 0 ? "" : ` Added to Truth ${displayTime(claim.createdAt)}.`}
        </p>
      ) : claim.createdBy?.kind === "agent_run" || claim.createdBy?.kind === "agent" ? (
        <p className="wb-cowork-truth__preparation">Prepared by an agent run.</p>
      ) : null}
      {claim.healthReason !== null ? (
        <p className="wb-cowork-truth__notice">
          {healthReasonLabel(claim.healthReason)}
        </p>
      ) : null}
      {refreshError === null ? null : (
        <div className="wb-cowork-truth__refresh-warning" role="status">
          <span>{refreshError}</span>
          {onRetryRefresh === undefined ? null : (
            <button type="button" onClick={onRetryRefresh}>Try again</button>
          )}
        </div>
      )}

      <section className="wb-cowork-truth__detail-section">
        <h4>Passages</h4>
        {claim.connections.length === 0 ? (
          <p className="wb-cowork-truth__empty-inline">This claim is not connected to document prose.</p>
        ) : (
          <ul className="wb-cowork-truth__connection-list">
            {connections.map((connection) => (
              <li key={connection.expressionId || `${connection.documentId}:${connection.spanId}`}>
                <span>
                  <strong>{passageRoleLabel(connection.role)}</strong>
                  {connection.documentTitle !== null
                    ? ` in ${connection.documentTitle}`
                    : connection.documentPath !== null
                      ? ` in ${connection.documentPath}`
                      : ""}
                </span>
                <q>{connection.quote}</q>
                {connection.provenance?.preparedBy !== null &&
                connection.provenance?.preparedBy !== undefined ? (
                  <>
                    <p className="wb-cowork-truth__connection-meta">
                      Prepared by the analysis ({connection.provenance.preparedBy.providerId} ·{" "}
                      {connection.provenance.preparedBy.modelId})
                    </p>
                    <p className="wb-cowork-truth__connection-meta">
                      Added
                      {connection.provenance.addedBy.at.length === 0
                        ? ""
                        : ` ${displayTime(connection.provenance.addedBy.at)}`} by{" "}
                      {connection.provenance.addedBy.kind === "human" ? "a person" : "an agent"}
                    </p>
                  </>
                ) : connection.createdAt.length > 0 || connection.createdBy !== null ? (
                  <p className="wb-cowork-truth__connection-meta">
                    Connected
                    {connection.createdAt.length === 0
                      ? ""
                      : ` ${displayTime(connection.createdAt)}`}
                    {connection.createdBy === null
                      ? ""
                      : connection.createdBy.kind === "human" ? " by a person" : " by an agent run"}
                  </p>
                ) : null}
                {staleSentence(connection) === null ? null : <p className="wb-cowork-truth__notice">{staleSentence(connection)}</p>}
                {onRevealPassage !== undefined ? (
                  <button type="button" onClick={() => {
                    onRevealPassage(connection);
                    setLastRevealedPassageId(connection.expressionId);
                  }}>
                    {connection.currentDocument
                      ? "Show in document"
                      : "Open and show passage"}
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
        {navigableConnections.length > 1 && onRevealPassage !== undefined ? (
          <button type="button" onClick={() => {
            const lastIndex = navigableConnections.findIndex((connection) => connection.expressionId === lastRevealedPassageId);
            const next = navigableConnections[(lastIndex + 1) % navigableConnections.length];
            onRevealPassage(next);
            setLastRevealedPassageId(next.expressionId);
          }}>Next passage</button>
        ) : null}
      </section>

      <section className="wb-cowork-truth__detail-section">
        <h4>Evidence</h4>
        {claim.support.supportSpanIds.length > 0 ? (
          <p className="wb-cowork-truth__support-summary">
            {claim.support.usableSpanIds.length} of {claim.support.supportSpanIds.length} recorded{" "}
            {claim.support.supportSpanIds.length === 1 ? "receipt is" : "receipts are"} currently usable.
          </p>
        ) : null}
        {claim.support.quarantinedOnly ? (
          <p className="wb-cowork-truth__notice">
            The only otherwise usable support is quarantined, so this claim cannot be confirmed here.
          </p>
        ) : null}
        {claim.support.storeDerivedOnly ? (
          <p className="wb-cowork-truth__notice">
            Every recorded source comes from this Truth record; no independent source is attached.
          </p>
        ) : null}
        {claim.decisionBinding?.agentAuthoredOnly === true || claim.support.agentAuthoredOnly ? (
          <p className="wb-cowork-truth__notice is-important">
            The recorded support is entirely agent-authored. Confirming this claim would be a human decision based only on agent-authored material.
          </p>
        ) : null}
        {claim.receipts.length === 0 ? (
          <p className="wb-cowork-truth__empty-inline">No evidence recorded.</p>
        ) : (
          <ul className="wb-cowork-truth__evidence-list">
            {claim.receipts.map((receipt) => (
              <li key={receipt.linkId || `${receipt.evidenceId}:${receipt.spanId}`}>
                <div>
                  <strong>{receipt.sourceLocator}</strong>
                  <span>{trustSentence(receipt)}</span>
                </div>
                {receipt.quote === null ? null : <blockquote>{receipt.quote}</blockquote>}
                {receipt.integrity === null ? null : (
                  <p className="wb-cowork-truth__source-check" data-state={receipt.integrity.state}>
                    Source check: {pretty(receipt.integrity.state)}
                    {receipt.integrity.detail === null ? "" : `. ${receipt.integrity.detail}`}
                  </p>
                )}
                <details className="wb-cowork-truth__source-details">
                  <summary>Source details</summary>
                  <dl>
                    {receipt.mediaType === null ? null : <><dt>Media type</dt><dd>{receipt.mediaType}</dd></>}
                    {receipt.acquiredAt === null ? null : <><dt>Acquired</dt><dd>{displayTime(receipt.acquiredAt)}</dd></>}
                    {receipt.acquisitionMethod === null ? null : <><dt>Method</dt><dd>{pretty(receipt.acquisitionMethod)}</dd></>}
                    {receipt.integrity?.verifiabilityClass === null || receipt.integrity?.verifiabilityClass === undefined ? null : <><dt>Verifiability</dt><dd>{pretty(receipt.integrity.verifiabilityClass)}</dd></>}
                    {receipt.integrity === null ? null : <><dt>Captured copy</dt><dd>{receipt.integrity.snapshotPresent ? "Available" : "Not recorded"}</dd></>}
                    {receipt.derivedFromStore === null ? null : <><dt>Derived from</dt><dd>{receipt.derivedFromStore}</dd></>}
                    {receipt.spanSha256.length === 0 ? null : <><dt>Passage fingerprint</dt><dd><code>{receipt.spanSha256}</code></dd></>}
                    {receipt.contentSha256.length === 0 ? null : <><dt>Source fingerprint</dt><dd><code>{receipt.contentSha256}</code></dd></>}
                  </dl>
                </details>
              </li>
            ))}
          </ul>
        )}
      </section>

      {claim.conflicts.length > 0 ? (
        <section className="wb-cowork-truth__detail-section">
          <h4>Challenges and conflicts</h4>
          <ul className="wb-cowork-truth__plain-list">
            {claim.conflicts.map((conflict) => (
              <li key={conflict.relationId || conflict.claimId}>
                {conflict.direction === "challenged_by"
                  ? "Challenged by: "
                  : conflict.direction === "challenges"
                    ? "Challenges: "
                    : "Conflict: "}
                {conflict.proposition ?? `Claim ${conflict.claimId.slice(0, 8)}`}
                {conflict.conflictType === null ? "" : ` · ${pretty(conflict.conflictType)}`}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {claim.derivations.length > 0 ? (
        <section className="wb-cowork-truth__detail-section">
          <h4>Derivation</h4>
          {claim.derivations.map((derivation, index) => (
            <div key={`${derivation.method}:${String(index)}`} className="wb-cowork-truth__derivation">
              <strong>{pretty(derivation.method)}</strong>
              {derivation.rationale === null ? null : <p>{derivation.rationale}</p>}
              {derivation.premises.length === 0 ? null : (
                <ul>
                  {derivation.premises.map((premise) => (
                    <li key={`${premise.kind}:${premise.ref}`}>
                      {premise.proposition ?? premise.ref}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
        </section>
      ) : null}

      {claim.derivations.length > 0 ||
      claim.premises.localUnconfirmed.length > 0 ||
      claim.premises.unresolvedUris.length > 0 ? (
        <section className="wb-cowork-truth__detail-section">
          <h4>Premise check</h4>
          <p>{claim.premises.confirmed ? "All recorded premises are confirmed." : "Some premises are not currently confirmed."}</p>
          {claim.premises.localUnconfirmed.length === 0 ? null : (
            <p>{claim.premises.localUnconfirmed.length} local {claim.premises.localUnconfirmed.length === 1 ? "premise needs" : "premises need"} confirmation.</p>
          )}
          {claim.premises.unresolvedUris.length === 0 ? null : (
            <p>{claim.premises.unresolvedUris.length} external {claim.premises.unresolvedUris.length === 1 ? "premise is" : "premises are"} unavailable.</p>
          )}
        </section>
      ) : null}

      {readOnly ? (
        <p className="wb-cowork-truth__notice" role="status">
          Truth is read-only. You can still inspect claims and their evidence.
        </p>
      ) : decisions.length > 0 && onDecide !== undefined && claim.decisionBinding !== null ? (
        <section className="wb-cowork-truth__decisions" aria-label="Manage claim">
          <h4>Manage claim</h4>
          {draft === null ? (
            <div className="wb-cowork-truth__decision-buttons">
              {decisions.includes("confirm") ? (
                <button type="button" onClick={(event) => openDecision("confirm", event.currentTarget)}>
                  Confirm
                </button>
              ) : null}
              {decisions.includes("reaffirm") ? (
                <button type="button" onClick={(event) => openDecision("reaffirm", event.currentTarget)}>
                  Reaffirm
                </button>
              ) : null}
              {decisions.includes("reject") ? (
                <button type="button" onClick={(event) => openDecision("reject", event.currentTarget)}>
                  Reject
                </button>
              ) : null}
              {decisions.includes("redact") ? (
                <button
                  type="button"
                  className="is-danger"
                  onClick={(event) => openDecision("redact", event.currentTarget)}
                >
                  Redact…
                </button>
              ) : null}
            </div>
          ) : (
            <div className="wb-cowork-truth__decision-confirm">
              <strong ref={confirmationRef} tabIndex={-1}>
                {draft.decision === "confirm"
                  ? "Confirm this exact claim?"
                  : draft.decision === "reaffirm"
                    ? "Reaffirm this exact claim?"
                  : draft.decision === "reject"
                    ? "Reject this proposed claim?"
                    : "Permanently remove readable claim content?"}
              </strong>
              <blockquote>{claim.proposition}</blockquote>
              <p>{claim.receipts.length} evidence {claim.receipts.length === 1 ? "receipt was" : "receipts were"} shown.</p>
              {draft.decision === "reject" ? (
                <p>This marks the proposed claim as rejected. It was not a confirmed fact. Its audit history remains available.</p>
              ) : null}
              {draft.decision === "redact" ? (
                <>
                  <p className="wb-cowork-truth__danger-copy">
                    This permanently removes the readable content. Its non-readable fingerprints and audit history remain.
                  </p>
                  <label htmlFor={reasonId}>Reason for redaction</label>
                  <select
                    id={reasonId}
                    value={draft.reason}
                    disabled={busy}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        reason: event.target.value as TruthRedactionReason,
                      })
                    }
                  >
                    <option value="privacy">Privacy</option>
                    <option value="source_takedown">Source takedown</option>
                    <option value="rejected_content">Rejected content</option>
                    <option value="expired_content">Expired content</option>
                  </select>
                </>
              ) : null}
              {(draft.decision === "confirm" || draft.decision === "reaffirm") &&
              claim.decisionBinding.agentAuthoredOnly ? (
                <p className="wb-cowork-truth__notice is-important">
                  Only agent-authored support is recorded for this claim.
                </p>
              ) : null}
              {error === null ? null : <p className="wb-cowork-truth__error" role="alert">{error}</p>}
              <div className="wb-cowork-truth__decision-buttons">
                <button
                  type="button"
                  className={draft.decision === "redact" ? "is-danger" : "is-primary"}
                  disabled={
                    busy
                  }
                  onClick={submitDecision}
                >
                  {busy
                    ? "Saving…"
                    : draft.decision === "confirm"
                      ? "Confirm claim"
                      : draft.decision === "reaffirm"
                        ? "Reaffirm claim"
                      : draft.decision === "reject"
                        ? "Reject claim"
                        : "Redact claim content"}
                </button>
                <button type="button" disabled={busy} onClick={cancelDecision}>
                  Cancel
                </button>
              </div>
            </div>
          )}
        </section>
      ) : decisions.length > 0 && claim.decisionBinding === null ? (
        <p className="wb-cowork-truth__notice" role="status">
          The information required to manage this claim safely is unavailable. Reload and review it again before making a decision.
        </p>
      ) : null}
      {!readOnly && claim.baseStatus === "rejected" ? (
        <section className="wb-cowork-truth__correction">
          <button type="button" disabled={onAddCorrectedClaim === undefined || !navigableConnections.some((connection) => connection.currentDocument)} onClick={() => onAddCorrectedClaim?.(claim)}>
            Add a corrected claim
          </button>
          {onAddCorrectedClaim === undefined || !navigableConnections.some((connection) => connection.currentDocument) ? (
            <p>A current passage in this document is needed to add a corrected claim.</p>
          ) : null}
        </section>
      ) : null}
      <details className="wb-cowork-truth__record-details">
        <summary>History and record details</summary>
      <dl className="wb-cowork-truth__facts">
        <div>
          <dt>Ledger status</dt>
          <dd>{pretty(claim.baseStatus)}</dd>
        </div>
        <div>
          <dt>Standing</dt>
          <dd>
            {claim.needsReview
              ? "Needs review"
              : isTruthFact(claim)
                ? "Fact"
                : "Not currently a fact"}
          </dd>
        </div>
        <div>
          <dt>Health</dt>
          <dd>{pretty(claim.health)}</dd>
        </div>
        <div>
          <dt>Scope</dt>
          <dd>{claim.scope === "store" ? "Folder" : pretty(claim.scope)}</dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd>{displayTime(claim.createdAt)}</dd>
        </div>
        {claim.provenance?.preparedBy === null ||
        claim.provenance?.preparedBy === undefined ? (
          claim.createdBy === null || claim.createdBy === undefined ? null : (
            <div>
              <dt>Created by</dt>
              <dd>{actorLabel(claim.createdBy.kind, claim.createdBy.ref)}</dd>
            </div>
          )
        ) : (
          <>
            <div>
              <dt>Prepared by</dt>
              <dd>
                {claim.provenance.preparedBy.providerId} · {claim.provenance.preparedBy.modelId}
              </dd>
            </div>
            <div>
              <dt>Added by</dt>
              <dd>
                {actorLabel(
                  claim.provenance.addedBy.kind,
                  claim.provenance.addedBy.ref,
                )}
              </dd>
            </div>
          </>
        )}
        {claim.validFrom == null && claim.validTo == null ? null : (
          <div>
            <dt>Claimed validity</dt>
            <dd>
              {claim.validFrom == null
                ? "From the beginning"
                : displayTime(claim.validFrom)}{" "}
              –{" "}
              {claim.validTo == null ? "open-ended" : displayTime(claim.validTo)}
            </dd>
          </div>
        )}
        {claim.effectiveValidFrom === null && claim.effectiveValidTo === null ? null : (
          <div>
            <dt>Current validity</dt>
            <dd>
              {claim.effectiveValidFrom === null
                ? "From the beginning"
                : displayTime(claim.effectiveValidFrom)}{" "}
              –{" "}
              {claim.effectiveValidTo === null
                ? "present"
                : displayTime(claim.effectiveValidTo)}
            </dd>
          </div>
        )}
      </dl>
      {structuredEntries.length === 0 ? null : (
        <section className="wb-cowork-truth__detail-section">
          <h4>Structured claim details</h4>
          <dl className="wb-cowork-truth__structured">
            {structuredEntries.map(([key, value]) => (
              <div key={key}>
                <dt>{pretty(key)}</dt>
                <dd><pre>{structuredValue(value)}</pre></dd>
              </div>
            ))}
          </dl>
        </section>
      )}
      <section className="wb-cowork-truth__detail-section">
        <h4>History</h4>
        {claim.lifecycle.length === 0 ? (
          <p className="wb-cowork-truth__empty-inline">No lifecycle history was returned.</p>
        ) : (
          <ol className="wb-cowork-truth__timeline">
            {claim.lifecycle.map((event) => (
              <li key={event.eventId}>
                <strong>{pretty(event.status)}</strong>
                <time dateTime={event.at}>{displayTime(event.at)}</time>
                <span>{actorLabel(event.actorKind, event.actorRef)}</span>
                {event.note === null ? null : <p>{event.note}</p>}
              </li>
            ))}
          </ol>
        )}
      </section>

        <dl className="wb-cowork-truth__structured">
          <div><dt>Claim fingerprint</dt><dd><code>{claim.canonicalSha256}</code></dd></div>
          {claim.decisionBinding === null ? null : <div><dt>Decision context fingerprint</dt><dd><code>{claim.decisionBinding.contextSha256}</code></dd></div>}
        </dl>
      </details>
    </section>
  );
}

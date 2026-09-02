import { useState, type SyntheticEvent } from "react";

import { Button } from "../../../ui";
import type {
  CoworkDocumentCapabilityEnvelope,
  CoworkTruthActivation,
} from "../contracts";
import type {
  TruthActivationPolicySnapshot,
  TruthActivationTransitionRequest,
} from "./HttpCoworkTruthClient";
import "./truthActivationControl.css";

export interface TruthActivationClient {
  loadActivationPolicy(): Promise<TruthActivationPolicySnapshot>;
  transitionTruthActivation(
    request: TruthActivationTransitionRequest,
  ): Promise<TruthActivationPolicySnapshot>;
}

export interface TruthActivationPlan {
  readonly nextState: CoworkTruthActivation;
  readonly label: string;
  readonly explanation: string;
  readonly variant: "primary" | "danger";
}

export const truthActivationPlan = (
  envelope: CoworkDocumentCapabilityEnvelope,
): TruthActivationPlan | null => {
  const { eligibility, activation, ledgerPresent } = envelope.truth;
  if (eligibility === "unsupported" || activation === null) return null;
  if (activation === "disabled") {
    return {
      nextState: "enabled",
      label: "Turn on Truth",
      explanation:
        "Truth claims, review, and analysis will become available for this document. Provenance remains available either way.",
      variant: "primary",
    };
  }
  if (activation === "paused") {
    return {
      nextState: "enabled",
      label: "Resume Truth",
      explanation:
        "Existing Truth history will stay visible and new Truth writes and analysis will resume.",
      variant: "primary",
    };
  }
  if (eligibility === "required" || ledgerPresent) {
    return {
      nextState: "paused",
      label: "Pause Truth",
      explanation:
        "Existing Truth history will remain visible, while new Truth writes and analysis stop.",
      variant: "danger",
    };
  }
  return {
    nextState: "disabled",
    label: "Turn off Truth",
    explanation:
      "This document has no Truth ledger history. Truth claims and analysis will be removed from its editing surface; provenance stays on.",
    variant: "danger",
  };
};

const activationLabel = (
  activation: CoworkTruthActivation | null,
): string => {
  if (activation === "enabled") return "On";
  if (activation === "paused") return "Paused";
  return "Off";
};

const errorMessage = (error: unknown): string =>
  error instanceof Error
    ? error.message
    : "Truth settings could not be updated.";

const newIntentId = (): string =>
  `truth-activation:${globalThis.crypto.randomUUID()}`;

/**
 * A deliberate policy control for provenance-capable documents. Opening it
 * refreshes the exact policy/head pair; every mutation then carries those CAS
 * values plus a one-shot human gesture. There is no automatic promotion path.
 */
export function TruthActivationControl({
  client,
  envelope,
  readOnly = false,
  onChanged,
  intentIdFactory = newIntentId,
}: {
  readonly client: TruthActivationClient;
  readonly envelope: CoworkDocumentCapabilityEnvelope;
  readonly readOnly?: boolean;
  readonly onChanged: (envelope: CoworkDocumentCapabilityEnvelope) => void;
  readonly intentIdFactory?: () => string;
}) {
  const [snapshot, setSnapshot] =
    useState<TruthActivationPolicySnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [reason, setReason] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const current = snapshot?.capabilityEnvelope ?? envelope;
  const plan = truthActivationPlan(current);

  if (envelope.truth.eligibility === "unsupported") return null;

  const refresh = async (): Promise<void> => {
    setLoading(true);
    setError(null);
    setMessage(null);
    setConfirmed(false);
    try {
      const observed = await client.loadActivationPolicy();
      setSnapshot(observed);
      onChanged(observed.capabilityEnvelope);
    } catch (cause) {
      setSnapshot(null);
      setError(errorMessage(cause));
    } finally {
      setLoading(false);
    }
  };

  const handleToggle = (event: SyntheticEvent<HTMLDetailsElement>): void => {
    if (event.currentTarget.open) void refresh();
  };

  const transition = async (): Promise<void> => {
    if (snapshot === null || plan === null || !confirmed || busy || readOnly) {
      return;
    }
    const revision = snapshot.capabilityEnvelope.truth.activationRevision;
    const contractDigest =
      snapshot.capabilityEnvelope.interactionContract.digest;
    if (revision === null || contractDigest === null || contractDigest === undefined) {
      setError("Truth returned an incomplete activation policy.");
      return;
    }
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const changed = await client.transitionTruthActivation({
        nextState: plan.nextState,
        expectedActivationRevision: revision,
        expectedInteractionContractSha256: contractDigest,
        expectedDocumentHeadSha256: snapshot.documentHeadSha256,
        intentId: intentIdFactory(),
        ...(reason.trim().length > 0 ? { reason: reason.trim() } : {}),
      });
      setSnapshot(changed);
      setConfirmed(false);
      setReason("");
      setMessage(`Truth is now ${activationLabel(changed.capabilityEnvelope.truth.activation).toLowerCase()}.`);
      onChanged(changed.capabilityEnvelope);
    } catch (cause) {
      setConfirmed(false);
      setError(`${errorMessage(cause)} Refresh the settings before trying again.`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="wb-truth-activation" onToggle={handleToggle}>
      <summary>
        <span>Truth settings</span>
        <strong>{activationLabel(current.truth.activation)}</strong>
      </summary>
      <div className="wb-truth-activation__panel">
        <p>
          Provenance stays available independently. Truth is changed only by
          this explicit, revision-checked action.
        </p>
        {loading ? <p role="status">Loading current Truth policy…</p> : null}
        {!loading && current.truth.unavailableReason ? (
          <p role="alert">{current.truth.unavailableReason}</p>
        ) : null}
        {!loading && readOnly ? (
          <p role="status">This document is read-only.</p>
        ) : null}
        {!loading && !current.truth.unavailableReason && plan !== null ? (
          <>
            <p>{plan.explanation}</p>
            <label className="wb-truth-activation__reason">
              Reason <span>(optional)</span>
              <input
                value={reason}
                onChange={(event) => setReason(event.currentTarget.value)}
                disabled={busy || readOnly || snapshot === null}
              />
            </label>
            <label className="wb-truth-activation__confirm">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(event) => setConfirmed(event.currentTarget.checked)}
                disabled={busy || readOnly || snapshot === null}
              />
              I confirm this explicit Truth change.
            </label>
            <div className="wb-truth-activation__actions">
              <Button
                size="small"
                variant={plan.variant}
                onClick={() => void transition()}
                disabled={
                  busy ||
                  readOnly ||
                  snapshot === null ||
                  !confirmed ||
                  current.truth.unavailableReason !== null
                }
              >
                {busy ? "Updating…" : plan.label}
              </Button>
              <Button
                size="small"
                variant="ghost"
                onClick={() => void refresh()}
                disabled={busy || loading}
              >
                Refresh
              </Button>
            </div>
          </>
        ) : null}
        <p
          className="wb-truth-activation__status"
          role={error === null ? "status" : "alert"}
          aria-live="polite"
        >
          {error ?? message ?? ""}
        </p>
      </div>
    </details>
  );
}

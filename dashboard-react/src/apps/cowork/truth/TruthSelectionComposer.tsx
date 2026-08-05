import { useCallback, useEffect, useId, useRef, useState } from "react";

import type {
  TruthClaimSummary,
  TruthEditorIntegration,
  TruthExpressionRole,
  TruthMutationReceipt,
  TruthRailProvider,
  TruthSelectionCapture,
} from "./contracts";
import type { TruthComposer } from "./store";

const errorMessage = (cause: unknown, fallback: string): string =>
  cause instanceof Error && cause.message.trim().length > 0
    ? cause.message
    : fallback;

const candidateIsConnectable = (claim: TruthClaimSummary): boolean =>
  !claim.redacted &&
  !claim.voided &&
  !["rejected", "retracted", "expired", "superseded"].includes(
    claim.baseStatus,
  );

export interface TruthSelectionComposerProps {
  readonly mode: Exclude<TruthComposer, null>;
  readonly provider: TruthRailProvider;
  readonly editor: TruthEditorIntegration;
  readonly allowedClaimKinds: readonly string[];
  onCancel(): void;
  onComplete(receipt: TruthMutationReceipt): void;
}

const claimKindLabel = (kind: string): string =>
  kind === "fact"
    ? "Factual claim"
    : kind
        .split("_")
        .join(" ")
        .replace(/^./u, (first) => first.toLocaleUpperCase());

export function TruthSelectionComposer({
  mode,
  provider,
  editor,
  allowedClaimKinds,
  onCancel,
  onComplete,
}: TruthSelectionComposerProps) {
  const [capture, setCapture] = useState<TruthSelectionCapture | null>(null);
  const [captureError, setCaptureError] = useState<string | null>(null);
  const [capturing, setCapturing] = useState(true);
  const [proposition, setProposition] = useState("");
  const [claimKind, setClaimKind] = useState(allowedClaimKinds[0] ?? "fact");
  const [role, setRole] = useState<TruthExpressionRole>("quote");
  const [candidates, setCandidates] = useState<readonly TruthClaimSummary[]>([]);
  const [claimId, setClaimId] = useState("");
  const [candidatesLoading, setCandidatesLoading] = useState(mode === "connect");
  const [candidatesError, setCandidatesError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const captureSequence = useRef(0);
  const candidateSequence = useRef(0);
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const propositionId = useId();
  const kindId = useId();
  const roleId = useId();
  const claimIdControl = useId();

  const captureSelection = useCallback(() => {
    const request = ++captureSequence.current;
    setCapturing(true);
    setCaptureError(null);
    void editor.captureSelection().then(
      (next) => {
        if (request !== captureSequence.current) return;
        if (next.selector.exact.trim().length === 0) {
          setCapture(null);
          setCaptureError("Select some text in the editor, then try again.");
        } else {
          setCapture(next);
          setProposition((current) =>
            current.length === 0 ? next.selector.exact.trim() : current,
          );
        }
        setCapturing(false);
      },
      (cause: unknown) => {
        if (request !== captureSequence.current) return;
        setCapture(null);
        setCaptureError(
          errorMessage(cause, "The current editor selection could not be captured."),
        );
        setCapturing(false);
      },
    );
  }, [editor]);

  useEffect(() => {
    captureSelection();
    headingRef.current?.focus();
    return () => {
      captureSequence.current += 1;
    };
  }, [captureSelection]);

  const loadCandidates = useCallback(() => {
    if (mode !== "connect") return;
    const request = ++candidateSequence.current;
    setCandidatesLoading(true);
    setCandidatesError(null);
    void provider.load({ scope: "folder", filter: "all" }).then(
      (snapshot) => {
        if (request !== candidateSequence.current) return;
        const next = snapshot.claims.filter(candidateIsConnectable);
        setCandidates(next);
        setClaimId((current) =>
          next.some((claim) => claim.claimId === current)
            ? current
            : (next[0]?.claimId ?? ""),
        );
        setCandidatesLoading(false);
      },
      (cause: unknown) => {
        if (request !== candidateSequence.current) return;
        setCandidatesError(
          errorMessage(cause, "Existing claims could not be loaded."),
        );
        setCandidatesLoading(false);
      },
    );
  }, [mode, provider]);

  useEffect(() => {
    loadCandidates();
    return () => {
      candidateSequence.current += 1;
    };
  }, [loadCandidates]);

  const submit = (): void => {
    if (capture === null || busy) return;
    const trimmedProposition = proposition.trim();
    if (mode === "propose" && trimmedProposition.length === 0) return;
    if (mode === "connect" && claimId.length === 0) return;
    setBusy(true);
    setSubmitError(null);
    const mutation =
      mode === "propose"
        ? provider.proposeClaim({
            capture,
            proposition: trimmedProposition,
            claimKind,
            role,
          })
        : provider.connectClaim({ capture, claimId, role });
    void mutation.then(
      (receipt) => {
        setBusy(false);
        onComplete(receipt);
      },
      (cause: unknown) => {
        setBusy(false);
        setSubmitError(
          errorMessage(cause, "Truth could not save that connection."),
        );
      },
    );
  };

  const submitDisabled =
    busy ||
    capturing ||
    capture === null ||
    (mode === "propose" && proposition.trim().length === 0) ||
    (mode === "connect" && (candidatesLoading || claimId.length === 0));

  return (
    <section className="wb-cowork-truth__composer" aria-label={mode === "propose" ? "Propose a claim" : "Connect an existing claim"}>
      <div className="wb-cowork-truth__details-head">
        <h3 ref={headingRef} tabIndex={-1}>{mode === "propose" ? "Propose from selection" : "Connect selection"}</h3>
        <button
          type="button"
          className="wb-cowork-truth__close"
          disabled={busy}
          onClick={onCancel}
        >
          Close
        </button>
      </div>

      {capturing ? <p className="wb-cowork-truth__state">Capturing selection…</p> : null}
      {captureError === null ? null : (
        <div className="wb-cowork-truth__state is-error" role="alert">
          <p>{captureError}</p>
          <button type="button" onClick={captureSelection}>Try selection again</button>
        </div>
      )}
      {capture === null ? null : (
        <div className="wb-cowork-truth__selection-preview">
          <span>Selected passage</span>
          <blockquote>{capture.selector.exact}</blockquote>
        </div>
      )}

      <fieldset disabled={busy || capture === null}>
        <legend className="wb-cowork-truth__visually-hidden">
          Claim connection
        </legend>
        {mode === "propose" ? (
          <>
            <label htmlFor={propositionId}>Claim</label>
            <textarea
              id={propositionId}
              rows={4}
              value={proposition}
              onChange={(event) => setProposition(event.target.value)}
            />
            <label htmlFor={kindId}>Kind</label>
            <select id={kindId} value={claimKind} onChange={(event) => setClaimKind(event.target.value)}>
              {(allowedClaimKinds.length === 0 ? ["fact"] : allowedClaimKinds).map((kind) => (
                <option key={kind} value={kind}>{claimKindLabel(kind)}</option>
              ))}
            </select>
          </>
        ) : (
          <>
            <label htmlFor={claimIdControl}>Existing claim</label>
            {candidatesLoading ? <p className="wb-cowork-truth__state">Loading claims…</p> : candidatesError !== null ? (
              <div className="wb-cowork-truth__state is-error" role="alert">
                <p>{candidatesError}</p>
                <button type="button" onClick={loadCandidates}>Try again</button>
              </div>
            ) : candidates.length === 0 ? (
              <p className="wb-cowork-truth__state">There are no active claims to connect.</p>
            ) : (
              <select id={claimIdControl} value={claimId} onChange={(event) => setClaimId(event.target.value)}>
                {candidates.map((claim) => (
                  <option key={claim.claimId} value={claim.claimId}>{claim.proposition}</option>
                ))}
              </select>
            )}
          </>
        )}
        <label htmlFor={roleId}>How the passage expresses the claim</label>
        <select id={roleId} value={role} onChange={(event) => setRole(event.target.value as TruthExpressionRole)}>
          <option value="quote">Directly states it</option>
          <option value="paraphrase">Paraphrases it</option>
          <option value="summary">Summarizes it</option>
          <option value="instantiation">Gives a concrete instance</option>
        </select>
      </fieldset>

      {submitError === null ? null : <p className="wb-cowork-truth__error" role="alert">{submitError}</p>}
      <div className="wb-cowork-truth__composer-actions">
        <button type="button" className="is-primary" disabled={submitDisabled} onClick={submit}>
          {busy ? "Saving…" : mode === "propose" ? "Propose and connect" : "Connect claim"}
        </button>
        <button type="button" disabled={busy} onClick={onCancel}>Cancel</button>
      </div>
    </section>
  );
}

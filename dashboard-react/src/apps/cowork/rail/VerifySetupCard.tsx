import { useEffect, useState, type FormEvent } from "react";

import type {
  CoworkVerifyCapability,
  VerificationConfiguration,
  VerificationCriterion,
  VerifyCriterionDraftInput,
} from "./contracts";

const originLabel = (criterion: VerificationCriterion): string =>
  criterion.definitionOrigin === "system" ? "Built in" : "User-authored";

const statusLabel = (criterion: VerificationCriterion): string => {
  switch (criterion.operationalState) {
    case "active":
      return "Runs next time";
    case "inactive":
      return "Off";
    case "unavailable":
      return "Check unavailable";
    case "blocked_required_check":
      return "Required check blocked";
  }
};

export interface VerifySetupCardProps {
  readonly capability: CoworkVerifyCapability;
  readonly configuration: VerificationConfiguration;
  readonly onSetEnabled?: (
    criterionKey: string,
    enabled: boolean,
    expectedActivationId: string | null,
  ) => Promise<void>;
  readonly onCreateDraft?: (
    draft: VerifyCriterionDraftInput,
  ) => Promise<void>;
  /** Reports whether a visible setup mutation is awaiting authoritative state. */
  readonly onBusyChange?: (busy: boolean) => void;
}

/**
 * Criterion-first setup: the expectation is primary, while executor mechanics,
 * limitations, provenance, and data sharing remain inspectable underneath.
 */
export function VerifySetupCard({
  capability,
  configuration,
  onSetEnabled,
  onCreateDraft,
  onBusyChange,
}: VerifySetupCardProps) {
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftDescription, setDraftDescription] = useState("");
  const [draftInstructions, setDraftInstructions] = useState("");
  const [draftLimitation, setDraftLimitation] = useState("");
  const [draftBusy, setDraftBusy] = useState(false);
  const setupBusy = busyKey !== null || draftBusy;
  const activeCount = configuration.criteria.filter(
    (criterion) => criterion.operationalState === "active",
  ).length;
  const unavailableCount = configuration.criteria.filter(
    (criterion) =>
      criterion.operationalState === "unavailable" ||
      criterion.operationalState === "blocked_required_check",
  ).length;

  useEffect(() => {
    onBusyChange?.(setupBusy);
  }, [onBusyChange, setupBusy]);

  useEffect(
    () => () => {
      onBusyChange?.(false);
    },
    [onBusyChange],
  );

  const setEnabled = (
    criterion: VerificationCriterion,
    enabled: boolean,
  ): void => {
    if (onSetEnabled === undefined) return;
    setBusyKey(criterion.stableKey);
    setError(null);
    void onSetEnabled(
      criterion.stableKey,
      enabled,
      criterion.activationId,
    )
      .catch((cause: unknown) => {
        setError(
          cause instanceof Error
            ? cause.message
            : "Verify setup could not be changed.",
        );
      })
      .finally(() => setBusyKey(null));
  };

  const createDraft = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (onCreateDraft === undefined || setupBusy) return;
    setDraftBusy(true);
    setError(null);
    void onCreateDraft({
      title: draftTitle.trim(),
      description: draftDescription.trim(),
      evaluationInstructions: draftInstructions.trim(),
      limitations:
        draftLimitation.trim().length === 0
          ? []
          : [draftLimitation.trim()],
    })
      .then(() => {
        setDraftTitle("");
        setDraftDescription("");
        setDraftInstructions("");
        setDraftLimitation("");
      })
      .catch((cause: unknown) => {
        setError(
          cause instanceof Error
            ? cause.message
            : "The criterion draft could not be saved.",
        );
      })
      .finally(() => setDraftBusy(false));
  };

  return (
    <details className="wb-cowork-verify-setup">
      <summary>
        <span>Verify setup</span>
        <span className="wb-cowork-verify-setup__count">
          {activeCount.toLocaleString()} active
          {unavailableCount > 0
            ? ` · ${unavailableCount.toLocaleString()} unavailable`
            : ""}
        </span>
      </summary>
      <div className="wb-cowork-verify-setup__body">
        <p className="wb-cowork-verify-setup__intro">
          Criteria say what this work should satisfy. Changes apply to the next
          run; a run already started keeps its frozen setup.
        </p>
        {!capability.canConfigure && capability.disabledReason !== null ? (
          <p className="wb-cowork-verify-setup__notice">
            {capability.disabledReason}
          </p>
        ) : null}
        {configuration.criteria.length === 0 ? (
          <p className="wb-cowork-verify-setup__notice">
            No verification criteria are available for this document.
          </p>
        ) : (
          <ul className="wb-cowork-verify-setup__criteria">
            {configuration.criteria.map((criterion) => {
              const cannotEnable =
                criterion.operationalState === "unavailable" ||
                criterion.operationalState === "blocked_required_check";
              const toggleDisabled =
                setupBusy ||
                criterion.locked ||
                !capability.canConfigure ||
                onSetEnabled === undefined ||
                (!criterion.enabled && cannotEnable);
              return (
                <li
                  className="wb-cowork-verify-criterion"
                  key={`${criterion.stableKey}:${criterion.version.toString()}`}
                >
                  <div className="wb-cowork-verify-criterion__heading">
                    <div>
                      <strong>{criterion.title}</strong>
                      <span className="wb-cowork-verify-criterion__meta">
                        {originLabel(criterion)} · {statusLabel(criterion)}
                        {criterion.required ? " · Required" : " · Optional"}
                      </span>
                    </div>
                    <label className="wb-cowork-verify-criterion__toggle">
                      <input
                        type="checkbox"
                        aria-label={`${criterion.title}: include in Verify runs`}
                        checked={criterion.enabled}
                        disabled={toggleDisabled}
                        onChange={(event) =>
                          setEnabled(criterion, event.currentTarget.checked)
                        }
                      />
                      <span>{criterion.enabled ? "On" : "Off"}</span>
                    </label>
                  </div>
                  <p>{criterion.description}</p>
                  {criterion.authorizedBy !== null ? (
                    <p className="wb-cowork-verify-criterion__authority">
                      Effective setting authorized by{" "}
                      {criterion.authorizedBy.kind === "human"
                        ? "you"
                        : criterion.authorizedBy.kind}
                      .
                    </p>
                  ) : null}
                  {criterion.checks.map((check) => (
                    <details
                      className="wb-cowork-verify-check"
                      key={check.bindingId}
                    >
                      <summary>
                        {check.title} · {check.mechanism} v
                        {check.version.toString()}
                      </summary>
                      <dl>
                        <div>
                          <dt>Availability</dt>
                          <dd>
                            {check.availability === "available"
                              ? "Available"
                              : check.unavailableReason?.replace(/_/gu, " ") ??
                                "Unavailable"}
                          </dd>
                        </div>
                        <div>
                          <dt>Data sharing</dt>
                          <dd>
                            {check.externalEgress === false
                              ? "In-process deterministic checker · checker egress: none"
                              : check.dataSharingClass.replace(/_/gu, " ")}
                          </dd>
                        </div>
                        <div>
                          <dt>Origin</dt>
                          <dd>
                            {check.definitionOrigin === "system"
                              ? "Built in"
                              : "User-authored"}
                          </dd>
                        </div>
                      </dl>
                      {check.limitations.length > 0 ? (
                        <>
                          <strong>Limitations</strong>
                          <ul>
                            {check.limitations.map((limitation) => (
                              <li key={limitation}>{limitation}</li>
                            ))}
                          </ul>
                        </>
                      ) : null}
                    </details>
                  ))}
                </li>
              );
            })}
          </ul>
        )}
        {onCreateDraft !== undefined && capability.canConfigure ? (
          <details className="wb-cowork-verify-draft">
            <summary>Add a user-authored criterion</summary>
            <p>
              This saves the criterion and a proposed checker as an unavailable
              draft. It does not run a model, share document content, or admit
              the checker. Admission remains a separate reviewed step.
            </p>
            <form onSubmit={createDraft}>
              <label>
                Criterion name
                <input
                  required
                  maxLength={160}
                  value={draftTitle}
                  onChange={(event) => setDraftTitle(event.currentTarget.value)}
                />
              </label>
              <label>
                What should be true?
                <textarea
                  required
                  value={draftDescription}
                  onChange={(event) =>
                    setDraftDescription(event.currentTarget.value)
                  }
                />
              </label>
              <label>
                Proposed evaluation instructions
                <textarea
                  required
                  value={draftInstructions}
                  onChange={(event) =>
                    setDraftInstructions(event.currentTarget.value)
                  }
                />
              </label>
              <label>
                Known limitation (optional)
                <textarea
                  value={draftLimitation}
                  onChange={(event) =>
                    setDraftLimitation(event.currentTarget.value)
                  }
                />
              </label>
              <button type="submit" disabled={setupBusy}>
                {draftBusy ? "Saving…" : "Save unavailable draft"}
              </button>
            </form>
          </details>
        ) : null}
        {error !== null ? (
          <p className="wb-cowork-verify-setup__error" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </details>
  );
}

import { X } from "@phosphor-icons/react/X";
import { useEffect, useState, type FormEvent } from "react";

import { Button, IconButton } from "../../../ui";
import type {
  CoworkVerifyCapability,
  VerificationConfiguration,
  VerificationCriterion,
  VerifyCheckInput,
} from "../rail/contracts";
import "./styles.css";

export type VerifyCheckPage = "select" | "add";

const originLabel = (criterion: VerificationCriterion): string =>
  criterion.definitionOrigin === "system" ? "Built in" : "Yours";

const unavailableLabel = (criterion: VerificationCriterion): string | null => {
  switch (criterion.operationalState) {
    case "active":
    case "inactive":
      return null;
    case "unavailable":
      return "Needs setup";
    case "blocked_required_check":
      return "Required · unavailable";
  }
};

export interface VerifyCheckControlProps {
  readonly capability: CoworkVerifyCapability;
  readonly configuration: VerificationConfiguration;
  readonly page: VerifyCheckPage;
  readonly onPageChange: (page: VerifyCheckPage) => void;
  readonly onSetEnabled?: (
    criterionKey: string,
    enabled: boolean,
    expectedActivationId: string | null,
  ) => Promise<void>;
  readonly onCreateCheck?: (
    check: VerifyCheckInput,
  ) => Promise<void>;
  /** Reports whether a visible setup mutation is awaiting authoritative state. */
  readonly onBusyChange?: (busy: boolean) => void;
}

/**
 * User-facing Verify control: choose checks, or replace that page with one
 * focused form for adding a check. Criterion/executor mechanics stay behind
 * the configuration boundary instead of leaking into the run surface.
 */
export function VerifyCheckControl({
  capability,
  configuration,
  page,
  onPageChange,
  onSetEnabled,
  onCreateCheck,
  onBusyChange,
}: VerifyCheckControlProps) {
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftDescription, setDraftDescription] = useState("");
  const [draftLimitation, setDraftLimitation] = useState("");
  const [draftBusy, setDraftBusy] = useState(false);
  const setupBusy = busyKey !== null || draftBusy;
  const selectedCount = configuration.criteria.filter(
    (criterion) => criterion.enabled,
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
            : "The selected checks could not be changed.",
        );
      })
      .finally(() => setBusyKey(null));
  };

  const createDraft = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (onCreateCheck === undefined || setupBusy) return;
    setDraftBusy(true);
    setError(null);
    void onCreateCheck({
      title: draftTitle.trim(),
      description: draftDescription.trim(),
      evaluationInstructions: draftDescription.trim(),
      limitations:
        draftLimitation.trim().length === 0
          ? []
          : [draftLimitation.trim()],
    })
      .then(() => {
        setDraftTitle("");
        setDraftDescription("");
        setDraftLimitation("");
        onPageChange("select");
      })
      .catch((cause: unknown) => {
        setError(
          cause instanceof Error
            ? cause.message
            : "The check could not be saved.",
        );
      })
      .finally(() => setDraftBusy(false));
  };

  if (page === "add") {
    return (
      <section
        className="wb-cowork-verify-setup wb-cowork-verify-setup--add"
        aria-label="Add verification check"
      >
        <header className="wb-cowork-verify-setup__add-header">
          <strong>Add check</strong>
          <IconButton
            label="Close add check"
            icon={<X />}
            size="small"
            variant="ghost"
            disabled={draftBusy}
            onClick={() => onPageChange("select")}
          />
        </header>
        <form className="wb-cowork-verify-draft" onSubmit={createDraft}>
          <label>
            Name
            <input
              required
              maxLength={160}
              value={draftTitle}
              onChange={(event) => setDraftTitle(event.currentTarget.value)}
            />
          </label>
          <label>
            What should it check?
            <textarea
              required
              value={draftDescription}
              onChange={(event) =>
                setDraftDescription(event.currentTarget.value)
              }
            />
          </label>
          <label>
            Exceptions <span>(optional)</span>
            <textarea
              value={draftLimitation}
              onChange={(event) =>
                setDraftLimitation(event.currentTarget.value)
              }
            />
          </label>
          <div className="wb-cowork-verify-draft__actions">
            <Button
              type="submit"
              size="small"
              variant="primary"
              disabled={
                setupBusy ||
                onCreateCheck === undefined ||
                !capability.canConfigure
              }
            >
              {draftBusy ? "Saving…" : "Save check"}
            </Button>
          </div>
        </form>
        {error !== null ? (
          <p className="wb-cowork-verify-setup__error" role="alert">
            {error}
          </p>
        ) : null}
      </section>
    );
  }

  return (
    <section
      className="wb-cowork-verify-setup"
      aria-label="Verification checks"
    >
      <details className="wb-cowork-verify-setup__menu">
        <summary>
          <span>Checks</span>
          <span className="wb-cowork-verify-setup__count">
            {selectedCount.toLocaleString()} selected
          </span>
        </summary>
        <div className="wb-cowork-verify-setup__menu-body">
          {configuration.criteria.length === 0 ? (
            <p className="wb-cowork-verify-setup__notice">
              No checks are available.
            </p>
          ) : (
            <ul className="wb-cowork-verify-setup__criteria">
              {configuration.criteria.map((criterion) => {
                const unavailable = unavailableLabel(criterion);
                const cannotEnable = unavailable !== null;
                const toggleDisabled =
                  setupBusy ||
                  criterion.locked ||
                  !capability.canConfigure ||
                  onSetEnabled === undefined ||
                  (!criterion.enabled && cannotEnable);
                return (
                  <li key={`${criterion.stableKey}:${criterion.version.toString()}`}>
                    <label className="wb-cowork-verify-criterion">
                      <input
                        type="checkbox"
                        aria-label={`${criterion.title}: include in Verify runs`}
                        checked={criterion.enabled}
                        disabled={toggleDisabled}
                        onChange={(event) =>
                          setEnabled(criterion, event.currentTarget.checked)
                        }
                      />
                      <span className="wb-cowork-verify-criterion__copy">
                        <strong>{criterion.title}</strong>
                        <span>{criterion.description}</span>
                      </span>
                      <span className="wb-cowork-verify-criterion__meta">
                        {originLabel(criterion)}
                        {criterion.required ? " · Required" : ""}
                        {unavailable === null ? "" : ` · ${unavailable}`}
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
          )}
          {!capability.canConfigure && capability.disabledReason !== null ? (
            <p className="wb-cowork-verify-setup__notice">
              {capability.disabledReason}
            </p>
          ) : null}
          {error !== null ? (
            <p className="wb-cowork-verify-setup__error" role="alert">
              {error}
            </p>
          ) : null}
        </div>
      </details>
      {onCreateCheck !== undefined ? (
        <Button
          size="small"
          variant="secondary"
          disabled={!capability.canConfigure || setupBusy}
          onClick={() => {
            setError(null);
            onPageChange("add");
          }}
        >
          Add check
        </Button>
      ) : null}
    </section>
  );
}

import {
  type FormEvent,
  type KeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { useWidgetDraft } from "../../dashboard/drafts";
import { HelpTarget } from "../../dashboard/help";
import type { IntentResult } from "../../dashboard/contributions/contracts";
import { sha256Hex } from "../../security/localIdentity";
import {
  Button,
  InlineAlert,
  SelectField,
  SwitchField,
  TextAreaField,
} from "../../ui";
import { createCorrelationId, StatusBadge } from "../shared";
import type {
  CaptureDraftRequest,
  CaptureSubmitMode,
  CaptureSecondaryAction,
  CaptureSubmissionRecord,
  QuickTextCaptureInput,
} from "./contracts";
import { FollowUpLinks, safeCaptureAppHref } from "./FollowUpLinks";
import { captureSmartDisclosureSha256 } from "./smartDisclosure";
import { journalInstantAtLocalTime } from "../shared/journalDayTime";
import "./styles.css";

export interface CaptureComposerProps {
  readonly input: QuickTextCaptureInput;
  readonly density: "compact" | "standard" | "expanded";
  onSubmit(request: CaptureDraftRequest): IntentResult | Promise<IntentResult> | void;
  onRetry?(capture: CaptureSubmissionRecord): Promise<unknown> | void;
  onRefreshAvailability?(): Promise<unknown> | void;
}

interface CaptureComposerDraft {
  readonly text: string;
  readonly targetId: string;
  readonly mode: CaptureSubmitMode;
  readonly localTime?: string;
  /** Additive recovery metadata in the existing host draft, never a second source copy. */
  readonly pendingSubmission?: {
    readonly envelopeVersion?: 2;
    readonly clientMutationId: string;
    readonly requestSha256: string;
    readonly smartDisclosureSha256?: string;
  };
}

/**
 * The destination that stands for a routing decision instead of a place. It
 * is presented apart from the literal destinations beside it.
 */
const AUTOMATIC_TARGET_ID = "auto";

const statusTone = (status: string) => {
  if (status === "failed") return "danger" as const;
  if (status === "pending") return "warning" as const;
  if (status === "succeeded" || status === "persisted" || status === "placed") {
    return "success" as const;
  }
  return "neutral" as const;
};

export function CaptureComposer({ input, density, onSubmit, onRetry, onRefreshAvailability }: CaptureComposerProps) {
  const [retrying, setRetrying] = useState<string>();
  const [saving, setSaving] = useState(false);
  const [submitError, setSubmitError] = useState<string>();
  const savingRef = useRef(false);
  const firstTarget = input.targets.find((target) => target.enabled) ?? input.targets[0];
  const initialDraft = useMemo<CaptureComposerDraft>(
    () => ({
      text: "",
      targetId: firstTarget?.targetId ?? "",
      mode: (firstTarget?.defaultMode ?? "dumb") as CaptureSubmitMode,
      localTime: "",
    }),
    [firstTarget?.defaultMode, firstTarget?.targetId],
  );
  const draftState = useWidgetDraft("capture", initialDraft, {
    isPristine: (value) => value.text.length === 0,
  });
  const setDraftValue = draftState.setValue;
  const clearDraft = draftState.clear;
  const flushDraft = draftState.flush;
  const getDraftSnapshot = draftState.getSnapshot;
  const { text: draft, targetId, mode } = draftState.value;
  const localTime = draftState.value.localTime ?? "";
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const target = useMemo(
    () => input.targets.find((candidate) => candidate.targetId === targetId),
    [input.targets, targetId],
  );
  const readOnly = input.access.mode === "read_only";
  const targetSupportsMode = target?.supportedModes.includes(mode) ?? false;
  const smartAvailable = input.targets.some(
    (candidate) => candidate.enabled && candidate.supportedModes.includes("smart"),
  );
  const smartAvailability = input.smartAvailability;
  const smartHelp = smartAvailability ? {
    summary: smartAvailability.reason,
    details: [
      smartAvailability.disclosure.provider && smartAvailability.disclosure.model
        ? `${smartAvailability.disclosure.provider} · ${smartAvailability.disclosure.model}.`
        : "No model is currently ready.",
      `When Smart is on, up to ${Math.round(smartAvailability.disclosure.maxInputBytes / 1024)} KiB of exact saved text is sent for processing.`,
      "No tools or web access. Direct capture does not send text to a model.",
      "Smart may propose a task for your review; it cannot create one.",
    ].join(" "),
  } : input.smartHelp ?? {
    summary: "Run a smart follow-up after capturing.",
    details: "After preserving your exact text, Smart asks the owning App to interpret its context and run the configured follow-up processing. That may classify or enrich the capture and propose further actions; governed operations still follow Work Buddy's permission and confirmation rules.",
  };
  const smartAction = smartAvailability?.action;
  const smartSetupHref = smartAction?.kind === "app_link" ? safeCaptureAppHref(smartAction.href) : undefined;
  const retrospectiveTime = input.retrospectiveTime?.targetIds.includes(targetId)
    ? input.retrospectiveTime
    : undefined;

  useEffect(() => {
    if (target?.enabled) return;
    // Availability changes are not an explicit choice of another request.
    // Keep an uncertain capture's identity until the user edits its draft.
    if (getDraftSnapshot().value.pendingSubmission !== undefined) return;
    const replacement = input.targets.find((candidate) => candidate.enabled);
    if (replacement !== undefined) {
      setDraftValue((current) => ({
        ...current,
        targetId: replacement.targetId,
        mode: replacement.defaultMode,
        pendingSubmission: undefined,
      }));
    }
  }, [draftState.value.pendingSubmission, getDraftSnapshot, input.targets, setDraftValue, target]);

  useEffect(() => {
    const current = getDraftSnapshot();
    const pending = current.value.pendingSubmission;
    if (!current.ready || pending === undefined) return;
    const result = input.recentSubmissions.find(
      (submission) => submission.clientMutationId === pending.clientMutationId,
    );
    if (result?.persistenceStatus === "persisted") {
      setSubmitError(undefined);
      void clearDraft({ ifRevision: current.revision }).then((cleared) => {
        if (cleared) textareaRef.current?.focus({ preventScroll: true });
      });
    }
  }, [clearDraft, draftState.value.pendingSubmission, getDraftSnapshot, input.recentSubmissions]);

  const selectTarget = (nextTargetId: string) => {
    const nextTarget = input.targets.find(
      (candidate) => candidate.targetId === nextTargetId,
    );
    setDraftValue((current) => ({
      ...current,
      targetId: nextTargetId,
      pendingSubmission: undefined,
      mode:
        nextTarget?.supportedModes.includes(current.mode) === true
          ? current.mode
          : (nextTarget?.defaultMode ?? current.mode),
    }));
  };

  const save = async (secondary?: CaptureSecondaryAction) => {
    // React's disabled paint may arrive after another click or keyboard submit.
    // Lock synchronously before hashing, draft flush, or provider work can yield.
    if (savingRef.current) return;
    const snapshot = getDraftSnapshot();
    const selectedTarget = input.targets.find((item) => item.targetId === (secondary?.targetId ?? snapshot.value.targetId));
    const selectedMode = secondary?.mode ?? snapshot.value.mode;
    if (
      !snapshot.ready || snapshot.value.text.length === 0 ||
      selectedTarget === undefined ||
      !selectedTarget.enabled ||
      !selectedTarget.supportedModes.includes(selectedMode) ||
      readOnly
    ) return;
    savingRef.current = true;
    setSaving(true);
    setSubmitError(undefined);
    let dispatched = false;
    try {
      const selectedRetrospectiveTime = input.retrospectiveTime?.targetIds.includes(
        selectedTarget.targetId,
      ) ? input.retrospectiveTime : undefined;
      const statedAt = selectedRetrospectiveTime && snapshot.value.localTime
        ? journalInstantAtLocalTime(selectedRetrospectiveTime, snapshot.value.localTime)
        : undefined;
      const request = {
        dayId: input.dayId,
        targetId: selectedTarget.targetId,
        mode: selectedMode,
        exactText: snapshot.value.text,
        ...(statedAt ? { statedAt } : {}),
        ...(secondary ? { followUpActionId: secondary.actionId } : {}),
      };
      const disclosure = selectedMode === "smart" ? input.smartAvailability?.disclosure : undefined;
      const legacyFingerprint = JSON.stringify({
        request,
        smartDisclosure: disclosure ? { provider: disclosure.provider, model: disclosure.model,
          maxInputBytes: disclosure.maxInputBytes, tools: disclosure.tools, web: disclosure.web } : null,
      });
      const [requestSha256, smartDisclosureSha256] = await Promise.all([
        sha256Hex(JSON.stringify(request)),
        captureSmartDisclosureSha256(disclosure),
      ]);
      if (getDraftSnapshot().revision !== snapshot.revision) {
        setSubmitError("Your draft changed before saving. Review it and capture again.");
        return;
      }
      const pending = snapshot.value.pendingSubmission;
      if (pending !== undefined) {
        const sameRequest = pending.envelopeVersion === 2
          ? pending.requestSha256 === requestSha256 && pending.smartDisclosureSha256 === smartDisclosureSha256
          : pending.requestSha256 === await sha256Hex(legacyFingerprint);
        if (!sameRequest || typeof pending.clientMutationId !== "string" || pending.clientMutationId.length === 0) {
          setSubmitError("This unconfirmed capture's destination, action, or Smart setup changed. Restore the previous choices to retry the same save, or edit the draft to start a new capture.");
          return;
        }
      }
      if (getDraftSnapshot().revision !== snapshot.revision) {
        setSubmitError("Your draft changed before saving. Review it and capture again.");
        return;
      }
      const clientMutationId = pending?.clientMutationId ?? createCorrelationId("capture");
      const draftRevision = setDraftValue({ ...snapshot.value,
        pendingSubmission: { envelopeVersion: 2, clientMutationId, requestSha256,
          ...(smartDisclosureSha256 ? { smartDisclosureSha256 } : {}) } });
      // The exact draft and its retry identity must both survive before dispatch.
      await flushDraft();
      dispatched = true;
      const result = await onSubmit({ clientMutationId, ...request,
        ...(smartDisclosureSha256 ? { smartDisclosureSha256 } : {}) });
      if (result?.status === "accepted") {
        if (getDraftSnapshot().value.pendingSubmission?.clientMutationId === clientMutationId) {
          const cleared = await clearDraft({ ifRevision: draftRevision });
          if (cleared) textareaRef.current?.focus({ preventScroll: true });
        }
      } else if (result !== undefined) {
        setSubmitError(`${result.message ?? "The capture could not finish."} Your draft remains here; retrying it unchanged checks the same save.`);
      }
    } catch (error) {
      setSubmitError(dispatched
        ? "Could not confirm the save. Your draft remains here; retrying it unchanged checks the same capture."
        : error instanceof Error
          ? `${error.message} Your draft remains here; no capture was sent.`
          : "The draft could not be prepared for capture. Your text remains here; no capture was sent.");
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  const submit = (event: FormEvent) => { event.preventDefault(); void save(); };

  const handleShortcut = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  const recentLimit = density === "expanded" ? 5 : density === "standard" ? 2 : 0;
  const recent =
    recentLimit === 0 ? [] : input.recentSubmissions.slice(-recentLimit).reverse();

  if (!draftState.ready) {
    return <p className="wb-capture__draft-loading" aria-busy="true">Restoring draft…</p>;
  }

  return (
    <form className={`wb-capture wb-capture--${density}${retrospectiveTime ? " wb-capture--time" : ""}`} onSubmit={submit} aria-busy={saving}>
      {readOnly && input.accessNotice !== "view" ? (
        <InlineAlert tone="warning">{input.access.reason}</InlineAlert>
      ) : null}
      {draftState.error ? (
        <InlineAlert tone="danger">{draftState.error} Your current text remains open.</InlineAlert>
      ) : null}
      {submitError ? <InlineAlert tone="danger">{submitError}</InlineAlert> : null}
      <TextAreaField
        ref={textareaRef}
        className="wb-capture__field"
        label="Capture text"
        value={draft}
        rows={density === "compact" ? 2 : 3}
        disabled={readOnly}
        placeholder="Write exactly what you want to preserve…"
        help={{
          summary: "Write the exact text you want Work Buddy to preserve.",
          details:
            "This is recoverable draft text until you capture it. Press Ctrl + Enter to capture from the keyboard; changing the destination or Smart setting does not alter the text itself.",
        }}
        onChange={(text) => setDraftValue((current) => ({ ...current, text, pendingSubmission: undefined }))}
        onKeyDown={handleShortcut}
      />

      <div className="wb-capture__controls">
        <div className="wb-capture__smart-controls">
          {(smartAvailable || mode === "smart") && (
            <SwitchField
              className="wb-capture__smart"
              label="Smart"
              help={smartHelp}
              selected={mode === "smart"}
              disabled={readOnly}
              onChange={(selected) =>
                setDraftValue((current) => ({
                  ...current,
                  mode: selected ? "smart" : "dumb",
                  pendingSubmission: undefined,
                }))
              }
            />
          )}
          {smartSetupHref && smartAction ? (
            <HelpTarget content={smartHelp} placement="bottom start">
              <a className="wb-capture__smart-setup" href={smartSetupHref}>{smartAction.label}</a>
            </HelpTarget>
          ) : smartAction?.kind === "retry" && onRefreshAvailability ? (
            <HelpTarget content={smartHelp} placement="bottom start" reactAriaComposite>
              <Button type="button" size="small" variant="ghost" disabled={retrying === "availability"}
                onClick={() => { setRetrying("availability"); void Promise.resolve(onRefreshAvailability()).finally(() => setRetrying(undefined)); }}>
                {retrying === "availability" ? "Checking Smart setup…" : smartAction.label}
              </Button>
            </HelpTarget>
          ) : !smartAvailable && mode !== "smart" && smartAvailability ? (
            <HelpTarget content={smartHelp} focusable ariaLabel="About Smart availability">
              <span className="wb-capture__smart-setup">Smart unavailable</span>
            </HelpTarget>
          ) : null}
        </div>

        <SelectField
          className="wb-capture__target"
          label="Destination"
          hideLabel
          value={targetId}
          disabled={readOnly || input.targets.length === 0}
          help={{
            summary: "Choose what kind of saved item this capture should become.",
            details:
              "The destination controls where and how the exact text is preserved. Each available choice explains its own result in the menu; changing it does not submit or rewrite your draft.",
          }}
          options={input.targets.map((option) => ({
            value: option.targetId,
            label: option.label,
            description: option.description,
            disabled: !option.enabled || !option.supportedModes.includes(mode),
            automatic: option.targetId === AUTOMATIC_TARGET_ID,
          }))}
          onChange={selectTarget}
        />

        {retrospectiveTime ? (
          <label className="wb-capture__time">
            <span>Time <small>(optional)</small></span>
            <input
              type="time"
              aria-label="Log time"
              value={localTime}
              disabled={readOnly}
              onChange={(event) => setDraftValue((current) => ({
                ...current,
                localTime: event.target.value,
                pendingSubmission: undefined,
              }))}
            />
            <small>{retrospectiveTime.timezone}; blank uses capture time</small>
          </label>
        ) : null}

        <Button
          variant="primary"
          type="submit"
          disabled={
            saving || readOnly || draft.length === 0 || !target?.enabled || !targetSupportsMode
          }
        >
          {saving ? "Saving…" : "Capture"}
        </Button>
      </div>
      {mode === "smart" && smartAvailability?.state === "ready" ? (
        <small className="wb-capture__smart-boundary" role="status" aria-label="Smart processing">
          {smartAvailability.disclosure.provider} · {smartAvailability.disclosure.model} · Saved text, up to {Math.round(smartAvailability.disclosure.maxInputBytes / 1024)} KiB
        </small>
      ) : null}
      {saving ? <p role="status">Saving your exact capture…</p> : null}

      {input.secondaryActions?.map((action) => (
        <div key={action.actionId} className="wb-capture__secondary">
          <HelpTarget content={{ summary: action.label, details: action.description }} reactAriaComposite>
            <Button type="button" variant="ghost" disabled={saving || readOnly || draft.length === 0} onClick={() => { void save(action); }}>{action.label}</Button>
          </HelpTarget>
        </div>
      ))}

      {target !== undefined && !target.enabled ? (
        <InlineAlert tone="warning">
          <strong>{target.label}:</strong> {target.unavailableReason}
        </InlineAlert>
      ) : null}

      {target !== undefined && target.enabled && !targetSupportsMode ? (
        <InlineAlert tone="warning">
          {target.supportedModes.includes("smart")
            ? `Turn on Smart to use ${target.label}.`
            : `${target.label} is not available while Smart is on.`}
        </InlineAlert>
      ) : null}

      {recent.length > 0 && (
        <section className="wb-capture__recent" aria-label="Recent captures">
          <h3>Recent</h3>
          <ul>
            {recent.map((submission) => (
              <li key={submission.clientMutationId}>
                <span className="wb-capture__exact-text">
                  {submission.exactText ?? "Saved capture awaiting its Journal destination"}
                </span>
                <span className="wb-library-meta-row">
                  <StatusBadge
                    label={submission.persistenceStatus}
                    tone={statusTone(submission.persistenceStatus)}
                  />
                  {submission.placementStatus ? (
                    <StatusBadge
                      label={submission.placementStatus}
                      tone={statusTone(submission.placementStatus)}
                    />
                  ) : null}
                  <StatusBadge
                    label={submission.processingStatus.replace(/_/g, " ")}
                    tone={statusTone(submission.processingStatus)}
                  />
                </span>
                {submission.errorMessage && (
                  <InlineAlert tone="danger">{submission.errorMessage}</InlineAlert>
                )}
                {submission.followUps ? <FollowUpLinks items={submission.followUps} /> : null}
                {submission.retryable && submission.captureId && submission.revision !== undefined && onRetry ? (
                  <Button type="button" variant="ghost" disabled={readOnly || retrying === submission.captureId}
                    onClick={() => { setRetrying(submission.captureId); void Promise.resolve(onRetry(submission)).finally(() => setRetrying(undefined)); }}>
                    {retrying === submission.captureId ? "Retrying…" : "Retry follow-up"}
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      )}

      <p className="wb-capture__count">{input.capturesToday} captures today</p>
    </form>
  );
}

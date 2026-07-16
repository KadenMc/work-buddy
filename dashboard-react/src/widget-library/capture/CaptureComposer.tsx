import {
  type FormEvent,
  type KeyboardEvent,
  useEffect,
  useMemo,
  useRef,
} from "react";

import { useWidgetDraft } from "../../dashboard/drafts";
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
  QuickTextCaptureInput,
} from "./contracts";
import "./styles.css";

export interface CaptureComposerProps {
  readonly input: QuickTextCaptureInput;
  readonly density: "compact" | "standard" | "expanded";
  onSubmit(request: CaptureDraftRequest): void;
}

const statusTone = (status: string) => {
  if (status === "failed") return "danger" as const;
  if (status === "pending") return "warning" as const;
  if (status === "succeeded" || status === "persisted") return "success" as const;
  return "neutral" as const;
};

export function CaptureComposer({ input, density, onSubmit }: CaptureComposerProps) {
  const firstTarget = input.targets.find((target) => target.enabled) ?? input.targets[0];
  const initialDraft = useMemo(
    () => ({
      text: "",
      targetId: firstTarget?.targetId ?? "",
      mode: (firstTarget?.defaultMode ?? "dumb") as CaptureSubmitMode,
    }),
    [firstTarget?.defaultMode, firstTarget?.targetId],
  );
  const draftState = useWidgetDraft("capture", initialDraft, {
    isPristine: (value) => value.text.length === 0,
  });
  const setDraftValue = draftState.setValue;
  const clearDraft = draftState.clear;
  const flushDraft = draftState.flush;
  const { text: draft, targetId, mode } = draftState.value;
  const pendingRef = useRef<{
    readonly id: string;
    readonly exactText: string;
    readonly draftRevision: number;
  } | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const target = useMemo(
    () => input.targets.find((candidate) => candidate.targetId === targetId),
    [input.targets, targetId],
  );
  const readOnly = input.access.mode === "read_only";
  const targetSupportsMode = target?.supportedModes.includes(mode) ?? false;
  const smartAvailable = input.targets.some((candidate) =>
    candidate.supportedModes.includes("smart"),
  );

  useEffect(() => {
    if (target?.enabled) return;
    const replacement = input.targets.find((candidate) => candidate.enabled);
    if (replacement !== undefined) {
      setDraftValue((current) => ({
        ...current,
        targetId: replacement.targetId,
        mode: replacement.defaultMode,
      }));
    }
  }, [input.targets, setDraftValue, target]);

  useEffect(() => {
    const pending = pendingRef.current;
    if (pending === null) return;
    const result = input.recentSubmissions.find(
      (submission) => submission.clientMutationId === pending.id,
    );
    if (result?.persistenceStatus === "persisted") {
      pendingRef.current = null;
      void clearDraft({ ifRevision: pending.draftRevision }).then((cleared) => {
        if (cleared) textareaRef.current?.focus({ preventScroll: true });
      });
    }
  }, [clearDraft, input.recentSubmissions]);

  const selectTarget = (nextTargetId: string) => {
    const nextTarget = input.targets.find(
      (candidate) => candidate.targetId === nextTargetId,
    );
    setDraftValue((current) => ({
      ...current,
      targetId: nextTargetId,
      mode:
        nextTarget?.supportedModes.includes(current.mode) === true
          ? current.mode
          : (nextTarget?.defaultMode ?? current.mode),
    }));
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (
      draft.length === 0 ||
      target === undefined ||
      !target.enabled ||
      !targetSupportsMode ||
      readOnly
    ) return;
    const clientMutationId = createCorrelationId("capture");
    const draftRevision = draftState.revision;
    void flushDraft()
      .then(() => {
        pendingRef.current = { id: clientMutationId, exactText: draft, draftRevision };
        onSubmit({
          clientMutationId,
          dayId: input.dayId,
          targetId: target.targetId,
          mode,
          exactText: draft,
        });
      })
      .catch(() => {
        // The hook exposes a persistent inline error; do not dispatch an intent whose
        // exact source material was not safely written first.
      });
  };

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
    <form className={`wb-capture wb-capture--${density}`} onSubmit={submit}>
      {readOnly && <InlineAlert tone="warning">{input.access.reason}</InlineAlert>}
      {draftState.error ? (
        <InlineAlert tone="danger">{draftState.error} Your current text remains open.</InlineAlert>
      ) : null}
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
        onChange={(text) => setDraftValue((current) => ({ ...current, text }))}
        onKeyDown={handleShortcut}
      />

      <div className="wb-capture__controls">
        {smartAvailable && (
          <SwitchField
            className="wb-capture__smart"
            label="Smart"
            help={{
              summary: "Run a smart follow-up after capturing.",
              details:
                "After preserving your exact text, Smart asks the owning App to interpret its context and run the configured follow-up processing. That may classify or enrich the capture and propose further actions; governed operations still follow Work Buddy's permission and confirmation rules.",
            }}
            selected={mode === "smart"}
            disabled={readOnly}
            onChange={(selected) =>
              setDraftValue((current) => ({
                ...current,
                mode: selected ? "smart" : "dumb",
              }))
            }
          />
        )}

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
          }))}
          onChange={selectTarget}
        />

        <Button
          variant="primary"
          type="submit"
          disabled={
            readOnly || draft.length === 0 || !target?.enabled || !targetSupportsMode
          }
        >
          Capture
        </Button>
      </div>

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
                <span className="wb-capture__exact-text">{submission.exactText}</span>
                <span className="wb-library-meta-row">
                  <StatusBadge
                    label={submission.persistenceStatus}
                    tone={statusTone(submission.persistenceStatus)}
                  />
                  <StatusBadge
                    label={submission.processingStatus.replace(/_/g, " ")}
                    tone={statusTone(submission.processingStatus)}
                  />
                </span>
                {submission.errorMessage && (
                  <InlineAlert tone="danger">{submission.errorMessage}</InlineAlert>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      <p className="wb-capture__count">{input.capturesToday} captures today</p>
    </form>
  );
}

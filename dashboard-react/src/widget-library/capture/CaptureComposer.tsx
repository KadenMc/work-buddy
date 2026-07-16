import {
  type FormEvent,
  type KeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  Button,
  InlineAlert,
  SegmentedControl,
  SelectField,
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
  const [targetId, setTargetId] = useState(firstTarget?.targetId ?? "");
  const [mode, setMode] = useState<CaptureSubmitMode>(
    firstTarget?.defaultMode ?? "dumb",
  );
  const [draft, setDraft] = useState("");
  const pendingRef = useRef<{ readonly id: string; readonly exactText: string } | null>(
    null,
  );
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const target = useMemo(
    () => input.targets.find((candidate) => candidate.targetId === targetId),
    [input.targets, targetId],
  );
  const readOnly = input.access.mode === "read_only";

  useEffect(() => {
    if (target?.enabled) return;
    const replacement = input.targets.find((candidate) => candidate.enabled);
    if (replacement !== undefined) {
      setTargetId(replacement.targetId);
      setMode(replacement.defaultMode);
    }
  }, [input.targets, target]);

  useEffect(() => {
    if (target !== undefined && !target.supportedModes.includes(mode)) {
      setMode(target.defaultMode);
    }
  }, [mode, target]);

  useEffect(() => {
    const pending = pendingRef.current;
    if (pending === null) return;
    const result = input.recentSubmissions.find(
      (submission) => submission.clientMutationId === pending.id,
    );
    if (result?.persistenceStatus === "persisted") {
      setDraft((current) => (current === pending.exactText ? "" : current));
      pendingRef.current = null;
      textareaRef.current?.focus({ preventScroll: true });
    }
  }, [input.recentSubmissions]);

  const selectTarget = (nextTargetId: string) => {
    setTargetId(nextTargetId);
    const nextTarget = input.targets.find(
      (candidate) => candidate.targetId === nextTargetId,
    );
    if (nextTarget !== undefined) setMode(nextTarget.defaultMode);
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (draft.length === 0 || target === undefined || !target.enabled || readOnly) return;
    const clientMutationId = createCorrelationId("capture");
    pendingRef.current = { id: clientMutationId, exactText: draft };
    onSubmit({
      clientMutationId,
      dayId: input.dayId,
      targetId: target.targetId,
      mode,
      exactText: draft,
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

  return (
    <form className={`wb-capture wb-capture--${density}`} onSubmit={submit}>
      {readOnly && <InlineAlert tone="warning">{input.access.reason}</InlineAlert>}
      <TextAreaField
        ref={textareaRef}
        className="wb-capture__field"
        label="Capture text"
        value={draft}
        rows={density === "compact" ? 2 : 3}
        disabled={readOnly}
        placeholder="Write exactly what you want to preserve…"
        description="Press Ctrl + Enter to capture"
        onChange={setDraft}
        onKeyDown={handleShortcut}
      />

      <div className="wb-capture__controls">
        <SelectField
          className="wb-capture__target"
          label="Destination"
          value={targetId}
          disabled={readOnly || input.targets.length === 0}
          options={input.targets.map((option) => ({
            value: option.targetId,
            label: option.label,
            description: option.description,
            disabled: !option.enabled,
          }))}
          onChange={selectTarget}
        />

        {target !== undefined && target.supportedModes.length > 1 && (
          <SegmentedControl<CaptureSubmitMode>
            label="After capture"
            value={mode}
            disabled={readOnly}
            options={target.supportedModes.map((option) => ({
              value: option,
              label: option === "dumb" ? "Save only" : "Save + smart follow-up",
            }))}
            onChange={setMode}
          />
        )}

        <Button
          variant="primary"
          type="submit"
          disabled={readOnly || draft.length === 0 || !target?.enabled}
        >
          Capture
        </Button>
      </div>

      {target !== undefined && !target.enabled ? (
        <InlineAlert tone="warning">
          <strong>{target.label}:</strong> {target.unavailableReason}
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

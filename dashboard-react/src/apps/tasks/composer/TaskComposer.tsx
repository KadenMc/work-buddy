import {
  type ClipboardEvent,
  type FormEvent,
  type KeyboardEvent,
  useRef,
  useState,
} from "react";

import type {
  IntentResult,
  JsonValue,
  WidgetIntent,
  WidgetRendererProps,
} from "../../../dashboard/contributions/contracts";
import { useDashboardAnnouncer } from "../../../dashboard/accessibility/DashboardAnnouncer";
import { useWidgetDraft } from "../../../dashboard/drafts";
import { Button, InlineAlert, TextAreaField } from "../../../ui";
import { createCorrelationId, createWidgetIntent } from "../../../widget-library/shared";
import {
  TASK_INTENTS,
  type TaskBatchPreview,
  type TaskQuickAddInput,
  type TaskUrgency,
} from "../contracts";

export interface TaskCreateDraft {
  readonly title: string;
  readonly attention_state: string;
  readonly urgency: TaskUrgency;
  readonly due_date: string;
  readonly deadline_date: string;
  readonly project: string;
  readonly namespaces: string;
  readonly summary: string;
  readonly desired_outcome: string;
  readonly next_action: string;
  readonly definition_of_done: string;
  readonly dependencies: string;
  readonly batch_lines: readonly string[];
}

export const EMPTY_TASK_CREATE_DRAFT: TaskCreateDraft = {
  title: "",
  attention_state: "inbox",
  urgency: "medium",
  due_date: "",
  deadline_date: "",
  project: "",
  namespaces: "",
  summary: "",
  desired_outcome: "",
  next_action: "",
  definition_of_done: "",
  dependencies: "",
  batch_lines: [],
};

export const isTaskCreateDraftPristine = (value: TaskCreateDraft): boolean =>
  JSON.stringify(value) === JSON.stringify(EMPTY_TASK_CREATE_DRAFT);

export interface BatchPreviewRow {
  readonly title: string;
  readonly duplicate: boolean;
}

export function parseTaskBatch(text: string): readonly BatchPreviewRow[] {
  const seen = new Set<string>();
  return text
    .split(/\r?\n/)
    .map((line) => line.replace(/^\s*(?:[-*+]\s+|\d+[.)]\s+|\[[ xX]\]\s*)/, "").trim())
    .filter(Boolean)
    .map((title) => {
      const key = title.toLocaleLowerCase();
      const duplicate = seen.has(key);
      seen.add(key);
      return { title, duplicate };
    });
}

const csv = (value: string): readonly string[] =>
  value.split(",").map((part) => part.trim()).filter(Boolean);

const optional = (value: string): string | null => value.trim() || null;

function acceptedMessage(result: IntentResult, fallback: string): string {
  return result.message?.trim() || fallback;
}

export default function TaskComposer({
  input,
  emit,
  presentation,
}: WidgetRendererProps<TaskQuickAddInput>) {
  const { announce } = useDashboardAnnouncer();
  const titleRef = useRef<HTMLInputElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const previewRequestRef = useRef(0);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [serverPreview, setServerPreview] = useState<{
    readonly mutationId: string;
    readonly preview: TaskBatchPreview;
  } | null>(null);
  const [structureConfirmation, setStructureConfirmation] = useState<readonly string[]>([]);
  const [message, setMessage] = useState<{ tone: "danger" | "success" | "warning"; text: string } | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Readonly<Record<string, string>>>({});
  const draft = useWidgetDraft("task-create", EMPTY_TASK_CREATE_DRAFT, {
    isPristine: isTaskCreateDraftPristine,
  });
  const value = draft.value;
  const batchRows = serverPreview?.preview.rows ?? [];
  const readOnly = input.access.mode === "read_only";

  const fieldError = (...keys: readonly string[]): string | undefined => {
    for (const [key, text] of Object.entries(fieldErrors)) {
      if (keys.some((candidate) => key === candidate || key.startsWith(`${candidate}.`))) {
        return text;
      }
    }
    return undefined;
  };

  const update = <Key extends keyof TaskCreateDraft>(key: Key, next: TaskCreateDraft[Key]) => {
    if (key === "project" || key === "namespaces") setStructureConfirmation([]);
    if (key === "batch_lines") {
      previewRequestRef.current += 1;
      setServerPreview(null);
      setPreviewing(false);
    }
    draft.setValue((current) => ({ ...current, [key]: next }));
  };

  const dispatch = async (
    intentType: string,
    payload: JsonValue,
    clientMutationId: string,
  ): Promise<IntentResult> => {
    const intent = createWidgetIntent(presentation, intentType, payload, {
      intentId: clientMutationId,
      clientMutationId,
    }) as WidgetIntent;
    return emit(intent);
  };

  const finish = async (result: IntentResult, revision: number, label: string) => {
    if (result.status !== "accepted") {
      const text = acceptedMessage(result, `Tasks could not ${label}.`);
      setFieldErrors(result.fieldErrors ?? {});
      setMessage({ tone: result.status === "conflict" ? "warning" : "danger", text });
      announce(text, "assertive");
      window.requestAnimationFrame(() => {
        const invalid = formRef.current?.querySelector<HTMLElement>("[aria-invalid='true']");
        (invalid ?? titleRef.current)?.focus({ preventScroll: true });
      });
      return;
    }
    const text = acceptedMessage(result, label === "create the batch" ? "Tasks created." : "Task created.");
    await draft.clear({ ifRevision: revision });
    setFieldErrors({});
    setMessage({ tone: "success", text });
    announce(text);
    titleRef.current?.focus({ preventScroll: true });
  };

  const createOne = async (structureApproved: boolean) => {
    if (readOnly || submitting || value.title.trim().length === 0) return;
    const knownProjects = new Set(input.options.projects.map((option) => option.value.toLocaleLowerCase()));
    const knownNamespaces = new Set(input.options.namespaces.map((option) => option.value.toLocaleLowerCase()));
    const requestedStructures = [
      ...(value.project.trim() && !knownProjects.has(value.project.trim().toLocaleLowerCase())
        ? [`project “${value.project.trim()}”`]
        : []),
      ...csv(value.namespaces)
        .filter((namespace) => !knownNamespaces.has(namespace.toLocaleLowerCase()))
        .map((namespace) => `namespace “${namespace}”`),
    ];
    if (requestedStructures.length > 0 && !structureApproved) {
      setStructureConfirmation(requestedStructures);
      const text = "Confirm the new task structure before creating this task.";
      setMessage({ tone: "warning", text });
      announce(text, "assertive");
      return;
    }
    const revision = draft.revision;
    const clientMutationId = createCorrelationId("task-create");
    setSubmitting(true);
    setMessage(null);
    try {
      await draft.flush();
      const result = await dispatch(
        TASK_INTENTS.create,
        {
          title: value.title.trim(),
          attention_state: value.attention_state,
          urgency: value.urgency,
          due_date: optional(value.due_date),
          deadline_date: optional(value.deadline_date),
          project: optional(value.project),
          namespaces: csv(value.namespaces),
          summary: optional(value.summary),
          desired_outcome: optional(value.desired_outcome),
          next_action: optional(value.next_action),
          definition_of_done: optional(value.definition_of_done),
          dependencies: csv(value.dependencies),
        },
        clientMutationId,
      );
      await finish(result, revision, "create the task");
      if (result.status === "accepted") setStructureConfirmation([]);
    } catch (error) {
      const text = error instanceof Error ? error.message : "Task draft could not be saved.";
      setMessage({ tone: "danger", text });
      announce(text, "assertive");
    } finally {
      setSubmitting(false);
    }
  };

  const submitOne = (event: FormEvent) => {
    event.preventDefault();
    void createOne(false);
  };

  const itemsForBatch = (lines: readonly string[], clientMutationId: string) =>
    lines.map((title, index) => ({
      title,
      attention_state: "inbox",
      urgency: "medium",
      child_mutation_id: `${clientMutationId}:${index + 1}`,
    }));

  const previewBatch = async (lines: readonly string[]) => {
    if (readOnly || lines.length === 0) return;
    const requestSequence = ++previewRequestRef.current;
    const clientMutationId = createCorrelationId("task-batch");
    setPreviewing(true);
    setServerPreview(null);
    setMessage(null);
    try {
      const result = await dispatch(
        TASK_INTENTS.batchPreview,
        { items: itemsForBatch(lines, clientMutationId) },
        clientMutationId,
      );
      const preview = (result.value as unknown as { readonly preview?: TaskBatchPreview } | undefined)?.preview;
      if (result.status !== "accepted" || preview === undefined) {
        if (requestSequence !== previewRequestRef.current) return;
        const text = acceptedMessage(result, "Tasks could not preview the pasted rows.");
        setMessage({ tone: "danger", text });
        announce(text, "assertive");
        return;
      }
      if (requestSequence !== previewRequestRef.current) return;
      setServerPreview({ mutationId: clientMutationId, preview });
      const text = preview.can_commit
        ? `Preview ready. ${preview.accepted_count} tasks can be created.`
        : "Preview ready, but no valid new tasks can be created.";
      announce(text, preview.can_commit ? "polite" : "assertive");
    } catch (error) {
      const text = error instanceof Error ? error.message : "Task batch could not be previewed.";
      if (requestSequence !== previewRequestRef.current) return;
      setMessage({ tone: "danger", text });
      announce(text, "assertive");
    } finally {
      if (requestSequence === previewRequestRef.current) setPreviewing(false);
    }
  };

  const submitBatch = async () => {
    if (
      readOnly ||
      submitting ||
      serverPreview === null ||
      !serverPreview.preview.can_commit
    ) return;
    const revision = draft.revision;
    const clientMutationId = serverPreview.mutationId;
    setSubmitting(true);
    setMessage(null);
    try {
      await draft.flush();
      const result = await dispatch(
        TASK_INTENTS.batchCreate,
        {
          preview_confirmed: true,
          preview_token: serverPreview.preview.preview_token,
          accepted_indices: serverPreview.preview.accepted_indices,
          items: itemsForBatch(value.batch_lines, clientMutationId),
        },
        clientMutationId,
      );
      await finish(result, revision, "create the batch");
    } catch (error) {
      const text = error instanceof Error ? error.message : "Task batch could not be saved.";
      setMessage({ tone: "danger", text });
      announce(text, "assertive");
    } finally {
      setSubmitting(false);
    }
  };

  const capturePaste = (event: ClipboardEvent<HTMLInputElement>) => {
    const text = event.clipboardData.getData("text");
    const rows = parseTaskBatch(text);
    if (rows.length < 2) return;
    event.preventDefault();
    const retainedTitle = value.title.trim();
    const lines = [
      ...(retainedTitle ? [retainedTitle] : []),
      ...rows.map((row) => row.title),
    ];
    update("batch_lines", lines);
    setMessage(null);
    announce(
      retainedTitle
        ? `Previewing ${lines.length} tasks, including the title already entered.`
        : `Previewing ${lines.length} pasted tasks.`,
    );
    void previewBatch(lines);
  };

  const enterFastPath = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.ctrlKey && !event.metaKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  if (!draft.ready) return <p className="wb-tasks-loading" aria-busy="true">Restoring task draft…</p>;

  return (
    <form ref={formRef} className="wb-task-composer" onSubmit={submitOne} noValidate>
      {draft.error ? <InlineAlert tone="danger">{draft.error} Your draft remains open.</InlineAlert> : null}
      {message ? <InlineAlert tone={message.tone}>{message.text}</InlineAlert> : null}
      {structureConfirmation.length > 0 ? (
        <InlineAlert tone="warning">
          <span>
            This will create {structureConfirmation.join(" and ")}. The task is not saved yet.
          </span>{" "}
          <Button
            size="small"
            disabled={readOnly || submitting}
            onClick={() => void createOne(true)}
          >
            Confirm structure and add
          </Button>
        </InlineAlert>
      ) : null}

      {value.batch_lines.length > 0 ? (
        <section className="wb-task-batch" aria-labelledby="wb-task-batch-title">
          <div className="wb-task-batch__header">
            <div>
              <h3 id="wb-task-batch-title">Review pasted tasks</h3>
              <p>
                {previewing
                  ? `Checking ${value.batch_lines.length} rows against Tasks…`
                  : serverPreview === null
                    ? "Server preview unavailable. Cancel and paste again to retry."
                    : `${batchRows.length} rows · ${batchRows.filter((row) => row.duplicate).length} duplicates skipped · ${batchRows.filter((row) => !row.valid).length} invalid skipped`}
              </p>
            </div>
            <div className="wb-task-field__inline">
              <Button size="small" variant="ghost" disabled={previewing || readOnly} onClick={() => void previewBatch(value.batch_lines)}>Preview again</Button>
              <Button size="small" onClick={() => update("batch_lines", [])}>Cancel</Button>
            </div>
          </div>
          <ol>
            {batchRows.map((row, index) => (
              <li key={`${row.index}:${row.title}:${index}`} className={row.duplicate || !row.valid ? "is-duplicate" : ""}>
                <span>{row.title}</span>
                {row.duplicate ? (
                  <span className="wb-task-badge">
                    {row.duplicate_reason === "existing_title" ? "Already in Tasks" : "Repeated in paste"}
                  </span>
                ) : null}
                {!row.valid ? (
                  <span className="wb-task-field-error">
                    {Object.values(row.field_errors).join(" ") || "Invalid task"}
                  </span>
                ) : null}
              </li>
            ))}
          </ol>
          <Button
            variant="primary"
            disabled={submitting || previewing || readOnly || serverPreview?.preview.can_commit !== true}
            onClick={() => void submitBatch()}
          >
            {submitting ? "Creating…" : previewing ? "Previewing…" : `Create ${serverPreview?.preview.accepted_count ?? 0} tasks`}
          </Button>
        </section>
      ) : (
        <>
          <div className="wb-task-composer__fast-path">
            <label className="wb-task-field wb-task-field--grow">
              <span>New task</span>
              <input
                ref={titleRef}
                autoComplete="off"
                value={value.title}
                disabled={readOnly || submitting}
                aria-invalid={fieldError("title", "description") ? "true" : undefined}
                aria-describedby={fieldError("title", "description") ? "wb-task-create-title-error" : "wb-task-create-title-help"}
                placeholder="What needs doing?"
                onChange={(event) => update("title", event.target.value)}
                onKeyDown={enterFastPath}
                onPaste={capturePaste}
              />
            </label>
            <Button type="submit" variant="primary" disabled={readOnly || submitting || value.title.trim().length === 0}>
              {submitting ? "Adding…" : "Add task"}
            </Button>
          </div>
          <p id="wb-task-create-title-help" className="wb-task-field-help">Press Enter to add to Inbox. Paste several lines to preview a batch.</p>
          {fieldError("title", "description") ? <p id="wb-task-create-title-error" className="wb-task-field-error">{fieldError("title", "description")}</p> : null}

          <Button size="small" variant="ghost" onClick={() => setDetailsOpen((open) => !open)} aria-expanded={detailsOpen}>
            {detailsOpen ? "Hide details" : "Add details"}
          </Button>

          {detailsOpen ? (
            <div className="wb-task-composer__details">
              <label className="wb-task-field"><span>State</span><select value={value.attention_state} aria-invalid={fieldError("attention_state", "state") ? "true" : undefined} aria-describedby={fieldError("attention_state", "state") ? "wb-task-create-state-error" : undefined} onChange={(event) => update("attention_state", event.target.value)}><option value="inbox">Inbox</option><option value="mit">Most Important</option><option value="active">Active</option><option value="focused">Focused</option><option value="waiting">Waiting</option></select>{fieldError("attention_state", "state") ? <small id="wb-task-create-state-error" className="wb-task-field-error">{fieldError("attention_state", "state")}</small> : null}</label>
              <label className="wb-task-field"><span>Urgency</span><select value={value.urgency} aria-invalid={fieldError("urgency") ? "true" : undefined} aria-describedby={fieldError("urgency") ? "wb-task-create-urgency-error" : undefined} onChange={(event) => update("urgency", event.target.value as TaskUrgency)}><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option></select>{fieldError("urgency") ? <small id="wb-task-create-urgency-error" className="wb-task-field-error">{fieldError("urgency")}</small> : null}</label>
              <label className="wb-task-field"><span>Due date</span><input type="date" value={value.due_date} aria-invalid={fieldError("due_date") ? "true" : undefined} aria-describedby={fieldError("due_date") ? "wb-task-create-due-error" : undefined} onChange={(event) => update("due_date", event.target.value)} />{fieldError("due_date") ? <small id="wb-task-create-due-error" className="wb-task-field-error">{fieldError("due_date")}</small> : null}</label>
              <label className="wb-task-field"><span>Hard deadline</span><input type="date" value={value.deadline_date} aria-invalid={fieldError("deadline_date") ? "true" : undefined} aria-describedby={fieldError("deadline_date") ? "wb-task-create-deadline-error" : undefined} onChange={(event) => update("deadline_date", event.target.value)} />{fieldError("deadline_date") ? <small id="wb-task-create-deadline-error" className="wb-task-field-error">{fieldError("deadline_date")}</small> : null}</label>
              <label className="wb-task-field"><span>Project</span><input list="wb-task-project-options" value={value.project} onChange={(event) => update("project", event.target.value)} /></label>
              <label className="wb-task-field"><span>Namespaces</span><input value={value.namespaces} placeholder="personal, errands" onChange={(event) => update("namespaces", event.target.value)} /></label>
              <datalist id="wb-task-project-options">{input.options.projects.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</datalist>
              <TextAreaField label="Summary" value={value.summary} rows={2} aria-invalid={fieldError("summary", "summary_text") ? "true" : undefined} description={fieldError("summary", "summary_text")} onChange={(next) => update("summary", next)} />
              <TextAreaField label="Desired outcome" value={value.desired_outcome} rows={2} aria-invalid={fieldError("desired_outcome", "outcome_text") ? "true" : undefined} description={fieldError("desired_outcome", "outcome_text")} onChange={(next) => update("desired_outcome", next)} />
              <TextAreaField label="Next action" value={value.next_action} rows={2} aria-invalid={fieldError("next_action", "next_action_text") ? "true" : undefined} description={fieldError("next_action", "next_action_text")} onChange={(next) => update("next_action", next)} />
              <TextAreaField label="Definition of done" value={value.definition_of_done} rows={2} aria-invalid={fieldError("definition_of_done") ? "true" : undefined} description={fieldError("definition_of_done")} onChange={(next) => update("definition_of_done", next)} />
              <label className="wb-task-field wb-task-field--wide"><span>Dependencies</span><input value={value.dependencies} placeholder="Comma-separated" aria-invalid={fieldError("dependencies") ? "true" : undefined} aria-describedby={fieldError("dependencies") ? "wb-task-create-dependencies-error" : undefined} onChange={(event) => update("dependencies", event.target.value)} />{fieldError("dependencies") ? <small id="wb-task-create-dependencies-error" className="wb-task-field-error">{fieldError("dependencies")}</small> : null}</label>
            </div>
          ) : null}
        </>
      )}
    </form>
  );
}

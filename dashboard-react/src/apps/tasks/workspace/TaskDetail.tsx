import { type FormEvent, type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";

import type {
  IntentResult,
  JsonValue,
  WidgetIntent,
  WidgetPresentationContext,
} from "../../../dashboard/contributions/contracts";
import { useDashboardAnnouncer } from "../../../dashboard/accessibility/DashboardAnnouncer";
import { useWidgetDraft } from "../../../dashboard/drafts";
import { InlineAlert, TextAreaField } from "../../../ui";
import { TaskButton as Button, TaskHelp, TASK_HELP } from "./TaskHelp";
import { createCorrelationId, createWidgetIntent } from "../../../widget-library/shared";
import { formatActorLabel } from "../../../widget-library/shared/format";
import { LinkedLocalFilesPanel } from "../../cowork/documents/LinkedLocalFilesPanel";
import {
  HttpCoworkLocalFileClient,
  type CoworkLocalFileClient,
  type CoworkLocalFileLink,
} from "../../cowork/localFiles";
import {
  TASK_INTENTS,
  type TaskDetail as TaskDetailModel,
  type TaskUrgency,
  type TaskOptions,
} from "../contracts";
import { attentionLabel, MultiSelect } from "./TaskFilters";
import { TaskCompletionDialog, type CompletionTask } from "./TaskCompletionDialog";
import { TaskNamespacePills } from "./TaskNamespacePills";

interface TaskEditDraft {
  readonly base_revision?: number;
  readonly remove_unresolved_projects?: readonly string[];
  readonly title: string;
  readonly attention_state: string;
  readonly urgency: TaskUrgency;
  readonly due_date: string;
  readonly deadline_date: string;
  readonly project_ids: readonly number[];
  readonly namespaces: string;
  readonly tags: string;
  readonly summary: string;
  readonly desired_outcome: string;
  readonly next_action: string;
  readonly definition_of_done: string;
  readonly dependencies: string;
  readonly contract: string;
  readonly required_contexts: string;
  readonly automation_tier: string;
}

const fromTask = (task: TaskDetailModel): TaskEditDraft => ({
  base_revision: task.revision,
  remove_unresolved_projects: [],
  title: task.title,
  attention_state: task.attention_state,
  urgency: task.urgency,
  due_date: task.due_date ?? "",
  deadline_date: task.deadline_date ?? "",
  project_ids: task.project_ids ?? [],
  namespaces: task.namespaces.join(", "),
  tags: task.tags.filter((tag) => !task.namespaces.includes(tag)).join(", "),
  summary: task.summary,
  desired_outcome: task.desired_outcome,
  next_action: task.next_action,
  definition_of_done: task.definition_of_done,
  dependencies: task.dependencies.join(", "),
  contract: task.contract ?? "",
  required_contexts: task.required_contexts.join(", "),
  automation_tier: task.automation_tier ?? "",
});

const csv = (value: string) => value.split(",").map((item) => item.trim()).filter(Boolean);
const optional = (value: string): string | null => value.trim() || null;
const comparable = ({ base_revision: _revision, ...fields }: TaskEditDraft) => JSON.stringify(fields);

export interface TaskDetailProps {
  readonly task: TaskDetailModel;
  readonly options?: TaskOptions;
  readonly readOnly: boolean;
  readonly presentation: WidgetPresentationContext;
  emit(intent: WidgetIntent): Promise<IntentResult>;
  onClose(): void;
  readonly undoDeleteRevision: number | null;
  onDeleteAcknowledged(revision: number): void;
  onDeleteUndone(): void;
}

export function TaskDetail({
  task: suppliedTask,
  options,
  readOnly,
  presentation,
  emit,
  onClose,
  undoDeleteRevision,
  onDeleteAcknowledged,
  onDeleteUndone,
}: TaskDetailProps) {
  const [acknowledgedTask, setAcknowledgedTask] = useState<TaskDetailModel | null>(null);
  // A successful receipt can arrive before the provider's refreshed snapshot.
  const task = acknowledgedTask?.task_id === suppliedTask.task_id && acknowledgedTask.revision > suppliedTask.revision
    ? acknowledgedTask : suppliedTask;
  const initial = useMemo(() => fromTask(task), [task.task_id]);
  const baselineRef = useRef(initial);
  const latestTaskRef = useRef(task); latestTaskRef.current = task;
  const draft = useWidgetDraft("task-edit", initial, {
    isPristine: (value) => comparable(value) === comparable(fromTask(task)),
  });
  useEffect(() => {
    if (!draft.ready || baselineRef.current.base_revision === task.revision) return;
    const next = fromTask(task);
    if (comparable(draft.value) === comparable(baselineRef.current)) {
      baselineRef.current = next;
      draft.setValue(next);
    }
  }, [task.revision, draft.ready]);
  const { announce } = useDashboardAnnouncer();
  const titleRef = useRef<HTMLInputElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => { if (draft.ready) headingRef.current?.focus(); }, [draft.ready, task.task_id]);
  const formRef = useRef<HTMLFormElement>(null);
  const deleteTriggerRef = useRef<HTMLButtonElement>(null);
  const deleteDialogRef = useRef<HTMLDivElement>(null);
  const [actionBusy, setBusy] = useState(false);
  const [namespaceBusy, setNamespaceBusy] = useState(false);
  const busy = actionBusy || namespaceBusy;
  const namespaceInFlight = useRef(false);
  const namespaceRequests = useRef(new Map<string, { task_id: string; expected_revision: number; namespaces: string[] }>());
  const [message, setMessage] = useState<{ tone: "danger" | "success" | "warning"; text: string } | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Readonly<Record<string, string>>>({});
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [completionTask, setCompletionTask] = useState<CompletionTask | null>(null);
  const [snoozeDate, setSnoozeDate] = useState(task.snooze_until?.slice(0, 10) ?? "");
  const [actionText, setActionText] = useState("");
  const [editingActionId, setEditingActionId] = useState<string | null>(null);
  const [editingActionText, setEditingActionText] = useState("");
  const value = draft.value;
  const stale = (value.base_revision ?? initial.base_revision) !== task.revision;
  const activeActionItems = task.action_items.filter((item) => !item.deleted_at);
  const deletedActionItems = task.action_items.filter((item) => item.deleted_at);
  const deleted = task.deleted_at !== null;
  const canMutate = !readOnly && !deleted;
  const namespaceAllowedRef = useRef(false);
  namespaceAllowedRef.current = canMutate && !actionBusy;

  const fieldError = (...keys: readonly string[]): string | undefined => {
    for (const [key, message] of Object.entries(fieldErrors)) {
      if (keys.some((candidate) => key === candidate || key.startsWith(`${candidate}.`))) {
        return message;
      }
    }
    return undefined;
  };

  const errorDescription = (id: string, message: string | undefined) =>
    message ? <small id={id} className="wb-task-field-error">{message}</small> : null;

  const update = <Key extends keyof TaskEditDraft>(key: Key, next: TaskEditDraft[Key]) => {
    draft.setValue((current) => ({ ...current, [key]: next }));
  };

  const dispatch = async (type: string, payload: JsonValue, reuseId?: string): Promise<IntentResult> => {
    const parts = type.split(".");
    const id = reuseId ?? createCorrelationId(parts[parts.length - 1] ?? "task");
    return emit(createWidgetIntent(presentation, type, payload, {
      intentId: id,
      clientMutationId: id,
    }) as WidgetIntent);
  };

  // The pill control retains this revision closure with the reviewed change.
  const namespaceRevision = value.base_revision ?? task.revision;
  const changeNamespaces = async (namespaces: readonly string[], mutationId = createCorrelationId("task-namespace")): Promise<IntentResult> => {
    if (!namespaceAllowedRef.current || namespaceInFlight.current) {
      return { intent_id: mutationId, status: "unavailable", message: "Namespace changes are unavailable while this task cannot be edited." };
    }
    let request = namespaceRequests.current.get(mutationId);
    if (!request) {
      request = { task_id: task.task_id, expected_revision: namespaceRevision, namespaces: [...namespaces] };
      namespaceRequests.current.set(mutationId, request);
    }
    namespaceInFlight.current = true;
    setNamespaceBusy(true);
    setMessage(null);
    try {
      const result = await dispatch(TASK_INTENTS.update, request, mutationId);
      if (result.status === "accepted") {
        const saved = (result.value as { task?: TaskDetailModel } | undefined)?.task;
        if (!saved || saved.task_id !== request.task_id || saved.revision <= request.expected_revision) {
          return { ...result, status: "unavailable", message: "The saved namespace change could not be verified. Retry the same change to confirm its outcome." };
        }
        if (saved.revision >= (baselineRef.current.base_revision ?? 0)) {
          const next = fromTask(saved);
          baselineRef.current = next;
          setAcknowledgedTask(saved);
          // Only namespace assignments were submitted. Keep edits made before
          // or during the request, rebased against its acknowledged revision.
          draft.setValue((current) => ({ ...current, namespaces: next.namespaces, base_revision: saved.revision }));
        }
        setFieldErrors({});
        const text = "Namespaces saved. Other field edits remain in your draft.";
        setMessage({ tone: "success", text });
        announce(text);
      } else if (result.status === "conflict") {
        await dispatch(TASK_INTENTS.locationChange, { patch: { task: task.task_id }, replace: true }).catch(() => undefined);
      }
      return result;
    } catch (error) {
      return { intent_id: mutationId, status: "unavailable", message: error instanceof Error ? error.message : "The namespace change could not be confirmed." };
    } finally {
      namespaceInFlight.current = false;
      setNamespaceBusy(false);
    }
  };

  const localFileClient = useMemo<CoworkLocalFileClient>(() => {
    const links: readonly CoworkLocalFileLink[] = task.local_files.map((file) => ({
      linkId: file.link_id,
      href: `wb-local-file:${file.link_id}`,
      displayName: file.display_name,
      suffix: file.allowed_action === "reveal" ? ".ppk" : ".pdf",
      mediaType: file.media_type,
      byteLength: file.byte_length,
      sensitivity: file.sensitivity,
      allowedAction: file.allowed_action,
      availability:
        file.availability === "available"
          ? "verified"
          : file.availability === "changed"
            ? "changed"
            : "unavailable",
      localActionAvailable: canMutate && file.host_action_available,
    }));
    const inspectionClient =
      task.document.store_id !== null && task.document.document_id !== null
        ? new HttpCoworkLocalFileClient({
            storeId: task.document.store_id,
            documentId: task.document.document_id,
          })
        : null;
    return {
      // The task response seeds first paint. Explicit rechecks go back to the
      // metadata-only Co-work endpoint so an unavailable file is never trapped
      // in the task-detail snapshot captured by this render.
      list: async (options) => {
        if (!options?.refresh || inspectionClient === null) {
          if (task.local_files_error) throw new Error(task.local_files_error);
          return links;
        }
        const inspected = await inspectionClient.list(options);
        return inspected.map((link) => ({
          ...link,
          localActionAvailable: canMutate && link.localActionAvailable,
        }));
      },
      activate: async (link) => {
        if (!canMutate) {
          throw new Error("This task does not currently allow local file actions.");
        }
        const result = await dispatch(TASK_INTENTS.localFileAction, {
          task_id: task.task_id,
          expected_revision: task.revision,
          link_id: link.linkId,
          action: link.allowedAction,
        });
        if (result.status !== "accepted") {
          throw new Error(result.message ?? "The linked local file could not be opened.");
        }
      },
    };
  }, [
    canMutate,
    emit,
    presentation,
    task.document.document_id,
    task.document.store_id,
    task.local_files,
    task.local_files_error,
    task.revision,
    task.task_id,
  ]);

  const action = async (type: string, extra: Record<string, JsonValue> = {}) => {
    if (readOnly || busy || (deleted && type !== TASK_INTENTS.restore)) return;
    setBusy(true);
    setMessage(null);
    const result = await dispatch(type, {
      task_id: task.task_id,
      expected_revision: task.revision,
      ...extra,
    }).catch((error: unknown): IntentResult => ({
      intent_id: "failed",
      status: "unavailable",
      message: error instanceof Error ? error.message : "Tasks is unavailable.",
    }));
    setBusy(false);
    if (result.status === "accepted") {
      const text = result.message ?? "Task updated.";
      setMessage({ tone: "success", text });
      announce(text);
    } else {
      const text = result.message ?? "Task could not be updated.";
      setFieldErrors(result.fieldErrors ?? {});
      setMessage({ tone: result.status === "conflict" ? "warning" : "danger", text });
      announce(text, "assertive");
      if (Object.keys(result.fieldErrors ?? {}).length > 0) {
        window.requestAnimationFrame(() => {
          const invalid = formRef.current?.querySelector<HTMLElement>("[aria-invalid='true']");
          (invalid ?? titleRef.current)?.focus({ preventScroll: true });
        });
      }
      if (result.status === "conflict") {
        await dispatch(TASK_INTENTS.locationChange, {
          patch: { task: task.task_id },
          replace: true,
        });
      }
    }
    return result;
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (stale || !draft.ready) return;
    try { await draft.flush(); } catch (error: unknown) {
      setMessage({ tone: "danger", text: error instanceof Error ? error.message : "Draft could not be saved." });
      return;
    }
    const submittedDraftRevision = draft.getSnapshot().revision;
    const result = await action(TASK_INTENTS.update, {
      expected_revision: value.base_revision ?? task.revision,
      title: value.title.trim(),
      attention_state: value.attention_state,
      urgency: value.urgency,
      due_date: optional(value.due_date),
      deadline_date: optional(value.deadline_date),
      project_ids: [...(value.project_ids ?? task.project_ids ?? [])],
      remove_unresolved_projects: [...(value.remove_unresolved_projects ?? [])],
      namespaces: csv(value.namespaces),
      tags: csv(value.tags),
      summary: value.summary,
      desired_outcome: value.desired_outcome,
      next_action: value.next_action,
      definition_of_done: value.definition_of_done,
      dependencies: csv(value.dependencies),
      contract: optional(value.contract),
      required_contexts: csv(value.required_contexts),
      automation_tier: optional(value.automation_tier),
    });
    if (result?.status === "accepted") {
      const saved = (result.value as unknown as { task?: TaskDetailModel } | undefined)?.task ?? latestTaskRef.current;
      const next = fromTask(saved);
      baselineRef.current = next;
      const current = draft.getSnapshot();
      setAcknowledgedTask(saved);
      draft.setValue(current.revision === submittedDraftRevision ? next : { ...current.value, base_revision: saved.revision });
    }
  };

  const openDocument = async () => {
    const result = await dispatch(TASK_INTENTS.openDocument, { task_id: task.task_id });
    if (result.status !== "accepted") {
      const text = result.message ?? "The knowledge document could not be opened.";
      setMessage({ tone: "danger", text });
      announce(text, "assertive");
    }
  };

  const deleteTask = async () => {
    setDeleteConfirm(false);
    const result = await action(TASK_INTENTS.delete);
    if (result?.status !== "accepted") return;
    const resultValue = result.value;
    const resultTask = resultValue !== null && typeof resultValue === "object"
      ? (resultValue as { readonly task?: { readonly revision?: unknown } }).task
      : undefined;
    onDeleteAcknowledged(
      typeof resultTask?.revision === "number" ? resultTask.revision : task.revision + 1,
    );
  };

  const undoDelete = async () => {
    if (undoDeleteRevision === null) return;
    const result = await action(TASK_INTENTS.restore, {
      expected_revision: undoDeleteRevision,
    });
    if (result?.status === "accepted") onDeleteUndone();
  };

  const restoreTask = async () => {
    const result = await action(TASK_INTENTS.restore);
    if (result?.status === "accepted") onDeleteUndone();
  };

  const reorderActionItem = (actionItemId: string, offset: -1 | 1) => {
    const ids = activeActionItems.map((item) => item.action_item_id);
    const index = ids.indexOf(actionItemId);
    const nextIndex = index + offset;
    if (index < 0 || nextIndex < 0 || nextIndex >= ids.length) return;
    [ids[index], ids[nextIndex]] = [ids[nextIndex]!, ids[index]!];
    void action(TASK_INTENTS.actionItemReorder, { action_item_ids: ids });
  };

  const closeDeleteDialog = () => {
    setDeleteConfirm(false);
    window.requestAnimationFrame(() => {
      deleteTriggerRef.current?.focus({ preventScroll: true });
    });
  };

  const trapDeleteFocus = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeDeleteDialog();
      return;
    }
    if (event.key !== "Tab") return;
    const controls = [...(deleteDialogRef.current?.querySelectorAll<HTMLElement>(
      "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled])",
    ) ?? [])];
    if (controls.length === 0) return;
    const first = controls[0]!;
    const last = controls[controls.length - 1]!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const closeOnEscape = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== "Escape" || event.defaultPrevented) return;
    event.preventDefault();
    if (deleteConfirm) {
      closeDeleteDialog();
      return;
    }
    // Portalled overlays still bubble through this article. Their own Escape
    // handling controls dismissal, including keeping a pending mutation open.
    if (event.target instanceof Element && event.target.closest('[role="dialog"], [role="alertdialog"]')) return;
    void closePreservingDraft();
  };
  const closePreservingDraft = async () => {
    try { await draft.flush(); onClose(); }
    catch (error) { setMessage({ tone: "danger", text: error instanceof Error ? error.message : "Your draft could not be saved. Keep this view open to retain your edits." }); }
  };

  if (!draft.ready) return <p className="wb-tasks-loading" aria-busy="true">Restoring task edits…</p>;

  return (
    <article className="wb-task-detail" aria-labelledby="wb-task-detail-title" onKeyDown={closeOnEscape}>
      <div className="wb-task-detail__header">
        <div>
          <p className="wb-task-detail__kicker">{attentionLabel(task.attention_state)} · Revision {task.revision}</p>
          <h2 ref={headingRef} tabIndex={-1} id="wb-task-detail-title">Task details</h2>
        </div>
        <Button size="small" variant="ghost" onClick={() => void closePreservingDraft()}>Back to tasks</Button>
      </div>
      {message ? <InlineAlert tone={message.tone}>{message.text}</InlineAlert> : null}
      {draft.error ? <InlineAlert tone="danger">{draft.error} Your edits remain in this view.</InlineAlert> : null}
      {stale ? <InlineAlert tone="warning">This task changed elsewhere. Your draft is preserved; review the saved values before saving again.<details><summary>Compare with saved task</summary><dl>{Object.entries(fromTask(task)).filter(([key, saved]) => key !== "base_revision" && JSON.stringify(saved) !== JSON.stringify(value[key as keyof TaskEditDraft])).map(([key, saved]) => <div key={key}><dt>{key.replace(/_/g, " ")}</dt><dd>Saved: {Array.isArray(saved) ? saved.join(", ") : String(saved || "Empty")}</dd><dd>Your draft: {String(value[key as keyof TaskEditDraft] || "Empty")}</dd></div>)}</dl></details><div className="wb-task-actions"><Button size="small" disabled={busy} onClick={() => { const next = fromTask(task); baselineRef.current = next; draft.setValue(next); }}>Discard my edits and load saved task</Button><Button size="small" disabled={busy || readOnly} onClick={() => { baselineRef.current = fromTask(task); draft.setValue({ ...value, base_revision: task.revision }); }}>Keep my draft against this saved version</Button></div></InlineAlert> : null}
      {undoDeleteRevision !== null ? (
        <InlineAlert tone="success">
          Task moved to trash. <Button size="small" disabled={readOnly || busy} onClick={() => void undoDelete()}>Undo delete</Button>
        </InlineAlert>
      ) : null}
      {deleted ? <InlineAlert tone="warning">Restore this task before editing it.</InlineAlert> : null}

      <form ref={formRef} className="wb-task-detail__form" onSubmit={save} noValidate>
        <label className="wb-task-field wb-task-field--wide">
          <span>Title</span>
          <input
            ref={titleRef}
            value={value.title}
            disabled={!canMutate}
            aria-invalid={fieldError("title", "description") ? "true" : undefined}
            aria-describedby={fieldError("title", "description") ? "wb-task-edit-title-error" : undefined}
            onChange={(event) => update("title", event.target.value)}
          />
          {errorDescription("wb-task-edit-title-error", fieldError("title", "description"))}
        </label>
        <label className="wb-task-field">
          <span>Attention</span>
          <TaskHelp content={TASK_HELP.editAttention}><select
            value={value.attention_state}
            disabled={!canMutate}
            aria-invalid={fieldError("attention_state", "state") ? "true" : undefined}
            aria-describedby={fieldError("attention_state", "state") ? "wb-task-edit-state-error" : undefined}
            onChange={(event) => update("attention_state", event.target.value)}
          >
            <option value="inbox">Inbox</option>
            <option value="mit">Most Important</option>
            <option value="active">Active</option>
            <option value="focused">Working on now</option>
            <option value="waiting">Waiting</option>
            {value.attention_state === "snoozed" ? <option value="snoozed" disabled>Snoozed</option> : null}
            {value.attention_state === "done" ? <option value="done" disabled>Done</option> : null}
          </select></TaskHelp>
          {errorDescription("wb-task-edit-state-error", fieldError("attention_state", "state"))}
        </label>
        <label className="wb-task-field">
          <span>Urgency</span>
          <select
            value={value.urgency}
            disabled={!canMutate}
            aria-invalid={fieldError("urgency") ? "true" : undefined}
            aria-describedby={fieldError("urgency") ? "wb-task-edit-urgency-error" : undefined}
            onChange={(event) => update("urgency", event.target.value as TaskUrgency)}
          >
            <option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option>
          </select>
          {errorDescription("wb-task-edit-urgency-error", fieldError("urgency"))}
        </label>
        <label className="wb-task-field"><span>Due date</span><input type="date" value={value.due_date} disabled={!canMutate} aria-invalid={fieldError("due_date") ? "true" : undefined} aria-describedby={fieldError("due_date") ? "wb-task-edit-due-error" : undefined} onChange={(event) => update("due_date", event.target.value)} />{errorDescription("wb-task-edit-due-error", fieldError("due_date"))}</label>
        <label className="wb-task-field"><span>Hard deadline</span><input type="date" value={value.deadline_date} disabled={!canMutate} aria-invalid={fieldError("deadline_date") ? "true" : undefined} aria-describedby={fieldError("deadline_date") ? "wb-task-edit-deadline-error" : undefined} onChange={(event) => update("deadline_date", event.target.value)} />{errorDescription("wb-task-edit-deadline-error", fieldError("deadline_date"))}</label>
        <div className="wb-task-field"><span>Projects</span><MultiSelect purpose="selection" label="Linked projects" values={(value.project_ids ?? task.project_ids ?? []).map(String)} options={options?.projects.filter((option) => /^\d+$/.test(option.value)) ?? []} searchable disabled={!canMutate} onChange={(values) => update("project_ids", values.map(Number))} /><small>{(value.project_ids ?? task.project_ids ?? []).map((id) => options?.projects.find((option) => option.value === String(id))?.label ?? `Project ${id}`).join(", ") || "No linked projects"}</small>{errorDescription("wb-task-edit-project-error", fieldError("project_ids"))}{task.unresolved_projects?.length ? <InlineAlert tone="warning"><p>Historical project links need review. Choose the correct registered projects above, then remove any resolved historical references below. Unchecked references are retained.</p>{[...new Set(task.unresolved_projects.map((link) => link.legacy_value))].map((legacy) => <label className="wb-task-checkbox" key={legacy}><input type="checkbox" disabled={!canMutate} checked={value.remove_unresolved_projects?.includes(legacy) ?? false} onChange={() => update("remove_unresolved_projects", value.remove_unresolved_projects?.includes(legacy) ? value.remove_unresolved_projects.filter((item) => item !== legacy) : [...(value.remove_unresolved_projects ?? []), legacy])} /><span>Remove historical project link {legacy}</span></label>)}</InlineAlert> : null}</div>
        <div className="wb-task-field"><span>Namespaces</span><TaskNamespacePills taskId={task.task_id} namespaces={csv(value.namespaces)} options={options?.namespaces ?? []} readOnly={!canMutate} disabled={busy || stale} onChange={changeNamespaces} />{errorDescription("wb-task-edit-namespaces-error", fieldError("namespaces"))}</div>
        <label className="wb-task-field wb-task-field--wide"><span>Tags</span><input value={value.tags} disabled={!canMutate} aria-invalid={fieldError("tags") ? "true" : undefined} aria-describedby={fieldError("tags") ? "wb-task-edit-tags-error" : undefined} onChange={(event) => update("tags", event.target.value)} />{errorDescription("wb-task-edit-tags-error", fieldError("tags"))}</label>
        <TextAreaField label="Summary" value={value.summary} rows={3} disabled={!canMutate} aria-invalid={fieldError("summary", "summary_text") ? "true" : undefined} description={fieldError("summary", "summary_text")} onChange={(next) => update("summary", next)} />
        <TextAreaField label="Desired outcome" value={value.desired_outcome} rows={3} disabled={!canMutate} aria-invalid={fieldError("desired_outcome", "outcome_text") ? "true" : undefined} description={fieldError("desired_outcome", "outcome_text")} onChange={(next) => update("desired_outcome", next)} />
        <TextAreaField label="Next action" value={value.next_action} rows={3} disabled={!canMutate} aria-invalid={fieldError("next_action", "next_action_text") ? "true" : undefined} description={fieldError("next_action", "next_action_text")} onChange={(next) => update("next_action", next)} />
        <TextAreaField label="Definition of done" value={value.definition_of_done} rows={3} disabled={!canMutate} aria-invalid={fieldError("definition_of_done") ? "true" : undefined} description={fieldError("definition_of_done")} onChange={(next) => update("definition_of_done", next)} />
        <label className="wb-task-field wb-task-field--wide"><span>Dependencies</span><input value={value.dependencies} disabled={!canMutate} aria-invalid={fieldError("dependencies") ? "true" : undefined} aria-describedby={fieldError("dependencies") ? "wb-task-edit-dependencies-error" : undefined} onChange={(event) => update("dependencies", event.target.value)} />{errorDescription("wb-task-edit-dependencies-error", fieldError("dependencies"))}</label>
        <label className="wb-task-field"><span>Contract</span><input value={value.contract} disabled={!canMutate} aria-invalid={fieldError("contract") ? "true" : undefined} aria-describedby={fieldError("contract") ? "wb-task-edit-contract-error" : undefined} onChange={(event) => update("contract", event.target.value)} />{errorDescription("wb-task-edit-contract-error", fieldError("contract"))}</label>
        <label className="wb-task-field"><span>Required contexts</span><input value={value.required_contexts} disabled={!canMutate} aria-invalid={fieldError("required_contexts", "user_required_contexts") ? "true" : undefined} aria-describedby={fieldError("required_contexts", "user_required_contexts") ? "wb-task-edit-contexts-error" : undefined} onChange={(event) => update("required_contexts", event.target.value)} />{errorDescription("wb-task-edit-contexts-error", fieldError("required_contexts", "user_required_contexts"))}</label>
        <label className="wb-task-field"><span>Automation tier</span><input type="number" min="0" max="4" value={value.automation_tier} disabled={!canMutate} aria-invalid={fieldError("automation_tier", "automation_tier_achievable") ? "true" : undefined} aria-describedby={fieldError("automation_tier", "automation_tier_achievable") ? "wb-task-edit-automation-error" : undefined} onChange={(event) => update("automation_tier", event.target.value)} />{errorDescription("wb-task-edit-automation-error", fieldError("automation_tier", "automation_tier_achievable"))}</label>
        <div className="wb-task-detail__save"><Button type="submit" variant="primary" disabled={!canMutate || busy || stale || !draft.ready || value.title.trim().length === 0}>{busy ? "Saving…" : "Save changes"}</Button><span>{draft.dirty ? "Unsaved changes · draft kept on this device" : "All changes saved"}</span></div>
      </form>

      <section className="wb-task-detail__actions" aria-labelledby="wb-task-lifecycle-title">
        <h3 id="wb-task-lifecycle-title">Lifecycle</h3>
        <div className="wb-task-actions">
          {deleted ? (
            <Button size="small" disabled={readOnly || busy} onClick={() => void restoreTask()}>Restore</Button>
          ) : (
            <>
              <Button size="small" disabled={readOnly || busy} onClick={() => { if (task.attention_state === "done" || task.completed_at) void action(TASK_INTENTS.reopen); else setCompletionTask({ task_id: task.task_id, title: task.title, revision: task.revision }); }}>{task.attention_state === "done" || task.completed_at ? "Reopen" : "Complete"}</Button>
              <Button size="small" disabled={readOnly || busy} onClick={() => void action(TASK_INTENTS.focus)}>Working on now</Button>
              <label className="wb-task-field wb-task-field--inline"><span>Snooze until</span><input type="date" value={snoozeDate} disabled={readOnly || busy} onChange={(event) => setSnoozeDate(event.target.value)} /></label>
              <Button size="small" disabled={readOnly || busy || !snoozeDate} onClick={() => void action(TASK_INTENTS.snooze, { snooze_until: snoozeDate })}>Snooze</Button>
              <Button size="small" disabled={readOnly || busy} onClick={() => void action(task.archived_at ? TASK_INTENTS.unarchive : TASK_INTENTS.archive)}>{task.archived_at ? "Unarchive" : "Archive"}</Button>
              <Button ref={deleteTriggerRef} size="small" variant="danger" disabled={readOnly || busy} onClick={() => setDeleteConfirm(true)}>Move to trash</Button>
            </>
          )}
        </div>
        {completionTask ? <TaskCompletionDialog task={completionTask} onClose={() => setCompletionTask(null)} onConfirm={async (id) => {
          const result = await dispatch(TASK_INTENTS.complete, { task_id: completionTask.task_id, expected_revision: completionTask.revision }, id);
          if (result.status === "accepted") { const text = result.message ?? "Task completed."; setMessage({ tone: "success", text }); announce(text); }
          return result;
        }} /> : null}
        {deleteConfirm ? (
          <div
            className="wb-task-delete-backdrop"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) closeDeleteDialog();
            }}
          >
            <div
              ref={deleteDialogRef}
              className="wb-task-delete-confirm"
              role="alertdialog"
              aria-modal="true"
              aria-labelledby="wb-task-delete-title"
              aria-describedby="wb-task-delete-description"
              onKeyDown={trapDeleteFocus}
            >
              <h4 id="wb-task-delete-title">Move this task to trash?</h4>
              <p id="wb-task-delete-description">The task and knowledge document remain recoverable.</p>
              <div>
                <Button autoFocus size="small" onClick={closeDeleteDialog}>Cancel</Button>
                <Button help={TASK_HELP.confirmTrash} size="small" variant="danger" disabled={readOnly || busy} onClick={() => void deleteTask()}>Move to trash</Button>
              </div>
            </div>
          </div>
        ) : null}
      </section>

      <section className="wb-task-document" aria-labelledby="wb-task-document-title">
        <div className="wb-task-section-heading"><div><h3 id="wb-task-document-title">Knowledge document</h3><p>{task.document.updated_at ? `Edited ${task.document.updated_at}${task.document.updated_by ? ` by ${formatActorLabel(task.document.updated_by)}` : ""}` : "Long-form context lives in Co-work."}</p></div><div>{task.document.state === "available" ? <Button size="small" disabled={deleted} onClick={() => void openDocument()}>Open in Co-work</Button> : <Button size="small" variant="primary" disabled={!canMutate || busy} onClick={() => void action(TASK_INTENTS.createDocument)}>Create knowledge document</Button>}</div></div>
        {task.document.excerpt ? <blockquote>{task.document.excerpt}</blockquote> : <p className="wb-task-muted">No knowledge excerpt yet.</p>}
      </section>

      {task.document.store_id !== null && task.document.document_id !== null && (task.local_files.length > 0 || task.local_files_error !== null) ? (
        <LinkedLocalFilesPanel
          storeId={task.document.store_id}
          documentId={task.document.document_id}
          client={localFileClient}
        />
      ) : null}

      <section aria-labelledby="wb-task-action-items-title">
        <div className="wb-task-section-heading">
          <div><h3 id="wb-task-action-items-title">Action items</h3><p>Concrete steps owned by this task.</p></div>
        </div>
        <form className="wb-task-action-create" onSubmit={(event) => { event.preventDefault(); const text = actionText.trim(); if (!text) return; void action(TASK_INTENTS.actionItemCreate, { text }).then((result) => { if (result?.status === "accepted") setActionText(""); }); }}>
          <label className="wb-task-field wb-task-field--grow"><span>New action item</span><input value={actionText} disabled={!canMutate} onChange={(event) => setActionText(event.target.value)} /></label>
          <Button type="submit" size="small" aria-label="Add action item" disabled={!canMutate || busy || !actionText.trim()}>Add</Button>
        </form>
        <ul className="wb-task-action-items">
          {activeActionItems.map((item, index) => (
            <li key={item.action_item_id}>
              {editingActionId === item.action_item_id ? (
                <label className="wb-task-field wb-task-field--grow">
                  <span>Edit action item</span>
                  <input value={editingActionText} disabled={!canMutate} autoFocus onChange={(event) => setEditingActionText(event.target.value)} />
                </label>
              ) : (
                <span>{item.current ? <strong>Current · </strong> : null}{item.text}{item.approval_state !== "not_required" ? ` · ${item.approval_state}` : ""}</span>
              )}
              <span>
                {editingActionId === item.action_item_id ? (
                  <>
                    <Button size="small" aria-label={`Save action item ${item.text}`} disabled={!canMutate || busy || !editingActionText.trim()} onClick={() => void action(TASK_INTENTS.actionItemUpdate, { action_item_id: item.action_item_id, text: editingActionText.trim() }).then((result) => { if (result?.status === "accepted") setEditingActionId(null); })}>Save item</Button>
                    <Button size="small" variant="ghost" aria-label={`Cancel editing action item ${item.text}`} onClick={() => setEditingActionId(null)}>Cancel</Button>
                  </>
                ) : (
                  <Button size="small" variant="ghost" aria-label={`Edit action item ${item.text}`} disabled={!canMutate || busy} onClick={() => { setEditingActionId(item.action_item_id); setEditingActionText(item.text); }}>Edit</Button>
                )}
                <Button size="small" variant="ghost" aria-label={`${item.completed ? "Reopen" : "Complete"} action item ${item.text}`} disabled={!canMutate || busy} onClick={() => void action(TASK_INTENTS.actionItemUpdate, { action_item_id: item.action_item_id, completed: !item.completed })}>{item.completed ? "Reopen item" : "Complete item"}</Button>
                <Button size="small" variant="ghost" aria-label={`Move action item ${item.text} up`} disabled={!canMutate || busy || index === 0} onClick={() => reorderActionItem(item.action_item_id, -1)}>Move up</Button>
                <Button size="small" variant="ghost" aria-label={`Move action item ${item.text} down`} disabled={!canMutate || busy || index === activeActionItems.length - 1} onClick={() => reorderActionItem(item.action_item_id, 1)}>Move down</Button>
                {!item.current ? <Button size="small" variant="ghost" aria-label={`Make action item ${item.text} current`} disabled={!canMutate || busy} onClick={() => void action(TASK_INTENTS.actionItemCurrent, { action_item_id: item.action_item_id })}>Make current</Button> : null}
                {item.approval_state === "pending" ? <Button size="small" aria-label={`Approve action item ${item.text}`} disabled={!canMutate || busy} onClick={() => void action(TASK_INTENTS.actionItemApprove, { action_item_id: item.action_item_id })}>Approve</Button> : null}
                <Button size="small" variant="ghost" aria-label={`Remove action item ${item.text}`} disabled={!canMutate || busy} onClick={() => void action(TASK_INTENTS.actionItemDelete, { action_item_id: item.action_item_id })}>Remove</Button>
              </span>
            </li>
          ))}
        </ul>
        {deletedActionItems.length > 0 ? (
          <details>
            <summary>Removed action items ({deletedActionItems.length})</summary>
            <ul className="wb-task-action-items">
              {deletedActionItems.map((item) => (
                <li key={item.action_item_id}>
                  <span>{item.text}</span>
                  <Button size="small" aria-label={`Restore action item ${item.text}`} disabled={!canMutate || busy} onClick={() => void action(TASK_INTENTS.actionItemRestore, { action_item_id: item.action_item_id })}>Restore item</Button>
                </li>
              ))}
            </ul>
          </details>
        ) : null}
      </section>

      <details className="wb-task-history"><summary>History and provenance</summary><p>Created {task.provenance.created_at || "at an unknown time"} by {formatActorLabel(task.provenance.created_by)} via {task.provenance.source}.</p><ol>{task.history.map((entry) => <li key={entry.history_id}><time dateTime={entry.occurred_at}>{entry.occurred_at}</time><span><strong>{entry.action}</strong> · {entry.summary} · {formatActorLabel(entry.actor)}</span></li>)}</ol></details>
    </article>
  );
}

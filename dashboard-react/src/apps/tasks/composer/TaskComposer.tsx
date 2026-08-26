import {
  type ClipboardEvent,
  type FormEvent,
  type KeyboardEvent,
  useEffect,
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
import { AssistDraftButton, useAssistedDraft } from "../../../dashboard/assistance";
import { Button, InlineAlert } from "../../../ui";
import { createCorrelationId, createWidgetIntent } from "../../../widget-library/shared";
import {
  TASK_INTENTS,
  type TaskBatchPreview,
  type TaskQuickAddInput,
  type TaskProposal,
} from "../contracts";
import { TaskDraftFields } from "./TaskDraftFields";
import {
  EMPTY_TASK_CREATE_DRAFT, additionalTaskProposalParameters, canClearRealizedProposal, draftFromTaskProposal, isTaskCreateDraftPristine, newTaskStructures,
  retainedProposalResolution, taskDraftFields, taskDraftFingerprint, taskDraftSha256, taskProposalParameters, taskProposalResolution,
  type TaskCreateDraft,
} from "./taskDraft";
export { EMPTY_TASK_CREATE_DRAFT, isTaskCreateDraftPristine, type TaskCreateDraft } from "./taskDraft";

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
  const [message, setMessage] = useState<{ tone: "danger" | "success" | "warning"; text: string; taskId?: string } | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Readonly<Record<string, string>>>({});
  const draft = useWidgetDraft("task-create", EMPTY_TASK_CREATE_DRAFT, {
    isPristine: isTaskCreateDraftPristine,
  });
  const value = draft.value;
  const batchRows = serverPreview?.preview.rows ?? [];
  const readOnly = input.access.mode === "read_only" || presentation.interactionMode === "arrange";
  const proposalReadOnly = readOnly || presentation.interactionMode !== "operate";
  const assistance = useAssistedDraft("task-create", draft, {
    title: "Shape this task", interactionMode: presentation.interactionMode,
    readOnly: readOnly || submitting || value.batch_lines.length > 0,
    onOpen: () => setDetailsOpen(true),
  });
  const proposalChanged = value.proposal_ref !== undefined && value.proposal_ref.draftFingerprint !== taskDraftFingerprint(value);
  const selectedLinkedProposal = [input.observedProposal, input.selectedProposal]
    .filter((proposal): proposal is TaskProposal => proposal != null && value.proposal_ref !== undefined
      && proposal.thread_id === value.proposal_ref.threadId && proposal.proposal_event_id >= value.proposal_ref.proposalEventId)
    .sort((left, right) => right.proposal_event_id - left.proposal_event_id
      || Number(taskProposalResolution(right) !== undefined) - Number(taskProposalResolution(left) !== undefined))[0];
  const resolution = (selectedLinkedProposal && taskProposalResolution(selectedLinkedProposal))
    ?? retainedProposalResolution(value.proposal_ref);
  const proposalNeedsReview = value.proposal_ref !== undefined && (selectedLinkedProposal === undefined
    || selectedLinkedProposal.status !== "ready" || selectedLinkedProposal.proposal_event_id !== value.proposal_ref.proposalEventId);
  const proposalOutdated = selectedLinkedProposal !== undefined && selectedLinkedProposal !== null && selectedLinkedProposal.proposal_event_id !== value.proposal_ref?.proposalEventId;
  const requiresDetailedReview = value.proposal_ref?.requiresDetailedReview === true
    || (selectedLinkedProposal != null && additionalTaskProposalParameters(selectedLinkedProposal).length > 0);

  useEffect(() => {
    if (proposalReadOnly || submitting || !draft.ready || selectedLinkedProposal === undefined) return;
    const current = draft.getSnapshot();
    const reference = current.value.proposal_ref;
    const terminal = taskProposalResolution(selectedLinkedProposal);
    if (!current.ready || current.status === "error" || current.status === "conflict" || !reference || !terminal
      || reference.threadId !== selectedLinkedProposal.thread_id || terminal.proposalEventId < reference.proposalEventId) return;
    // A prior save-success message is superseded by this validated terminal
    // outcome. Preserve actual storage/decision errors for human recovery.
    setMessage((notice) => notice?.tone === "success" ? null : notice);
    // A retained hint only suppresses old decisions. It cannot authorize a
    // clear, and later edits cannot be cleared by replaying old terminal news.
    if (!retainedProposalResolution(reference) && canClearRealizedProposal(current.value, selectedLinkedProposal)) {
      void draft.clear({ ifRevision: current.revision }).then((cleared) => {
        if (!cleared) return;
        setFieldErrors({});
        setStructureConfirmation([]);
        setMessage({ tone: "success", text: "Task created from the saved proposal.", taskId: selectedLinkedProposal.realization!.task_id });
        announce("Task created from the saved proposal. Quick Add is ready for another task.");
      });
    } else if (JSON.stringify(retainedProposalResolution(reference)) !== JSON.stringify(terminal)) {
      draft.compareAndSet(current.revision, { ...current.value, proposal_ref: { ...reference, resolution: terminal } });
    }
  }, [announce, draft.clear, draft.compareAndSet, draft.getSnapshot, draft.ready, draft.revision, proposalReadOnly, selectedLinkedProposal, submitting]);

  const useRetainedFields = async () => {
    if (proposalReadOnly || submitting || !resolution) return;
    const current = draft.getSnapshot();
    if (!current.ready || current.status === "error" || current.status === "conflict") return;
    const { proposal_ref: _reference, proposal_pending: _pending, ...fields } = current.value;
    setSubmitting(true);
    try {
      // The host revokes old helpers and atomically replaces the stored value;
      // there is never a delete-then-restore window for retained human edits.
      if (!await draft.reset(fields, { ifRevision: current.revision })) {
        const latest = draft.getSnapshot();
        if (latest.revision === current.revision + 1 && (latest.status === "error" || latest.status === "conflict")) {
          setMessage({ tone: "danger", text: "The new draft could not be confirmed in storage. Your fields remain open; resolve the draft storage error before submitting." });
        }
        return;
      }
      setStructureConfirmation([]);
      setFieldErrors({});
      setMessage({ tone: "success", text: "These fields are now a new draft. No task has been created from it." });
      announce("Retained fields are now a new draft. Review them before adding a task.");
    } catch (error) {
      setMessage({ tone: "danger", text: error instanceof Error ? error.message : "The retained fields could not be saved as a new draft." });
    } finally {
      setSubmitting(false);
    }
  };

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
    if (readOnly || submitting || value.title.trim().length === 0 || (value.proposal_ref !== undefined && proposalReadOnly)) return;
    if (resolution || proposalNeedsReview) return;
    if (requiresDetailedReview) {
      setMessage({ tone: "warning", text: "This proposal includes additional task settings. Review and create it in the proposal details below." });
      return;
    }
    if (proposalOutdated || value.proposal_pending !== undefined || proposalChanged) {
      setMessage({ tone: "warning", text: proposalOutdated ? "The saved proposal changed. Review its current revision before creating a task; your Quick Add edits are preserved." : value.proposal_pending ? "Retry the pending proposal save before creating a task. Your draft is preserved." : "Save your proposal changes before creating the task, so the reviewed proposal matches these fields." });
      return;
    }
    const requestedStructures = newTaskStructures(value, input.options);
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
        value.proposal_ref ? TASK_INTENTS.proposalAccept : TASK_INTENTS.create,
        value.proposal_ref ? { thread_id: value.proposal_ref.threadId, expected_proposal_event_id: value.proposal_ref.proposalEventId } : taskDraftFields(value),
        clientMutationId,
      );
      if (value.proposal_ref && result.status === "accepted") {
        const proposal = (result.value as unknown as { proposal?: TaskProposal })?.proposal;
        if (proposal?.status !== "realized") {
          setMessage({ tone: "warning", text: "The proposal is still being resolved. Open it to check progress; do not create another task." });
          return;
        }
      }
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

  const saveProposal = async () => {
    if (proposalReadOnly || submitting || !value.title.trim() || value.batch_lines.length > 0) return;
    if (resolution || (proposalNeedsReview && value.proposal_pending === undefined)) return;
    if (requiresDetailedReview && value.proposal_pending === undefined) {
      setMessage({ tone: "warning", text: "This proposal includes additional task settings. Edit and create it in the full proposal review so those settings are preserved." });
      return;
    }
    setSubmitting(true);
    setMessage(null);
    try {
      await draft.flush();
      let pending = value.proposal_pending;
      if (!pending) {
        pending = {
          clientMutationId: createCorrelationId(value.proposal_ref ? "task-proposal-revise" : "task-proposal"),
          parameters: taskProposalParameters(value), draftFingerprint: taskDraftFingerprint(value),
          origin: value.proposal_ref ? {} : { kind: "task_quick_add", id: `${presentation.viewId}:${presentation.instanceId}`, label: "Tasks Quick Add", revision: draft.revision, sha256: await taskDraftSha256(value) },
          ...(value.proposal_ref ? { revisionOf: { threadId: value.proposal_ref.threadId, proposalEventId: value.proposal_ref.proposalEventId } } : {}),
        };
        draft.setValue((current) => ({ ...current, proposal_pending: pending }));
        await draft.flush();
      }
      const result = await dispatch(
        pending.revisionOf ? TASK_INTENTS.proposalRevise : TASK_INTENTS.proposalCreate,
        pending.revisionOf ? {
          thread_id: pending.revisionOf.threadId,
          expected_proposal_event_id: pending.revisionOf.proposalEventId,
          parameters: pending.parameters,
        } : { action: { name: "task_create", parameters: pending.parameters }, origin: pending.origin },
        pending.clientMutationId,
      );
      const proposal = (result.value as unknown as { proposal?: TaskProposal } | undefined)?.proposal;
      if (result.status !== "accepted" || !proposal) {
        setMessage({ tone: result.status === "conflict" ? "warning" : "danger", text: result.message ?? "The proposal could not be saved. Your draft is preserved." });
        setFieldErrors(result.fieldErrors ?? {});
        if (result.status === "conflict" && pending.revisionOf) {
          await dispatch(TASK_INTENTS.locationChange, { patch: { proposal: pending.revisionOf.threadId, task: null }, replace: true }, createCorrelationId("proposal-refresh"));
        }
        return;
      }
      // Replaying a confirmed mutation returns the current Thread projection,
      // which another tab may already have revised. Never bind that new event
      // fence to the old submitted fields and thereby approve unseen changes.
      const fingerprint = taskDraftFingerprint(draftFromTaskProposal(proposal));
      draft.setValue((current) => {
        const { proposal_pending: _pending, ...fields } = current;
        return { ...fields, proposal_ref: { threadId: proposal.thread_id, proposalEventId: proposal.proposal_event_id, draftFingerprint: fingerprint, requiresDetailedReview: additionalTaskProposalParameters(proposal).length > 0, ...(taskProposalResolution(proposal) ? { resolution: taskProposalResolution(proposal) } : {}) } };
      });
      await draft.flush();
      const text = proposal.status === "ready"
        ? "Proposal saved. No task has been created. Review it below, or share its link."
        : "Proposal retrieved. Review its current state below. Your Quick Add draft is preserved.";
      setMessage({ tone: "success", text });
      announce(text);
      await dispatch(TASK_INTENTS.locationChange, { patch: { proposal: proposal.thread_id, task: null } }, createCorrelationId("proposal-open"));
    } catch (error) {
      const text = error instanceof Error ? error.message : "The proposal save could not be confirmed. Retry safely with this draft.";
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
    if (value.proposal_ref || value.proposal_pending) {
      setMessage({ tone: "warning", text: "This draft is attached to a proposal. Review it or clear this draft before starting a batch." });
      return;
    }
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
      {message ? <InlineAlert tone={message.tone}>{message.text}{message.taskId ? <> <a href={`/app/tasks?task=${encodeURIComponent(message.taskId)}`}>Open existing task</a></> : null}</InlineAlert> : null}
      {structureConfirmation.length > 0 ? (
        <InlineAlert tone="warning">
          <span>
            This will create {structureConfirmation.join(" and ")}. The task is not saved yet.
          </span>{" "}
          <Button
            size="small"
            disabled={readOnly || submitting || !!resolution || proposalNeedsReview || (value.proposal_ref !== undefined && proposalReadOnly)}
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
                    ? presentation.interactionMode === "preview"
                      ? `${value.batch_lines.length} local rows · validation and creation are paused in Preview.`
                      : "Server preview unavailable. Cancel and paste again to retry."
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
            {serverPreview === null && presentation.interactionMode === "preview"
              ? value.batch_lines.map((title, index) => <li key={`local:${index}`}>
                <span>{title}</span><span className="wb-task-badge">Preview only</span>
              </li>)
              : null}
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
                {...assistance.fieldProps(["title"])}
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
            <Button type="submit" variant="primary" disabled={readOnly || submitting || value.title.trim().length === 0 || requiresDetailedReview || !!resolution || proposalNeedsReview || (value.proposal_ref !== undefined && proposalReadOnly)}>
              {submitting ? "Saving…" : resolution?.status === "realized" ? "Task already created" : resolution?.status === "rejected" ? "Proposal dismissed" : value.proposal_ref ? "Create task from proposal" : "Add task"}
            </Button>
          </div>
          <p id="wb-task-create-title-help" className="wb-task-field-help">{resolution ? "This proposal is closed. Retained fields have not been submitted again." : value.proposal_ref ? "This draft is linked to a proposal. Creating the task accepts that proposal, without making a second copy." : "Press Enter to add to Inbox. Paste several lines to preview a batch."}</p>
          {fieldError("title", "description") ? <p id="wb-task-create-title-error" className="wb-task-field-error">{fieldError("title", "description")}</p> : null}

          <div className="wb-task-field__inline">
            <Button size="small" variant="ghost" onClick={() => setDetailsOpen((open) => !open)} aria-expanded={detailsOpen}>
              {detailsOpen ? "Hide details" : "Add details"}
            </Button>
            <AssistDraftButton assistance={assistance} />
            <Button size="small" disabled={proposalReadOnly || submitting || !!resolution || !value.title.trim() || (!value.proposal_pending && (proposalNeedsReview || requiresDetailedReview || (value.proposal_ref !== undefined && !proposalChanged)))} onClick={() => void saveProposal()}>
              {value.proposal_pending ? "Retry proposal save" : value.proposal_ref ? "Save proposal changes" : "Save proposal"}
            </Button>
            {value.proposal_ref && resolution?.status !== "realized" ? <a href={`/app/tasks?proposal=${encodeURIComponent(value.proposal_ref.threadId)}`}>Review saved proposal</a> : null}
          </div>
          {resolution ? <InlineAlert tone={resolution.status === "realized" ? "success" : "warning"}>
            {resolution.status === "realized" ? <>This proposal already created a task. Your retained fields are preserved. <a href={`/app/tasks?task=${encodeURIComponent(resolution.taskId)}`}>Open existing task</a></> : "This proposal was dismissed. Your source draft is preserved; no task was created."}
            <Button size="small" disabled={proposalReadOnly || submitting} onClick={() => void useRetainedFields()}>Use retained fields for a new draft</Button>
          </InlineAlert> : null}
          {!resolution && proposalNeedsReview && !proposalOutdated ? <InlineAlert tone="warning">Review the saved proposal before making another decision. Your fields and any exact pending retry are preserved.</InlineAlert> : null}
          {!resolution && proposalChanged ? <InlineAlert tone="warning">Your draft has changes that are not yet in the saved proposal.</InlineAlert> : null}
          {!resolution && requiresDetailedReview ? <InlineAlert tone="warning">This proposal includes additional task settings. Edit, review, and create it in the proposal details below so those settings are preserved.</InlineAlert> : null}
          {!resolution && proposalOutdated && selectedLinkedProposal ? <InlineAlert tone="warning">The saved proposal has a newer revision. Your Quick Add edits are preserved.
            <Button size="small" disabled={readOnly || submitting} onClick={() => {
              const current = draftFromTaskProposal(selectedLinkedProposal);
              draft.setValue({ ...current, proposal_ref: { threadId: selectedLinkedProposal.thread_id, proposalEventId: selectedLinkedProposal.proposal_event_id, draftFingerprint: taskDraftFingerprint(current), requiresDetailedReview: additionalTaskProposalParameters(selectedLinkedProposal).length > 0 } });
              setMessage(null); setFieldErrors({});
            }}>Discard Quick Add edits and load current proposal</Button>
          </InlineAlert> : null}

          {detailsOpen ? (
            <TaskDraftFields value={value} options={input.options} disabled={readOnly || submitting} idPrefix="wb-task-create" errors={fieldErrors} update={update} fieldProps={assistance.fieldProps} />
          ) : null}
        </>
      )}
    </form>
  );
}

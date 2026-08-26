import { useMemo, useState } from "react";
import type { IntentResult, JsonValue, WidgetIntent, WidgetPresentationContext } from "../../../dashboard/contributions/contracts";
import { useDashboardAnnouncer } from "../../../dashboard/accessibility/DashboardAnnouncer";
import { useWidgetDraft } from "../../../dashboard/drafts";
import { Button, InlineAlert } from "../../../ui";
import { createCorrelationId, createWidgetIntent } from "../../../widget-library/shared";
import { TASK_INTENTS, type TaskOptions, type TaskProposal, type TaskProposalSelection } from "../contracts";
import { TaskDraftFields } from "../composer/TaskDraftFields";
import { additionalTaskProposalParameters, draftFromTaskProposal, newTaskStructures, taskDraftFingerprint, taskProposalParameters, type TaskCreateDraft } from "../composer/taskDraft";

interface ProposalDraft extends TaskCreateDraft { readonly baseProposalEventId: number }

export interface TaskProposalDetailProps {
  readonly selection: TaskProposalSelection;
  readonly options: TaskOptions;
  readonly readOnly: boolean;
  readonly presentation: WidgetPresentationContext;
  readonly emit: (intent: WidgetIntent) => Promise<IntentResult>;
  readonly onClose: () => void;
}

export function TaskProposalDetail(props: TaskProposalDetailProps) {
  if (props.selection.kind === "unavailable") return <section className="wb-task-detail" aria-label="Task proposal unavailable">
    <div className="wb-task-detail__header"><h2>Task proposal unavailable</h2><Button size="small" onClick={props.onClose}>Close</Button></div>
    <InlineAlert tone="warning">{props.selection.message}</InlineAlert>
    <p className="wb-task-muted">No task was created by opening this link.</p>
  </section>;
  return <ProposalEditor {...props} proposal={props.selection.proposal} />;
}

function ProposalEditor({ proposal, options, readOnly: accessReadOnly, presentation, emit, onClose }: TaskProposalDetailProps & { readonly proposal: TaskProposal }) {
  const { announce } = useDashboardAnnouncer();
  const seed = useMemo<ProposalDraft>(() => ({ ...draftFromTaskProposal(proposal), baseProposalEventId: proposal.proposal_event_id }), [proposal]);
  const draft = useWidgetDraft("task-proposal-edit", seed, {
    isPristine: (value) => value.baseProposalEventId === proposal.proposal_event_id && taskDraftFingerprint(value) === taskDraftFingerprint(seed),
  });
  const [busy, setBusy] = useState(false);
  const [dismissConfirm, setDismissConfirm] = useState(false);
  const [structureConfirm, setStructureConfirm] = useState<readonly string[]>([]);
  const [notice, setNotice] = useState<{ tone: "warning" | "danger" | "success"; text: string } | null>(null);
  const [errors, setErrors] = useState<Readonly<Record<string, string>>>({});
  const readOnly = accessReadOnly || presentation.interactionMode === "arrange";
  const proposalReadOnly = readOnly || presentation.interactionMode !== "operate";
  const editable = proposal.status === "ready";
  const stale = draft.value.baseProposalEventId !== proposal.proposal_event_id;
  const changed = taskDraftFingerprint(draft.value) !== taskDraftFingerprint(seed);
  const disabled = readOnly || busy || !editable;
  const decisionDisabled = disabled || proposalReadOnly;
  const originLabel = typeof proposal.origin.label === "string" ? proposal.origin.label : "Captured suggestion";
  const additionalParameters = additionalTaskProposalParameters(proposal);

  const send = (type: string, payload: Record<string, JsonValue>) => {
    const id = createCorrelationId("task-proposal-decision");
    return emit(createWidgetIntent(presentation, type, payload, { intentId: id, clientMutationId: id }) as WidgetIntent);
  };
  const refresh = () => send(TASK_INTENTS.locationChange, { patch: { proposal: proposal.thread_id, task: null }, replace: true });
  const update = <Key extends keyof TaskCreateDraft>(key: Key, value: TaskCreateDraft[Key]) => {
    setStructureConfirm([]);
    draft.setValue((current) => ({ ...current, [key]: value }));
  };
  const act = async (type: string, structureApproved = false) => {
    if (proposalReadOnly || busy || stale) return;
    if (type === TASK_INTENTS.proposalAccept && changed) {
      setNotice({ tone: "warning", text: "Save your changes before creating the task. Acceptance always uses the exact reviewed proposal." });
      return;
    }
    if (type === TASK_INTENTS.proposalAccept && !structureApproved) {
      const requested = newTaskStructures(draft.value, options);
      if (requested.length > 0) { setStructureConfirm(requested); return; }
    }
    setBusy(true);
    setNotice(null);
    const revision = draft.revision;
    try {
      await draft.flush();
      const result = await send(type, {
        thread_id: proposal.thread_id,
        expected_proposal_event_id: draft.value.baseProposalEventId,
        ...(type === TASK_INTENTS.proposalRevise ? { parameters: { ...proposal.parameters, ...taskProposalParameters(draft.value) } as Record<string, JsonValue> } : {}),
      });
      const next = (result.value as unknown as { proposal?: TaskProposal } | undefined)?.proposal;
      const text = result.message ?? (result.status === "accepted" ? "Proposal updated." : "The proposal could not be updated. Your edits are preserved.");
      setNotice({ tone: result.status === "accepted" ? "success" : result.status === "conflict" ? "warning" : "danger", text });
      setErrors(result.fieldErrors ?? {});
      announce(text, result.status === "accepted" ? "polite" : "assertive");
      if (result.status === "accepted" && next) {
        if (next.status === "realized" || next.status === "rejected") await draft.clear({ ifRevision: revision });
        else draft.setValue((current) => ({ ...current, baseProposalEventId: next.proposal_event_id }));
        setDismissConfirm(false);
        setStructureConfirm([]);
      } else if (result.status === "conflict") await refresh();
    } catch (error) {
      const text = error instanceof Error ? error.message : "The proposal request could not be confirmed. You can safely retry.";
      setNotice({ tone: "danger", text });
      announce(text, "assertive");
    } finally { setBusy(false); }
  };

  if (!draft.ready) return <p aria-busy="true">Restoring proposal edits…</p>;
  return <section className="wb-task-detail wb-task-proposal" aria-label="Task proposal">
    <div className="wb-task-detail__header">
      <div><p className="wb-task-detail__kicker">Task proposal · {originLabel}</p><h2>Review before creating</h2></div>
      <Button size="small" onClick={onClose}>Close</Button>
    </div>
    <p className="wb-task-muted">{proposal.status === "rejected" ? "This proposal was dismissed. No task was created; its original capture is preserved." : "This is a proposal, not a task. Only Create task below adds it to your task list."}</p>
    <a href={proposal.href}>Link to this proposal</a>
    {draft.error ? <InlineAlert tone="danger">{draft.error} Your edits remain here.</InlineAlert> : null}
    {notice ? <InlineAlert tone={notice.tone}>{notice.text}</InlineAlert> : null}
    {stale ? <InlineAlert tone="warning">This proposal changed elsewhere. Your local edits are preserved, but cannot overwrite the newer version.
      <Button size="small" disabled={busy || readOnly} onClick={() => { draft.setValue(seed); setNotice(null); }}>Discard local edits and load current proposal</Button>
    </InlineAlert> : null}
    {proposal.status === "executing" ? <InlineAlert tone="warning">Task creation is in progress. Retrying this proposal will resolve to the same task.<Button size="small" onClick={() => void refresh()}>Refresh proposal</Button><Button size="small" disabled={proposalReadOnly || busy || stale} onClick={() => void act(TASK_INTENTS.proposalAccept)}>Retry creating task</Button></InlineAlert> : null}
    {proposal.status === "needs_attention" ? <InlineAlert tone="warning">Creation was not fully confirmed. Retry this same proposal safely; do not create a separate task.<Button size="small" disabled={proposalReadOnly || busy || stale} onClick={() => void act(TASK_INTENTS.proposalAccept)}>Retry creating task</Button></InlineAlert> : null}
    {proposal.status === "unavailable" ? <InlineAlert tone="warning">This proposal is no longer available for task creation.</InlineAlert> : null}
    <form onSubmit={(event) => { event.preventDefault(); void act(TASK_INTENTS.proposalRevise); }}>
      <label className="wb-task-field"><span>Proposed task title</span><input value={draft.value.title} disabled={disabled} onChange={(event) => update("title", event.target.value)} aria-invalid={errors.task_text || errors.title ? "true" : undefined} /></label>
      <TaskDraftFields value={draft.value} options={options} disabled={disabled} idPrefix="wb-task-proposal" errors={errors} update={update} />
      {additionalParameters.length > 0 ? <section aria-label="Additional proposed task settings">
        <h3>Additional proposed settings</h3>
        <p className="wb-task-muted">These settings are also part of this proposal. Saving the fields above keeps them; creating the task accepts them too.</p>
        <dl>{additionalParameters.map(([key, setting]) => <div key={key}>
          <dt>{key.replace(/_/g, " ").replace(/^./, (letter) => letter.toUpperCase())}</dt>
          <dd>{setting === null ? "Not set" : Array.isArray(setting) ? setting.map((item) => typeof item === "object" ? JSON.stringify(item) : String(item)).join(", ") || "None" : typeof setting === "boolean" ? setting ? "Yes" : "No" : typeof setting === "object" ? JSON.stringify(setting) : String(setting)}</dd>
        </div>)}</dl>
      </section> : null}
      {editable ? <div className="wb-task-actions">
        <Button type="submit" disabled={decisionDisabled || stale || !changed || !draft.value.title.trim()}>Save proposal changes</Button>
        <Button variant="primary" disabled={decisionDisabled || stale || changed || !draft.value.title.trim()} onClick={() => void act(TASK_INTENTS.proposalAccept)}>{busy ? "Saving…" : "Create task"}</Button>
        <Button variant="ghost" disabled={decisionDisabled || stale} onClick={() => setDismissConfirm(true)}>Dismiss proposal</Button>
      </div> : null}
    </form>
    {structureConfirm.length > 0 ? <InlineAlert tone="warning">This will create {structureConfirm.join(" and ")}. Review the structure before proceeding. <Button size="small" disabled={proposalReadOnly || busy || stale || changed} onClick={() => void act(TASK_INTENTS.proposalAccept, true)}>Confirm structure and create task</Button></InlineAlert> : null}
    {dismissConfirm ? <InlineAlert tone="warning">Dismiss this proposal? Its original capture will be kept. <Button size="small" disabled={decisionDisabled || stale} onClick={() => void act(TASK_INTENTS.proposalReject)}>Confirm dismissal</Button> <Button size="small" onClick={() => setDismissConfirm(false)}>Keep proposal</Button></InlineAlert> : null}
  </section>;
}

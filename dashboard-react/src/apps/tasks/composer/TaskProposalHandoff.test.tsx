import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DashboardAnnouncer } from "../../../dashboard/accessibility/DashboardAnnouncer";
import type { IntentResult, JsonValue, WidgetIntent, WidgetPresentationContext } from "../../../dashboard/contributions/contracts";
import { InMemoryWidgetDraftRepository, WidgetDraftRuntimeProvider, WidgetDraftScopeProvider, type WidgetDraftIdentity } from "../../../dashboard/drafts";
import { InteractionSurfaceProvider } from "../../../dashboard/interactions";
import { TASKS_INSTANCE_IDS, TASKS_VIEW_ID } from "../bindings";
import { TASKS_APP_CONTRIBUTION } from "../contribution";
import { TASK_INTENTS, type TaskProposal, type TaskQuickAddInput } from "../contracts";
import { TaskProposalDetail } from "../workspace/TaskProposalDetail";
import TaskComposer from "./TaskComposer";
import { EMPTY_TASK_CREATE_DRAFT, draftFromTaskProposal, taskDraftFingerprint, type TaskCreateDraft } from "./taskDraft";

const definition = TASKS_APP_CONTRIBUTION.widgetDefinitions[0]!;
const identity: WidgetDraftIdentity = {
  profileId: "handoff-profile", workspaceId: "handoff-workspace",
  appId: definition.publisherAppId, viewId: TASKS_VIEW_ID,
  instanceId: TASKS_INSTANCE_IDS.quickAdd, widgetTypeId: definition.typeId,
  draftName: "task-create", scopeKey: "view",
};
const presentation: WidgetPresentationContext = {
  instanceId: TASKS_INSTANCE_IDS.quickAdd, viewId: TASKS_VIEW_ID,
  width: 800, height: 320, sizeMode: "standard", interactionMode: "operate", editing: false,
  theme: {
    contractVersion: 1, preference: { scheme: "light", skinId: "wb.default" }, resolvedScheme: "light",
    skin: { id: "wb.default", version: 2, publisherAppId: "wb.core" },
    accessibility: { forcedColors: false, reducedMotion: false, reducedTransparency: false },
  },
  getCanvasTheme: () => ({ surfaceCanvas: "", surfaceRaised: "", textPrimary: "", textSecondary: "", borderDefault: "", focusRing: "", dataSeries: [] }),
};
const input: TaskQuickAddInput = {
  instanceId: TASKS_INSTANCE_IDS.quickAdd, revision: 7, access: { mode: "read_write" },
  options: { projects: [], namespaces: [], contracts: [], contexts: [] },
};
const ready: TaskProposal = {
  thread_id: "th-linked", proposal_event_id: 7, status: "ready",
  parameters: { task_text: "Reviewed task", summary: "Reviewed context" },
  origin: { kind: "task_quick_add", label: "Quick Add" }, realization: null,
  href: "/app/tasks?proposal=th-linked",
};
const realized: TaskProposal = {
  ...ready, status: "realized",
  realization: { task_id: "t-created", task_revision: 1, receipt_id: "receipt-1", href: "/app/tasks?task=t-created" },
};
const fields = draftFromTaskProposal(ready);
const linked: TaskCreateDraft = {
  ...fields,
  proposal_ref: { threadId: ready.thread_id, proposalEventId: ready.proposal_event_id, draftFingerprint: taskDraftFingerprint(fields) },
};

async function seed(value: TaskCreateDraft) {
  const repository = new InMemoryWidgetDraftRepository();
  await repository.save({ ...identity, draftSchema: definition.drafts![0]!.schema, value: value as unknown as JsonValue });
  return repository;
}
const stored = async (repository: InMemoryWidgetDraftRepository) => (await repository.load(identity))?.value as unknown as TaskCreateDraft | undefined;

function element(
  repository: InMemoryWidgetDraftRepository,
  emit: (intent: WidgetIntent) => Promise<IntentResult>,
  widgetInput: TaskQuickAddInput = input,
  options: { mode?: WidgetPresentationContext["interactionMode"]; review?: TaskProposal } = {},
) {
  const context = { ...presentation, interactionMode: options.mode ?? "operate" };
  const reviewContext = { ...context, instanceId: TASKS_INSTANCE_IDS.workspace };
  return <DashboardAnnouncer><InteractionSurfaceProvider>
    <WidgetDraftRuntimeProvider repository={repository} profileId={identity.profileId} workspaceId={identity.workspaceId}>
      <WidgetDraftScopeProvider definition={definition} viewId={TASKS_VIEW_ID} instanceId={TASKS_INSTANCE_IDS.quickAdd} input={widgetInput} persistenceMode={options.mode === "preview" ? "ephemeral" : "normal"}>
        <TaskComposer input={widgetInput} presentation={context} emit={emit} />
      </WidgetDraftScopeProvider>
      {options.review ? <WidgetDraftScopeProvider definition={TASKS_APP_CONTRIBUTION.widgetDefinitions[1]!} viewId={TASKS_VIEW_ID} instanceId={TASKS_INSTANCE_IDS.workspace} input={{ selectedProposal: { kind: "loaded", proposal: options.review } }}>
        <TaskProposalDetail selection={{ kind: "loaded", proposal: options.review }} options={input.options} readOnly={false} presentation={reviewContext} emit={emit} onClose={() => undefined} />
      </WidgetDraftScopeProvider> : null}
    </WidgetDraftRuntimeProvider>
  </InteractionSurfaceProvider></DashboardAnnouncer>;
}

describe("Quick Add proposal completion handoff", () => {
  it("supersedes the actual Save success notice when full review creates the task", async () => {
    const repository = await seed(EMPTY_TASK_CREATE_DRAFT);
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => {
      if (intent.intent_type === TASK_INTENTS.locationChange) {
        view.rerender(element(repository, emit, { ...input, observedProposal: ready, selectedProposal: ready }, { review: ready }));
      }
      if (intent.intent_type === TASK_INTENTS.proposalAccept) {
        view.rerender(element(repository, emit, { ...input, observedProposal: realized, selectedProposal: null }));
      }
      return { intent_id: intent.intent_id, status: "accepted", value: { proposal: intent.intent_type === TASK_INTENTS.proposalAccept ? realized : ready } };
    });
    const view = render(element(repository, emit));
    const title = await screen.findByRole("textbox", { name: "New task" });
    await user.type(title, "Reviewed task");
    await user.click(screen.getByRole("button", { name: "Save proposal" }));
    const saveNotice = "Proposal saved. No task has been created. Review it below, or share its link.";
    await screen.findAllByText(saveNotice);
    await screen.findByRole("textbox", { name: "Proposed task title" });
    await user.type(title, " with later edits");
    await user.click(screen.getByRole("button", { name: "Create task" }));
    await waitFor(async () => expect((await stored(repository))?.proposal_ref?.resolution?.status).toBe("realized"));
    expect(title).toHaveValue("Reviewed task with later edits");
    expect(screen.getByRole("button", { name: "Task already created" })).toBeDisabled();
    expect(screen.queryByText(saveNotice)).not.toBeInTheDocument();
    expect(emit.mock.calls.filter(([intent]) => intent.intent_type === TASK_INTENTS.proposalAccept)).toHaveLength(1);
    expect(emit.mock.calls.some(([intent]) => intent.intent_type === TASK_INTENTS.create)).toBe(false);
  });

  it("preserves a real conflicting-save warning when terminal evidence arrives", async () => {
    const repository = await seed({ ...linked, title: "A later local revision" });
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => intent.intent_type === TASK_INTENTS.proposalRevise
      ? { intent_id: intent.intent_id, status: "conflict", message: "Review the competing revision. Your local edits are preserved." }
      : { intent_id: intent.intent_id, status: "accepted" });
    const view = render(element(repository, emit, { ...input, observedProposal: ready }));
    await user.click(await screen.findByRole("button", { name: "Save proposal changes" }));
    const conflict = "Review the competing revision. Your local edits are preserved.";
    await screen.findByText(conflict);
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry proposal save" })).toBeEnabled());
    view.rerender(element(repository, emit, { ...input, observedProposal: realized }));
    await waitFor(async () => expect((await stored(repository))?.proposal_ref?.resolution?.status).toBe("realized"));
    expect(screen.getByText(conflict)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "New task" })).toHaveValue("A later local revision");
    expect(screen.getByRole("button", { name: "Retry proposal save" })).toBeDisabled();
  });

  it.each([false, true])("reflects a real full-review acceptance while preserving later edits: %s", async (edited) => {
    const repository = await seed(linked);
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => {
      expect(intent.intent_type).toBe(TASK_INTENTS.proposalAccept);
      // The provider's canonical task redirect drops selectedProposal but keeps
      // its one validated observedProposal projection for the other widget.
      view.rerender(element(repository, emit, { ...input, selectedProposal: null, observedProposal: realized }));
      return { intent_id: intent.intent_id, status: "accepted", value: { proposal: realized } as unknown as JsonValue };
    });
    const view = render(element(repository, emit, { ...input, selectedProposal: ready, observedProposal: ready }, { review: ready }));
    const title = await screen.findByRole("textbox", { name: "New task" });
    await screen.findByRole("textbox", { name: "Proposed task title" });
    if (edited) await user.type(title, " with later edits");
    await user.click(screen.getByRole("button", { name: "Create task" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    expect(emit.mock.calls[0]?.[0]).toMatchObject({ instance_id: TASKS_INSTANCE_IDS.workspace, intent_type: TASK_INTENTS.proposalAccept, payload: { thread_id: ready.thread_id, expected_proposal_event_id: 7 } });
    if (edited) {
      expect(title).toHaveValue("Reviewed task with later edits");
      expect(screen.getByRole("button", { name: "Task already created" })).toBeDisabled();
      await waitFor(async () => expect((await stored(repository))?.proposal_ref?.resolution).toEqual({ status: "realized", proposalEventId: 7, taskId: "t-created" }));
    } else {
      await waitFor(() => expect(title).toHaveValue(""));
      await waitFor(async () => expect(await stored(repository)).toBeUndefined());
      expect(screen.getByRole("button", { name: "Add task" })).toBeDisabled();
      expect(title).not.toHaveFocus();
    }
    expect(screen.getByRole("link", { name: "Open existing task" })).toHaveAttribute("href", "/app/tasks?task=t-created");
    expect(screen.queryByRole("link", { name: "Review saved proposal" })).not.toBeInTheDocument();
  });

  it("retains terminal evidence across reload and starts fresh only by an atomic human reset", async () => {
    const repository = await seed({ ...linked, title: "My later task" });
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "accepted" }));
    const first = render(element(repository, emit, { ...input, observedProposal: realized }));
    expect(await screen.findByRole("textbox", { name: "New task" })).toHaveValue("My later task");
    await waitFor(async () => expect((await stored(repository))?.proposal_ref?.resolution).toEqual({ status: "realized", proposalEventId: 7, taskId: "t-created" }));
    first.unmount();
    render(element(repository, emit));
    expect(await screen.findByRole("textbox", { name: "New task" })).toHaveValue("My later task");
    expect(screen.getByRole("button", { name: "Task already created" })).toBeDisabled();
    expect(screen.getByRole("link", { name: "Open existing task" })).toHaveAttribute("href", "/app/tasks?task=t-created");
    const deleted = vi.spyOn(repository, "delete");
    const saved = vi.spyOn(repository, "save");
    await user.click(screen.getByRole("button", { name: "Use retained fields for a new draft" }));
    await screen.findByText("These fields are now a new draft. No task has been created from it.");
    expect(screen.getByRole("textbox", { name: "New task" })).toHaveValue("My later task");
    expect(await stored(repository)).toEqual({ ...fields, title: "My later task" });
    expect(saved).toHaveBeenCalledTimes(1);
    expect(deleted).not.toHaveBeenCalled();
    expect(emit).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Add task" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    expect(emit.mock.calls[0]?.[0]).toMatchObject({ intent_type: TASK_INTENTS.create, payload: { title: "My later task" } });
  });

  it("preserves an unchanged dismissed source until the human chooses a new draft", async () => {
    const repository = await seed(linked);
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "accepted" }));
    const deleted = vi.spyOn(repository, "delete");
    render(element(repository, emit, { ...input, observedProposal: { ...ready, status: "rejected" } }));
    expect(await screen.findByRole("textbox", { name: "New task" })).toHaveValue("Reviewed task");
    expect(screen.getByText(/This proposal was dismissed. Your source draft is preserved/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Proposal dismissed" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save proposal changes" })).toBeDisabled();
    expect(screen.getByRole("link", { name: "Review saved proposal" })).toHaveAttribute("href", ready.href);
    expect(screen.queryByRole("link", { name: "Open existing task" })).not.toBeInTheDocument();
    await waitFor(async () => expect((await stored(repository))?.proposal_ref?.resolution).toEqual({ status: "rejected", proposalEventId: 7 }));
    await user.click(screen.getByRole("button", { name: "Use retained fields for a new draft" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Add task" })).toBeEnabled());
    expect(await stored(repository)).toEqual(fields);
    expect(deleted).not.toHaveBeenCalled();
    expect(emit).not.toHaveBeenCalled();
  });

  it("does not clear from persisted terminal hints alone, even when fields match again", async () => {
    const value: TaskCreateDraft = { ...linked, proposal_ref: { ...linked.proposal_ref!, resolution: { status: "realized", proposalEventId: 7, taskId: "t-created" } } };
    const repository = await seed(value);
    const deleted = vi.spyOn(repository, "delete");
    const emit = vi.fn();
    const view = render(element(repository, emit));
    expect(await screen.findByRole("textbox", { name: "New task" })).toHaveValue("Reviewed task");
    expect(screen.getByRole("button", { name: "Task already created" })).toBeDisabled();
    view.rerender(element(repository, emit, { ...input, observedProposal: realized }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole("textbox", { name: "New task" })).toHaveValue("Reviewed task");
    expect(await stored(repository)).toEqual(value);
    expect(deleted).not.toHaveBeenCalled();
    expect(emit).not.toHaveBeenCalled();
  });

  it("requires a current reviewed projection after a bound draft reload", async () => {
    const repository = await seed(linked);
    const user = userEvent.setup();
    const emit = vi.fn();
    const view = render(element(repository, emit));
    await screen.findByRole("textbox", { name: "New task" });
    expect(screen.getByText(/Review the saved proposal before making another decision/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create task from proposal" })).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "New task" }), " later");
    expect(screen.getByRole("button", { name: "Save proposal changes" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Create task from proposal" }));
    expect(emit).not.toHaveBeenCalled();
    view.rerender(element(repository, emit, { ...input, observedProposal: ready }));
    expect(screen.queryByText(/Review the saved proposal before making another decision/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save proposal changes" })).toBeEnabled();
  });

  it("preserves exact pending revision replay on reload without inventing a new request", async () => {
    const pending = { clientMutationId: "lost-revision-key", parameters: { task_text: "Reviewed retry fields" }, origin: {}, draftFingerprint: "retry-fingerprint", revisionOf: { threadId: ready.thread_id, proposalEventId: 7 } };
    const repository = await seed({ ...linked, title: "Later unsent fields", proposal_pending: pending });
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "unavailable", message: "Still uncertain. Retry this same save." }));
    render(element(repository, emit));
    expect(await screen.findByRole("button", { name: "Retry proposal save" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Create task from proposal" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Retry proposal save" }));
    await screen.findByText("Still uncertain. Retry this same save.");
    await user.click(screen.getByRole("button", { name: "Retry proposal save" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(2));
    expect(emit.mock.calls[0]?.[0]).toMatchObject({ intent_type: TASK_INTENTS.proposalRevise, client_mutation_id: pending.clientMutationId, payload: { thread_id: ready.thread_id, expected_proposal_event_id: 7, parameters: pending.parameters } });
    expect(emit.mock.calls[1]?.[0]).toEqual(emit.mock.calls[0]?.[0]);
    expect((await stored(repository))?.proposal_pending).toEqual(pending);
    expect(screen.getByRole("textbox", { name: "New task" })).toHaveValue("Later unsent fields");
  });

  it("retains uncertain pending fields but disables old replay after terminal evidence", async () => {
    const pending = { clientMutationId: "lost-revision-key", parameters: { task_text: "Pending fields" }, origin: {}, draftFingerprint: "pending-fingerprint", revisionOf: { threadId: ready.thread_id, proposalEventId: 7 } };
    const repository = await seed({ ...linked, proposal_pending: pending });
    const emit = vi.fn();
    render(element(repository, emit, { ...input, observedProposal: realized }));
    expect(await screen.findByRole("textbox", { name: "New task" })).toHaveValue("Reviewed task");
    expect(screen.getByRole("button", { name: "Retry proposal save" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Task already created" })).toBeDisabled();
    await waitFor(async () => expect((await stored(repository))?.proposal_ref?.resolution?.status).toBe("realized"));
    expect((await stored(repository))?.proposal_pending).toEqual(pending);
    expect(emit).not.toHaveBeenCalled();
  });

  it.each([
    { label: "different Thread", proposal: { ...realized, thread_id: "th-other" }, terminal: false },
    { label: "older event", proposal: { ...realized, proposal_event_id: 6 }, terminal: false },
    { label: "newer event", proposal: { ...realized, proposal_event_id: 9 }, terminal: true },
    { label: "different canonical fields", proposal: { ...realized, parameters: { task_text: "Unseen canonical title" } }, terminal: true },
  ])("preserves the source for $label", async ({ proposal, terminal }) => {
    const repository = await seed(linked);
    const emit = vi.fn();
    const deleted = vi.spyOn(repository, "delete");
    render(element(repository, emit, { ...input, observedProposal: proposal }));
    expect(await screen.findByRole("textbox", { name: "New task" })).toHaveValue("Reviewed task");
    if (terminal) await waitFor(async () => expect((await stored(repository))?.proposal_ref?.resolution?.status).toBe("realized"));
    else expect((await stored(repository))?.proposal_ref?.resolution).toBeUndefined();
    expect(deleted).not.toHaveBeenCalled();
    expect(emit).not.toHaveBeenCalled();
  });

  it.each([
    { mode: "preview" as const, access: input.access },
    { mode: "arrange" as const, access: input.access },
    { mode: "operate" as const, access: { mode: "read_only" as const, reason: "Read only" } },
  ])("does not reconcile the persistent source outside editable Operate %#", async ({ mode, access }) => {
    const repository = await seed(linked);
    const saved = vi.spyOn(repository, "save");
    const deleted = vi.spyOn(repository, "delete");
    const emit = vi.fn();
    render(element(repository, emit, { ...input, access, observedProposal: realized }, { mode }));
    expect(await screen.findByRole("textbox", { name: "New task" })).toHaveValue("Reviewed task");
    expect(screen.getByRole("button", { name: "Use retained fields for a new draft" })).toBeDisabled();
    await act(async () => { await Promise.resolve(); });
    expect(await stored(repository)).toEqual(linked);
    expect(saved).not.toHaveBeenCalled();
    expect(deleted).not.toHaveBeenCalled();
    expect(emit).not.toHaveBeenCalled();
  });

  it("never turns malformed retained navigation evidence into a link or decision", async () => {
    const value = { ...linked, proposal_ref: { ...linked.proposal_ref!, resolution: { status: "realized", proposalEventId: 7, taskId: "javascript:alert(1)", href: "https://untrusted.invalid" } } } as TaskCreateDraft;
    const repository = await seed(value);
    const emit = vi.fn();
    render(element(repository, emit));
    expect(await screen.findByRole("textbox", { name: "New task" })).toHaveValue("Reviewed task");
    expect(screen.queryByRole("link", { name: "Open existing task" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create task from proposal" })).toBeDisabled();
    expect(emit).not.toHaveBeenCalled();
  });
});

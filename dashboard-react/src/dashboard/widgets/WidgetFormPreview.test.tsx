import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentType } from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { JOB_AUTHORING_WIDGET, JOBS_INSTANCE_ID, JOBS_VIEW_ID, JOBS_WIDGET_MODULE } from "../../apps/jobs/contribution";
import { EMPTY_JOB_DRAFT, JOB_INTENTS, type JobAuthoringInput } from "../../apps/jobs/contracts";
import { TASKS_INSTANCE_IDS, TASKS_VIEW_ID } from "../../apps/tasks/bindings";
import { EMPTY_TASK_CREATE_DRAFT, taskDraftFingerprint, type TaskCreateDraft } from "../../apps/tasks/composer/taskDraft";
import { TASKS_APP_CONTRIBUTION } from "../../apps/tasks/contribution";
import { TASK_INTENTS, type TaskProposal } from "../../apps/tasks/contracts";
import { tasksWidgetLabInputs } from "../../apps/tasks/fixtures/widgetLab";
import { TASKS_WIDGET_MODULES } from "../../apps/tasks/widgetModule";
import { ThemeProvider } from "../../theme/ThemeProvider";
import { DashboardAnnouncer } from "../accessibility/DashboardAnnouncer";
import { AssistedDraftRuntimeProvider } from "../assistance";
import type { IntentResult, JsonValue, ViewId, WidgetDefinition, WidgetInstanceId, WidgetIntent, WidgetModule, WidgetPresentationContext, WidgetRendererProps } from "../contributions/contracts";
import { ForkedWidgetDraftRepository, InMemoryWidgetDraftRepository, WidgetDraftRuntimeProvider, type WidgetDraftIdentity } from "../drafts";
import { InteractionSurfaceProvider } from "../interactions";
import { WidgetHost } from "./WidgetHost";

const READY = { timeout: 10_000 };
type Mode = WidgetPresentationContext["interactionMode"];
interface FormFixture {
  readonly definition: WidgetDefinition;
  readonly module: WidgetModule;
  readonly viewId: ViewId;
  readonly instanceId: WidgetInstanceId;
  readonly input: object;
  readonly draftName: string;
  readonly scopeKey: string;
}
const proposal: TaskProposal = {
  thread_id: "th-preview01", proposal_event_id: 7, status: "ready",
  parameters: { task_text: "Current proposal", state: "inbox" },
  origin: { kind: "journal", id: "capture-preview", label: "Journal" },
  realization: null, href: "/app/tasks?proposal=th-preview01",
};
const quickAdd: FormFixture = {
  definition: TASKS_APP_CONTRIBUTION.widgetDefinitions[0]!, module: TASKS_WIDGET_MODULES[0]!,
  viewId: TASKS_VIEW_ID, instanceId: TASKS_INSTANCE_IDS.quickAdd,
  input: tasksWidgetLabInputs().quickAdd, draftName: "task-create", scopeKey: "view",
};
const jobs: FormFixture = {
  definition: JOB_AUTHORING_WIDGET, module: JOBS_WIDGET_MODULE,
  viewId: JOBS_VIEW_ID, instanceId: JOBS_INSTANCE_ID,
  input: { access: { mode: "read_write" }, timeZone: "America/New_York", capabilities: [], workflows: [] } satisfies JobAuthoringInput,
  draftName: "job-create", scopeKey: "view",
};
const proposalForm = (status: TaskProposal["status"] = "ready"): FormFixture => {
  const input = tasksWidgetLabInputs().workspace;
  return {
    definition: TASKS_APP_CONTRIBUTION.widgetDefinitions[1]!, module: TASKS_WIDGET_MODULES[1]!,
    viewId: TASKS_VIEW_ID, instanceId: TASKS_INSTANCE_IDS.workspace,
    input: { ...input, selectedProposal: { kind: "loaded", proposal: { ...proposal, status } }, query: { ...input.query, proposal: proposal.thread_id } },
    draftName: "task-proposal-edit", scopeKey: proposal.thread_id,
  };
};
const identityFor = (fixture: FormFixture): WidgetDraftIdentity => ({
  profileId: "preview-test", workspaceId: "preview-workspace", appId: fixture.definition.publisherAppId,
  viewId: fixture.viewId, instanceId: fixture.instanceId, widgetTypeId: fixture.definition.typeId,
  draftName: fixture.draftName, scopeKey: fixture.scopeKey,
});
const seedDraft = (repository: InMemoryWidgetDraftRepository, fixture: FormFixture, value: object) => repository.save({
  ...identityFor(fixture), draftSchema: fixture.definition.drafts!.find((draft) => draft.draftName === fixture.draftName)!.schema,
  value: value as unknown as JsonValue,
});

function renderForm(fixture: FormFixture, repository = new InMemoryWidgetDraftRepository(), mode: Mode = "preview", readOnly = false) {
  const provider = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "unavailable" }));
  const assistantFetch = vi.fn<typeof fetch>().mockRejectedValue(new Error("Unexpected assistance request"));
  const rendererIntents: WidgetIntent[] = [];
  // Observe the renderer boundary as well as the real host's outward boundary.
  // Proposal guards must stop before dispatch, not merely rely on host blocking.
  const module: WidgetModule = { ...fixture.module, load: async () => {
    const loaded = await fixture.module.load();
    const Renderer = loaded.default as ComponentType<WidgetRendererProps>;
    return { default: (props: WidgetRendererProps) => <Renderer {...props} emit={(intent) => {
      rendererIntents.push(intent);
      return props.emit(intent);
    }} /> };
  } };
  const element = (interactionMode: Mode, locked = readOnly) => <ThemeProvider initialPreference={{ scheme: "light", skinId: "wb.default" }}>
    <InteractionSurfaceProvider>
      <WidgetDraftRuntimeProvider repository={repository} profileId="preview-test" workspaceId="preview-workspace">
        <DashboardAnnouncer><AssistedDraftRuntimeProvider fetchImpl={assistantFetch}>
          <WidgetHost definition={fixture.definition} module={module} instanceId={fixture.instanceId} viewId={fixture.viewId}
            input={locked ? { ...fixture.input, access: { mode: "read_only" } } : fixture.input}
            status="ready" width={1200} height={800} sizeMode="expanded" interactionMode={interactionMode} emit={provider} />
        </AssistedDraftRuntimeProvider></DashboardAnnouncer>
      </WidgetDraftRuntimeProvider>
    </InteractionSurfaceProvider>
  </ThemeProvider>;
  const view = render(element(mode));
  return { ...view, provider, assistantFetch, rendererIntents, setMode: (next: Mode) => view.rerender(element(next)) };
}

describe("Manual forms in real WidgetHost Preview", () => {
  beforeAll(async () => {
    // The real Workspace renderer imports the document/editor graph. Resolve
    // that cold transform in bounded setup, not inside a form readiness check.
    // WidgetHost still loads and mounts the actual registered module in each test.
    await Promise.all([quickAdd.module.load(), jobs.module.load(), TASKS_WIDGET_MODULES[1]!.load()]);
  }, 30_000);
  beforeEach(() => {
    vi.stubGlobal("matchMedia", vi.fn((query: string) => ({
      media: query, matches: false, onchange: null, addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    })));
  });
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("allows Quick Add edits and host-blocked submit, then restores the original Operate draft", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const original = await seedDraft(repository, quickAdd, { ...EMPTY_TASK_CREATE_DRAFT, title: "Original task draft" });
    const saves = vi.spyOn(ForkedWidgetDraftRepository.prototype, "save");
    const view = renderForm(quickAdd, repository, "operate");
    expect(await screen.findByRole("textbox", { name: "New task" }, READY)).toHaveValue("Original task draft");
    view.setMode("preview");
    const title = await screen.findByRole("textbox", { name: "New task" }, READY);
    expect(title).toBeEnabled();
    await userEvent.clear(title);
    await userEvent.type(title, "Disposable task draft");
    expect(screen.getByRole("button", { name: "AI help" })).toBeDisabled();
    const saveProposal = screen.getByRole("button", { name: "Save proposal" });
    expect(saveProposal).toBeDisabled();
    await userEvent.click(saveProposal);
    await userEvent.click(screen.getByRole("button", { name: "Add task" }));
    await waitFor(() => expect(view.rendererIntents.map((intent) => intent.intent_type)).toEqual([TASK_INTENTS.create]));
    await waitFor(() => expect(screen.getByRole("button", { name: "Add task" })).toBeEnabled());
    expect(title).toHaveValue("Disposable task draft");
    expect(view.provider).not.toHaveBeenCalled();
    expect(view.assistantFetch).not.toHaveBeenCalled();
    expect(saves).toHaveBeenCalled();
    for (const [request] of saves.mock.calls) expect(request.value).not.toHaveProperty("proposal_pending");
    expect(await repository.load(identityFor(quickAdd))).toEqual(original);

    view.setMode("operate");
    expect(await screen.findByRole("textbox", { name: "New task" }, READY)).toHaveValue("Original task draft");
    expect(await repository.load(identityFor(quickAdd))).toEqual(original);
  });

  it("shows disposable pasted batch rows and allows cancel without a server token or provider call", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const original = await seedDraft(repository, quickAdd, { ...EMPTY_TASK_CREATE_DRAFT, title: "Retained title" });
    const view = renderForm(quickAdd, repository);
    const title = await screen.findByRole("textbox", { name: "New task" }, READY);
    fireEvent.paste(title, { clipboardData: { getData: () => "First pasted task\nSecond pasted task" } });
    const batch = await screen.findByRole("region", { name: "Review pasted tasks" }, READY);
    await within(batch).findByText("3 local rows · validation and creation are paused in Preview.");
    expect(within(batch).getAllByRole("listitem").map((row) => row.textContent)).toEqual([
      "Retained titlePreview only", "First pasted taskPreview only", "Second pasted taskPreview only",
    ]);
    expect(within(batch).getByRole("button", { name: "Create 0 tasks" })).toBeDisabled();
    expect(view.rendererIntents.map((intent) => intent.intent_type)).toEqual([TASK_INTENTS.batchPreview]);
    expect(view.rendererIntents[0]?.payload).not.toHaveProperty("preview_token");
    await userEvent.click(within(batch).getByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("textbox", { name: "New task" }, READY)).toHaveValue("Retained title");
    expect(view.provider).not.toHaveBeenCalled();
    expect(view.assistantFetch).not.toHaveBeenCalled();
    expect(await repository.load(identityFor(quickAdd))).toEqual(original);
  });

  it("allows Jobs Preview edits and blocks submit and cron/model calls while preserving the real draft", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const original = await seedDraft(repository, jobs, { ...EMPTY_JOB_DRAFT, name: "original-job", schedule: "0 9 * * 1", prompt: "Original prompt" });
    const view = renderForm(jobs, repository);
    const name = await screen.findByRole("textbox", { name: "Job name" }, READY);
    expect(name).toBeEnabled();
    await userEvent.clear(name);
    await userEvent.type(name, "preview-job");
    fireEvent.change(screen.getByRole("textbox", { name: "Schedule" }), { target: { value: "0 10 * * 2" } });
    expect(screen.getByRole("button", { name: "AI help" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Create job" }));
    await waitFor(() => expect(view.rendererIntents.map((intent) => intent.intent_type)).toEqual([JOB_INTENTS.create]));
    await waitFor(() => expect(screen.getByRole("button", { name: "Create job" })).toBeEnabled());
    expect(name).toHaveValue("preview-job");
    expect(view.provider).not.toHaveBeenCalled();
    expect(view.assistantFetch).not.toHaveBeenCalled();
    expect(await repository.load(identityFor(jobs))).toEqual(original);

    view.setMode("operate");
    expect(await screen.findByRole("textbox", { name: "Job name" }, READY)).toHaveValue("original-job");
    expect(screen.getByRole("textbox", { name: "Schedule" })).toHaveValue("0 9 * * 1");
    expect(await repository.load(identityFor(jobs))).toEqual(original);
  });

  it.each(["linked", "pending ingress", "pending revision"] as const)("cannot accept or save a %s Quick Add proposal in Preview or mint a new request", async (binding) => {
    const repository = new InMemoryWidgetDraftRepository();
    const fields = { ...EMPTY_TASK_CREATE_DRAFT, title: "Bound task draft" };
    const pending = binding === "linked" ? undefined : {
      clientMutationId: "original-pending-request", parameters: { task_text: fields.title }, origin: {}, draftFingerprint: taskDraftFingerprint(fields),
      ...(binding === "pending revision" ? { revisionOf: { threadId: proposal.thread_id, proposalEventId: proposal.proposal_event_id } } : {}),
    };
    const value: TaskCreateDraft = { ...fields,
      ...(binding !== "pending ingress" ? { proposal_ref: { threadId: proposal.thread_id, proposalEventId: proposal.proposal_event_id, draftFingerprint: taskDraftFingerprint(fields) } } : {}),
      ...(pending ? { proposal_pending: pending } : {}),
    };
    const original = await seedDraft(repository, quickAdd, value);
    const saves = vi.spyOn(ForkedWidgetDraftRepository.prototype, "save");
    const view = renderForm(quickAdd, repository);
    const title = await screen.findByRole("textbox", { name: "New task" }, READY);
    if (binding !== "pending ingress") expect(screen.getByRole("button", { name: "Create task from proposal" })).toBeDisabled();
    await userEvent.type(title, " local edit");
    const save = screen.getByRole("button", { name: binding === "linked" ? "Save proposal changes" : "Retry proposal save" });
    expect(save).toBeDisabled();
    await userEvent.click(save);
    fireEvent.submit(title.closest("form")!);
    await waitFor(() => expect(saves).toHaveBeenCalled());
    for (const [request] of saves.mock.calls) expect((request.value as unknown as TaskCreateDraft).proposal_pending).toEqual(pending);
    expect(view.rendererIntents).toEqual([]);
    expect(view.provider).not.toHaveBeenCalled();
    expect(view.assistantFetch).not.toHaveBeenCalled();
    expect(await repository.load(identityFor(quickAdd))).toEqual(original);
  });

  it("allows only disposable local proposal edits and disables all ready-proposal decisions", async () => {
    const fixture = proposalForm();
    const repository = new InMemoryWidgetDraftRepository();
    const original = await seedDraft(repository, fixture, { ...EMPTY_TASK_CREATE_DRAFT, title: "Current proposal", baseProposalEventId: 7 });
    const view = renderForm(fixture, repository);
    const title = await screen.findByRole("textbox", { name: "Proposed task title" }, READY);
    expect(title).toBeEnabled();
    expect(screen.getByRole("button", { name: "Create task" })).toBeDisabled();
    await userEvent.type(title, " local review");
    for (const label of ["Save proposal changes", "Create task", "Dismiss proposal"]) {
      const button = screen.getByRole("button", { name: label });
      expect(button).toBeDisabled();
      await userEvent.click(button);
    }
    fireEvent.submit(title.closest("form")!);
    expect(screen.queryByRole("button", { name: "Confirm dismissal" })).not.toBeInTheDocument();
    expect(view.rendererIntents).toEqual([]);
    expect(view.provider).not.toHaveBeenCalled();
    expect(view.assistantFetch).not.toHaveBeenCalled();
    expect(await repository.load(identityFor(fixture))).toEqual(original);
    view.setMode("operate");
    expect(await screen.findByRole("textbox", { name: "Proposed task title" }, READY)).toHaveValue("Current proposal");
  });

  it.each(["executing", "needs_attention"] as const)("blocks %s proposal execution retries in Preview", async (status) => {
    const view = renderForm(proposalForm(status));
    const retry = await screen.findByRole("button", { name: "Retry creating task" }, READY);
    expect(retry).toBeDisabled();
    await userEvent.click(retry);
    expect(view.rendererIntents).toEqual([]);
    expect(view.provider).not.toHaveBeenCalled();
    expect(view.assistantFetch).not.toHaveBeenCalled();
  });

  it.each([
    ["Quick Add", "arrange", false, quickAdd, "New task"],
    ["Quick Add", "preview", true, quickAdd, "New task"],
    ["Jobs", "arrange", false, jobs, "Job name"],
    ["Jobs", "preview", true, jobs, "Job name"],
    ["proposal", "arrange", false, proposalForm(), "Proposed task title"],
    ["proposal", "preview", true, proposalForm(), "Proposed task title"],
  ] as const)("locks %s editing in %s (read-only=%s)", async (_name, mode, readOnly, fixture, label) => {
    const view = renderForm(fixture, new InMemoryWidgetDraftRepository(), mode, readOnly);
    const field = await screen.findByLabelText(label, {}, READY);
    expect(field).toBeDisabled();
    const before = (field as HTMLInputElement).value;
    await userEvent.type(field, "Must not change");
    expect(field).toHaveValue(before);
    const assistance = screen.queryByRole("button", { name: "AI help", hidden: true });
    if (assistance) expect(assistance).toBeDisabled();
    expect(view.rendererIntents).toEqual([]);
    expect(view.provider).not.toHaveBeenCalled();
    expect(view.assistantFetch).not.toHaveBeenCalled();
  });
});

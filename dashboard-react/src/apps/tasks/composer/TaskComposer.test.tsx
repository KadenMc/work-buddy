import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { webcrypto } from "node:crypto";

import { DashboardAnnouncer } from "../../../dashboard/accessibility/DashboardAnnouncer";
import { DashboardHelpProvider } from "../../../dashboard/help";
import type {
  IntentResult,
  WidgetIntent,
  WidgetPresentationContext,
} from "../../../dashboard/contributions/contracts";
import { WidgetDraftTestScope } from "../../../test/DashboardTestRuntime";
import { TASKS_INSTANCE_IDS, TASKS_VIEW_ID } from "../bindings";
import { TASKS_APP_CONTRIBUTION } from "../contribution";
import { TASK_INTENTS, type TaskProposal, type TaskQuickAddInput } from "../contracts";
import TaskComposer, {
  EMPTY_TASK_CREATE_DRAFT,
  isTaskCreateDraftPristine,
  parseTaskBatch,
  type TaskCreateDraft,
} from "./TaskComposer";

const presentation: WidgetPresentationContext = {
  instanceId: TASKS_INSTANCE_IDS.quickAdd,
  viewId: TASKS_VIEW_ID,
  width: 800,
  height: 320,
  sizeMode: "standard",
  interactionMode: "operate",
  editing: false,
  theme: {
    contractVersion: 1,
    preference: { scheme: "light", skinId: "wb.default" },
    resolvedScheme: "light",
    skin: { id: "wb.default", version: 2, publisherAppId: "wb.core" },
    accessibility: { forcedColors: false, reducedMotion: false, reducedTransparency: false },
  },
  getCanvasTheme: () => ({ surfaceCanvas: "", surfaceRaised: "", textPrimary: "", textSecondary: "", borderDefault: "", focusRing: "", dataSeries: [] }),
};

const input: TaskQuickAddInput = {
  instanceId: TASKS_INSTANCE_IDS.quickAdd,
  revision: 7,
  access: { mode: "read_write" },
  options: { projects: [{ value: "work-buddy", label: "Work Buddy" }], namespaces: [], contracts: [], contexts: [] },
};

const composerElement = (
  emit: (intent: WidgetIntent) => Promise<IntentResult>,
  widgetInput: TaskQuickAddInput = input,
) => (
  <DashboardAnnouncer>
    <WidgetDraftTestScope definition={TASKS_APP_CONTRIBUTION.widgetDefinitions[0]} presentation={presentation} input={widgetInput}>
      <TaskComposer input={widgetInput} emit={emit} presentation={presentation} />
    </WidgetDraftTestScope>
  </DashboardAnnouncer>
);

const renderComposer = (
  emit: (intent: WidgetIntent) => Promise<IntentResult>,
  widgetInput: TaskQuickAddInput = input,
) => render(composerElement(emit, widgetInput));

describe("TaskComposer", () => {
  it("keeps keyboard and batch instructions in contextual help without changing Enter submission", async () => {
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "accepted" }));
    const user = userEvent.setup();
    const view = (help: boolean) => <DashboardHelpProvider enabled={help}>{composerElement(emit)}</DashboardHelpProvider>;
    const rendered = render(view(false));
    let title = await screen.findByRole("textbox", { name: "New task" });
    expect(screen.queryByText(/Press Enter to add/)).not.toBeInTheDocument();
    expect(title).not.toHaveAttribute("aria-describedby");
    await user.hover(title);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    rendered.rerender(view(true));
    title = screen.getByRole("textbox", { name: "New task" });
    await user.hover(document.body);
    await user.hover(title);
    expect(await screen.findByRole("tooltip", {}, { timeout: 3000 })).toHaveTextContent("Paste several lines to preview a batch");
    expect(screen.getByRole("tooltip")).toHaveTextContent("New tasks default to Inbox");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument());
    await user.tab();
    expect(title).toHaveFocus();
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Press Enter to add the task");
    await user.type(title, "Read the short draft");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(emit).toHaveBeenCalledWith(expect.objectContaining({ intent_type: TASK_INTENTS.create })));
  });

  it("keeps save-proposal guidance on the existing action without submitting on hover", async () => {
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "accepted" }));
    render(<DashboardHelpProvider enabled>{composerElement(emit)}</DashboardHelpProvider>);
    await userEvent.type(await screen.findByRole("textbox", { name: "New task" }), "Review this idea");
    const save = screen.getByRole("button", { name: "Save proposal" });
    expect(screen.queryByText("Save a proposal for review.")).not.toBeInTheDocument();
    await userEvent.hover(save);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("without creating a task");
    expect(emit).not.toHaveBeenCalled();
  });

  it("requires full review when replayed proposal settings are not shown by Quick Add", async () => {
    vi.stubGlobal("crypto", webcrypto);
    const user = userEvent.setup();
    const proposal: TaskProposal = { thread_id: "th-1234abcd", proposal_event_id: 9, status: "ready", parameters: { task_text: "Reviewed title", contract: "Additional commitment", automation_tier_achievable: 3 }, origin: {}, realization: null, href: "/app/tasks?proposal=th-1234abcd" };
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "accepted", value: { proposal } }));
    renderComposer(emit, { ...input, observedProposal: proposal });
    await user.type(await screen.findByRole("textbox", { name: "New task" }), "Reviewed title");
    await user.click(screen.getByRole("button", { name: "Save proposal" }));
    expect(await screen.findByText(/This proposal includes additional task settings/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create task from proposal" })).toBeDisabled();
    expect(screen.getByRole("link", { name: "Review saved proposal" })).toHaveAttribute("href", proposal.href);
    await user.type(screen.getByRole("textbox", { name: "New task" }), " with a local edit");
    const revise = screen.getByRole("button", { name: "Save proposal changes" });
    expect(revise).toBeDisabled();
    await user.click(revise);
    expect(emit.mock.calls.some(([intent]) => intent.intent_type === TASK_INTENTS.proposalRevise)).toBe(false);
    expect(emit.mock.calls.some(([intent]) => intent.intent_type === TASK_INTENTS.proposalAccept)).toBe(false);
  });

  it.each(["ingress", "revision"] as const)("never accepts unseen fields when a lost %s response replays a newer proposal", async (operation) => {
    vi.stubGlobal("crypto", webcrypto);
    const user = userEvent.setup();
    const original: TaskProposal = { thread_id: "th-1234abcd", proposal_event_id: 7, status: "ready", parameters: { task_text: "My reviewed title" }, origin: {}, realization: null, href: "/app/tasks?proposal=th-1234abcd" };
    const replayed = { ...original, proposal_event_id: 11, parameters: { task_text: "Another tab's unseen title", summary: "Different scope" } };
    let saves = 0;
    const lostIntent = operation === "ingress" ? TASK_INTENTS.proposalCreate : TASK_INTENTS.proposalRevise;
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => {
      if (intent.intent_type === lostIntent) {
        saves += 1;
        if (saves === 1) return { intent_id: intent.intent_id, status: "unavailable", message: "The committed response was lost." };
        return { intent_id: intent.intent_id, status: "accepted", value: { proposal: replayed } };
      }
      return { intent_id: intent.intent_id, status: "accepted", value: { proposal: original } };
    });
    renderComposer(emit, { ...input, observedProposal: original });
    const title = await screen.findByRole("textbox", { name: "New task" });
    await user.type(title, "My reviewed title");
    await user.click(screen.getByRole("button", { name: "Save proposal" }));
    if (operation === "revision") {
      await screen.findByRole("link", { name: "Review saved proposal" });
      await user.type(title, " revised by me");
      await user.click(screen.getByRole("button", { name: "Save proposal changes" }));
    }
    await screen.findByText("The committed response was lost.");
    await user.click(screen.getByRole("button", { name: "Retry proposal save" }));
    await screen.findByText(/Your draft has changes that are not yet in the saved proposal/);
    expect(title).toHaveValue(operation === "revision" ? "My reviewed title revised by me" : "My reviewed title");
    await user.click(screen.getByRole("button", { name: "Create task from proposal" }));
    expect(emit.mock.calls.some(([intent]) => intent.intent_type === TASK_INTENTS.proposalAccept || intent.intent_type === TASK_INTENTS.create)).toBe(false);
    const attempts = emit.mock.calls.filter(([intent]) => intent.intent_type === lostIntent);
    expect(attempts[1]?.[0]).toEqual(attempts[0]?.[0]);
  });

  it("preserves conflicting Quick Add edits until explicitly loading the current proposal", async () => {
    vi.stubGlobal("crypto", webcrypto);
    const user = userEvent.setup();
    const proposal: TaskProposal = { thread_id: "th-1234abcd", proposal_event_id: 7, status: "ready", parameters: { task_text: "Original" }, origin: {}, realization: null, href: "/app/tasks?proposal=th-1234abcd" };
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "accepted", value: { proposal } }));
    const view = renderComposer(emit, { ...input, observedProposal: proposal });
    const title = await screen.findByRole("textbox", { name: "New task" });
    await user.type(title, "Original");
    await user.click(screen.getByRole("button", { name: "Save proposal" }));
    await screen.findByRole("link", { name: "Review saved proposal" });
    await user.type(title, " local changes");
    view.rerender(composerElement(emit, { ...input, selectedProposal: { ...proposal, proposal_event_id: 9, parameters: { task_text: "New authoritative version" } } }));
    expect(title).toHaveValue("Original local changes");
    await user.click(screen.getByRole("button", { name: "Discard Quick Add edits and load current proposal" }));
    expect(title).toHaveValue("New authoritative version");
    await user.click(screen.getByRole("button", { name: "Create task from proposal" }));
    expect(emit).toHaveBeenCalledWith(expect.objectContaining({ intent_type: TASK_INTENTS.proposalAccept, payload: { thread_id: proposal.thread_id, expected_proposal_event_id: 9 } }));
  });
  it("retries a lost proposal revision with the same key, version and exact parameters", async () => {
    vi.stubGlobal("crypto", webcrypto);
    const user = userEvent.setup();
    const proposal: TaskProposal = { thread_id: "th-1234abcd", proposal_event_id: 7, status: "ready", parameters: { task_text: "Review draft" }, origin: {}, realization: null, href: "/app/tasks?proposal=th-1234abcd" };
    let revisions = 0;
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => {
      if (intent.intent_type === TASK_INTENTS.proposalRevise && ++revisions === 1) return { intent_id: intent.intent_id, status: "unavailable", message: "Revision response lost." };
      return { intent_id: intent.intent_id, status: "accepted", value: { proposal: { ...proposal, proposal_event_id: revisions > 1 ? 9 : 7 } } };
    });
    renderComposer(emit, { ...input, observedProposal: proposal });
    const title = await screen.findByRole("textbox", { name: "New task" });
    await user.type(title, "Review draft");
    await user.click(screen.getByRole("button", { name: "Save proposal" }));
    await screen.findByRole("link", { name: "Review saved proposal" });
    await user.type(title, " carefully");
    await user.click(screen.getByRole("button", { name: "Save proposal changes" }));
    await screen.findByText("Revision response lost.");
    await user.type(title, " later");
    await user.click(screen.getByRole("button", { name: "Retry proposal save" }));
    await waitFor(() => expect(emit.mock.calls.filter(([intent]) => intent.intent_type === TASK_INTENTS.proposalRevise)).toHaveLength(2));
    const sent = emit.mock.calls.filter(([intent]) => intent.intent_type === TASK_INTENTS.proposalRevise);
    expect(sent[1]?.[0]).toEqual(sent[0]?.[0]);
    expect(sent[0]?.[0]).toMatchObject({ payload: { expected_proposal_event_id: 7, parameters: { task_text: "Review draft carefully" } } });
    expect(title).toHaveValue("Review draft carefully later");
    expect(screen.getByText(/Your draft has changes that are not yet in the saved proposal/)).toBeInTheDocument();
  });
  it("saves a proposal without making a task, preserves the draft, then uses fenced acceptance", async () => {
    vi.stubGlobal("crypto", webcrypto);
    const user = userEvent.setup();
    const proposal: TaskProposal = { thread_id: "th-1234abcd", proposal_event_id: 7, status: "ready", parameters: { task_text: "Review draft" }, origin: { kind: "task_quick_add", id: "widget" }, realization: null, href: "/app/tasks?proposal=th-1234abcd" };
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "accepted", value: intent.intent_type === TASK_INTENTS.proposalAccept ? { proposal: { ...proposal, status: "realized", realization: { task_id: "t-1234abcd", receipt_id: "receipt-1", task_revision: 1, href: "/app/tasks?task=t-1234abcd" } } } : { proposal } }));
    renderComposer(emit, { ...input, observedProposal: proposal });
    await user.type(await screen.findByRole("textbox", { name: "New task" }), "Review draft");
    await user.click(screen.getByRole("button", { name: "Save proposal" }));
    await screen.findByRole("link", { name: "Review saved proposal" });
    expect(screen.getByRole("textbox", { name: "New task" })).toHaveValue("Review draft");
    expect(emit.mock.calls.filter(([intent]) => intent.intent_type === TASK_INTENTS.create)).toHaveLength(0);
    expect(emit.mock.calls[0]?.[0]).toMatchObject({ intent_type: TASK_INTENTS.proposalCreate, payload: { action: { name: "task_create", parameters: { task_text: "Review draft", state: "inbox" } } } });
    await user.click(screen.getByRole("button", { name: "Create task from proposal" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "New task" })).toHaveValue(""));
    expect(emit).toHaveBeenCalledWith(expect.objectContaining({ intent_type: TASK_INTENTS.proposalAccept, payload: { thread_id: "th-1234abcd", expected_proposal_event_id: 7 } }));
  });

  it("replays the exact pending proposal ingress and blocks direct creation after a lost response", async () => {
    vi.stubGlobal("crypto", webcrypto);
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "unavailable", message: "Response lost; retry this proposal." }));
    renderComposer(emit);
    await user.type(await screen.findByRole("textbox", { name: "New task" }), "Pending draft");
    await user.click(screen.getByRole("button", { name: "Save proposal" }));
    await screen.findByText("Response lost; retry this proposal.");
    await user.click(screen.getByRole("button", { name: "Add task" }));
    expect(emit).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Retry proposal save" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(2));
    expect(emit.mock.calls[1]?.[0]).toEqual(emit.mock.calls[0]?.[0]);
    expect(screen.getByRole("textbox", { name: "New task" })).toHaveValue("Pending draft");
  });

  it("normalizes pasted checklists and marks duplicate lines", () => {
    expect(parseTaskBatch("- First\n[ ] Second\n1. first\n\n* Third")).toEqual([
      { title: "First", duplicate: false },
      { title: "Second", duplicate: false },
      { title: "first", duplicate: true },
      { title: "Third", duplicate: false },
    ]);
  });

  it("treats every authored draft field as non-pristine", () => {
    const changed: readonly TaskCreateDraft[] = [
      { ...EMPTY_TASK_CREATE_DRAFT, title: "Task" },
      { ...EMPTY_TASK_CREATE_DRAFT, attention_state: "focused" },
      { ...EMPTY_TASK_CREATE_DRAFT, urgency: "high" },
      { ...EMPTY_TASK_CREATE_DRAFT, due_date: "2026-08-24" },
      { ...EMPTY_TASK_CREATE_DRAFT, deadline_date: "2026-08-25" },
      { ...EMPTY_TASK_CREATE_DRAFT, project: "work-buddy" },
      { ...EMPTY_TASK_CREATE_DRAFT, namespaces: "engineering" },
      { ...EMPTY_TASK_CREATE_DRAFT, summary: "Context" },
      { ...EMPTY_TASK_CREATE_DRAFT, desired_outcome: "Done" },
      { ...EMPTY_TASK_CREATE_DRAFT, next_action: "Start" },
      { ...EMPTY_TASK_CREATE_DRAFT, definition_of_done: "Shipped" },
      { ...EMPTY_TASK_CREATE_DRAFT, dependencies: "Review" },
      { ...EMPTY_TASK_CREATE_DRAFT, batch_lines: ["Task"] },
    ];

    expect(isTaskCreateDraftPristine(EMPTY_TASK_CREATE_DRAFT)).toBe(true);
    expect(changed.every((value) => !isTaskCreateDraftPristine(value))).toBe(true);
  });

  it("disables creation without repeating the view-level editing notice", async () => {
    const reason = "Task editing is temporarily unavailable while setup finishes.";
    renderComposer(vi.fn(), {
      ...input,
      access: { mode: "read_only", reason },
    });

    expect(await screen.findByRole("textbox", { name: "New task" })).toBeDisabled();
    expect(screen.queryByText(reason)).not.toBeInTheDocument();
  });

  it("captures title plus Enter through one idempotent create intent", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({
      intent_id: intent.intent_id,
      client_mutation_id: intent.client_mutation_id,
      status: "accepted" as const,
      revision: 8,
      message: "Task created.",
    }));
    renderComposer(emit);

    const title = await screen.findByRole("textbox", { name: "New task" });
    await user.type(title, "Back up task store{Enter}");

    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    expect(emit.mock.calls[0]?.[0]).toMatchObject({
      intent_type: TASK_INTENTS.create,
      client_mutation_id: expect.stringMatching(/^task-create:/),
      payload: {
        title: "Back up task store",
        attention_state: "inbox",
        urgency: "medium",
      },
    });
    await waitFor(() => expect(title).toHaveValue(""));
    expect(title).toHaveFocus();
  });

  it("uses the server preview before commit and retains an entered title in the batch", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => {
      if (intent.intent_type === TASK_INTENTS.batchPreview) {
        return {
          intent_id: intent.intent_id,
          status: "accepted",
          revision: 7,
          value: {
            preview: {
              rows: [
                { index: 0, title: "Retained title", valid: true, field_errors: {}, duplicate: false, duplicate_reason: null, will_create: true },
                { index: 1, title: "First task", valid: true, field_errors: {}, duplicate: false, duplicate_reason: null, will_create: true },
                { index: 2, title: "Second task", valid: true, field_errors: {}, duplicate: false, duplicate_reason: null, will_create: true },
                { index: 3, title: "first task", valid: true, field_errors: {}, duplicate: true, duplicate_reason: "batch", will_create: false },
              ],
              accepted_indices: [0, 1, 2],
              accepted_count: 3,
              can_commit: true,
              collection_revision: 7,
              preview_token: "server-preview-token",
            },
          },
        };
      }
      return { intent_id: intent.intent_id, status: "accepted", revision: 8 };
    });
    renderComposer(emit);
    const title = await screen.findByRole("textbox", { name: "New task" });
    await user.type(title, "Retained title");

    fireEvent.paste(title, { clipboardData: { getData: () => "- First task\n- Second task\n- first task" } });

    expect(await screen.findByRole("heading", { name: "Review pasted tasks" })).toBeInTheDocument();
    expect(await screen.findByText("Repeated in paste")).toBeInTheDocument();
    expect(emit.mock.calls[0]?.[0]).toMatchObject({
      intent_type: TASK_INTENTS.batchPreview,
      payload: {
        items: [
          { title: "Retained title" },
          { title: "First task" },
          { title: "Second task" },
          { title: "first task" },
        ],
      },
    });
    await user.click(screen.getByRole("button", { name: "Create 3 tasks" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(2));
    expect(emit.mock.calls[1]?.[0]).toMatchObject({
      intent_type: TASK_INTENTS.batchCreate,
      payload: {
        preview_confirmed: true,
        preview_token: "server-preview-token",
        accepted_indices: [0, 1, 2],
        items: [
          { title: "Retained title" },
          { title: "First task" },
          { title: "Second task" },
          { title: "first task" },
        ],
      },
    });
  });

  it("requires explicit confirmation before minting new project structure", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" as const, revision: 8 }));
    renderComposer(emit);

    await user.type(await screen.findByRole("textbox", { name: "New task" }), "Plan launch");
    await user.click(screen.getByRole("button", { name: "Add details" }));
    await user.type(screen.getByRole("combobox", { name: "Project" }), "new-project");
    await user.click(screen.getByRole("button", { name: "Add task" }));

    expect(emit).not.toHaveBeenCalled();
    expect(screen.getByText(/This will create project “new-project”/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm structure and add" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    expect(emit.mock.calls[0]?.[0]).toMatchObject({
      intent_type: TASK_INTENTS.create,
      payload: { project: "new-project" },
    });
  });

  it("retains structure confirmation while disabling it after access becomes read-only", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({
      intent_id: intent.intent_id,
      status: "accepted" as const,
      revision: 8,
    }));
    const view = renderComposer(emit);

    await user.type(await screen.findByRole("textbox", { name: "New task" }), "Plan launch");
    await user.click(screen.getByRole("button", { name: "Add details" }));
    await user.type(screen.getByRole("combobox", { name: "Project" }), "new-project");
    await user.click(screen.getByRole("button", { name: "Add task" }));

    const confirmation = screen.getByRole("button", { name: "Confirm structure and add" });
    expect(confirmation).toBeEnabled();
    view.rerender(composerElement(emit, {
      ...input,
      access: { mode: "read_only", reason: "Editing is temporarily unavailable." },
    }));

    expect(screen.getByText(/This will create project “new-project”/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm structure and add" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "New task" })).toHaveValue("Plan launch");
    await user.click(screen.getByRole("button", { name: "Confirm structure and add" }));
    expect(emit).not.toHaveBeenCalled();
  });

  it("associates server field errors and focuses the first invalid control", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({
      intent_id: intent.intent_id,
      status: "rejected" as const,
      message: "Focused tasks need more context.",
      fieldErrors: { state: "Add a summary or knowledge document first." },
    }));
    renderComposer(emit);

    await user.type(await screen.findByRole("textbox", { name: "New task" }), "Focus this task");
    await user.click(screen.getByRole("button", { name: "Add details" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "State" }), "focused");
    await user.click(screen.getByRole("button", { name: "Add task" }));

    const state = await screen.findByRole("combobox", { name: /^State/ });
    await waitFor(() => expect(state).toHaveFocus());
    expect(state).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Add a summary or knowledge document first.")).toBeInTheDocument();
  });
});

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DashboardAnnouncer } from "../../../dashboard/accessibility/DashboardAnnouncer";
import type {
  IntentResult,
  WidgetIntent,
  WidgetPresentationContext,
} from "../../../dashboard/contributions/contracts";
import { WidgetDraftTestScope } from "../../../test/DashboardTestRuntime";
import { TASKS_INSTANCE_IDS, TASKS_VIEW_ID } from "../bindings";
import { TASKS_APP_CONTRIBUTION } from "../contribution";
import { TASK_INTENTS, type TaskQuickAddInput } from "../contracts";
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

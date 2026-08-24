import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DashboardAnnouncer } from "../../../dashboard/accessibility/DashboardAnnouncer";
import type {
  IntentResult,
  WidgetIntent,
  WidgetPresentationContext,
} from "../../../dashboard/contributions/contracts";
import { WidgetDraftTestScope } from "../../../test/DashboardTestRuntime";
import { expectNoAccessibilityViolations } from "../../../test/setup";
import { TASKS_INSTANCE_IDS, TASKS_VIEW_ID } from "../bindings";
import { TASKS_APP_CONTRIBUTION } from "../contribution";
import {
  TASK_INTENTS,
  type TaskDetail,
  type TaskWorkspaceInput,
} from "../contracts";
import TaskWorkspace, { tomorrow } from "./TaskWorkspace";

const presentation: WidgetPresentationContext = {
  instanceId: TASKS_INSTANCE_IDS.workspace,
  viewId: TASKS_VIEW_ID,
  width: 1200,
  height: 800,
  sizeMode: "expanded",
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

const detail: TaskDetail = {
  task_id: "task-1",
  title: "Prepare launch notes",
  revision: 3,
  attention_state: "inbox",
  urgency: "high",
  due_date: "2026-08-25",
  deadline_date: null,
  snooze_until: null,
  project: "work-buddy",
  namespaces: ["project/work-buddy"],
  tags: ["writing"],
  current_action: "Draft outline",
  has_document: true,
  completed_at: null,
  archived_at: null,
  deleted_at: null,
  updated_at: "2026-08-23T12:00:00Z",
  summary: "Prepare a handoff.",
  desired_outcome: "A useful launch note.",
  next_action: "Draft outline",
  definition_of_done: "Published",
  dependencies: [],
  contract: null,
  required_contexts: [],
  automation_tier: null,
  provenance: { created_by: "Owner", created_at: "2026-08-20T12:00:00Z", source: "dashboard" },
  action_items: [{ action_item_id: "action-1", text: "Draft outline", position: 0, completed: false, current: true, approval_state: "pending", deleted_at: null }],
  history: [{ history_id: "history-1", occurred_at: "2026-08-20T12:00:00Z", actor: "Owner", action: "created", summary: "Task created" }],
  document: { state: "available", store_id: "store-1", document_id: "document-1", excerpt: "Launch context", updated_at: "2026-08-23T12:00:00Z", updated_by: "Owner", href: null },
  local_files: [{ link_id: "link-1", display_name: "launch.pdf", media_type: "application/pdf", byte_length: 2048, sensitivity: "ordinary", allowed_action: "open", availability: "available", host_action_available: true, unavailable_reason: null }],
  local_files_error: null,
};

const input = (selectedTask: TaskDetail | null = null): TaskWorkspaceInput => ({
  instanceId: TASKS_INSTANCE_IDS.workspace,
  revision: 17,
  access: { mode: "read_write" },
  query: { lens: "inbox", q: "", project: "", namespace: "", urgency: "", due: "", state: "", note: "", task: selectedTask?.task_id ?? null },
  facets: {
    counts: { focused: 1, inbox: 2, active: 5, snoozed: 1, completed: 3, trash: 1, triage: 2 },
    projects: { "work-buddy": 1 },
    namespaces: {},
    urgencies: { high: 1 },
  },
  tasks: [detail],
  selectedTask,
  options: { projects: [{ value: "work-buddy", label: "Work Buddy" }], namespaces: [], contracts: [], contexts: [] },
});

const workspaceElement = (
  workspaceInput: TaskWorkspaceInput,
  emit: (intent: WidgetIntent) => Promise<IntentResult>,
) => (
  <DashboardAnnouncer>
    <WidgetDraftTestScope definition={TASKS_APP_CONTRIBUTION.widgetDefinitions[1]} presentation={presentation} input={workspaceInput}>
      <TaskWorkspace input={workspaceInput} emit={emit} presentation={presentation} />
    </WidgetDraftTestScope>
  </DashboardAnnouncer>
);

const renderWorkspace = (
  workspaceInput: TaskWorkspaceInput,
  emit: (intent: WidgetIntent) => Promise<IntentResult>,
) => render(workspaceElement(workspaceInput, emit));

describe("TaskWorkspace", () => {
  it("formats tomorrow from local calendar components", () => {
    expect(tomorrow(new Date(2026, 7, 23, 23, 30))).toBe("2026-08-24");
  });

  it("disables task changes without repeating the view-level editing notice", async () => {
    const reason = "Task editing is temporarily unavailable while setup finishes.";
    renderWorkspace(
      { ...input(detail), access: { mode: "read_only", reason } },
      vi.fn(),
    );

    expect(await screen.findByRole("textbox", { name: "Title" })).toBeDisabled();
    expect(screen.queryByText(reason)).not.toBeInTheDocument();
    expect(screen.queryByText("This task collection is read-only.")).not.toBeInTheDocument();
  });

  it("has no automated accessibility violations in its ready detail state", async () => {
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" as const, revision: 17 }));
    const view = renderWorkspace(input(detail), emit);

    await screen.findByRole("textbox", { name: "Title" });
    await expectNoAccessibilityViolations(view.container);
  });

  it("removes the inactive mobile pane from interaction and the accessibility tree", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: true,
      media: "(max-width: 767px)",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })));
    try {
      const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" as const, revision: 17 }));
      renderWorkspace(input(detail), emit);
      const listPanel = document.getElementById("wb-task-list-panel")!;
      const detailPanel = document.getElementById("wb-task-detail-panel")!;

      await screen.findByRole("textbox", { name: "Title" });
      expect(listPanel).toHaveAttribute("hidden");
      expect(listPanel).toHaveAttribute("inert");
      expect(detailPanel).not.toHaveAttribute("hidden");

      const detailsTab = screen.getByRole("tab", { name: "Details" });
      const listTab = screen.getByRole("tab", { name: "List" });
      expect(detailsTab).toHaveAttribute("tabindex", "0");
      detailsTab.focus();
      fireEvent.keyDown(detailsTab, { key: "ArrowLeft" });
      expect(listTab).toHaveFocus();
      expect(listTab).toHaveAttribute("tabindex", "0");
      expect(listPanel).not.toHaveAttribute("hidden");
      expect(detailPanel).toHaveAttribute("hidden");
      expect(detailPanel).toHaveAttribute("inert");
      fireEvent.keyDown(listTab, { key: "ArrowRight" });
      expect(detailsTab).toHaveFocus();
      expect(detailPanel).not.toHaveAttribute("hidden");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("emits URL intents for lenses and task selection", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" as const, revision: 17 }));
    renderWorkspace(input(), emit);

    await user.click(screen.getByRole("button", { name: /Focused/ }));
    await user.click(screen.getByText("Prepare launch notes").closest("button")!);

    expect(emit.mock.calls.map((call) => call[0])).toEqual([
      expect.objectContaining({ intent_type: TASK_INTENTS.locationChange, payload: { patch: { lens: "focused", task: null }, replace: false } }),
      expect.objectContaining({ intent_type: TASK_INTENTS.locationChange, payload: { patch: { task: "task-1" }, replace: false } }),
    ]);
  });

  it("uses the task revision for an immediate complete gesture", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" as const, revision: 18, message: "Task completed." }));
    renderWorkspace(input(), emit);

    await user.click(screen.getByRole("button", { name: "Complete Prepare launch notes" }));

    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    expect(emit.mock.calls[0]?.[0]).toMatchObject({
      intent_type: TASK_INTENTS.complete,
      client_mutation_id: expect.any(String),
      payload: { task_id: "task-1", expected_revision: 3 },
    });
    expect((await screen.findAllByText("Task completed.")).length).toBeGreaterThan(0);
  });

  it("promotes triage work to the native MIT attention state", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" as const, revision: 18 }));
    const triage = input();
    renderWorkspace({ ...triage, query: { ...triage.query, lens: "triage" } }, emit);

    await user.click(screen.getByRole("button", { name: "Most Important this week" }));

    expect(emit).toHaveBeenCalledWith(expect.objectContaining({
      intent_type: TASK_INTENTS.update,
      payload: {
        task_id: "task-1",
        expected_revision: 3,
        attention_state: "mit",
      },
    }));
  });

  it("skips a triage task locally and reveals the next candidate without mutating it", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" as const, revision: 18 }));
    const tasks = Array.from({ length: 6 }, (_, index) => ({
      ...detail,
      task_id: `task-${index + 1}`,
      title: `Triage task ${index + 1}`,
    }));
    const triage = input();
    renderWorkspace({
      ...triage,
      query: { ...triage.query, lens: "triage" },
      tasks,
    }, emit);

    expect(screen.queryByText("Triage task 6")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Skip Triage task 1 this pass" }));

    expect(screen.queryByText("Triage task 1")).not.toBeInTheDocument();
    expect(screen.getByText("Triage task 6")).toBeInTheDocument();
    expect(emit).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText("Triage task 2").closest("button")).toHaveFocus());
  });

  it("edits structured fields explicitly and opens the bound Co-work document", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" as const, revision: 18, message: "Task saved." }));
    renderWorkspace(input(detail), emit);
    const title = await screen.findByRole("textbox", { name: "Title" });

    await user.clear(title);
    await user.type(title, "Prepare final launch notes");
    const tags = screen.getByRole("textbox", { name: "Tags" });
    await user.clear(tags);
    await user.type(tags, "writing, release");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    expect(emit.mock.calls[0]?.[0]).toMatchObject({
      intent_type: TASK_INTENTS.update,
      payload: {
        task_id: "task-1",
        expected_revision: 3,
        title: "Prepare final launch notes",
        tags: ["writing", "release"],
      },
    });

    await user.click(screen.getByRole("button", { name: "Open in Co-work" }));
    expect(emit.mock.calls[1]?.[0]).toMatchObject({
      intent_type: TASK_INTENTS.openDocument,
      payload: { task_id: "task-1" },
    });
  });

  it("reuses the shared linked-file panel through the provider intent boundary", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" as const, revision: 18 }));
    renderWorkspace(input(detail), emit);

    expect(await screen.findByText("launch.pdf")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("C:\\");
    await user.click(screen.getByText("Linked local files (1)"));
    await user.click(screen.getByRole("button", { name: "Open locally" }));
    await waitFor(() => expect(emit).toHaveBeenCalledWith(expect.objectContaining({
      intent_type: TASK_INTENTS.localFileAction,
      payload: { task_id: "task-1", expected_revision: 3, link_id: "link-1", action: "open" },
    })));
  });

  it("keeps linked-file registry drift visible when no catalog rows can be read", async () => {
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" as const, revision: 18 }));
    renderWorkspace(input({
      ...detail,
      local_files: [],
      local_files_error: "Linked-file metadata is unavailable.",
    }), emit);

    expect(await screen.findByText("Linked local files")).toBeInTheDocument();
    expect(await screen.findByText("Co-work couldn’t inspect the linked local files.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("re-probes an unavailable linked file instead of replaying captured task props", async () => {
    const user = userEvent.setup();
    const linkId = `pdf_${"r".repeat(28)}`;
    const storeId = "s".repeat(32);
    const documentId = "d".repeat(32);
    const unavailableDetail: TaskDetail = {
      ...detail,
      document: { ...detail.document, store_id: storeId, document_id: documentId },
      local_files: [{
        ...detail.local_files[0],
        link_id: linkId,
        availability: "unavailable",
        host_action_available: false,
        unavailable_reason: "The linked file is unavailable.",
      }],
    };
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      new Response(JSON.stringify({
        ok: true,
        links: [{
          link_id: linkId,
          href: `wb-local-file:${linkId}`,
          display_name: "launch.pdf",
          suffix: ".pdf",
          media_type: "application/pdf",
          byte_length: 2048,
          sensitivity: "ordinary",
          allowed_action: "open",
          availability: "verified",
          local_action_available: true,
          relative_path: "private/never-render-this.pdf",
        }],
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchImpl);
    try {
      const emit = vi.fn(async (intent) => ({
        intent_id: intent.intent_id,
        status: "accepted" as const,
        revision: 18,
      }));
      renderWorkspace(input(unavailableDetail), emit);

      await user.click(await screen.findByText("Linked local files (1)"));
      expect(screen.getByRole("button", { name: "Open locally" })).toBeDisabled();
      await user.click(screen.getByRole("button", { name: "Recheck availability" }));

      await waitFor(() => {
        expect(screen.getByRole("button", { name: "Open locally" })).toBeEnabled();
      });
      expect(fetchImpl).toHaveBeenCalledTimes(1);
      expect(String(fetchImpl.mock.calls[0]?.[0])).toBe(
        `/api/truth/doc/${documentId}/local-files?store_id=${storeId}`,
      );
      expect(fetchImpl.mock.calls[0]?.[1]).toMatchObject({ cache: "no-store" });
      expect(document.body).not.toHaveTextContent("private/never-render-this.pdf");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("offers an acknowledged undo after a soft delete", async () => {
    const user = userEvent.setup();
    const deletedDetail = { ...detail, revision: 4, deleted_at: "2026-08-23T13:00:00Z" };
    const restoredDetail = { ...detail, revision: 5, deleted_at: null };
    let view: ReturnType<typeof renderWorkspace>;
    const emit = vi.fn(async (intent) => {
      if (intent.intent_type === TASK_INTENTS.delete) {
        view.rerender(workspaceElement(input(deletedDetail), emit));
        await Promise.resolve();
        return {
          intent_id: intent.intent_id,
          status: "accepted" as const,
          revision: 18,
          value: { task: deletedDetail },
        };
      }
      if (intent.intent_type === TASK_INTENTS.restore) {
        view.rerender(workspaceElement(input(restoredDetail), emit));
        await Promise.resolve();
      }
      return { intent_id: intent.intent_id, status: "accepted" as const, revision: 19 };
    });
    view = renderWorkspace(input(detail), emit);

    await user.click(await screen.findByRole("button", { name: "Move to trash" }));
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    const cancel = within(dialog).getByRole("button", { name: "Cancel" });
    const confirm = within(dialog).getByRole("button", { name: "Move to trash" });
    await waitFor(() => expect(cancel).toHaveFocus());
    fireEvent.keyDown(cancel, { key: "Tab", shiftKey: true });
    expect(confirm).toHaveFocus();
    fireEvent.keyDown(confirm, { key: "Tab" });
    expect(cancel).toHaveFocus();

    view.rerender(workspaceElement({
      ...input(detail),
      access: { mode: "read_only", reason: "Editing is temporarily unavailable." },
    }, emit));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Move to trash" })).toBeDisabled();
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Move to trash" }));
    expect(emit).not.toHaveBeenCalled();

    view.rerender(workspaceElement(input(detail), emit));
    expect(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Move to trash" })).toBeEnabled();
    const retainedConfirm = within(screen.getByRole("alertdialog")).getByRole("button", { name: "Move to trash" });
    await user.click(retainedConfirm);

    await screen.findByRole("button", { name: "Undo delete" });
    view.rerender(workspaceElement({
      ...input(deletedDetail),
      access: { mode: "read_only", reason: "Editing is temporarily unavailable." },
    }, emit));
    expect(screen.getByRole("button", { name: "Undo delete" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Undo delete" }));
    expect(emit).toHaveBeenCalledTimes(1);

    view.rerender(workspaceElement(input(deletedDetail), emit));
    expect(screen.getByRole("button", { name: "Undo delete" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Undo delete" }));

    expect(emit.mock.calls[0]?.[0]).toMatchObject({
      intent_type: TASK_INTENTS.delete,
      payload: { task_id: "task-1", expected_revision: 3 },
    });
    expect(emit.mock.calls[1]?.[0]).toMatchObject({
      intent_type: TASK_INTENTS.restore,
      payload: { task_id: "task-1", expected_revision: 4 },
    });
  });

  it("refreshes the selected task after a CAS conflict while retaining its draft", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => intent.intent_type === TASK_INTENTS.update
      ? {
          intent_id: intent.intent_id,
          status: "conflict" as const,
          message: "This task changed while you were editing it.",
          value: { task: { ...detail, revision: 4 } },
        }
      : { intent_id: intent.intent_id, status: "accepted" as const });
    renderWorkspace(input(detail), emit);
    const title = await screen.findByRole("textbox", { name: "Title" });
    await user.clear(title);
    await user.type(title, "My unsaved conflict draft");

    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(emit).toHaveBeenCalledTimes(2));
    expect(emit.mock.calls[1]?.[0]).toMatchObject({
      intent_type: TASK_INTENTS.locationChange,
      payload: { patch: { task: "task-1" }, replace: true },
    });
    expect(title).toHaveValue("My unsaved conflict draft");
  });

  it("edits an action item through its CAS endpoint", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" as const, revision: 18 }));
    renderWorkspace(input(detail), emit);

    await user.click(await screen.findByRole("button", { name: "Edit action item Draft outline" }));
    const editor = screen.getByRole("textbox", { name: "Edit action item" });
    await user.clear(editor);
    await user.type(editor, "Draft and review outline");
    await user.click(screen.getByRole("button", { name: "Save action item Draft outline" }));

    await waitFor(() => expect(emit).toHaveBeenCalledWith(expect.objectContaining({
      intent_type: TASK_INTENTS.actionItemUpdate,
      payload: {
        task_id: "task-1",
        expected_revision: 3,
        action_item_id: "action-1",
        text: "Draft and review outline",
      },
    })));
  });

  it("completes and reopens an action item through its CAS endpoint", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" as const, revision: 18 }));
    renderWorkspace(input(detail), emit);

    await user.click(await screen.findByRole("button", { name: "Complete action item Draft outline" }));

    expect(emit).toHaveBeenCalledWith(expect.objectContaining({
      intent_type: TASK_INTENTS.actionItemUpdate,
      payload: {
        task_id: "task-1",
        expected_revision: 3,
        action_item_id: "action-1",
        completed: true,
      },
    }));
  });

  it("keeps a deleted task inert except for restore and navigation", async () => {
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" as const, revision: 18 }));
    renderWorkspace(input({ ...detail, revision: 4, deleted_at: "2026-08-23T13:00:00Z" }), emit);

    expect(await screen.findByRole("textbox", { name: "Title" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "New action item" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Restore" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Complete" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Move to trash" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open in Co-work" })).toBeDisabled();
  });

});

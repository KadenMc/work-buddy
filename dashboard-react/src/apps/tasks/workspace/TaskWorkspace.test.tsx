import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DashboardAnnouncer } from "../../../dashboard/accessibility/DashboardAnnouncer";
import { DashboardHelpProvider } from "../../../dashboard/help";
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
  type TaskProposal,
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
  workspacePresentation: WidgetPresentationContext = presentation,
) => (
  <DashboardAnnouncer>
    <WidgetDraftTestScope definition={TASKS_APP_CONTRIBUTION.widgetDefinitions[1]} presentation={workspacePresentation} input={workspaceInput}>
      <TaskWorkspace input={workspaceInput} emit={emit} presentation={workspacePresentation} />
    </WidgetDraftTestScope>
  </DashboardAnnouncer>
);

const renderWorkspace = (
  workspaceInput: TaskWorkspaceInput,
  emit: (intent: WidgetIntent) => Promise<IntentResult>,
  workspacePresentation: WidgetPresentationContext = presentation,
) => render(workspaceElement(workspaceInput, emit, workspacePresentation));

const proposal: TaskProposal = { thread_id: "th-1234abcd", proposal_event_id: 7, status: "ready", parameters: { task_text: "Review captured idea", state: "inbox" }, origin: { kind: "journal", id: "capture-1", label: "Journal" }, realization: null, href: "/app/tasks?proposal=th-1234abcd" };
const proposalInput = (value = proposal): TaskWorkspaceInput => ({ ...input(), selectedProposal: { kind: "loaded", proposal: value }, query: { ...input().query, proposal: value.thread_id } });

describe("Task proposal review", () => {
  it("reveals proposal mechanics on existing headings while retaining the proposed values", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "accepted" }));
    const value = proposalInput({ ...proposal, parameters: { ...proposal.parameters, contract: "Reviewed commitment" } });
    render(<DashboardHelpProvider enabled>{workspaceElement(value, emit)}</DashboardHelpProvider>);
    const heading = await screen.findByRole("heading", { name: "Review before creating" });
    expect(screen.queryByText(/This is a proposal, not a task/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Saving the fields above keeps/)).not.toBeInTheDocument();
    expect(screen.getByText("Reviewed commitment")).toBeVisible();
    await user.hover(document.body);
    await user.hover(heading);
    expect(await screen.findByRole("tooltip", {}, { timeout: 3000 })).toHaveTextContent("A saved proposal is not a task");
    await user.keyboard("{Escape}");
    await user.hover(document.body);
    await user.hover(screen.getByRole("heading", { name: "Additional proposed settings" }));
    expect(await screen.findByRole("tooltip", {}, { timeout: 3000 })).toHaveTextContent("Create task accepts all the proposed settings");
    expect(emit).not.toHaveBeenCalled();
  });

  it("keeps actual proposal outcomes and unavailable reasons visible without help mode", async () => {
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "accepted" }));
    const rendered = renderWorkspace(proposalInput({ ...proposal, status: "rejected" }), emit);
    expect(await screen.findByText(/This proposal was dismissed. No task was created/)).toBeVisible();
    rendered.rerender(workspaceElement({ ...proposalInput(), selectedProposal: { kind: "unavailable", threadId: proposal.thread_id, code: "not_found", message: "This proposal could not be found." } }, emit));
    expect(await screen.findByText("This proposal could not be found.")).toBeVisible();
    expect(screen.queryByText("No task was created by opening this link.")).not.toBeInTheDocument();
  });

  it("shows additional task settings and preserves them when common fields are revised", async () => {
    const user = userEvent.setup();
    const additional = { contract: "Additional commitment", automation_tier_achievable: 3, agent_required_contexts: ["repository", "browser"] };
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "accepted" }));
    renderWorkspace(proposalInput({ ...proposal, parameters: { ...proposal.parameters, ...additional } }), emit);

    const settings = await screen.findByRole("region", { name: "Additional proposed task settings" });
    expect(within(settings).getByText("Contract")).toBeInTheDocument();
    expect(within(settings).getByText("Additional commitment")).toBeInTheDocument();
    expect(within(settings).getByText("Automation tier achievable")).toBeInTheDocument();
    expect(within(settings).getByText("3")).toBeInTheDocument();
    expect(within(settings).getByText("repository, browser")).toBeInTheDocument();
    expect(emit).not.toHaveBeenCalled();

    await user.type(screen.getByRole("textbox", { name: "Proposed task title" }), " tomorrow");
    await user.click(screen.getByRole("button", { name: "Save proposal changes" }));
    expect(emit).toHaveBeenCalledWith(expect.objectContaining({
      intent_type: TASK_INTENTS.proposalRevise,
      payload: expect.objectContaining({ parameters: expect.objectContaining({ ...additional, task_text: "Review captured idea tomorrow" }) }),
    }));
  });

  it("offers an explicit idempotent retry for interrupted creation while refresh stays read-only", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "accepted" }));
    renderWorkspace(proposalInput({ ...proposal, status: "executing" }), emit);
    await user.click(await screen.findByRole("button", { name: "Refresh proposal" }));
    expect(emit.mock.calls[0]?.[0]).toMatchObject({ intent_type: TASK_INTENTS.locationChange, payload: { patch: { proposal: proposal.thread_id, task: null }, replace: true } });
    expect(emit.mock.calls.some(([intent]) => intent.intent_type === TASK_INTENTS.proposalAccept)).toBe(false);
    await user.click(screen.getByRole("button", { name: "Retry creating task" }));
    expect(emit.mock.calls[1]?.[0]).toMatchObject({ intent_type: TASK_INTENTS.proposalAccept, payload: { thread_id: proposal.thread_id, expected_proposal_event_id: proposal.proposal_event_id } });
  });
  it("does not create from a deep link; the explicit button carries the reviewed event", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "accepted" }));
    renderWorkspace(proposalInput(), emit);
    expect(await screen.findByRole("textbox", { name: "Proposed task title" })).toHaveValue("Review captured idea");
    expect(emit).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: /^Create task$/ }));
    expect(emit).toHaveBeenCalledWith(expect.objectContaining({ intent_type: TASK_INTENTS.proposalAccept, payload: { thread_id: "th-1234abcd", expected_proposal_event_id: 7 } }));
  });
  it("makes local edits explicit and never accepts an unsaved draft", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "accepted" }));
    renderWorkspace(proposalInput(), emit);
    await user.type(await screen.findByRole("textbox", { name: "Proposed task title" }), " tomorrow");
    expect(screen.getByRole("button", { name: /^Create task$/ })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Save proposal changes" }));
    expect(emit).toHaveBeenCalledWith(expect.objectContaining({ intent_type: TASK_INTENTS.proposalRevise, payload: expect.objectContaining({ expected_proposal_event_id: 7, parameters: expect.objectContaining({ task_text: "Review captured idea tomorrow" }) }) }));
  });
  it("preserves local edits and blocks stale decisions when the proposal changes elsewhere", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "accepted" }));
    const view = renderWorkspace(proposalInput(), emit);
    await user.type(await screen.findByRole("textbox", { name: "Proposed task title" }), " locally");
    view.rerender(workspaceElement(proposalInput({ ...proposal, proposal_event_id: 9, parameters: { task_text: "Updated elsewhere" } }), emit));
    expect(screen.getByRole("textbox", { name: "Proposed task title" })).toHaveValue("Review captured idea locally");
    expect(screen.getByRole("button", { name: "Save proposal changes" })).toBeDisabled();
    expect(screen.getByText(/This proposal changed elsewhere/)).toBeInTheDocument();
  });
  it("requires a second explicit dismissal gesture and preserves the capture", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => ({ intent_id: intent.intent_id, status: "accepted" }));
    renderWorkspace(proposalInput(), emit);
    await user.click(await screen.findByRole("button", { name: "Dismiss proposal" }));
    expect(emit).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Confirm dismissal" }));
    expect(emit).toHaveBeenCalledWith(expect.objectContaining({ intent_type: TASK_INTENTS.proposalReject, payload: { thread_id: "th-1234abcd", expected_proposal_event_id: 7 } }));
  });
});

describe("TaskWorkspace", () => {
  it("shows readable task actors without changing their canonical identities", async () => {
    const actorRef = JSON.stringify({
      schema: "wb.actor-ref/v1",
      issuer_authority_id: "private-authority",
      subject: "private-human",
      kind: "human",
      tenant_scope_id: "private-scope",
    });
    const task = {
      ...detail,
      document: { ...detail.document, updated_by: actorRef },
      provenance: { ...detail.provenance, created_by: actorRef },
      history: detail.history.map((entry) => ({ ...entry, actor: actorRef })),
    };
    const emit = vi.fn();
    const view = renderWorkspace(input(task), emit);

    expect(await screen.findByText("Edited 2026-08-23T12:00:00Z by Human")).toBeInTheDocument();
    await userEvent.click(screen.getByText("History and provenance"));
    expect(screen.getByText("Created 2026-08-20T12:00:00Z by Human via dashboard.")).toBeVisible();
    expect(view.container.querySelector(".wb-task-history ol")).toHaveTextContent("created · Task created · Human");
    expect(view.container.textContent).not.toContain("wb.actor-ref/v1");
    expect(view.container.textContent).not.toContain("private-human");
    expect(task.document.updated_by).toBe(actorRef);
    expect(task.provenance.created_by).toBe(actorRef);
    expect(task.history[0]?.actor).toBe(actorRef);
    expect(emit).not.toHaveBeenCalled();
  });

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

  it("uses the narrow host on a wide viewport and restores both panes when the host widens", async () => {
    vi.stubGlobal("matchMedia", vi.fn((query: string) => ({
      matches: false, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    })));
    try {
      const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" as const }));
      const view = renderWorkspace(input(detail), emit, { ...presentation, width: 767 });
      const workspace = screen.getByRole("region", { name: "Task workspace" });
      const listPanel = document.getElementById("wb-task-list-panel")!;
      const detailPanel = document.getElementById("wb-task-detail-panel")!;

      await screen.findByRole("textbox", { name: "Title" });
      expect(workspace).toHaveAttribute("data-layout", "stacked");
      expect(listPanel).toHaveAttribute("hidden");
      expect(listPanel).toHaveAttribute("inert");
      expect(detailPanel).not.toHaveAttribute("hidden");
      await userEvent.click(screen.getByRole("tab", { name: "List" }));
      expect(listPanel).not.toHaveAttribute("hidden");
      expect(detailPanel).toHaveAttribute("hidden");
      expect(detailPanel).toHaveAttribute("inert");

      view.rerender(workspaceElement(input(detail), emit, { ...presentation, width: 1200 }));
      expect(workspace).toHaveAttribute("data-layout", "wide");
      for (const panel of [listPanel, detailPanel]) {
        expect(panel).not.toHaveAttribute("hidden");
        expect(panel).not.toHaveAttribute("inert");
      }
      expect(screen.getByRole("textbox", { name: "Title" })).toBeVisible();
      expect(emit).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it.each([[768, "compact"], [785, "compact"], [1100, "compact"], [1101, "wide"]] as const)(
    "uses the %s px host breakpoint for %s layout on a wide viewport",
    async (width, layout) => {
      vi.stubGlobal("matchMedia", vi.fn((query: string) => ({
        matches: false, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn(),
      })));
      try {
        renderWorkspace(input(detail), vi.fn(), { ...presentation, width });
        await screen.findByRole("textbox", { name: "Title" });
        expect(screen.getByRole("region", { name: "Task workspace" })).toHaveAttribute("data-layout", layout);
        for (const id of ["wb-task-list-panel", "wb-task-detail-panel"]) {
          expect(document.getElementById(id)).not.toHaveAttribute("hidden");
          expect(document.getElementById(id)).not.toHaveAttribute("inert");
        }
      } finally {
        vi.unstubAllGlobals();
      }
    },
  );

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
      expect.objectContaining({ intent_type: TASK_INTENTS.locationChange, payload: { patch: { lens: "focused", task: null, proposal: null }, replace: false } }),
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

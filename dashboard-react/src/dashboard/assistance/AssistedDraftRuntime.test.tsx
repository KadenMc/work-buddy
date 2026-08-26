import { webcrypto } from "node:crypto";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CAPTURE_APP_CONTRIBUTION } from "../../widget-library/capture/contribution";
import { asViewId, asWidgetInstanceId, type WidgetDefinition } from "../contributions/contracts";
import { InMemoryWidgetDraftRepository, WidgetDraftRuntimeProvider, WidgetDraftScopeProvider, useWidgetDraft } from "../drafts";
import { DashboardHelpProvider, HelpModeProvider } from "../help";
import { HelpModeToggle } from "../help/HelpModeToggle";
import { DashboardAnnouncer } from "../accessibility/DashboardAnnouncer";
import { HttpChatConversationProvider } from "../conversations/HttpChatConversationProvider";
import { exactHumanAuthorityHeaders } from "../../security/humanAuthority";
import type { ChatExecutionSnapshot } from "../../widget-library/chat";
import type { AssistanceSession, AssistedDraftPatch, DraftPatchReceipt, PreparedDraftSnapshot } from "./contracts";
import { AssistDraftButton, AssistedDraftRuntimeProvider, AssistedDraftWorkspace, useAssistedDraft } from "./AssistedDraftRuntime";
import { assistedDraftDeclaration, assistedForms } from "./schema";
import * as assistanceSchema from "./schema";

vi.mock("../../security/humanAuthority", async (original) => ({
  ...await original<typeof import("../../security/humanAuthority")>(),
  exactHumanAuthorityHeaders: vi.fn(async () => ({ "X-Test-Authority": "exact-action" })),
}));

const initial = { title: "Original title", summary: "", next_action: "", batch_lines: [], proposal_ref: { threadId: "th-test" }, proposal_pending: { clientMutationId: "never-disclose" } };
const declaration = assistedDraftDeclaration("task-create");
const nativeRect = HTMLElement.prototype.getBoundingClientRect;
let workspaceWidth = 1200;
const definition: WidgetDefinition = {
  ...CAPTURE_APP_CONTRIBUTION.widgetDefinitions[0],
  drafts: [{ draftName: "task-create", schema: declaration.schema, scope: { kind: "view" }, persistence: "device", sensitivity: "private", clearPolicy: "widget-managed", maxBytes: 32768 }],
  assistableDrafts: [declaration],
};
const availability = { available: true, code: "ready", providerId: "fixture-provider", modelId: "fixture-model", purpose: "dashboard.assisted_draft", message: "Ready to start", disclosure: "Explicit test disclosure: allowlisted fields and messages only." };
const spawnFailureMessage = "AI help could not launch. Your form is unchanged. Choose Launch to try again or continue manually.";

const initialExecution = (): ChatExecutionSnapshot => ({
  selection: { providerId: "fixture-provider", modelId: "fixture-model", providerLabel: "Fixture Claude", modelLabel: "Model one", revision: "execution:1" },
  providers: [
    { id: "fixture-provider", label: "Fixture Claude", available: true, models: [{ id: "fixture-model", label: "Model one", available: true }] },
    { id: "fixture-codex", label: "Fixture Codex", available: true, models: [{ id: "fixture-model-two", label: "Model two", available: true }] },
  ],
});

function fakeBroker(options: { delayed?: boolean; unavailable?: boolean; startFails?: boolean; startFailsOnce?: boolean; spawnFails?: boolean; delayedStartAcknowledgement?: boolean; respondFailsOnce?: boolean; stopFails?: boolean; stopMissing?: boolean; prepareFailsOnce?: boolean; receiptFailsOnce?: boolean; endFails?: boolean; initialGreeting?: boolean; question?: boolean; selectedUnavailable?: boolean; switchConflict?: boolean; legacyRecovery?: boolean } = {}) {
  const calls: { path: string; method: string; body?: Record<string, unknown> }[] = [];
  let session: AssistanceSession;
  let snapshot: PreparedDraftSnapshot;
  let patch: AssistedDraftPatch | null = null;
  let receipt: DraftPatchReceipt | null = null;
  const messagesBySession = new Map<string, Record<string, unknown>[]>();
  let release: (() => void) | undefined;
  let failedPreparation = false;
  let failedReceipt = false;
  let failedStart = false;
  let failedRespond = false;
  let releaseStart: (() => void) | undefined;
  const sessions = new Map<string, AssistanceSession>();
  const preparedRequests = new Map<string, string>();
  const startedRequests = new Set<string>();
  const stoppedRequests = new Set<string>();
  const saveSession = (next: AssistanceSession) => { session = next; sessions.set(next.assistantSessionId, next); return next; };
  const respond = (body: unknown, status = 200) => ({ ok: status < 400, status, json: async () => body }) as Response;
  const fetchImpl = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ path, method, body });
    if (path.endsWith("/availability")) return respond(options.unavailable ? { ...availability, available: false, code: "disabled", message: "Assistance is disabled" } : availability);
    if (path === "/api/assistance/sessions") {
      const prior = preparedRequests.get(body.requestId);
      if (prior) return respond(sessions.get(prior));
      const suffix = preparedRequests.size === 0 ? "" : `-${preparedRequests.size + 1}`;
      let execution = initialExecution();
      if (options.selectedUnavailable) execution = { ...execution, providers: execution.providers.map((provider, index) => index === 0 ? { ...provider, available: false, unavailableReason: "Fixture Claude is offline" } : provider) };
      const candidate: AssistanceSession = { protocol: "wb.assisted-draft.session/v2", phase: "prepared", activeStartId: null, controlRevision: 0, assistantSessionId: `as-test${suffix}`, conversationId: `conversation-test${suffix}`, identity: body.identity, schema: body.schema, expiresAt: "2099-01-01T00:00:00Z", availability: availability as AssistanceSession["availability"], execution, agent: { status: "not_started", alive: null, phase: "prepared", activeStartId: null, controlRevision: 0 } };
      preparedRequests.set(body.requestId, candidate.assistantSessionId);
      messagesBySession.set(candidate.assistantSessionId, []);
      return respond(saveSession(candidate));
    }
    if (path.endsWith("/execution")) {
      if (method === "PATCH") {
        const current = session.execution!;
        if (options.switchConflict) return respond({ code: "execution_selection_changed", error: "Changed in another tab", execution: current, agent: session.agent }, 409);
        const provider = current.providers.find((entry) => entry.id === body.provider_id)!;
        const model = provider.models.find((entry) => entry.id === body.model_id)!;
        saveSession({ ...session, phase: "prepared", activeStartId: null, controlRevision: session.controlRevision! + 1, agent: { status: "not_started", phase: "prepared", activeStartId: null }, execution: { ...current, selection: { providerId: provider.id, modelId: model.id, providerLabel: provider.label, modelLabel: model.label, revision: `execution:${calls.filter((call) => call.method === "PATCH").length + 1}` } } });
      }
      return respond({ execution: session.execution, agent: { ...session.agent, phase: session.phase, activeStartId: session.activeStartId, controlRevision: session.controlRevision } });
    }
    if (path.endsWith("/start")) {
      if (options.startFails) return respond({ error: "Provider startup unavailable" }, 503);
      const messages = messagesBySession.get(session.assistantSessionId)!;
      snapshot = body.initialSnapshot;
      if (!startedRequests.has(body.requestId)) {
        if (body.expected_control_revision !== session.controlRevision) return respond({ code: "assistance_control_changed", error: "Assistant lifecycle changed before Start" }, 409);
        startedRequests.add(body.requestId);
        saveSession({ ...session, phase: "active", activeStartId: body.requestId, controlRevision: session.controlRevision! + 1, agent: { status: "running", alive: true, phase: "active", activeStartId: body.requestId } });
        if (options.spawnFails) {
          // Production admits the launch before attempting the provider spawn.
          // A failed spawn returns a successful session response, advances the
          // control revision again, and leaves the admitted attempt fenced.
          const controlRevision = session.controlRevision! + 1;
          saveSession({ ...session, phase: "stopped", controlRevision, agent: { status: "spawn_failed", phase: "stopped", alive: false, started: false, activeStartId: body.requestId, controlRevision, error: spawnFailureMessage } });
        } else {
          if (options.initialGreeting) messages.push({ message_id: `greeting-${body.requestId}`, role: "agent", content: `Let's shape your task: ${snapshot.snapshot.title}. What is the next useful step?`, created_at: "2026-08-25T11:59:00Z" });
          if (options.question) messages.push({ message_id: "question-1", role: "agent", content: "Which task size?", message_type: "question", status: "pending", response_type: "choice", choices: [{ key: "small", label: "Small step" }, { key: "large", label: "Larger task" }] });
        }
      }
      if (options.startFailsOnce && !failedStart) { failedStart = true; throw new Error("Uncertain Start acknowledgement"); }
      const result = session;
      if (options.delayedStartAcknowledgement) await new Promise<void>((resolve) => { releaseStart = resolve; });
      return respond(result);
    }
    if (path.endsWith("/snapshots")) {
      snapshot = body;
      if (options.prepareFailsOnce && !failedPreparation) { failedPreparation = true; throw new Error("Uncertain snapshot acknowledgement"); }
      return respond({ prepared: true });
    }
    if (path.endsWith("/respond")) {
      if (options.respondFailsOnce && !failedRespond) { failedRespond = true; throw new Error("Message delivery failed before acceptance"); }
      const authorizedSession = session;
      const authorizedSnapshot = snapshot;
      const messages = messagesBySession.get(authorizedSession.assistantSessionId)!;
      if (options.delayed) await new Promise<void>((resolve) => { release = resolve; });
      if (body.in_reply_to) {
        const question = messages.find((message) => message.message_id === body.in_reply_to);
        if (question) question.status = "answered";
      }
      messages.push({ message_id: body.message_id, role: "user", content: body.value, created_at: "2026-08-25T12:00:00Z" });
      messages.push({ message_id: "assistant-1", role: "agent", content: "I suggested a title and a summary. You decide when to submit.", created_at: "2026-08-25T12:00:01Z" });
      patch = { protocol: "wb.assisted-draft.patch/v1", assistantSessionId: authorizedSession.assistantSessionId, conversationId: authorizedSession.conversationId, identity: authorizedSession.identity, schema: authorizedSession.schema, baseDraftRevision: authorizedSnapshot.baseDraftRevision, baseSnapshotHash: authorizedSnapshot.baseSnapshotHash, baseSnapshot: authorizedSnapshot.snapshot, patchId: "ap-test", operations: [{ op: "set", path: ["title"], value: "Assistant title" }, { op: "set", path: ["summary"], value: "Assistant summary" }] };
      return respond({ message_id: body.message_id });
    }
    if (/\/conversations\/conversation-test/.test(path)) {
      const boundSession = sessions.get(path.split("/")[3])!;
      if (!boundSession) return respond({ code: "assistance_session_not_found", error: "Session no longer exists" }, 404);
      return respond({ conversation: { conversation_id: path.split("/").pop(), status: "open", agent_alive: boundSession.phase === "active" }, messages: [...messagesBySession.get(boundSession.assistantSessionId)!] });
    }
    if (path.endsWith("/patches")) return respond({ patches: patch && path.includes(`/${patch.assistantSessionId}/`) ? [{ patch, receipt }] : [] });
    if (path.endsWith("/receipts")) {
      if (options.receiptFailsOnce && !failedReceipt) { failedReceipt = true; throw new Error("Receipt acknowledgement unavailable"); }
      receipt = body; return respond(receipt);
    }
    if (path.endsWith("/stop")) {
      if (options.stopFails) throw new Error("Stop offline");
      const parts = path.split("/");
      if (options.stopMissing) { sessions.delete(parts[parts.length - 2]); return respond({ code: "assistance_session_not_found", error: "Session no longer exists" }, 404); }
      const stopping = sessions.get(parts[parts.length - 2])!;
      const applicable = !stoppedRequests.has(body.requestId) && (body.expected_control_revision === stopping.controlRevision || (body.expected_control_revision + 1 === stopping.controlRevision && body.startRequestId === stopping.activeStartId));
      stoppedRequests.add(body.requestId);
      if (applicable && stopping.phase !== "ended" && stopping.phase !== "expired") {
        const next: AssistanceSession = { ...stopping, phase: "stopped", activeStartId: null, controlRevision: stopping.controlRevision! + 1, agent: { status: "stopped", alive: false, phase: "stopped", activeStartId: null } };
        sessions.set(next.assistantSessionId, next);
        if (next.assistantSessionId === session.assistantSessionId) session = next;
      }
      return respond({ stopped: true, outcome: applicable ? "stopped" : "superseded", controlRevision: sessions.get(stopping.assistantSessionId)!.controlRevision });
    }
    if (path.endsWith("/end")) {
      if (options.endFails) throw new Error("Cancellation offline");
      const parts = path.split("/");
      const id = parts[parts.length - 2];
      const ending = sessions.get(id)!;
      if (!ending) return respond({ code: "assistance_session_not_found", error: "Session no longer exists" }, 404);
      sessions.set(id, { ...ending, phase: "ended", activeStartId: null, controlRevision: ending.controlRevision! + 1 });
      if (session.assistantSessionId === id) session = sessions.get(id)!;
      return respond({ ended: true });
    }
    const found = sessions.get(path.split("/").pop()!);
    if (found) {
      if (options.legacyRecovery && found.assistantSessionId === "as-test") return respond({ code: "assistance_restart_required", error: "Legacy session requires a new Start", session: { ...found, protocol: undefined, phase: "restart_required" } }, 409);
      if (found.phase === "ended" || found.phase === "expired") return respond({ code: `assistance_session_${found.phase}`, error: "This session is terminal", session: { ...found, execution: undefined } }, 410);
      return respond(found);
    }
    if (/^\/api\/assistance\/as-test(?:-\d+)?$/.test(path)) return respond({ code: "assistance_session_not_found", error: "Session no longer exists" }, 404);
    throw new Error(`Unexpected test request: ${path}`);
  }) as typeof fetch;
  return { fetchImpl, calls, finish: () => release?.(), finishStart: () => releaseStart?.(), receipt: () => receipt, snapshot: () => snapshot, session: () => session, expire: () => saveSession({ ...session, phase: "expired" }) };
}

function Form({ initialMode = "operate", readOnly = false }: { initialMode?: "operate" | "arrange" | "preview"; readOnly?: boolean }) {
  const [mode, setMode] = useState(initialMode);
  const draft = useWidgetDraft("task-create", initial);
  const assistance = useAssistedDraft("task-create", draft, { interactionMode: mode, readOnly, title: "Shape test draft" });
  return <section aria-label="Task form">
    <label>Task title<input {...assistance.fieldProps(["title"])} value={draft.value.title} onChange={(event) => draft.setValue((current) => ({ ...current, title: event.target.value }))} /></label>
    <label>Task summary<input {...assistance.fieldProps(["summary"])} value={draft.value.summary} onChange={(event) => draft.setValue((current) => ({ ...current, summary: event.target.value }))} /></label>
    <output aria-label="Revision">{draft.revision}</output>
    <button type="button" onClick={() => setMode(mode === "operate" ? "arrange" : "operate")}>Toggle mode</button>
    <button type="button" onClick={() => { void draft.clear(); }}>Clear form</button>
    <button type="button" onClick={() => { void draft.reset({ ...initial, title: "Retained new draft" }, { ifRevision: draft.getSnapshot().revision }); }}>Retain as new draft</button>
    <AssistDraftButton assistance={assistance} />
    <button type="button">Normal human submit</button>
  </section>;
}

function mount(broker: ReturnType<typeof fakeBroker>, options: { initialMode?: "operate" | "arrange" | "preview"; readOnly?: boolean; help?: boolean } = {}, repository = new InMemoryWidgetDraftRepository()) {
  const { help = false, ...formOptions } = options;
  return render(<WidgetDraftRuntimeProvider repository={repository}>
    <AssistedDraftRuntimeProvider fetchImpl={broker.fetchImpl}>
      <DashboardHelpProvider enabled={help}>
        <header aria-label="Page chrome">Tasks</header>
        <AssistedDraftWorkspace viewId="wb.tasks.main">
          <WidgetDraftScopeProvider definition={definition} viewId={asViewId("wb.tasks.main")} instanceId={asWidgetInstanceId("task-create")} input={{}}><Form {...formOptions} /></WidgetDraftScopeProvider>
        </AssistedDraftWorkspace>
      </DashboardHelpProvider>
    </AssistedDraftRuntimeProvider>
  </WidgetDraftRuntimeProvider>);
}

async function openAndStart() {
  await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
  await userEvent.click(screen.getByRole("button", { name: "AI help" }));
  await screen.findByLabelText("Launch disclosure");
  await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
  await userEvent.click(screen.getByRole("button", { name: "Launch" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled());
}

beforeEach(() => {
  vi.stubGlobal("crypto", webcrypto);
  sessionStorage.clear(); localStorage.clear();
  workspaceWidth = 1200;
  // The real resize library uses document-level pointer hit testing. jsdom's
  // all-zero rectangles would put every simulated click on the separator.
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.hasAttribute("data-separator")) return new DOMRect(workspaceWidth * 0.67, 150, 11, 550);
    if (this.hasAttribute("data-group") || this.classList.contains("wb-assistance-workspace__body")) return new DOMRect(0, 150, workspaceWidth, 550);
    return nativeRect.call(this);
  });
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("Dashboard assisted draft host", () => {
  it("uses the actual shell Hover Help toggle for a dock rendered below the root runtime", async () => {
    const broker = fakeBroker();
    render(<WidgetDraftRuntimeProvider repository={new InMemoryWidgetDraftRepository()}>
      <AssistedDraftRuntimeProvider fetchImpl={broker.fetchImpl}>
        <HelpModeProvider><DashboardAnnouncer>
          <header aria-label="Page chrome"><HelpModeToggle /></header>
          <AssistedDraftWorkspace viewId="wb.tasks.main">
            <WidgetDraftScopeProvider definition={definition} viewId={asViewId("wb.tasks.main")} instanceId={asWidgetInstanceId("task-create")} input={{}}><Form /></WidgetDraftScopeProvider>
          </AssistedDraftWorkspace>
        </DashboardAnnouncer></HelpModeProvider>
      </AssistedDraftRuntimeProvider>
    </WidgetDraftRuntimeProvider>);
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    await screen.findByRole("button", { name: "Launch" });
    const heading = screen.getByRole("heading", { name: "Shape test draft" });
    expect(heading).not.toHaveAttribute("data-help-target");
    await userEvent.click(screen.getByRole("button", { name: "Hover help" }));
    expect(screen.getByRole("heading", { name: "Shape test draft" })).toHaveAttribute("data-help-target", "true");
    act(() => screen.getByRole("button", { name: "Launch" }).focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Launch does not send any unsent message.");
    await userEvent.keyboard("{Escape}");
    const separator = screen.getByRole("separator", { name: "Resize the AI help side panel" });
    act(() => separator.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Left and Right arrow keys");
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("complementary", { name: "Draft assistance" })).toBeVisible();
    expect(broker.calls.filter((call) => /\/(start|snapshots|respond)$/.test(call.path))).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "Hover help" }));
    expect(screen.getByRole("heading", { name: "Shape test draft" })).not.toHaveAttribute("data-help-target");
  });

  it("puts Launch in the retained composer instead of a separate Start button or disabled Send", async () => {
    const broker = fakeBroker();
    mount(broker);
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    const launch = await screen.findByRole("button", { name: "Launch" });
    const composer = screen.getByRole("textbox", { name: "Message" });
    expect(launch.closest("form")).toBe(composer.closest("form"));
    expect(launch).toHaveAttribute("type", "button");
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start AI help" })).not.toBeInTheDocument();
    expect(screen.queryByText("Ready for your Start.")).not.toBeInTheDocument();
    expect(within(launch.closest("form")!).getByRole("group", { name: "Launch disclosure" })).toHaveTextContent("Only you submit.");
    await userEvent.click(launch);
    await waitFor(() => expect(composer).toBeEnabled());
    expect(screen.getByRole("textbox", { name: "Message" })).toBe(composer);
    expect(screen.getByRole("button", { name: "Send" }).closest("form")).toBe(composer.closest("form"));
    expect(composer).toHaveFocus();
    expect(broker.calls.filter((call) => call.path.endsWith("/start"))).toHaveLength(1);
    expect(broker.calls.filter((call) => /\/(snapshots|respond)$/.test(call.path))).toHaveLength(0);
  });

  it("keeps compact panes mounted and restores focus only after the form is visible", async () => {
    workspaceWidth = 600;
    const broker = fakeBroker();
    mount(broker);
    const trigger = screen.getByRole("button", { name: "AI help" });
    const form = screen.getByRole("region", { name: "Task form" });
    await waitFor(() => expect(trigger).toBeEnabled());
    await userEvent.click(trigger);
    const heading = await screen.findByRole("heading", { name: "Shape test draft" });
    expect(heading).toHaveFocus();
    const composer = await screen.findByRole("textbox", { name: "Message" });
    expect(form).not.toBeVisible();
    await userEvent.click(screen.getByRole("radio", { name: "Form" }));
    expect(screen.getByRole("region", { name: "Task form" })).toBe(form);
    await userEvent.type(screen.getByRole("textbox", { name: "Task title" }), " retained");
    expect(screen.getByRole("button", { name: "Normal human submit" })).toBeEnabled();
    await userEvent.click(screen.getByRole("radio", { name: "AI help" }));
    expect(screen.getByRole("textbox", { name: "Message" })).toBe(composer);
    await userEvent.click(screen.getByRole("button", { name: "Close assistance" }));
    expect(form).toBeVisible();
    expect(trigger).toHaveFocus();
    expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Original title retained");
    expect(broker.calls.filter((call) => /\/(start|stop|respond)$/.test(call.path))).toHaveLength(0);
  });

  it("preserves form focus and the live conversation through compact and wide resizing", async () => {
    const broker = fakeBroker();
    mount(broker);
    await openAndStart();
    const title = screen.getByRole("textbox", { name: "Task title" });
    const composer = screen.getByRole("textbox", { name: "Message" });
    await userEvent.type(composer, "Retained unsent question");
    await userEvent.click(title);
    workspaceWidth = 600;
    act(() => window.dispatchEvent(new Event("resize")));
    await waitFor(() => expect(screen.getByRole("radio", { name: "Form" })).toBeChecked());
    expect(title).toHaveFocus();
    expect(title).toBeVisible();
    expect(composer).not.toBeVisible();
    await userEvent.click(screen.getByRole("radio", { name: "AI help" }));
    expect(screen.getByRole("textbox", { name: "Message" })).toBe(composer);
    expect(composer).toHaveValue("Retained unsent question");
    workspaceWidth = 1200;
    act(() => window.dispatchEvent(new Event("resize")));
    await waitFor(() => expect(screen.queryByRole("radio", { name: "AI help" })).not.toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "Shape test draft" })).toHaveFocus();
    expect(screen.getByRole("textbox", { name: "Task title" })).toBe(title);
    expect(screen.getByRole("textbox", { name: "Message" })).toBe(composer);
    expect(broker.calls.filter((call) => call.path.endsWith("/start"))).toHaveLength(1);
    expect(broker.calls.filter((call) => /\/(stop|respond)$/.test(call.path))).toHaveLength(0);
  });

  it("grows the workspace minimum for retained composer and expanded controls, not transcript length", async () => {
    workspaceWidth = 390;
    vi.stubGlobal("innerHeight", 420);
    let composerHeight = 200;
    let headerHeight = 40;
    let transcriptHeight = 80;
    const originalMeasurement = vi.mocked(HTMLElement.prototype.getBoundingClientRect).getMockImplementation()!;
    vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function (this: HTMLElement) {
      if (this.classList.contains("wb-assistance-dock")) return new DOMRect(0, 169, 390, 384);
      if (this.parentElement?.classList.contains("wb-assistance-dock") && this.tagName === "HEADER") return new DOMRect(0, 0, 366, 40);
      if (this.classList.contains("wb-chat-panel__header")) return new DOMRect(0, 0, 366, headerHeight);
      if (this.classList.contains("wb-chat-composer")) return new DOMRect(0, 0, 366, composerHeight);
      if (this.classList.contains("wb-chat-list")) return new DOMRect(0, 0, 366, transcriptHeight);
      return originalMeasurement.call(this);
    });
    const originalStyle = globalThis.getComputedStyle.bind(globalThis);
    vi.spyOn(globalThis, "getComputedStyle").mockImplementation((element, pseudo) => {
      const style = originalStyle(element, pseudo);
      return new Proxy(style, {
        get(target, property) {
          const dock = element.classList.contains("wb-assistance-dock");
          const scroll = element.classList.contains("wb-chat-list__scroll");
          if (property === "paddingTop" || property === "paddingBottom") return dock ? "12px" : scroll ? "8px" : "0px";
          if (property === "borderTopWidth" || property === "borderBottomWidth") return dock || scroll ? "1px" : "0px";
          if (property === "marginTop" || property === "marginBottom") return "0px";
          if (property === "rowGap") return dock || element.classList.contains("wb-chat-panel") ? "12px" : "0px";
          if (property === "fontSize") return "16px";
          if (property === "lineHeight") return "20px";
          const value = Reflect.get(target, property, target);
          return typeof value === "function" ? value.bind(target) : value;
        },
      });
    });
    const broker = fakeBroker();
    mount(broker);
    await openAndStart();
    const composer = screen.getByRole("textbox", { name: "Message" });
    const workspace = composer.closest(".wb-assistance-workspace__body") as HTMLElement;
    await waitFor(() => expect(workspace.style.getPropertyValue("--wb-assistance-workspace-content-minimum")).toBe("420px"));
    await userEvent.type(composer, "one{Shift>}{Enter}{/Shift}two{Shift>}{Enter}{/Shift}three{Shift>}{Enter}{/Shift}four{Shift>}{Enter}{/Shift}five{Shift>}{Enter}{/Shift}six");
    composerHeight = 320;
    await userEvent.click(screen.getByRole("button", { name: "Stop assistant" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
    headerHeight = 80;
    await userEvent.click(screen.getByText("Session actions"));
    await waitFor(() => expect(workspace.style.getPropertyValue("--wb-assistance-workspace-content-minimum")).toBe("580px"));
    expect(screen.getByRole("textbox", { name: "Message" })).toBe(composer);
    expect(composer).toHaveValue("one\ntwo\nthree\nfour\nfive\nsix");

    transcriptHeight = 4000;
    act(() => window.dispatchEvent(new Event("resize")));
    await waitFor(() => expect(workspace.style.getPropertyValue("--wb-assistance-workspace-content-minimum")).toBe("580px"));
    headerHeight = 40;
    composerHeight = 200;
    vi.stubGlobal("innerHeight", 900);
    act(() => window.dispatchEvent(new Event("resize")));
    await waitFor(() => expect(workspace.style.getPropertyValue("--wb-assistance-workspace-content-minimum")).toBe("420px"));
    expect(workspace.style.getPropertyValue("--wb-assistance-workspace-height")).toBe("734px");
    expect(broker.calls.filter((call) => call.path.endsWith("/start"))).toHaveLength(1);
    expect(broker.calls.filter((call) => call.path.endsWith("/respond"))).toHaveLength(0);
  });

  it("includes natural loading and error recovery content in the workspace minimum", async () => {
    workspaceWidth = 390;
    vi.stubGlobal("innerHeight", 420);
    let copyHeight = 420;
    const originalMeasurement = vi.mocked(HTMLElement.prototype.getBoundingClientRect).getMockImplementation()!;
    vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function (this: HTMLElement) {
      if (this.classList.contains("wb-assistance-dock")) return new DOMRect(0, 169, 390, 384);
      if (this.parentElement?.classList.contains("wb-assistance-dock") && this.tagName === "HEADER") return new DOMRect(0, 0, 366, 40);
      if (this.classList.contains("wb-chat-state")) return new DOMRect(0, 0, 366, 100);
      if (this.classList.contains("wb-chat-state__copy")) return new DOMRect(0, 0, 366, copyHeight);
      if (this.classList.contains("wb-chat-state__action")) return new DOMRect(0, 0, 200, 40);
      return originalMeasurement.call(this);
    });
    const originalStyle = globalThis.getComputedStyle.bind(globalThis);
    vi.spyOn(globalThis, "getComputedStyle").mockImplementation((element, pseudo) => {
      const style = originalStyle(element, pseudo);
      return new Proxy(style, {
        get(target, property) {
          const dock = element.classList.contains("wb-assistance-dock");
          const state = element.classList.contains("wb-chat-state");
          if (property === "paddingTop" || property === "paddingBottom") return dock ? "12px" : state ? "16px" : "0px";
          if (property === "borderTopWidth" || property === "borderBottomWidth") return dock ? "1px" : "0px";
          if (property === "marginTop" || property === "marginBottom") return "0px";
          if (property === "rowGap") return dock || state ? "12px" : "0px";
          if (property === "minHeight" && state) return "128px";
          const value = Reflect.get(target, property, target);
          return typeof value === "function" ? value.bind(target) : value;
        },
      });
    });
    let rejectAvailability: ((error: Error) => void) | undefined;
    const pending = new Promise<Response>((_resolve, reject) => { rejectAvailability = reject; });
    const broker = fakeBroker();
    vi.mocked(broker.fetchImpl).mockImplementation(() => pending);
    mount(broker);
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    expect(await screen.findByText("Preparing AI help…")).toBeVisible();
    const workspace = screen.getByRole("complementary", { name: "Draft assistance" }).closest(".wb-assistance-workspace__body") as HTMLElement;
    await waitFor(() => expect(workspace.style.getPropertyValue("--wb-assistance-workspace-content-minimum")).toBe("530px"));

    copyHeight = 580;
    act(() => rejectAvailability?.(new Error("Assistance is temporarily unavailable. ".repeat(20))));
    expect(await screen.findByRole("button", { name: "Retry availability" })).toBeEnabled();
    await waitFor(() => expect(workspace.style.getPropertyValue("--wb-assistance-workspace-content-minimum")).toBe("742px"));
    expect(screen.queryByRole("button", { name: "Launch" })).not.toBeInTheDocument();
  });

  it("renders a dock only in the workspace outlet that registered the full draft binding", async () => {
    const broker = fakeBroker();
    render(<WidgetDraftRuntimeProvider repository={new InMemoryWidgetDraftRepository()}>
      <AssistedDraftRuntimeProvider fetchImpl={broker.fetchImpl}>
        {["wb.tasks.main", "wb.jobs.authoring"].map((viewId) => <section key={viewId} aria-label={viewId}>
          <AssistedDraftWorkspace viewId={viewId}>
            <WidgetDraftScopeProvider definition={definition} viewId={asViewId(viewId)} instanceId={asWidgetInstanceId("task-create")} input={{}}><Form /></WidgetDraftScopeProvider>
          </AssistedDraftWorkspace>
        </section>)}
      </AssistedDraftRuntimeProvider>
    </WidgetDraftRuntimeProvider>);
    const first = within(screen.getByRole("region", { name: "wb.tasks.main" }));
    const other = within(screen.getByRole("region", { name: "wb.jobs.authoring" }));
    await waitFor(() => expect(first.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(first.getByRole("button", { name: "AI help" }));
    expect(await first.findByRole("button", { name: "Launch" })).toBeEnabled();
    expect(other.queryByRole("complementary", { name: "Draft assistance" })).not.toBeInTheDocument();
    expect(broker.calls.find((call) => call.path.endsWith("/sessions"))?.body?.identity).toMatchObject({ viewId: "wb.tasks.main", draftName: "task-create" });
  });

  it("puts drafting guidance on the shared button without starting assistance on focus", async () => {
    const broker = fakeBroker();
    mount(broker, { help: true });
    const assist = screen.getByRole("button", { name: "AI help" });
    await waitFor(() => expect(assist).toBeEnabled());
    expect(assist.querySelector('svg [opacity="0.2"]')).not.toBeNull();
    expect(screen.queryByText(/The assistant fills these fields/)).not.toBeInTheDocument();
    act(() => assist.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("The assistant fills these fields. You can edit or undo its suggestions; only you can submit the form.");
    expect(broker.calls).toHaveLength(0);
    await userEvent.click(assist);
    await waitFor(() => expect(screen.getByText(/Launch shares this form's allowed fields and recent chat with/)).toBeVisible());
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
    const disclosure = screen.getByText(/Launch shares this form's allowed fields and recent chat with/);
    await userEvent.hover(disclosure);
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent(availability.disclosure));
    await userEvent.unhover(disclosure);
    expect(screen.getByRole("complementary", { name: "Draft assistance" }).id).toBe(assist.getAttribute("aria-controls"));
    expect(broker.calls.filter((call) => /\/(start|snapshots|respond)$/.test(call.path))).toHaveLength(0);
    expect(broker.calls.find((call) => call.path === "/api/assistance/sessions")?.body).toEqual({ requestId: expect.any(String), identity: broker.session().identity, schema: declaration.schema, interactionMode: "operate", readOnly: false });
    const heading = screen.getByRole("heading", { name: "Shape test draft" });
    expect(heading).toHaveAttribute("data-help-target", "true");
    const close = screen.getByRole("button", { name: "Close assistance" });
    act(() => close.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Closing this panel keeps your draft and conversation. Reopen AI help to continue.");
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("complementary", { name: "Draft assistance" })).toBeVisible();
    await userEvent.click(close);
    expect(screen.queryByRole("complementary", { name: "Draft assistance" })).not.toBeInTheDocument();
    expect(assist).toHaveFocus();
  });

  it("keeps disclosure visible at Start but puts session mechanics on the relevant controls", async () => {
    const broker = fakeBroker();
    mount(broker, { help: true });
    await openAndStart();
    expect(broker.calls.find((call) => call.path.endsWith("/start"))?.body).toMatchObject({ disclosureAccepted: true, provider_id: availability.providerId, model_id: availability.modelId });
    expect(screen.queryByText("Bound to this form. Only you can submit it.")).not.toBeInTheDocument();
    expect(screen.queryByText("Send another message to resume. Closing this panel keeps your draft and conversation.")).not.toBeInTheDocument();
    const stop = screen.getByRole("button", { name: "Stop assistant" });
    act(() => stop.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Stopping preserves the draft and conversation. A fresh explicit Launch is required before the assistant receives more content.");
    expect(broker.calls.some((call) => call.path.endsWith("/stop"))).toBe(false);
    await userEvent.click(stop);
    await waitFor(() => expect(broker.calls.some((call) => call.path.endsWith("/stop"))).toBe(true));
    expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Original title");
    expect(screen.getByRole("button", { name: "Normal human submit" })).toBeEnabled();
  });

  it("reserves a sibling layout region without remounting or hiding the form", async () => {
    mount(fakeBroker());
    const form = screen.getByRole("region", { name: "Task form" });
    const hostContent = form.closest(".wb-assistance-workspace__form");
    const host = form.closest(".wb-assistance-workspace");
    expect(host).not.toHaveAttribute("data-assistance-open");
    await openAndStart();
    const dock = screen.getByRole("complementary", { name: "Draft assistance" });
    expect(host).toHaveAttribute("data-assistance-open", "true");
    expect(host?.contains(dock)).toBe(true);
    expect(hostContent?.contains(dock)).toBe(false);
    expect(host?.contains(screen.getByRole("banner", { name: "Page chrome" }))).toBe(false);
    expect(screen.getByRole("separator", { name: "Resize the AI help side panel" })).toBeVisible();
    expect(screen.getByRole("region", { name: "Task form" })).toBe(form);
    expect(screen.getByRole("button", { name: "Normal human submit" })).toBeEnabled();
    await userEvent.click(screen.getByRole("textbox", { name: "Task title" }));
    expect(screen.getByRole("textbox", { name: "Task title" })).toHaveFocus();
    await userEvent.click(screen.getByRole("button", { name: "Close assistance" }));
    expect(host).not.toHaveAttribute("data-assistance-open");
    expect(screen.getByRole("region", { name: "Task form" })).toBe(form);
  });

  it("starts only on explicit gesture, fills actual controls, and conditionally undoes", async () => {
    const broker = fakeBroker();
    mount(broker);
    expect(broker.calls).toHaveLength(0);
    await openAndStart();
    expect(broker.calls.filter((call) => call.path.endsWith("/respond"))).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Normal human submit" })).toBeEnabled();
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Help shape this");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Assistant title"));
    expect(screen.getByRole("textbox", { name: "Task summary" })).toHaveValue("Assistant summary");
    expect(screen.getByRole("textbox", { name: "Task title" })).toHaveAttribute("data-assisted-state", "applied");
    expect(broker.snapshot().snapshot).toEqual({ title: "Original title", summary: "", next_action: "" });
    await waitFor(() => expect(broker.receipt()?.status).toBe("applied"));
    const title = screen.getByRole("textbox", { name: "Task title" });
    await userEvent.clear(title);
    await userEvent.type(title, "My final title");
    await userEvent.click(screen.getByRole("button", { name: "Undo assistant changes" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Task summary" })).toHaveValue(""));
    expect(title).toHaveValue("My final title");
    expect(broker.calls.some((call) => /\/api\/(tasks|jobs)(?:\/|$)/.test(call.path))).toBe(false);
  });

  it("keeps a focused human field unchanged while applying other fields without focus theft", async () => {
    const broker = fakeBroker({ delayed: true });
    mount(broker);
    await openAndStart();
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Suggest fields");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(broker.calls.some((call) => call.path.endsWith("/respond"))).toBe(true));
    const title = screen.getByRole("textbox", { name: "Task title" });
    await userEvent.click(title);
    await userEvent.type(title, " edited");
    await act(async () => { broker.finish(); });
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Task summary" })).toHaveValue("Assistant summary"));
    expect(title).toHaveValue("Original title edited");
    expect(title).toHaveFocus();
    await waitFor(() => expect(broker.receipt()?.status).toBe("partial"));
    expect(broker.receipt()?.pendingFields).toEqual([{ path: ["title"], reason: "focused" }]);
    await userEvent.click(screen.getByRole("button", { name: "Apply suggestions" }));
    await waitFor(() => expect(title).toHaveValue("Assistant title"));
  });

  it("starts contextually from allowlisted form fields without inventing a human turn", async () => {
    const broker = fakeBroker({ initialGreeting: true });
    mount(broker);
    await openAndStart();
    expect(await screen.findByText("Let's shape your task: Original title. What is the next useful step?")).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveFocus();
    const start = broker.calls.find((call) => call.path.endsWith("/start"))!.body!;
    expect(start).toEqual({
      requestId: expect.any(String), disclosureAccepted: true,
      provider_id: "fixture-provider", model_id: "fixture-model", expected_revision: "execution:1", expected_control_revision: 0,
      initialSnapshot: { messageId: expect.any(String), baseDraftRevision: expect.any(Number), baseSnapshotHash: expect.stringMatching(/^[a-f0-9]{64}$/), snapshot: { title: "Original title", summary: "", next_action: "" } },
    });
    expect(JSON.stringify(start)).not.toMatch(/proposal_ref|proposal_pending|never-disclose|batch_lines/);
    expect(broker.calls.filter((call) => /\/(snapshots|respond)$/.test(call.path))).toHaveLength(0);
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("");
  });

  it("lets an opted-in user select an available model before Start when the default is offline", async () => {
    const broker = fakeBroker({ selectedUnavailable: true });
    mount(broker);
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    const picker = await screen.findByRole("button", { name: "Run with Fixture Claude · Model one" });
    await waitFor(() => expect(picker).toBeEnabled());
    expect(screen.getByText("Choose an available model to launch.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Launch" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    await userEvent.click(picker);
    expect(screen.getByRole("option", { name: /Fixture Claude, Model one/ })).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(screen.getByRole("option", { name: "Fixture Codex, Model two" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
    expect(broker.calls.find((call) => call.method === "PATCH")).toEqual({ path: "/api/assistance/sessions/as-test/execution", method: "PATCH", body: { provider_id: "fixture-codex", model_id: "fixture-model-two", expected_revision: "execution:1" } });
    expect(vi.mocked(exactHumanAuthorityHeaders)).toHaveBeenCalledWith(expect.objectContaining({ action: "dashboard.assistance.execution_select", context: expect.objectContaining({ method: "PATCH" }) }), broker.fetchImpl);
    expect(broker.calls.filter((call) => call.path.endsWith("/start"))).toHaveLength(0);
    expect(broker.calls.some((call) => /settings|config/.test(call.path))).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Launch" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled());
    expect(broker.calls.find((call) => call.path.endsWith("/start"))?.body).toMatchObject({ provider_id: "fixture-codex", model_id: "fixture-model-two", expected_revision: "execution:2" });
  });

  it("switches the active model without replacing the canonical chat or silently sharing more fields", async () => {
    const subscribe = vi.spyOn(HttpChatConversationProvider.prototype, "subscribe");
    const broker = fakeBroker();
    mount(broker);
    await openAndStart();
    const composer = screen.getByRole("textbox", { name: "Message" });
    await userEvent.type(composer, "Fill before switching");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(broker.receipt()?.status).toBe("applied"));
    await userEvent.type(composer, "Unsent question for later");
    await userEvent.click(screen.getByRole("button", { name: "Run with Fixture Claude · Model one" }));
    await userEvent.click(screen.getByRole("option", { name: "Fixture Codex, Model two" }));
    expect(screen.getByRole("dialog", { name: "Switch to Fixture Codex · Model two?" })).toHaveTextContent("choose Launch before the new model receives any content");
    await userEvent.click(screen.getByRole("button", { name: "Switch model" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
    expect(screen.getByRole("textbox", { name: "Message" })).toBe(composer);
    expect(composer).toHaveValue("Unsent question for later");
    expect(composer).toBeDisabled();
    expect(subscribe).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Fill before switching")).toBeVisible();
    expect(screen.getByRole("button", { name: "Undo assistant changes" })).toBeEnabled();
    expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Assistant title");
    await userEvent.click(screen.getByRole("button", { name: "Close assistance" }));
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    expect(broker.calls.filter((call) => call.path.endsWith("/start"))).toHaveLength(1);
    await userEvent.type(screen.getByRole("textbox", { name: "Task title" }), " after switch");
    await userEvent.click(screen.getByRole("button", { name: "Launch" }));
    await waitFor(() => expect(composer).toBeEnabled());
    const attempts = broker.calls.filter((call) => call.path.endsWith("/start"));
    expect(attempts).toHaveLength(2);
    expect(attempts[1].body?.requestId).not.toBe(attempts[0].body?.requestId);
    expect(attempts[1].body).toMatchObject({ provider_id: "fixture-codex", initialSnapshot: { snapshot: { title: "Assistant title after switch" } } });
    expect(broker.calls.filter((call) => call.path.endsWith("/respond"))).toHaveLength(1);
    expect(composer).toHaveValue("Unsent question for later");
    expect(subscribe).toHaveBeenCalledTimes(1);
  });

  it("preserves the form, unsent text and Undo after an admitted spawn failure and requires a fresh Launch", async () => {
    const subscribe = vi.spyOn(HttpChatConversationProvider.prototype, "subscribe");
    const options = { spawnFails: false };
    const broker = fakeBroker(options);
    mount(broker);
    await openAndStart();
    const title = screen.getByRole("textbox", { name: "Task title" });
    const summary = screen.getByRole("textbox", { name: "Task summary" });
    const composer = screen.getByRole("textbox", { name: "Message" });
    await userEvent.type(composer, "Suggest fields before this launch fails");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(broker.receipt()?.status).toBe("applied"));
    await waitFor(() => expect(composer).toHaveValue(""));
    await userEvent.type(composer, "Keep this unsent clarification");
    await userEvent.click(screen.getByRole("button", { name: "Stop assistant" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
    const previousControlRevision = broker.session().controlRevision!;

    options.spawnFails = true;
    await userEvent.click(screen.getByRole("button", { name: "Launch" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(spawnFailureMessage);
    expect(alert).not.toHaveTextContent("Start");
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
    const failedAttempt = broker.calls.filter((call) => call.path.endsWith("/start"))[1].body!;
    const failedCallIndex = broker.calls.findIndex((call) => call.path.endsWith("/start") && call.body?.requestId === failedAttempt.requestId);
    const response = await vi.mocked(broker.fetchImpl).mock.results[failedCallIndex].value as Response;
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({
      phase: "stopped", activeStartId: failedAttempt.requestId, controlRevision: previousControlRevision + 2,
      agent: { status: "spawn_failed", phase: "stopped", alive: false, started: false, controlRevision: previousControlRevision + 2, error: spawnFailureMessage },
    });
    expect(failedAttempt.expected_control_revision).toBe(previousControlRevision);
    expect(title).toHaveValue("Assistant title");
    expect(summary).toHaveValue("Assistant summary");
    expect(composer).toBeDisabled();
    expect(composer).toHaveValue("Keep this unsent clarification");
    expect(screen.getByRole("button", { name: "Normal human submit" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Undo assistant changes" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Retry Launch" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Launch with current fields" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop assistant" })).not.toBeInTheDocument();
    expect(Object.keys(sessionStorage).filter((key) => key.startsWith("wb.assistance.start:"))).toHaveLength(0);

    // Reopening and authoritative transcript/session refresh are passive:
    // neither may replay the failed attempt or create a new provider launch.
    await userEvent.click(screen.getByRole("button", { name: "Close assistance" }));
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    const reads = broker.calls.filter((call) => call.path === "/api/assistance/as-test").length;
    await act(async () => { (subscribe.mock.contexts[0] as HttpChatConversationProvider).invalidate(); });
    await waitFor(() => expect(broker.calls.filter((call) => call.path === "/api/assistance/as-test").length).toBeGreaterThan(reads));
    expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled();
    expect(screen.getByRole("textbox", { name: "Task title" })).toBe(title);
    expect(screen.getByRole("textbox", { name: "Message" })).toBe(composer);
    expect(composer).toHaveValue("Keep this unsent clarification");
    expect(screen.getByText("Suggest fields before this launch fails")).toBeVisible();
    expect(broker.calls.filter((call) => call.path.endsWith("/start"))).toHaveLength(2);
    expect(subscribe).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Undo assistant changes" }));
    await waitFor(() => expect(broker.receipt()?.status).toBe("undone"));
    expect(title).toHaveValue("Original title");
    expect(summary).toHaveValue("");
    expect(composer).toHaveValue("Keep this unsent clarification");

    await userEvent.type(title, " after the failed launch");
    options.spawnFails = false;
    await userEvent.click(screen.getByRole("button", { name: "Launch" }));
    await waitFor(() => expect(composer).toBeEnabled());
    const attempts = broker.calls.filter((call) => call.path.endsWith("/start"));
    expect(attempts).toHaveLength(3);
    expect(attempts[2].body?.requestId).not.toBe(failedAttempt.requestId);
    expect(attempts[2].body).toMatchObject({
      expected_control_revision: previousControlRevision + 2,
      initialSnapshot: { messageId: expect.any(String), snapshot: { title: "Original title after the failed launch", summary: "" } },
    });
    expect((attempts[2].body?.initialSnapshot as PreparedDraftSnapshot).messageId).not.toBe((failedAttempt.initialSnapshot as PreparedDraftSnapshot).messageId);
    expect(composer).toHaveValue("Keep this unsent clarification");
    expect(screen.queryByText(spawnFailureMessage)).not.toBeInTheDocument();
    expect(broker.calls.filter((call) => call.path.endsWith("/respond"))).toHaveLength(1);
    expect(broker.calls.filter((call) => call.path === "/api/assistance/sessions")).toHaveLength(1);
    expect(broker.calls.some((call) => /\/api\/(tasks|jobs)(?:\/|$)/.test(call.path))).toBe(false);
  });

  it("retries an uncertain Start with the exact frozen identity and fields", async () => {
    const broker = fakeBroker({ startFailsOnce: true });
    mount(broker);
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Launch" }));
    await screen.findByText("Uncertain Start acknowledgement");
    await userEvent.type(screen.getByRole("textbox", { name: "Task title" }), " later private edit");
    await userEvent.click(screen.getByRole("button", { name: "Retry Launch" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled());
    const attempts = broker.calls.filter((call) => call.path.endsWith("/start"));
    expect(attempts).toHaveLength(2);
    expect(attempts[0].body).toEqual(attempts[1].body);
    expect(broker.snapshot().snapshot.title).toBe("Original title");
    expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Original title later private edit");
  });

  it("keeps an ambiguous Start retry after an authoritative refresh with the same non-active control revision", async () => {
    const subscribe = vi.spyOn(HttpChatConversationProvider.prototype, "subscribe");
    const options = { startFails: true };
    const broker = fakeBroker(options);
    mount(broker);
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Launch" }));
    await screen.findByText("Provider startup unavailable");
    await userEvent.type(screen.getByRole("textbox", { name: "Task title" }), " later private edit");
    const reads = broker.calls.filter((call) => call.path === "/api/assistance/as-test").length;
    await act(async () => { (subscribe.mock.contexts[0] as HttpChatConversationProvider).invalidate(); });
    await waitFor(() => expect(broker.calls.filter((call) => call.path === "/api/assistance/as-test").length).toBeGreaterThan(reads));
    expect(broker.session()).toMatchObject({ phase: "prepared", controlRevision: 0 });
    expect(screen.getByRole("button", { name: "Retry Launch" })).toBeEnabled();
    options.startFails = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry Launch" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled());
    const attempts = broker.calls.filter((call) => call.path.endsWith("/start"));
    expect(attempts).toHaveLength(2);
    expect(attempts[1].body).toEqual(attempts[0].body);
    expect(broker.snapshot().snapshot.title).toBe("Original title");
  });

  it("keeps the frozen launch disclosure truthful while a retry acknowledgement is pending", async () => {
    const options = { startFails: true, delayedStartAcknowledgement: true };
    const broker = fakeBroker(options);
    mount(broker);
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    await userEvent.click(await screen.findByRole("button", { name: "Launch" }));
    await screen.findByText("Provider startup unavailable");
    await userEvent.type(screen.getByRole("textbox", { name: "Task title" }), " private later edit");
    options.startFails = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry Launch" }));
    await waitFor(() => expect(broker.calls.filter((call) => call.path.endsWith("/start"))).toHaveLength(2));
    expect(screen.getByRole("group", { name: "Launch disclosure" })).toHaveTextContent("Uses the fields and recent chat approved for this launch with");
    expect(screen.queryByText(/Launch shares this form's allowed fields/)).not.toBeInTheDocument();
    const attempts = broker.calls.filter((call) => call.path.endsWith("/start"));
    expect(attempts[1].body).toEqual(attempts[0].body);
    expect(broker.snapshot().snapshot.title).toBe("Original title");
    await act(async () => { broker.finishStart(); });
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled());
    expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Original title private later edit");
  });

  it("routes a canonical structured answer through the same snapshot and in_reply_to protocol", async () => {
    const broker = fakeBroker({ question: true });
    mount(broker);
    await openAndStart();
    await userEvent.click(await screen.findByRole("button", { name: "Small step" }));
    await waitFor(() => expect(broker.calls.filter((call) => call.path.endsWith("/respond"))).toHaveLength(1));
    const sent = broker.calls.find((call) => call.path.endsWith("/respond"))!.body!;
    expect(sent).toEqual({ value: "small", in_reply_to: "question-1", message_id: expect.stringMatching(/^chat-user-/) });
    expect(broker.snapshot().messageId).toBe(sent.message_id);
    expect(broker.snapshot().snapshot).toEqual({ title: "Original title", summary: "", next_action: "" });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Small step" })).not.toBeInTheDocument());
  });

  it("tombstones a reset before failed cancellation and retries without restoring the old binding", async () => {
    const options = { endFails: true };
    const broker = fakeBroker(options);
    mount(broker);
    await openAndStart();
    await userEvent.click(screen.getByRole("button", { name: "Clear form" }));
    expect(localStorage.getItem("wb.assistance.ended:as-test")).toBe("ended");
    await screen.findByText(/Assistant cancellation is not confirmed yet/);
    expect(Object.keys(sessionStorage).filter((key) => key.startsWith("wb.assistance.binding:"))).toHaveLength(0);
    expect(localStorage.getItem("wb.assistance.revocation/v1:as-test")).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
    expect(broker.session().assistantSessionId).toBe("as-test-2");
    expect(broker.calls.filter((call) => call.path.endsWith("/start"))).toHaveLength(1);
    options.endFails = false;
    await act(async () => { window.dispatchEvent(new Event("online")); });
    await waitFor(() => expect(screen.queryByText(/Assistant cancellation is not confirmed yet/)).not.toBeInTheDocument());
    expect(localStorage.getItem("wb.assistance.revocation/v1:as-test")).toBeNull();
    expect(localStorage.getItem("wb.assistance.ended:as-test")).toBe("ended");
    expect(broker.session().assistantSessionId).toBe("as-test-2");
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
  });

  it("keeps legacy transcript and Undo inspectable until an explicit separate-session cutover", async () => {
    const options = { legacyRecovery: false };
    const broker = fakeBroker(options);
    const repository = new InMemoryWidgetDraftRepository();
    const first = mount(broker, {}, repository);
    await openAndStart();
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Legacy conversation text");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(broker.receipt()?.status).toBe("applied"));
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Not sent before migration");
    first.unmount();
    options.legacyRecovery = true;
    mount(broker, {}, repository);
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    await screen.findByText(/This conversation used the previous assistant/);
    await waitFor(() => expect(screen.getByText("Legacy conversation text")).toBeVisible());
    expect(screen.getByRole("button", { name: "Undo assistant changes" })).toBeEnabled();
    expect(broker.calls.filter((call) => call.path === "/api/assistance/sessions")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "New AI help session" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("Not sent before migration");
    expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Assistant title");
    expect(broker.calls.filter((call) => call.path.endsWith("/start"))).toHaveLength(1);
    expect(broker.calls.filter((call) => call.path === "/api/assistance/sessions")[1].body).not.toHaveProperty("previousSessionId");
    await userEvent.click(screen.getByText("Previous AI help conversations"));
    const history = await screen.findByRole("region", { name: "Previous AI help conversation" });
    expect(await within(history).findByText("Legacy conversation text")).toBeVisible();
    expect(within(history).queryByRole("textbox", { name: "Message" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Undo assistant changes" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Original title"));
  });

  it.each([{ initialMode: "arrange" as const }, { initialMode: "preview" as const }, { readOnly: true }])("is inert outside editable Operate mode %#", async (options) => {
    const broker = fakeBroker();
    mount(broker, options);
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Original title"));
    expect(screen.getByRole("button", { name: "AI help" })).toBeDisabled();
    expect(broker.calls).toHaveLength(0);
  });

  it("freezes Start before asynchronous preparation without taking focus back from the form", async () => {
    const broker = fakeBroker();
    mount(broker);
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
    const hash = assistanceSchema.snapshotHash;
    let releaseHash: (() => void) | undefined;
    vi.spyOn(assistanceSchema, "snapshotHash").mockImplementationOnce((snapshot) => new Promise((resolve) => { releaseHash = () => { void hash(snapshot).then(resolve); }; }));
    await userEvent.click(screen.getByRole("button", { name: "Launch" }));
    const title = screen.getByRole("textbox", { name: "Task title" });
    await userEvent.type(title, " still private");
    await act(async () => { releaseHash?.(); });
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled());
    expect(title).toHaveFocus();
    expect(title).toHaveValue("Original title still private");
    expect(broker.snapshot().snapshot.title).toBe("Original title");
  });

  it("reloads typed ended-session recovery with readable transcript, draft and Undo", async () => {
    const broker = fakeBroker();
    const repository = new InMemoryWidgetDraftRepository();
    const first = mount(broker, {}, repository);
    await openAndStart();
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Keep this ended history");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(broker.receipt()?.status).toBe("applied"));
    await userEvent.click(screen.getByText("Session actions"));
    await userEvent.click(screen.getByRole("button", { name: "End session and keep draft" }));
    await screen.findByRole("button", { name: "New AI help session" });
    first.unmount();
    mount(broker, {}, repository);
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    expect(await screen.findByRole("button", { name: "New AI help session" })).toBeEnabled();
    await waitFor(() => expect(screen.getByText("Keep this ended history")).toBeVisible());
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    expect(broker.calls.filter((call) => call.path.endsWith("/start"))).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Undo assistant changes" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Original title"));
  });

  it("projects a real-shaped expiry while active as readable history with explicit new-session recovery", async () => {
    const subscribe = vi.spyOn(HttpChatConversationProvider.prototype, "subscribe");
    const broker = fakeBroker({ initialGreeting: true });
    mount(broker);
    await openAndStart();
    await screen.findByText("Let's shape your task: Original title. What is the next useful step?");
    await act(async () => {
      broker.expire();
      (subscribe.mock.contexts[0] as HttpChatConversationProvider).invalidate();
    });
    expect(await screen.findByRole("button", { name: "New AI help session" })).toBeEnabled();
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    expect(screen.getByText("Let's shape your task: Original title. What is the next useful step?")).toBeVisible();
    expect(screen.getByRole("button", { name: "Normal human submit" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Retry availability" })).not.toBeInTheDocument();
    expect(broker.calls.filter((call) => call.path.endsWith("/start"))).toHaveLength(1);
  });

  it("creates fresh send identity after a failed prepared send followed by model switch and Start", async () => {
    const broker = fakeBroker({ respondFailsOnce: true });
    mount(broker);
    await openAndStart();
    const composer = screen.getByRole("textbox", { name: "Message" });
    await userEvent.type(composer, "Retry this authored text");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Message delivery failed before acceptance");
    await userEvent.click(screen.getByRole("button", { name: "Run with Fixture Claude · Model one" }));
    await userEvent.click(screen.getByRole("option", { name: "Fixture Codex, Model two" }));
    await userEvent.click(screen.getByRole("button", { name: "Switch model" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
    await userEvent.type(screen.getByRole("textbox", { name: "Task title" }), " new context");
    await userEvent.click(screen.getByRole("button", { name: "Launch" }));
    await waitFor(() => expect(composer).toBeEnabled());
    expect(composer).toHaveValue("Retry this authored text");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(broker.calls.filter((call) => call.path.endsWith("/respond"))).toHaveLength(2));
    const snapshots = broker.calls.filter((call) => call.path.endsWith("/snapshots"));
    expect(snapshots).toHaveLength(2);
    expect(snapshots[1].body?.messageId).not.toBe(snapshots[0].body?.messageId);
    expect(snapshots[1].body).toMatchObject({ snapshot: { title: "Original title new context" } });
  });

  it("durably scopes a detach Stop to the pending Start before its acknowledgement arrives", async () => {
    const options = { delayedStartAcknowledgement: true, initialGreeting: true };
    const broker = fakeBroker(options);
    const repository = new InMemoryWidgetDraftRepository();
    const first = mount(broker, {}, repository);
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Launch" }));
    await waitFor(() => expect(broker.calls.filter((call) => call.path.endsWith("/start"))).toHaveLength(1));
    const pendingStartId = broker.calls.find((call) => call.path.endsWith("/start"))?.body?.requestId;
    first.unmount();
    await waitFor(() => expect(broker.calls.find((call) => call.path.endsWith("/stop"))?.body).toEqual({ requestId: expect.any(String), expected_control_revision: 0, startRequestId: pendingStartId }));
    await act(async () => { broker.finishStart(); });
    expect(broker.session().phase).toBe("stopped");
    mount(broker, {}, repository);
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
    expect(screen.queryByRole("button", { name: "Retry Launch" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Launch with current fields" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop assistant" })).not.toBeInTheDocument();
    expect(Object.keys(sessionStorage).filter((key) => key.startsWith("wb.assistance.start:"))).toHaveLength(0);
    expect(await screen.findByText("Let's shape your task: Original title. What is the next useful step?")).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    expect(broker.calls.filter((call) => call.path.endsWith("/start"))).toHaveLength(1);
    options.delayedStartAcknowledgement = false;
    await userEvent.click(screen.getByRole("button", { name: "Launch" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled());
    const attempts = broker.calls.filter((call) => call.path.endsWith("/start"));
    expect(attempts).toHaveLength(2);
    expect(attempts[1].body?.requestId).not.toBe(pendingStartId);
    expect(attempts[1].body?.expected_control_revision).toBe(2);
  });

  it("does not retain an unconfirmed-Stop banner when the bound session is confirmed absent", async () => {
    const broker = fakeBroker({ stopMissing: true });
    mount(broker);
    await openAndStart();
    await userEvent.click(screen.getByRole("button", { name: "Stop assistant" }));
    expect(await screen.findByRole("button", { name: "New AI help session" })).toBeEnabled();
    await waitFor(() => expect(Object.keys(localStorage).some((key) => key.startsWith("wb.assistance.pause/v1:"))).toBe(false));
    expect(screen.queryByText(/Stop is not confirmed|Stopping is not confirmed|cancellation is not confirmed/)).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Original title");
    await userEvent.click(screen.getByRole("button", { name: "New AI help session" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
    expect(broker.session().assistantSessionId).toBe("as-test-2");
    expect(broker.calls.filter((call) => call.path.endsWith("/start"))).toHaveLength(1);
  });

  it("uses the in-memory binding to revoke a reset even when session storage is unavailable", async () => {
    const read = Storage.prototype.getItem;
    const write = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(function (this: Storage, key: string) {
      return this === sessionStorage ? null : read.call(this, key);
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key: string, value: string) {
      if (this === sessionStorage) throw new Error("Session storage unavailable");
      write.call(this, key, value);
    });
    const broker = fakeBroker();
    mount(broker);
    await openAndStart();
    await userEvent.click(screen.getByRole("button", { name: "Clear form" }));
    expect(localStorage.getItem("wb.assistance.ended:as-test")).toBe("ended");
    await waitFor(() => expect(broker.calls.some((call) => call.path === "/api/assistance/sessions/as-test/end")).toBe(true));
    expect(screen.queryByRole("complementary", { name: "Draft assistance" })).not.toBeInTheDocument();
  });

  it("shows Settings/retry while disabled and leaves the form usable on start failure", async () => {
    const disabled = fakeBroker({ unavailable: true });
    const first = mount(disabled);
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    expect(await screen.findByRole("link", { name: "Set up form assistance" })).toHaveAttribute("href", "/app/settings/system/dashboard-ai?setting=wb.dashboard.assistance");
    expect(screen.getByText("Form assistance is off.")).toBeVisible();
    expect(screen.queryByText(availability.disclosure)).not.toBeInTheDocument();
    expect(disabled.calls.every((call) => call.method === "GET")).toBe(true);
    first.unmount();
    mount(fakeBroker({ startFails: true }));
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Launch" }));
    await screen.findByRole("alert");
    await userEvent.type(screen.getByRole("textbox", { name: "Task title" }), " still editable");
    expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Original title still editable");
    expect(screen.getByRole("button", { name: "Normal human submit" })).toBeEnabled();
  });

  it("preserves the prepared snapshot and shared message ID after uncertain preparation", async () => {
    const broker = fakeBroker({ prepareFailsOnce: true });
    mount(broker);
    await openAndStart();
    const composer = screen.getByRole("textbox", { name: "Message" });
    await userEvent.type(composer, "One logical turn");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Uncertain snapshot acknowledgement");
    expect(composer).toHaveValue("One logical turn");
    await userEvent.type(screen.getByRole("textbox", { name: "Task title" }), " later edit");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(broker.calls.filter((call) => call.path.endsWith("/snapshots"))).toHaveLength(2));
    const prepared = broker.calls.filter((call) => call.path.endsWith("/snapshots")).map((call) => call.body);
    expect(prepared[0]).toEqual(prepared[1]);
    expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Original title later edit");
  });

  it("retains the same Send disclosure when deferred hashing fails before a later retry", async () => {
    const broker = fakeBroker();
    mount(broker);
    await openAndStart();
    let rejectHash: ((reason: Error) => void) | undefined;
    vi.spyOn(assistanceSchema, "snapshotHash").mockImplementationOnce(() => new Promise<string>((_resolve, reject) => { rejectHash = reject; }));
    const reviewedRevision = Number(screen.getByRole("status", { name: "Revision" }).textContent);
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "One frozen disclosure");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(rejectHash).toBeDefined());
    await userEvent.type(screen.getByRole("textbox", { name: "Task title" }), " later private edit");
    await act(async () => { rejectHash?.(new Error("Hashing temporarily failed")); });
    await screen.findByText("Hashing temporarily failed");
    expect(broker.calls.filter((call) => call.path.endsWith("/snapshots"))).toHaveLength(0);
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("One frozen disclosure");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(broker.calls.filter((call) => call.path.endsWith("/respond"))).toHaveLength(1));
    expect(broker.snapshot().baseDraftRevision).toBe(reviewedRevision);
    expect(broker.snapshot().snapshot.title).toBe("Original title");
    expect(broker.snapshot().messageId).toBe(broker.calls.find((call) => call.path.endsWith("/respond"))?.body?.message_id);
    expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Original title later private edit");
  });

  it("freezes Send disclosure before pending persistence and preserves later private edits", async () => {
    const broker = fakeBroker();
    const repository = new InMemoryWidgetDraftRepository();
    const save = repository.save.bind(repository);
    let releaseSave: (() => void) | undefined;
    vi.spyOn(repository, "save").mockImplementation(async (request) => {
      if ((request.value as { title?: string }).title === "Original title before send") {
        await new Promise<void>((resolve) => { releaseSave = resolve; });
      }
      return save(request);
    });
    mount(broker, {}, repository);
    await openAndStart();
    const title = screen.getByRole("textbox", { name: "Task title" });
    await userEvent.type(title, " before send");
    await waitFor(() => expect(releaseSave).toBeDefined());
    const reviewedRevision = Number(screen.getByRole("status", { name: "Revision" }).textContent);
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Shape the reviewed fields");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(broker.calls.some((call) => call.path.endsWith("/snapshots"))).toBe(false);
    await userEvent.type(title, " later private edit");
    await userEvent.click(screen.getByRole("button", { name: "Normal human submit" }));
    await act(async () => { releaseSave?.(); });
    await waitFor(() => expect(broker.receipt()?.status).toBe("partial"));
    expect(broker.snapshot().baseDraftRevision).toBe(reviewedRevision);
    expect(broker.snapshot().snapshot.title).toBe("Original title before send");
    expect(title).toHaveValue("Original title before send later private edit");
    expect(broker.receipt()?.pendingFields).toEqual([{ path: ["title"], reason: "user_changed" }]);
    expect(screen.getByRole("textbox", { name: "Task summary" })).toHaveValue("Assistant summary");
  });

  it("retains conditional Undo after close/reopen and a complete host remount", async () => {
    const broker = fakeBroker();
    const repository = new InMemoryWidgetDraftRepository();
    const first = mount(broker, {}, repository);
    await openAndStart();
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Fill this");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(broker.receipt()?.status).toBe("applied"));
    await userEvent.click(screen.getByRole("button", { name: "Close assistance" }));
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    await screen.findByRole("button", { name: "Undo assistant changes" });
    first.unmount();
    mount(broker, {}, repository);
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    await userEvent.click(await screen.findByRole("button", { name: "Undo assistant changes" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Original title"));
    expect(screen.getByRole("textbox", { name: "Task summary" })).toHaveValue("");
    expect(broker.calls.filter((call) => call.path.endsWith("/respond"))).toHaveLength(1);
  });

  it("recovers a missing server acknowledgement without applying a patch twice", async () => {
    const broker = fakeBroker({ receiptFailsOnce: true });
    const repository = new InMemoryWidgetDraftRepository();
    const first = mount(broker, {}, repository);
    await openAndStart();
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Fill once");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText(/their receipt will retry/);
    const revision = screen.getByRole("status", { name: "Revision" }).textContent;
    first.unmount();
    mount(broker, {}, repository);
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    await waitFor(() => expect(broker.receipt()?.status).toBe("applied"));
    expect(screen.getByRole("status", { name: "Revision" })).toHaveTextContent(revision!);
    expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Assistant title");
  });

  it("retains the shared composer's unsent draft when the panel closes", async () => {
    const broker = fakeBroker();
    mount(broker);
    await openAndStart();
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Not sent yet");
    await userEvent.click(screen.getByRole("button", { name: "Close assistance" }));
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    expect(await screen.findByRole("textbox", { name: "Message" })).toHaveValue("Not sent yet");
    expect(broker.calls.filter((call) => call.path.endsWith("/respond"))).toHaveLength(0);
  });

  it("fences an old reply after the host clears its draft for a new editing lifetime", async () => {
    const broker = fakeBroker({ delayed: true });
    mount(broker);
    await openAndStart();
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Old form context");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(broker.calls.some((call) => call.path.endsWith("/respond"))).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: "Clear form" }));
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "Draft assistance" })).not.toBeInTheDocument());
    await act(async () => { broker.finish(); });
    expect(screen.getByRole("textbox", { name: "Task summary" })).toHaveValue("");
    expect(broker.receipt()).toBeNull();
    expect(Object.keys(sessionStorage).filter((key) => key.startsWith("wb.assistance.binding:"))).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
  });

  it("revokes old assistance before atomically resetting to retained fields", async () => {
    const broker = fakeBroker({ delayed: true });
    const repository = new InMemoryWidgetDraftRepository();
    const deleted = vi.spyOn(repository, "delete");
    mount(broker, {}, repository);
    await openAndStart();
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Old bound proposal");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(broker.calls.some((call) => call.path.endsWith("/respond"))).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: "Retain as new draft" }));
    expect(screen.queryByRole("complementary", { name: "Draft assistance" })).not.toBeInTheDocument();
    await act(async () => { broker.finish(); });
    expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Retained new draft");
    expect(screen.getByRole("textbox", { name: "Task summary" })).toHaveValue("");
    await waitFor(async () => expect((await repository.load(broker.session().identity))?.value).toEqual({ ...initial, title: "Retained new draft" }));
    expect(deleted).not.toHaveBeenCalled();
    expect(broker.receipt()).toBeNull();
    expect(Object.keys(sessionStorage).filter((key) => key.startsWith("wb.assistance.binding:"))).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "AI help" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
  });

  it("does not apply a late reply while the mounted form switches to Arrange", async () => {
    const broker = fakeBroker({ delayed: true });
    mount(broker);
    await openAndStart();
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Help");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(broker.calls.some((call) => call.path.endsWith("/respond"))).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: "Toggle mode" }));
    await act(async () => { broker.finish(); });
    expect(screen.getByRole("textbox", { name: "Task summary" })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    expect(broker.receipt()).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Toggle mode" }));
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    expect(broker.calls.filter((call) => call.path.endsWith("/start"))).toHaveLength(1);
    await userEvent.click(await screen.findByRole("button", { name: "Apply suggestions" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Task summary" })).toHaveValue("Assistant summary"));
  });

  it("does not resurrect old receipts when persistence completes after a draft reset", async () => {
    const broker = fakeBroker({ delayed: true });
    const repository = new InMemoryWidgetDraftRepository();
    const save = repository.save.bind(repository);
    let releaseSave: (() => void) | undefined;
    vi.spyOn(repository, "save").mockImplementation(async (request) => {
      if ((request.value as { summary?: string }).summary === "Assistant summary") {
        await new Promise<void>((resolve) => { releaseSave = resolve; });
      }
      return save(request);
    });
    const deleted = vi.spyOn(repository, "delete");
    mount(broker, {}, repository);
    await openAndStart();
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Fill the old form");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(broker.calls.some((call) => call.path.endsWith("/respond"))).toBe(true));
    await userEvent.click(screen.getByRole("textbox", { name: "Task title" }));
    await act(async () => { broker.finish(); });
    await waitFor(() => expect(releaseSave).toBeDefined());
    expect(screen.getByRole("textbox", { name: "Task summary" })).toHaveValue("Assistant summary");
    await userEvent.click(screen.getByRole("button", { name: "Clear form" }));
    await act(async () => { releaseSave?.(); });
    await waitFor(() => expect(deleted).toHaveBeenCalled());
    expect(screen.getByRole("textbox", { name: "Task summary" })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: "Task title" })).not.toHaveAttribute("data-assisted-state");
    expect(broker.receipt()).toBeNull();
    expect(screen.queryByRole("complementary", { name: "Draft assistance" })).not.toBeInTheDocument();
  });

  it("does not overwrite another tab's persisted draft when its CAS loses", async () => {
    const broker = fakeBroker({ delayed: true });
    const repository = new InMemoryWidgetDraftRepository();
    mount(broker, {}, repository);
    await openAndStart();
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Fill this");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(broker.calls.some((call) => call.path.endsWith("/respond"))).toBe(true));
    const session = broker.session();
    await repository.save({ ...session.identity, draftSchema: session.schema, value: { ...initial, title: "Other tab's title", summary: "Other tab's work" } });
    await act(async () => { broker.finish(); });
    await waitFor(() => expect(broker.receipt()?.pendingFields[0]?.reason).toBe("storage_conflict"));
    expect((await repository.load(session.identity))?.value).toEqual({ ...initial, title: "Other tab's title", summary: "Other tab's work" });
    expect(broker.receipt()?.appliedFields).toEqual([]);
    expect(screen.getAllByText(/storage conflicted/).length).toBeGreaterThan(0);
  });

  it("the canonical manifest matches the declared host draft", () => {
    expect(definition.drafts?.[0].schema).toEqual(assistedForms["task-create"].schema);
  });
});

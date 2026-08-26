import { webcrypto } from "node:crypto";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CAPTURE_APP_CONTRIBUTION } from "../../widget-library/capture/contribution";
import { asViewId, asWidgetInstanceId, type WidgetDefinition } from "../contributions/contracts";
import { InMemoryWidgetDraftRepository, WidgetDraftRuntimeProvider, WidgetDraftScopeProvider, useWidgetDraft } from "../drafts";
import { DashboardHelpProvider } from "../help";
import type { AssistanceSession, AssistedDraftPatch, DraftPatchReceipt, PreparedDraftSnapshot } from "./contracts";
import { AssistDraftButton, AssistedDraftRuntimeProvider, useAssistedDraft } from "./AssistedDraftRuntime";
import { assistedDraftDeclaration, assistedForms } from "./schema";

vi.mock("../../security/humanAuthority", async (original) => ({
  ...await original<typeof import("../../security/humanAuthority")>(),
  exactHumanAuthorityHeaders: vi.fn(async () => ({ "X-Test-Authority": "exact-action" })),
}));

const initial = { title: "Original title", summary: "", next_action: "", batch_lines: [], proposal_ref: { threadId: "th-test" }, proposal_pending: { clientMutationId: "never-disclose" } };
const declaration = assistedDraftDeclaration("task-create");
const definition: WidgetDefinition = {
  ...CAPTURE_APP_CONTRIBUTION.widgetDefinitions[0],
  drafts: [{ draftName: "task-create", schema: declaration.schema, scope: { kind: "view" }, persistence: "device", sensitivity: "private", clearPolicy: "widget-managed", maxBytes: 32768 }],
  assistableDrafts: [declaration],
};
const availability = { available: true, code: "ready", providerId: "fixture-provider", modelId: "fixture-model", purpose: "dashboard.assisted_draft", message: "Ready to start", disclosure: "Explicit test disclosure: allowlisted fields and messages only." };

function fakeBroker(options: { delayed?: boolean; unavailable?: boolean; startFails?: boolean; prepareFailsOnce?: boolean; receiptFailsOnce?: boolean } = {}) {
  const calls: { path: string; method: string; body?: Record<string, unknown> }[] = [];
  let session: AssistanceSession;
  let snapshot: PreparedDraftSnapshot;
  let patch: AssistedDraftPatch | null = null;
  let receipt: DraftPatchReceipt | null = null;
  const messages: Record<string, unknown>[] = [];
  let release: (() => void) | undefined;
  let failedPreparation = false;
  let failedReceipt = false;
  const respond = (body: unknown, status = 200) => ({ ok: status < 400, status, json: async () => body }) as Response;
  const fetchImpl = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ path, method, body });
    if (path.endsWith("/availability")) return respond(options.unavailable ? { ...availability, available: false, code: "disabled", message: "Assistance is disabled" } : availability);
    if (path === "/api/assistance/sessions") {
      if (options.startFails) return respond({ error: "Provider startup unavailable" }, 503);
      session = { assistantSessionId: "as-test", conversationId: "conversation-test", identity: body.identity, schema: body.schema, expiresAt: "2099-01-01T00:00:00Z", availability: availability as AssistanceSession["availability"] };
      return respond(session);
    }
    if (path.endsWith("/snapshots")) {
      snapshot = body;
      if (options.prepareFailsOnce && !failedPreparation) { failedPreparation = true; throw new Error("Uncertain snapshot acknowledgement"); }
      return respond({ prepared: true });
    }
    if (path.endsWith("/respond")) {
      if (options.delayed) await new Promise<void>((resolve) => { release = resolve; });
      messages.push({ message_id: body.message_id, role: "user", content: body.value, created_at: "2026-08-25T12:00:00Z" });
      messages.push({ message_id: "assistant-1", role: "agent", content: "I suggested a title and a summary. You decide when to submit.", created_at: "2026-08-25T12:00:01Z" });
      patch = { protocol: "wb.assisted-draft.patch/v1", assistantSessionId: session.assistantSessionId, conversationId: session.conversationId, identity: session.identity, schema: session.schema, baseDraftRevision: snapshot.baseDraftRevision, baseSnapshotHash: snapshot.baseSnapshotHash, baseSnapshot: snapshot.snapshot, patchId: "ap-test", operations: [{ op: "set", path: ["title"], value: "Assistant title" }, { op: "set", path: ["summary"], value: "Assistant summary" }] };
      return respond({ message_id: body.message_id });
    }
    if (path.endsWith("/conversations/conversation-test")) return respond({ conversation: { conversation_id: "conversation-test", status: "open", agent_alive: false }, messages: [...messages] });
    if (path.endsWith("/patches")) return respond({ patches: patch ? [{ patch, receipt }] : [] });
    if (path.endsWith("/receipts")) {
      if (options.receiptFailsOnce && !failedReceipt) { failedReceipt = true; throw new Error("Receipt acknowledgement unavailable"); }
      receipt = body; return respond(receipt);
    }
    if (path.endsWith("/stop")) return respond({ stopped: true });
    if (path.endsWith("/as-test")) return respond(session);
    throw new Error(`Unexpected test request: ${path}`);
  }) as typeof fetch;
  return { fetchImpl, calls, finish: () => release?.(), receipt: () => receipt, snapshot: () => snapshot, session: () => session };
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
  return render(<DashboardHelpProvider enabled={help}><WidgetDraftRuntimeProvider repository={repository}>
    <AssistedDraftRuntimeProvider fetchImpl={broker.fetchImpl}>
      <WidgetDraftScopeProvider definition={definition} viewId={asViewId("wb.tasks.main")} instanceId={asWidgetInstanceId("task-create")} input={{}}><Form {...formOptions} /></WidgetDraftScopeProvider>
    </AssistedDraftRuntimeProvider>
  </WidgetDraftRuntimeProvider></DashboardHelpProvider>);
}

async function openAndStart() {
  await waitFor(() => expect(screen.getByRole("button", { name: "Help me shape this" })).toBeEnabled());
  await userEvent.click(screen.getByRole("button", { name: "Help me shape this" }));
  await screen.findByText(availability.disclosure);
  await userEvent.click(screen.getByRole("button", { name: "Start assistance" }));
  await screen.findByRole("textbox", { name: "Message" });
}

beforeEach(() => { vi.stubGlobal("crypto", webcrypto); sessionStorage.clear(); });
afterEach(() => { vi.unstubAllGlobals(); });

describe("Dashboard assisted draft host", () => {
  it("puts drafting guidance on the shared button without starting assistance on focus", async () => {
    const broker = fakeBroker();
    mount(broker, { help: true });
    const assist = screen.getByRole("button", { name: "Help me shape this" });
    await waitFor(() => expect(assist).toBeEnabled());
    expect(screen.queryByText(/The assistant fills these fields/)).not.toBeInTheDocument();
    act(() => assist.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("The assistant fills these fields. You can edit or undo its suggestions; only you can submit the form.");
    expect(broker.calls).toHaveLength(0);
    await userEvent.click(assist);
    expect(await screen.findByText(availability.disclosure)).toBeVisible();
    expect(broker.calls.every((call) => call.method === "GET")).toBe(true);
    const heading = screen.getByRole("heading", { name: "Shape test draft" });
    expect(heading).toHaveAttribute("data-help-target", "true");
    const close = screen.getByRole("button", { name: "Close assistance" });
    act(() => close.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Closing this panel keeps your draft and conversation. Reopen form assistance to continue.");
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("complementary", { name: "Draft assistance" })).toBeVisible();
    await userEvent.click(close);
    expect(screen.queryByRole("complementary", { name: "Draft assistance" })).not.toBeInTheDocument();
  });

  it("keeps disclosure visible at Start but puts session mechanics on the relevant controls", async () => {
    const broker = fakeBroker();
    mount(broker, { help: true });
    await openAndStart();
    expect(broker.calls.find((call) => call.path === "/api/assistance/sessions")?.body).toMatchObject({ disclosureAccepted: true, providerId: availability.providerId, modelId: availability.modelId });
    expect(screen.queryByText("Bound to this form. Only you can submit it.")).not.toBeInTheDocument();
    expect(screen.queryByText("Send another message to resume. Closing this panel keeps your draft and conversation.")).not.toBeInTheDocument();
    const stop = screen.getByRole("button", { name: "Stop assistant" });
    act(() => stop.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Send another message to resume. Stopping does not submit or discard your draft.");
    expect(broker.calls.some((call) => call.path.endsWith("/stop"))).toBe(false);
    await userEvent.click(stop);
    await waitFor(() => expect(broker.calls.some((call) => call.path.endsWith("/stop"))).toBe(true));
    expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Original title");
    expect(screen.getByRole("button", { name: "Normal human submit" })).toBeEnabled();
  });

  it("reserves a sibling layout region without remounting or hiding the form", async () => {
    mount(fakeBroker());
    const form = screen.getByRole("region", { name: "Task form" });
    const hostContent = form.closest(".wb-assistance-host__content");
    const host = hostContent?.parentElement;
    expect(host).not.toHaveAttribute("data-assistance-open");
    await openAndStart();
    const dock = screen.getByRole("complementary", { name: "Draft assistance" });
    expect(host).toHaveAttribute("data-assistance-open", "true");
    expect(dock.parentElement).toBe(host);
    expect(hostContent?.contains(dock)).toBe(false);
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

  it.each([{ initialMode: "arrange" as const }, { initialMode: "preview" as const }, { readOnly: true }])("is inert outside editable Operate mode %#", async (options) => {
    const broker = fakeBroker();
    mount(broker, options);
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Task title" })).toHaveValue("Original title"));
    expect(screen.getByRole("button", { name: "Help me shape this" })).toBeDisabled();
    expect(broker.calls).toHaveLength(0);
  });

  it("shows Settings/retry while disabled and leaves the form usable on start failure", async () => {
    const disabled = fakeBroker({ unavailable: true });
    const first = mount(disabled);
    await waitFor(() => expect(screen.getByRole("button", { name: "Help me shape this" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Help me shape this" }));
    expect(await screen.findByRole("link", { name: "Set up form assistance" })).toHaveAttribute("href", "/app/settings/apps/dashboard?setting=wb.dashboard.assistance");
    expect(screen.getByText("Form assistance is off.")).toBeVisible();
    expect(screen.queryByText(availability.disclosure)).not.toBeInTheDocument();
    expect(disabled.calls.every((call) => call.method === "GET")).toBe(true);
    first.unmount();
    mount(fakeBroker({ startFails: true }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Help me shape this" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Help me shape this" }));
    await userEvent.click(await screen.findByRole("button", { name: "Start assistance" }));
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
    await userEvent.click(screen.getByRole("button", { name: "Help me shape this" }));
    await screen.findByRole("button", { name: "Undo assistant changes" });
    first.unmount();
    mount(broker, {}, repository);
    await waitFor(() => expect(screen.getByRole("button", { name: "Help me shape this" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Help me shape this" }));
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
    await waitFor(() => expect(screen.getByRole("button", { name: "Help me shape this" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Help me shape this" }));
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
    await userEvent.click(screen.getByRole("button", { name: "Help me shape this" }));
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
    await userEvent.click(screen.getByRole("button", { name: "Help me shape this" }));
    expect(await screen.findByRole("button", { name: "Start assistance" })).toBeEnabled();
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
    await userEvent.click(screen.getByRole("button", { name: "Help me shape this" }));
    expect(await screen.findByRole("button", { name: "Start assistance" })).toBeEnabled();
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

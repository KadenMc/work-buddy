import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../../security/humanAuthority", () => ({
  exactHumanAuthorityHeaders: vi.fn(async () => ({ "X-WB-Test-Authority": "yes" })),
}));

import type { DashboardIntent } from "../../../dashboard/contributions/contracts";
import type { ViewLocationAdapter } from "../../../dashboard/contributions/viewModules";
import { TASKS_APP_ID, TASKS_INSTANCE_IDS, TASKS_VIEW_ID } from "../bindings";
import { TASK_INTENTS } from "../contracts";
import { HttpTasksProvider } from "./HttpTasksProvider";

const task = {
  task_id: "task-1",
  description: "Prepare launch notes",
  state: "inbox",
  urgency: "high",
  revision: 3,
  due_date: "2026-08-25",
  deadline_date: null,
  snooze_until: null,
  completed_at: null,
  archived_at: null,
  deleted_at: null,
  project: "work-buddy",
  namespace_tags: ["project/work-buddy"],
  tags: ["writing"],
  created_at: "2026-08-20T12:00:00Z",
  updated_at: "2026-08-23T12:00:00Z",
  summary: "Prepare a handoff.",
  desired_outcome: "A useful launch note.",
  next_action: "Draft outline",
  definition_of_done: "Published",
  dependencies: [],
  provenance: { created_by: "Owner", created_at: "2026-08-20T12:00:00Z", source: "dashboard" },
  action_items: [],
  history: [],
  document: { state: "available", store_id: null, document_id: null, excerpt: "Launch context", updated_at: "2026-08-23T12:00:00Z", updated_by: "Owner", href: null },
  local_files: [],
  local_files_error: null,
};

const viewPayload = {
  ok: true,
  collection_revision: 17,
  observed_at: "2026-08-23T13:00:00Z",
  access: { mode: "read_write" },
  query: { lens: "inbox", q: "launch", task: "task-1" },
  facets: { counts: { focused: 1, inbox: 2, active: 5 }, projects: { "work-buddy": 1 } },
  tasks: [task],
  selected_task: task,
  options: { projects: ["work-buddy"], namespaces: ["project/work-buddy"], contracts: [], contexts: [] },
};

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const intent = (
  type: string,
  payload: Record<string, unknown>,
  clientMutationId?: string,
): DashboardIntent => ({
  intent_type: type,
  schema_version: 1,
  intent_id: clientMutationId ?? `intent:${type}`,
  view_id: TASKS_VIEW_ID,
  instance_id: TASKS_INSTANCE_IDS.workspace,
  ...(clientMutationId ? { client_mutation_id: clientMutationId } : {}),
  payload,
});

function locationAdapter(initial = "?lens=inbox&q=launch") {
  let search = initial;
  const pushes: string[] = [];
  const replaces: string[] = [];
  const location: ViewLocationAdapter = {
    getSearch: () => search,
    pushSearch: (next) => { search = next; pushes.push(next); },
    replaceSearch: (next) => { search = next; replaces.push(next); },
    subscribe: () => () => undefined,
  };
  return { location, pushes, replaces, getSearch: () => search };
}

describe("HttpTasksProvider", () => {
  beforeEach(() => vi.clearAllMocks());

  it("parses one coherent view snapshot for Quick Add and Workspace", async () => {
    const fetchImpl = vi.fn(async () => json(viewPayload));
    const route = locationAdapter();
    const provider = new HttpTasksProvider({ fetchImpl: fetchImpl as typeof fetch, location: route.location });

    const snapshot = await provider.loadView(TASKS_VIEW_ID, { reason: "mount" });

    expect(fetchImpl).toHaveBeenCalledWith("/api/tasks/view?lens=inbox&q=launch", expect.anything());
    expect(snapshot.revision).toBe(17);
    expect(snapshot.model.selectedTask).toMatchObject({
      task_id: "task-1",
      title: "Prepare launch notes",
      attention_state: "inbox",
      namespaces: ["project/work-buddy"],
    });
    expect(snapshot.widgetInputs[TASKS_INSTANCE_IDS.quickAdd]).toMatchObject({ revision: 17 });
    expect(snapshot.widgetInputs[TASKS_INSTANCE_IDS.workspace]).toMatchObject({
      tasks: [{ task_id: "task-1" }],
      selectedTask: { task_id: "task-1" },
    });
  });

  it("explains a stale dashboard/API build mismatch instead of leaking a JSON parse error", async () => {
    const fetchImpl = vi.fn(async () => new Response("<!doctype html>", {
      status: 404,
      headers: { "Content-Type": "text/html; charset=utf-8" },
    }));
    const provider = new HttpTasksProvider({
      fetchImpl: fetchImpl as typeof fetch,
      location: locationAdapter("").location,
    });

    await expect(provider.loadView(TASKS_VIEW_ID, { reason: "mount" })).rejects.toThrow(
      "The Tasks API is unavailable. Restart work-buddy so the dashboard and API use the same build.",
    );
  });

  it("fails closed when task access metadata is malformed", async () => {
    const fetchImpl = vi.fn(async () => json({ ...viewPayload, access: { mode: "mystery" } }));
    const provider = new HttpTasksProvider({ fetchImpl: fetchImpl as typeof fetch, location: locationAdapter().location });

    const snapshot = await provider.loadView(TASKS_VIEW_ID, { reason: "mount" });

    expect(snapshot.status).toBe("read-only");
    expect(snapshot.model.access).toEqual({
      mode: "read_only",
      reason: "Task write authority could not be verified.",
    });
  });

  it("returns a typed server batch preview without requesting mutation authority", async () => {
    const fetchImpl = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      expect(url).toBe("/api/tasks/batch/preview");
      expect(init?.headers).not.toHaveProperty("X-WB-Test-Authority");
      return json({
        ok: true,
        preview: {
          rows: [{ index: 0, title: "First", valid: true, field_errors: {}, duplicate: false, duplicate_reason: null, will_create: true }],
          accepted_indices: [0],
          accepted_count: 1,
          can_commit: true,
          collection_revision: 17,
          preview_token: "preview-token",
        },
      });
    });
    const provider = new HttpTasksProvider({ fetchImpl: fetchImpl as typeof fetch, location: locationAdapter("").location });

    const result = await provider.dispatch(intent(
      TASK_INTENTS.batchPreview,
      { items: [{ title: "First" }] },
      "batch-1",
    ));

    expect(result).toMatchObject({
      status: "accepted",
      revision: 17,
      value: { preview: { accepted_indices: [0], preview_token: "preview-token" } },
    });
  });

  it("canonicalizes renderer navigation without allowing direct history access", async () => {
    const route = locationAdapter("?lens=inbox&q=launch&provider=demo");
    const provider = new HttpTasksProvider({ fetchImpl: vi.fn() as typeof fetch, location: route.location });

    const result = await provider.dispatch(intent(TASK_INTENTS.locationChange, {
      patch: { lens: "focused", task: "task-1", q: "" },
    }));

    expect(result.status).toBe("accepted");
    expect(result.revision).toBeUndefined();
    expect(route.pushes).toEqual(["?lens=focused&task=task-1"]);
  });

  it("reloads the authoritative snapshot after a query-only location change", async () => {
    const route = locationAdapter("?lens=inbox&q=launch");
    const fetchImpl = vi.fn(async () => json(
      fetchImpl.mock.calls.length === 1
        ? viewPayload
        : {
            ...viewPayload,
            query: { ...viewPayload.query, lens: "focused", q: "" },
          },
    ));
    const provider = new HttpTasksProvider({ fetchImpl: fetchImpl as typeof fetch, location: route.location });
    await provider.loadView(TASKS_VIEW_ID, { reason: "mount" });

    const locationResult = await provider.dispatch(intent(TASK_INTENTS.locationChange, {
      patch: { lens: "focused", q: "" },
    }));
    const reconciled = await provider.reconcile({
      id: "query-change",
      appId: TASKS_APP_ID,
      viewIds: [TASKS_VIEW_ID],
      reason: "query-change",
      observedAt: "2026-08-23T13:01:00Z",
    });

    expect(locationResult.revision).toBeUndefined();
    expect(reconciled.changed).toBe(true);
    expect(reconciled.snapshot?.model).toMatchObject({ query: { lens: "focused", q: "" } });
  });

  it("signs a create with a stable action, subject, and exact HTTP body", async () => {
    const fetchImpl = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      expect(url).toBe("/api/tasks");
      expect(init).toMatchObject({ method: "POST", credentials: "same-origin" });
      expect(init?.headers).toMatchObject({ "X-WB-Test-Authority": "yes" });
      expect(JSON.parse(String(init?.body))).toEqual({
        title: "Write migration receipt",
        client_mutation_id: "create-1",
      });
      return json({ ok: true, task: { ...task, task_id: "task-2", description: "Write migration receipt", revision: 1 }, collection_revision: 18, receipt: { receipt_id: "r-1" } });
    });
    const provider = new HttpTasksProvider({ fetchImpl: fetchImpl as typeof fetch, location: locationAdapter("").location });

    const result = await provider.dispatch(intent(TASK_INTENTS.create, { title: "Write migration receipt" }, "create-1"));

    expect(result).toMatchObject({
      status: "accepted",
      revision: 18,
      value: { task: { task_id: "task-2", revision: 1 }, receipt: { receipt_id: "r-1" } },
    });
  });

  it("translates a CAS conflict into a renderer conflict with current task", async () => {
    const fetchImpl = vi.fn(async () => json({
      code: "task_revision_conflict",
      message: "This task changed while you were editing it.",
      field_errors: { title: "Review the current title." },
      current_revision: 4,
      current_task: {
        ...task,
        revision: 4,
        current_action_item_id: 7,
        action_items: [{
          id: 7,
          task_id: "task-1",
          sequence: 2,
          description: "Review the current version",
          state: "done",
          authorship: "agent_unapproved",
          created_at: "2026-08-23T12:00:00Z",
          updated_at: "2026-08-23T12:30:00Z",
        }],
        history: [{
          id: 9,
          created_at: "2026-08-23T12:31:00Z",
          actor: "human:owner",
          action: "action_item.updated",
        }],
      },
    }, 409));
    const provider = new HttpTasksProvider({ fetchImpl: fetchImpl as typeof fetch, location: locationAdapter("").location });

    const result = await provider.dispatch(intent(TASK_INTENTS.update, {
      task_id: "task-1",
      expected_revision: 3,
      title: "Changed",
    }, "update-1"));

    expect(result).toMatchObject({
      status: "conflict",
      message: "This task changed while you were editing it.",
      fieldErrors: { title: "Review the current title." },
      value: {
        task: {
          task_id: "task-1",
          revision: 4,
          action_items: [{
            action_item_id: "7",
            position: 2,
            completed: true,
            current: true,
            approval_state: "pending",
          }],
          history: [{ history_id: "9", action: "action_item.updated" }],
        },
      },
    });
  });

  it("maps an authority-unavailable 503 to an unavailable intent", async () => {
    const fetchImpl = vi.fn(async () => json({
      ok: false,
      error: {
        code: "task_authority_unavailable",
        message: "Task authority could not be verified.",
        retryable: true,
      },
    }, 503));
    const provider = new HttpTasksProvider({ fetchImpl: fetchImpl as typeof fetch, location: locationAdapter("").location });

    const result = await provider.dispatch(intent(
      TASK_INTENTS.create,
      { title: "Fail closed" },
      "unavailable-1",
    ));

    expect(result).toMatchObject({
      status: "unavailable",
      message: "Task authority could not be verified.",
    });
  });

  it.each([
    [TASK_INTENTS.update, "PATCH", "/api/tasks/task-1", {}],
    [TASK_INTENTS.complete, "POST", "/api/tasks/task-1/complete", {}],
    [TASK_INTENTS.reopen, "POST", "/api/tasks/task-1/reopen", {}],
    [TASK_INTENTS.focus, "POST", "/api/tasks/task-1/focus", {}],
    [TASK_INTENTS.snooze, "POST", "/api/tasks/task-1/snooze", { snooze_until: "2026-08-24" }],
    [TASK_INTENTS.archive, "POST", "/api/tasks/task-1/archive", {}],
    [TASK_INTENTS.unarchive, "POST", "/api/tasks/task-1/unarchive", {}],
    [TASK_INTENTS.delete, "DELETE", "/api/tasks/task-1", {}],
    [TASK_INTENTS.restore, "POST", "/api/tasks/task-1/restore", {}],
    [TASK_INTENTS.replaceTags, "PUT", "/api/tasks/task-1/tags", { tags: ["next"] }],
    [TASK_INTENTS.createDocument, "POST", "/api/tasks/task-1/document", {}],
    [TASK_INTENTS.actionItemCreate, "POST", "/api/tasks/task-1/action-items", { text: "Next step" }],
    [TASK_INTENTS.actionItemReorder, "POST", "/api/tasks/task-1/action-items/reorder", { action_item_ids: [2, 1] }],
    [TASK_INTENTS.actionItemUpdate, "PATCH", "/api/tasks/task-1/action-items/1", { action_item_id: "1", text: "Edited" }],
    [TASK_INTENTS.actionItemCurrent, "POST", "/api/tasks/task-1/action-items/1/current", { action_item_id: "1" }],
    [TASK_INTENTS.actionItemApprove, "POST", "/api/tasks/task-1/action-items/1/approve", { action_item_id: "1" }],
    [TASK_INTENTS.actionItemDelete, "DELETE", "/api/tasks/task-1/action-items/1", { action_item_id: "1" }],
    [TASK_INTENTS.actionItemRestore, "POST", "/api/tasks/task-1/action-items/1/restore", { action_item_id: "1" }],
  ])("maps %s to its CAS HTTP route", async (intentType, method, path, extra) => {
    const fetchImpl = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      expect(url).toBe(path);
      expect(init?.method).toBe(method);
      expect(JSON.parse(String(init?.body))).toMatchObject({
        task_id: "task-1",
        expected_revision: 3,
        client_mutation_id: "mutation-1",
        ...extra,
      });
      return json({
        ok: true,
        result: { task, collection_revision: 18, receipt: { receipt_id: "r-1" } },
      });
    });
    const provider = new HttpTasksProvider({ fetchImpl: fetchImpl as typeof fetch, location: locationAdapter("").location });

    const result = await provider.dispatch(intent(intentType, {
      task_id: "task-1",
      expected_revision: 3,
      ...extra,
    }, "mutation-1"));

    expect(result).toMatchObject({ status: "accepted", revision: 18 });
  });

  it("opens only an API-returned same-origin Co-work route", async () => {
    const navigate = vi.fn();
    const fetchImpl = vi.fn(async () => json({ ok: true, href: "/app/cowork?store=store-1&document=doc-1" }));
    const provider = new HttpTasksProvider({ fetchImpl: fetchImpl as typeof fetch, location: locationAdapter("").location, navigate });

    const result = await provider.dispatch(intent(TASK_INTENTS.openDocument, { task_id: "task-1" }));

    expect(result.status).toBe("accepted");
    expect(navigate).toHaveBeenCalledWith("/app/cowork?store=store-1&document=doc-1");
  });

  it("rejects an external document route returned by the API", async () => {
    const navigate = vi.fn();
    const fetchImpl = vi.fn(async () => json({ ok: true, href: "https://example.test/document" }));
    const provider = new HttpTasksProvider({ fetchImpl: fetchImpl as typeof fetch, location: locationAdapter("").location, navigate });

    const result = await provider.dispatch(intent(TASK_INTENTS.openDocument, { task_id: "task-1" }));

    expect(result.status).toBe("rejected");
    expect(navigate).not.toHaveBeenCalled();
  });

  it("adds the explicit host intent required for a linked-file reveal", async () => {
    const fetchImpl = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.headers).toMatchObject({
        "X-Work-Buddy-Intent": "cowork-local-file-reveal",
      });
      return json({ ok: true, action: "reveal", link_id: "link-1" });
    });
    const provider = new HttpTasksProvider({ fetchImpl: fetchImpl as typeof fetch, location: locationAdapter("").location });

    const result = await provider.dispatch(intent(TASK_INTENTS.localFileAction, {
      task_id: "task-1",
      expected_revision: 3,
      link_id: "link-1",
      action: "reveal",
    }, "local-1"));

    expect(result).toMatchObject({
      status: "accepted",
      value: { action: "reveal", link_id: "link-1" },
    });
  });
});

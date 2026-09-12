import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { InMemoryWidgetDraftRepository, WidgetDraftRuntimeProvider, WidgetDraftScopeProvider, useWidgetDraftScopeStatus } from "../../../dashboard/drafts";
import type { WidgetDraftIdentity } from "../../../dashboard/drafts/contracts";
import type { IntentResult, JsonValue } from "../../../dashboard/contributions/contracts";
import { TASKS_APP_CONTRIBUTION } from "../contribution";
import { TASKS_APP_ID, TASKS_INSTANCE_IDS, TASKS_VIEW_ID, TASKS_WIDGET_TYPE_IDS } from "../bindings";
import type { TaskQueryState, TaskWorkspaceInput } from "../contracts";
import { DEFAULT_TASK_BROWSE_STATE, taskBrowsePatch, taskBrowseState } from "./taskBrowseState";
import { useTaskBrowseState } from "./useTaskBrowseState";

const identity: WidgetDraftIdentity = {
  profileId: "local-profile", workspaceId: "default-workspace", appId: TASKS_APP_ID,
  viewId: TASKS_VIEW_ID, instanceId: TASKS_INSTANCE_IDS.workspace, widgetTypeId: TASKS_WIDGET_TYPE_IDS.workspace,
  draftName: "task-browse", scopeKey: "view",
};
const schema = { schemaId: "wb.tasks.browse.draft", version: 1 };
const query = (patch: Partial<TaskQueryState> = {}): TaskQueryState => ({
  ...DEFAULT_TASK_BROWSE_STATE, lens: "inbox", project: "", namespace: "", urgency: "", state: "", task: null, ...patch,
});
const input = (next: TaskQueryState, explicit = true): TaskWorkspaceInput => ({
  query: next, browseQueryExplicit: explicit,
} as TaskWorkspaceInput);
const saved = taskBrowseState({ statuses: ["completed"], projects: ["7", "8"], namespaces: ["research"], exact_namespaces: ["old//branch/"], attention: ["waiting"], urgencies: ["high"], q: "launch", due: "week", note: "yes", sort: "title", direction: "desc", offset: 100, limit: 50 });

function renderBrowsing(repository: InMemoryWidgetDraftRepository, initialInput: TaskWorkspaceInput, activeBrowse = true) {
  const onRestore = vi.fn(async (_patch: Record<string, JsonValue>): Promise<IntentResult> => ({ intent_id: "restore", status: "accepted" }));
  const rendered = renderHook(({ current, active }) => {
    const browse = useTaskBrowseState({ input: current, activeBrowse: active, onRestore });
    const clear = useWidgetDraftScopeStatus();
    return { ...browse, ...clear };
  }, {
    initialProps: { current: initialInput, active: activeBrowse },
    wrapper: ({ children }: { children: ReactNode }) => <WidgetDraftRuntimeProvider repository={repository}>
      <WidgetDraftScopeProvider definition={TASKS_APP_CONTRIBUTION.widgetDefinitions[1]} viewId={TASKS_VIEW_ID} instanceId={TASKS_INSTANCE_IDS.workspace} input={{}}>{children}</WidgetDraftScopeProvider>
    </WidgetDraftRuntimeProvider>,
  });
  return { ...rendered, onRestore };
}

describe("host-owned Task browsing persistence", () => {
  it("shows an explicit URL immediately while storage hydrates, without inheriting saved hidden filters", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const stored = await repository.save({ ...identity, draftSchema: schema, value: { ...saved } });
    let hydrate!: (value: typeof stored) => void;
    vi.spyOn(repository, "load").mockImplementationOnce(() => new Promise((resolve) => { hydrate = resolve; }));
    const explicit = query({ sort: "updated_at", direction: "asc" });
    const view = renderBrowsing(repository, input(explicit));
    expect(view.result.current.ready).toBe(true);
    expect(view.onRestore).not.toHaveBeenCalled();
    await act(async () => hydrate(stored));
    await waitFor(async () => expect((await repository.load(identity))?.value).toEqual(taskBrowseState(explicit)));
    expect(view.onRestore).not.toHaveBeenCalled();
  });

  it("remembers resolved filters and ordering across revisit while an explicit URL wins completely", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const first = renderBrowsing(repository, input(query(saved)));
    await waitFor(() => expect(first.result.current.hasDirtyDraft).toBe(true));
    await waitFor(async () => expect((await repository.load(identity))?.value).toEqual(saved));
    first.unmount();

    const revisit = renderBrowsing(repository, input(query(), false));
    await waitFor(() => expect(revisit.onRestore).toHaveBeenCalledWith(taskBrowsePatch(saved)));
    expect((await repository.load(identity))?.value).toEqual(saved);
    revisit.rerender({ current: input(query(saved)), active: true });
    await waitFor(() => expect(revisit.result.current.ready).toBe(true));
    expect(revisit.onRestore).toHaveBeenCalledTimes(1);
    revisit.unmount();

    const explicit = query({ statuses: [], sort: "updated_at", direction: "asc" });
    const bookmark = renderBrowsing(repository, input(explicit));
    await waitFor(async () => expect((await repository.load(identity))?.value).toEqual(taskBrowseState(explicit)));
    expect(bookmark.onRestore).not.toHaveBeenCalled();
  });

  it("uses the card clear action for all browse defaults and leaves stored task field drafts intact", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const editIdentity = { ...identity, draftName: "task-edit", scopeKey: "task-1" };
    const fieldDraft = await repository.save({ ...editIdentity, draftSchema: { schemaId: "wb.tasks.edit.draft", version: 1 }, value: { title: "Unsaved title" } });
    await repository.save({ ...identity, draftSchema: schema, value: { ...saved } });
    const view = renderBrowsing(repository, input(query(saved)));
    await waitFor(() => expect(view.result.current.dirtyDraftNames).toEqual(["task-browse"]));
    await act(async () => expect(await view.result.current.clearAll()).toBe(true));
    expect(view.onRestore).toHaveBeenCalledWith(taskBrowsePatch(DEFAULT_TASK_BROWSE_STATE));
    expect(view.result.current.hasDirtyDraft).toBe(false);
    expect(await repository.load(identity)).toBeUndefined();
    expect(await repository.load(editIdentity)).toEqual(fieldDraft);
    view.rerender({ current: input(query()), active: true });
    expect(view.result.current.hasDirtyDraft).toBe(false);
    view.unmount();
    const revisit = renderBrowsing(repository, input(query(), false));
    await waitFor(() => expect(revisit.result.current.ready).toBe(true));
    expect(revisit.onRestore).not.toHaveBeenCalled();
  });

  it("keeps browse state persistent but outside the detail card clear action", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    await repository.save({ ...identity, draftSchema: schema, value: { ...saved } });
    const view = renderBrowsing(repository, input(query({ ...saved, task: "task-1" })), false);
    await waitFor(() => expect(view.result.current.ready).toBe(true));
    expect(view.result.current.hasDirtyDraft).toBe(false);
    await act(async () => expect(await view.result.current.clearAll()).toBe(true));
    expect(view.onRestore).not.toHaveBeenCalled();
    expect((await repository.load(identity))?.value).toEqual(saved);
    view.rerender({ current: input(query({ ...saved, task: null })), active: true });
    await waitFor(() => expect(view.result.current.dirtyDraftNames).toEqual(["task-browse"]));
  });

  it("restores bare task/organizer links without changing selection and handles a later plain Tasks revisit", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    await repository.save({ ...identity, draftSchema: schema, value: { ...saved } });
    const view = renderBrowsing(repository, input(query({ task: "task-1" }), false), false);
    await waitFor(() => expect(view.onRestore).toHaveBeenCalledWith(taskBrowsePatch(saved)));
    expect(view.onRestore.mock.calls[0]![0]).not.toHaveProperty("task");
    view.rerender({ current: input(query(saved)), active: true });
    await waitFor(() => expect(view.result.current.ready).toBe(true));
    view.onRestore.mockClear();
    view.rerender({ current: input(query(), false), active: true });
    await waitFor(() => expect(view.onRestore).toHaveBeenCalledWith(taskBrowsePatch(saved)));
    view.rerender({ current: input(query(saved)), active: true });
    await waitFor(() => expect(view.result.current.ready).toBe(true));
    view.onRestore.mockClear();
    view.rerender({ current: input(query({ mode: "namespaces" }), false), active: false });
    await waitFor(() => expect(view.onRestore).toHaveBeenCalledWith({ ...taskBrowsePatch(saved), mode: "namespaces" }));
  });

  it("fences old provider input through asynchronous restore and reset acknowledgement", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    await repository.save({ ...identity, draftSchema: schema, value: { ...saved } });
    const view = renderBrowsing(repository, input(query(), false));
    let accept!: (result: IntentResult) => void;
    view.onRestore.mockImplementation(() => new Promise((resolve) => { accept = resolve; }));
    await waitFor(() => expect(view.onRestore).toHaveBeenCalledTimes(1));
    expect(view.result.current.ready).toBe(false);
    expect((await repository.load(identity))?.value).toEqual(saved);
    view.rerender({ current: { ...input(query(), false), revision: 22 }, active: true });
    expect((await repository.load(identity))?.value).toEqual(saved);
    await act(async () => accept({ intent_id: "restore", status: "accepted" }));
    expect(view.result.current.ready).toBe(false);
    view.rerender({ current: input(query(saved)), active: true });
    await waitFor(() => expect(view.result.current.ready).toBe(true));

    await act(async () => expect(await view.result.current.clearAll()).toBe(true));
    await waitFor(() => expect(view.onRestore).toHaveBeenCalledTimes(2));
    expect(view.result.current.ready).toBe(false);
    view.rerender({ current: { ...input(query(saved)), revision: 23 }, active: true });
    expect(await repository.load(identity)).toBeUndefined();
    await act(async () => accept({ intent_id: "reset", status: "accepted" }));
    expect(view.result.current.ready).toBe(false);
    expect(await repository.load(identity)).toBeUndefined();
    view.rerender({ current: input(query()), active: true });
    await waitFor(() => expect(view.result.current.ready).toBe(true));
    expect(await repository.load(identity)).toBeUndefined();
  });

  it("exposes a rejected restore for retry without indefinitely hiding controls or losing saved filters", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    await repository.save({ ...identity, draftSchema: schema, value: { ...saved } });
    const view = renderBrowsing(repository, input(query(), false));
    view.onRestore.mockResolvedValueOnce({ intent_id: "restore", status: "unavailable", message: "Task view is offline." });
    await waitFor(() => expect(view.result.current.error).toBe("Task view is offline."));
    expect(view.result.current.ready).toBe(true);
    expect((await repository.load(identity))?.value).toEqual(saved);
    await act(async () => view.result.current.retry());
    await waitFor(() => expect(view.onRestore).toHaveBeenCalledTimes(2));
    expect(view.result.current.ready).toBe(false);
    view.rerender({ current: input(query(saved)), active: true });
    await waitFor(() => expect(view.result.current.ready).toBe(true));
    expect(view.result.current.error).toBeUndefined();
    expect((await repository.load(identity))?.value).toEqual(saved);
  });

  it.each(["rejected", "thrown"])("restores saved preferences after a %s card reset, then allows a successful retry", async (failure) => {
    const repository = new InMemoryWidgetDraftRepository();
    await repository.save({ ...identity, draftSchema: schema, value: { ...saved } });
    const view = renderBrowsing(repository, input(query(saved)));
    await waitFor(() => expect(view.result.current.hasDirtyDraft).toBe(true));
    let fail!: () => void;
    view.onRestore.mockImplementationOnce(() => new Promise((resolve, reject) => {
      fail = () => failure === "thrown" ? reject(new Error("Reset is offline."))
        : resolve({ intent_id: "reset", status: "unavailable", message: "Reset is offline." });
    }));
    await act(async () => expect(await view.result.current.clearAll()).toBe(true));
    expect(await repository.load(identity)).toBeUndefined();
    expect(view.result.current.ready).toBe(false);
    await act(async () => fail());
    await waitFor(async () => expect((await repository.load(identity))?.value).toEqual(saved));
    expect(view.result.current.ready).toBe(true);
    expect(view.result.current.error).toBe("Reset is offline.");
    expect(view.result.current.canRetry).toBe(true);
    expect(view.result.current.hasDirtyDraft).toBe(true);

    await act(async () => view.result.current.retry());
    await waitFor(() => expect(view.onRestore).toHaveBeenCalledTimes(2));
    expect(view.onRestore).toHaveBeenLastCalledWith(taskBrowsePatch(DEFAULT_TASK_BROWSE_STATE));
    view.rerender({ current: input(query()), active: true });
    await waitFor(() => expect(view.result.current.ready).toBe(true));
    await waitFor(async () => expect(await repository.load(identity)).toBeUndefined());
    expect(view.result.current.error).toBeUndefined();
  });

  it("does not restore old saved filters when a delayed reset failure follows a newer explicit query", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    await repository.save({ ...identity, draftSchema: schema, value: { ...saved } });
    const view = renderBrowsing(repository, input(query(saved)));
    await waitFor(() => expect(view.result.current.hasDirtyDraft).toBe(true));
    let fail!: (result: IntentResult) => void;
    view.onRestore.mockImplementationOnce(() => new Promise((resolve) => { fail = resolve; }));
    await act(async () => expect(await view.result.current.clearAll()).toBe(true));
    const newer = query({ q: "New choice", sort: "updated_at", direction: "asc" });
    view.rerender({ current: input(newer), active: true });
    await act(async () => fail({ intent_id: "reset", status: "unavailable", message: "Old reset failed." }));
    await waitFor(async () => expect((await repository.load(identity))?.value).toEqual(taskBrowseState(newer)));
    expect(view.result.current.ready).toBe(true);
    expect(view.result.current.error).toBeUndefined();
  });
});

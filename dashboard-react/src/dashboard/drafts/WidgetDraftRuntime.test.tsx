import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { CAPTURE_APP_CONTRIBUTION } from "../../widget-library/capture/contribution";
import {
  asViewId,
  asWidgetInstanceId,
} from "../contributions/contracts";
import { InMemoryWidgetDraftRepository } from "./repository";
import type { WidgetDraftRepository } from "./contracts";
import {
  WidgetDraftRuntimeProvider,
  WidgetDraftScopeProvider,
  useWidgetDraft,
} from "./WidgetDraftRuntime";

const definition = CAPTURE_APP_CONTRIBUTION.widgetDefinitions[0];
const viewId = asViewId("wb.journal.main");
const instanceId = asWidgetInstanceId("journal:capture");
const emptyDraft = { text: "", targetId: "log", mode: "dumb" };
const savedDraft = { text: "Keep this draft", targetId: "log", mode: "dumb" };

function renderDraft(repository: WidgetDraftRepository) {
  return renderHook(
    () => useWidgetDraft("capture", emptyDraft, { isPristine: (value) => value.text.length === 0 }),
    {
      wrapper: ({ children }: { readonly children: ReactNode }) => (
        <WidgetDraftRuntimeProvider repository={repository}>
          <WidgetDraftScopeProvider
            definition={definition}
            viewId={viewId}
            instanceId={instanceId}
            input={{ dayId: "reset-test" }}
          >
            {children}
          </WidgetDraftScopeProvider>
        </WidgetDraftRuntimeProvider>
      ),
    },
  );
}

async function renderSavedDraft(repository: WidgetDraftRepository) {
  const rendered = renderDraft(repository);
  await waitFor(() => expect(rendered.result.current.ready).toBe(true));
  await act(async () => {
    rendered.result.current.setValue(savedDraft);
    await rendered.result.current.flush();
  });
  expect(rendered.result.current.status).toBe("saved");
  return rendered;
}

function DraftEditor() {
  const draft = useWidgetDraft(
    "capture",
    { text: "", targetId: "log", mode: "dumb" },
    { isPristine: (value) => value.text.length === 0 },
  );
  if (!draft.ready) return <p>Loading draft</p>;
  return (
    <>
      <label>
        Draft
        <input
          aria-label="Draft"
          value={draft.value.text}
          onChange={(event) =>
            draft.setValue((current) => ({ ...current, text: event.target.value }))
          }
        />
      </label>
      <output aria-label="Draft status">{draft.status}</output>
      <output aria-label="Draft revision">{draft.revision}</output>
    </>
  );
}

function Harness({
  repository,
  dayId = "2026-07-11",
}: {
  readonly repository: WidgetDraftRepository;
  readonly dayId?: string;
}) {
  return (
    <WidgetDraftRuntimeProvider repository={repository}>
      <WidgetDraftScopeProvider
        definition={definition}
        viewId={viewId}
        instanceId={instanceId}
        input={{ dayId }}
      >
        <DraftEditor />
      </WidgetDraftScopeProvider>
    </WidgetDraftRuntimeProvider>
  );
}

function FailingEditor() {
  const draft = useWidgetDraft("capture", { text: "", targetId: "log", mode: "dumb" }, {
    isPristine: (value) => value.text.length === 0,
  });
  const [flushResult, setFlushResult] = useState("not-run");
  if (!draft.ready) return <p>Loading draft</p>;
  return (
    <>
      <button type="button" onClick={() => draft.setValue({ ...draft.value, text: "unsafe" })}>
        Change
      </button>
      <button
        type="button"
        onClick={() => {
          void draft.flush().then(
            () => setFlushResult("resolved"),
            () => setFlushResult("rejected"),
          );
        }}
      >
        Flush
      </button>
      <output>{draft.status}</output>
      <output>{flushResult}</output>
    </>
  );
}

describe("WidgetDraftRuntime", () => {
  it.each([
    { name: "replacement fields", value: { text: "Retained later edit", targetId: "notes", mode: "smart" } },
    { name: "a pristine-shaped replacement", value: { text: "", targetId: "notes", mode: "smart" } },
  ])("resets to $name with one CAS save and no durable delete", async ({ value }) => {
    const repository = new InMemoryWidgetDraftRepository();
    const { result } = await renderSavedDraft(repository);
    const before = result.current.getSnapshot();
    const stored = await repository.load(result.current.identity);
    const save = vi.spyOn(repository, "save");
    const remove = vi.spyOn(repository, "delete");
    const onReset = vi.fn(() => {
      expect(result.current.getSnapshot()).toEqual(before);
      expect(save).not.toHaveBeenCalled();
      expect(remove).not.toHaveBeenCalled();
    });
    result.current.subscribeReset(onReset);
    let reset: Promise<boolean>;

    act(() => {
      reset = result.current.reset(value, { ifRevision: before.revision });
      expect(onReset).toHaveBeenCalledTimes(1);
      expect(result.current.getSnapshot()).toMatchObject({ value, revision: before.revision + 1, status: "saving" });
    });
    await act(async () => expect(await reset!).toBe(true));

    expect(save).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledWith(expect.objectContaining({
      ...result.current.identity, value, expectedRevision: stored!.revision,
    }));
    expect(remove).not.toHaveBeenCalled();
    expect(await repository.load(result.current.identity)).toMatchObject({ value, revision: stored!.revision + 1 });
    expect(result.current.status).toBe("saved");
    expect(result.current.value).toEqual(value);
  });

  it.each(["reset", "clear"] as const)("rejects stale %s without persistence or reset notifications", async (operation) => {
    const repository = new InMemoryWidgetDraftRepository();
    const { result } = await renderSavedDraft(repository);
    const reviewedRevision = result.current.revision;
    const later = { ...savedDraft, text: "A newer manual edit" };
    await act(async () => {
      result.current.setValue(later);
      await result.current.flush();
    });
    const before = result.current.getSnapshot();
    const stored = await repository.load(result.current.identity);
    const save = vi.spyOn(repository, "save");
    const remove = vi.spyOn(repository, "delete");
    const onReset = vi.fn();
    result.current.subscribeReset(onReset);

    await act(async () => {
      const accepted = operation === "reset"
        ? await result.current.reset(emptyDraft, { ifRevision: reviewedRevision })
        : await result.current.clear({ ifRevision: reviewedRevision });
      expect(accepted).toBe(false);
    });

    expect(onReset).not.toHaveBeenCalled();
    expect(save).not.toHaveBeenCalled();
    expect(remove).not.toHaveBeenCalled();
    expect(result.current.getSnapshot()).toEqual(before);
    expect(await repository.load(result.current.identity)).toEqual(stored);
  });

  it("reports a failed replacement save while retaining both the previous durable record and in-memory edits", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const { result } = await renderSavedDraft(repository);
    const stored = await repository.load(result.current.identity);
    const value = { ...savedDraft, text: "Keep this unsaved replacement", targetId: "notes" };
    const save = vi.spyOn(repository, "save").mockRejectedValueOnce(new Error("quota unavailable"));
    const remove = vi.spyOn(repository, "delete");
    const onReset = vi.fn();
    result.current.subscribeReset(onReset);

    await act(async () => {
      expect(await result.current.reset(value, { ifRevision: result.current.revision })).toBe(false);
    });

    expect(onReset).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledTimes(1);
    expect(remove).not.toHaveBeenCalled();
    expect(await repository.load(result.current.identity)).toEqual(stored);
    expect(result.current.value).toEqual(value);
    expect(result.current.status).toBe("error");
    expect(result.current.error).toContain("quota unavailable");
    await expect(result.current.flush()).rejects.toThrow("quota unavailable");
  });

  it("does not overwrite an edit made while a same-revision reset waits for its replacement save", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const { result } = await renderSavedDraft(repository);
    const before = result.current.getSnapshot();
    const stored = await repository.load(result.current.identity);
    const saveToRepository = repository.save.bind(repository);
    let releaseSave!: () => void;
    const gate = new Promise<void>((resolve) => { releaseSave = resolve; });
    const save = vi.spyOn(repository, "save").mockImplementationOnce(async (request) => {
      await gate;
      return saveToRepository(request);
    });
    const remove = vi.spyOn(repository, "delete");
    const replacement = { ...savedDraft, text: "Retained replacement" };
    const later = { ...replacement, text: "Manual edit while reset is saving" };
    let reset: Promise<boolean>;
    act(() => {
      reset = result.current.reset(replacement, { ifRevision: before.revision });
    });
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(await repository.load(result.current.identity)).toEqual(stored);
    act(() => { result.current.setValue(later); });
    expect(result.current.getSnapshot()).toMatchObject({ value: later, revision: before.revision + 2 });

    await act(async () => {
      releaseSave();
      expect(await reset!).toBe(false);
      await result.current.flush();
    });

    expect(result.current.value).toEqual(later);
    expect(result.current.status).toBe("saved");
    expect(await repository.load(result.current.identity)).toMatchObject({ value: later, revision: stored!.revision + 2 });
    expect(save).toHaveBeenCalledTimes(2);
    expect(save.mock.calls[0]?.[0]).toMatchObject({ value: replacement, expectedRevision: stored!.revision });
    expect(save.mock.calls[1]?.[0]).toMatchObject({ value: later, expectedRevision: stored!.revision + 1 });
    expect(remove).not.toHaveBeenCalled();
  });

  it("preserves another surface's durable revision when the reset replacement loses repository CAS", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const { result } = await renderSavedDraft(repository);
    const stored = await repository.load(result.current.identity);
    const external = await repository.save({
      ...result.current.identity,
      draftSchema: result.current.schema,
      value: { ...savedDraft, text: "Saved by another surface" },
      expectedRevision: stored!.revision,
    });
    const remove = vi.spyOn(repository, "delete");
    const replacement = { ...savedDraft, text: "Local replacement to recover" };

    await act(async () => {
      expect(await result.current.reset(replacement, { ifRevision: result.current.revision })).toBe(false);
    });

    expect(result.current.status).toBe("conflict");
    expect(result.current.value).toEqual(replacement);
    expect(await repository.load(result.current.identity)).toEqual(external);
    expect(remove).not.toHaveBeenCalled();
    await expect(result.current.flush()).rejects.toThrow("changed in another dashboard surface");
  });

  it("keeps clear as a synchronous lifetime reset followed by the existing durable delete", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const { result } = await renderSavedDraft(repository);
    const before = result.current.getSnapshot();
    const stored = await repository.load(result.current.identity);
    const save = vi.spyOn(repository, "save");
    const remove = vi.spyOn(repository, "delete");
    const onReset = vi.fn(() => expect(result.current.getSnapshot()).toEqual(before));
    result.current.subscribeReset(onReset);
    let cleared: Promise<boolean>;

    act(() => {
      cleared = result.current.clear({ ifRevision: before.revision });
      expect(onReset).toHaveBeenCalledTimes(1);
      expect(result.current.getSnapshot()).toMatchObject({ value: emptyDraft, revision: before.revision + 1, status: "saving" });
    });
    await act(async () => expect(await cleared!).toBe(true));

    expect(save).not.toHaveBeenCalled();
    expect(remove).toHaveBeenCalledTimes(1);
    expect(remove).toHaveBeenCalledWith(result.current.identity, stored!.revision);
    expect(await repository.load(result.current.identity)).toBeUndefined();
    expect(result.current.value).toEqual(emptyDraft);
    expect(result.current.status).toBe("pristine");
  });

  it("restores the exact structured value after the renderer unmounts", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const first = render(<Harness repository={repository} />);
    const input = await screen.findByRole("textbox", { name: "Draft" });
    await userEvent.type(input, "  exact draft  ");
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Draft status" })).toHaveTextContent(
        "saved",
      ),
    );
    first.unmount();

    render(<Harness repository={repository} />);
    expect(await screen.findByRole("textbox", { name: "Draft" })).toHaveValue(
      "  exact draft  ",
    );
  });

  it("scopes the same widget instance independently by the declared input field", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const rendered = render(<Harness repository={repository} dayId="day-1" />);
    await userEvent.type(await screen.findByRole("textbox", { name: "Draft" }), "day one");
    await waitFor(() => expect(screen.getByText("saved")).toBeInTheDocument());

    rendered.rerender(<Harness repository={repository} dayId="day-2" />);
    expect(await screen.findByRole("textbox", { name: "Draft" })).toHaveValue("");

    rendered.rerender(<Harness repository={repository} dayId="day-1" />);
    expect(await screen.findByRole("textbox", { name: "Draft" })).toHaveValue("day one");
  });

  it("rejects flush when device persistence fails so callers cannot dispatch unsafely", async () => {
    const repository: WidgetDraftRepository = {
      load: async () => undefined,
      save: async () => {
        throw new Error("quota unavailable");
      },
      delete: async () => undefined,
    };
    render(
      <WidgetDraftRuntimeProvider repository={repository}>
        <WidgetDraftScopeProvider
          definition={definition}
          viewId={viewId}
          instanceId={instanceId}
          input={{ dayId: "day-1" }}
        >
          <FailingEditor />
        </WidgetDraftScopeProvider>
      </WidgetDraftRuntimeProvider>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Change" }));
    await waitFor(() => expect(screen.getByText("error")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Flush" }));
    expect(await screen.findByText("rejected")).toBeInTheDocument();
  });
});

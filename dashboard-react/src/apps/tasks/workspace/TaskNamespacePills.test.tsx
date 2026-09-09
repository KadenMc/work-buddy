import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { RefObject } from "react";
import { describe, expect, it, vi } from "vitest";
import type { IntentResult } from "../../../dashboard/contributions/contracts";
import { DashboardHelpProvider } from "../../../dashboard/help";
import type { TaskSummary } from "../contracts";
import { TaskNamespacePills } from "./TaskNamespacePills";
import { TaskList } from "./TaskList";

const options = [{ value: "research", label: "Research" }, { value: "personal/review", label: "Personal review" }];
const accepted = (): IntentResult => ({ intent_id: "saved", status: "accepted" });
const dialog = () => screen.getByRole("alertdialog", { name: "Remove namespace from this task?" });

describe("Task namespace pills", () => {
  it("confirms one-task removal, defaults to Cancel, restores focus and then supports adding back", async () => {
    const user = userEvent.setup(); const onChange = vi.fn(async () => accepted());
    const namespaces = ["research", "personal/review"];
    render(<TaskNamespacePills namespaces={namespaces} options={options} onChange={onChange} />);
    const remove = screen.getByRole("button", { name: "Remove research from this task" });
    await user.click(remove);
    expect(dialog()).toHaveTextContent("The namespace and other tasks remain unchanged");
    await waitFor(() => expect(within(dialog()).getByRole("button", { name: "Cancel" })).toHaveFocus());
    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    await waitFor(() => expect(remove).toHaveFocus());
    expect(onChange).not.toHaveBeenCalled();
    await user.click(remove);
    await user.click(within(dialog()).getByRole("button", { name: "Remove namespace" }));
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1));
    expect(onChange).toHaveBeenCalledWith(["personal/review"], expect.any(String));
    expect(namespaces).toEqual(["research", "personal/review"]);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Remove research from this task" })).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("button", { name: "Add namespace to this task" })).toHaveFocus());
    await user.click(screen.getByRole("button", { name: "Add namespace to this task" }));
    await user.click(screen.getByRole("button", { name: "Add research to this task" }));
    expect(onChange).toHaveBeenLastCalledWith(["personal/review", "research"], expect.any(String));
  });

  it("pins removal fields, callback and mutation ID across parent changes and uncertain retries", async () => {
    const user = userEvent.setup();
    const original = vi.fn().mockRejectedValueOnce(new Error("Connection interrupted")).mockResolvedValue(accepted());
    const latest = vi.fn(async () => accepted());
    const view = render(<TaskNamespacePills namespaces={["research", "personal/review"]} options={options} onChange={original} />);
    await user.click(screen.getByRole("button", { name: "Remove research from this task" }));
    view.rerender(<TaskNamespacePills namespaces={["research", "newer"]} options={options} onChange={latest} disabled />);
    await user.click(within(dialog()).getByRole("button", { name: "Remove namespace" }));
    expect(await screen.findByText(/Connection interrupted/)).toBeInTheDocument();
    await user.click(within(dialog()).getByRole("button", { name: "Retry removal" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(original).toHaveBeenCalledTimes(2);
    expect(original.mock.calls[0]).toEqual(original.mock.calls[1]);
    expect(original.mock.calls[0]).toEqual([["personal/review"], expect.any(String)]);
    expect(latest).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Remove newer from this task" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove personal/review from this task" })).not.toBeInTheDocument();
  });

  it("keeps a conflict reviewable and honors a permission change before confirming", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn(async (): Promise<IntentResult> => ({ intent_id: "conflict", status: "conflict", message: "Task changed elsewhere." }));
    const view = render(<TaskNamespacePills namespaces={["research"]} options={options} onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "Remove research from this task" }));
    view.rerender(<TaskNamespacePills namespaces={["research"]} options={options} onChange={onChange} readOnly />);
    expect(within(dialog()).getByRole("button", { name: "Remove namespace" })).toBeDisabled();
    await user.click(within(dialog()).getByRole("button", { name: "Remove namespace" }));
    expect(onChange).not.toHaveBeenCalled();
    view.rerender(<TaskNamespacePills namespaces={["research"]} options={options} onChange={onChange} />);
    await user.click(within(dialog()).getByRole("button", { name: "Remove namespace" }));
    expect(await screen.findByText(/Task changed elsewhere/)).toBeInTheDocument();
    expect(within(dialog()).getByRole("button", { name: "Retry removal" })).toBeDisabled();
    expect(onChange).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(within(dialog()).getByRole("button", { name: "Cancel" })).toHaveFocus());
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });

  it("fences duplicate confirmation and dismissal while a removal is pending", async () => {
    const user = userEvent.setup(); let finish!: (value: IntentResult) => void;
    const onChange = vi.fn(() => new Promise<IntentResult>((resolve) => { finish = resolve; }));
    render(<TaskNamespacePills namespaces={["research"]} options={options} onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "Remove research from this task" }));
    await user.dblClick(within(dialog()).getByRole("button", { name: "Remove namespace" }));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(within(dialog()).getByRole("button", { name: "Cancel" })).toBeDisabled();
    await user.keyboard("{Escape}");
    expect(dialog()).toBeInTheDocument();
    await act(async () => finish(accepted()));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(screen.getByText("No namespaces")).toBeInTheDocument();
  });

  it("portals the searchable picker outside clipping content and excludes existing assignments", async () => {
    const user = userEvent.setup(); const onChange = vi.fn(async () => accepted());
    render(<div data-testid="clipped-form" style={{ overflow: "hidden" }}><TaskNamespacePills namespaces={["research"]} options={options} onChange={onChange} /></div>);
    await user.click(screen.getByRole("button", { name: "Add namespace to this task" }));
    const picker = await screen.findByRole("dialog", { name: "Add namespace" });
    expect(within(screen.getByTestId("clipped-form")).queryByRole("dialog")).not.toBeInTheDocument();
    expect(within(picker).queryByRole("button", { name: "Add research to this task" })).not.toBeInTheDocument();
    await user.type(within(picker).getByRole("searchbox", { name: "Find or create namespace" }), "personal/review");
    expect(within(picker).queryByRole("button", { name: "Create and add namespace" })).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
    await user.click(within(picker).getByRole("button", { name: "Add personal/review to this task" }));
    expect(onChange).toHaveBeenCalledWith(["research", "personal/review"], expect.any(String));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("requires explicit creation and retains an uncertain addition for the same safe retry", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn().mockRejectedValueOnce(new Error("Response lost")).mockResolvedValue(accepted());
    const newer = vi.fn();
    const view = render(<TaskNamespacePills namespaces={["research"]} options={options} onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "Add namespace to this task" }));
    const search = screen.getByRole("searchbox", { name: "Find or create namespace" });
    await user.type(search, "new/");
    expect(screen.getByRole("button", { name: "Create and add namespace" })).toBeDisabled();
    await user.type(search, "branch");
    expect(onChange).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Create and add namespace" }));
    await screen.findByText(/Response lost/);
    view.rerender(<TaskNamespacePills namespaces={["research", "remote"]} options={options} onChange={newer} disabled />);
    await user.click(screen.getByRole("button", { name: "Retry adding namespace" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(onChange).toHaveBeenCalledTimes(2);
    expect(onChange.mock.calls[0]).toEqual([["research", "new/branch"], expect.any(String)]);
    expect(onChange.mock.calls[1]).toEqual(onChange.mock.calls[0]);
    expect(newer).not.toHaveBeenCalled();
  });

  it("explains removal in Help mode without opening the confirmation or changing assignments", async () => {
    const user = userEvent.setup(); const onChange = vi.fn();
    render(<DashboardHelpProvider enabled><TaskNamespacePills namespaces={["research"]} options={options} onChange={onChange} /></DashboardHelpProvider>);
    await user.hover(screen.getByRole("button", { name: "Remove research from this task" }));
    expect(await screen.findByRole("tooltip", {}, { timeout: 3000 })).toHaveTextContent("does not delete the namespace or change other tasks");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("keeps row namespace controls outside navigation and removes bulk selection checkboxes", async () => {
    const user = userEvent.setup(); const onSelect = vi.fn(); const onNamespacesChange = vi.fn(async () => accepted());
    const task: TaskSummary = { task_id: "task-1", title: "Review findings", revision: 1, attention_state: "active", urgency: "high", due_date: null, deadline_date: null, snooze_until: null, project: null, namespaces: ["research"], tags: [], current_action: null, has_document: false, completed_at: null, archived_at: null, deleted_at: null, updated_at: "2026-09-09T10:00:00Z", created_at: "2026-09-09T09:00:00Z" };
    const refs: RefObject<Map<string, HTMLButtonElement>> = { current: new Map() };
    const view = render(<TaskList tasks={[task]} triage={false} readOnly={false} focusRefs={refs} namespaceOptions={options} onNamespacesChange={onNamespacesChange} onSelect={onSelect} onAction={vi.fn()} onSkip={vi.fn()} />);
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(view.container.querySelector("button button")).toBeNull();
    const navigation = view.container.querySelector<HTMLButtonElement>(".wb-task-list__select")!;
    const pills = screen.getByRole("group", { name: "Task namespaces" });
    expect(pills.parentElement).toBe(navigation.parentElement);
    expect(within(pills).getAllByRole("button")[0]).toHaveAccessibleName("Add namespace to this task");
    expect(navigation).toHaveTextContent("high"); expect(navigation).toHaveTextContent("Created");
    await user.click(screen.getByRole("button", { name: "Remove research from this task" }));
    expect(onSelect).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    await user.click(navigation);
    expect(onSelect).toHaveBeenCalledWith("task-1");
    expect(onNamespacesChange).not.toHaveBeenCalled();
  });
});

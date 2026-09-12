import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TaskNamespaceNode } from "../contracts";
import { MultiSelect, NamespaceRail } from "./TaskFilters";

const projects = [{ value: "1", label: "ECG research" }, { value: "2", label: "Work Buddy" }];

function ProjectEditor() {
  const [values, setValues] = useState<string[]>([]);
  return <div data-testid="scrolled-task-form" style={{ overflow: "hidden", transform: "translateY(-290px)" }}>
    <MultiSelect label="Linked projects" purpose="selection" values={values} options={projects} searchable onChange={setValues} />
    <output aria-label="Selected project IDs">{values.join(",")}</output>
  </div>;
}

describe("Task multi-select overlays", () => {
  it("portals the editor picker outside a scrolled or clipping form and retains both selected projects", async () => {
    const user = userEvent.setup();
    render(<ProjectEditor />);
    const trigger = screen.getByRole("button", { name: "Linked projects, none selected" });
    await user.click(trigger);
    const picker = await screen.findByRole("dialog", { name: "Linked projects options" });
    expect(within(screen.getByTestId("scrolled-task-form")).queryByRole("dialog")).not.toBeInTheDocument();
    expect(within(picker).getByText("Select projects")).toBeInTheDocument();
    expect(within(picker).getByText("No linked projects")).toBeInTheDocument();
    expect(within(picker).queryByText(/match any selected|Any linked projects/)).not.toBeInTheDocument();
    await user.click(within(picker).getByRole("checkbox", { name: "ECG research" }));
    await user.click(within(picker).getByRole("checkbox", { name: "Work Buddy" }));
    expect(screen.getByLabelText("Selected project IDs")).toHaveTextContent("1,2");
    await user.click(within(picker).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("button", { name: "Linked projects, 2 selected" })).toHaveFocus());
  });

  it("keeps match-any semantics for browsing filters and restores focus on Escape", async () => {
    const user = userEvent.setup();
    render(<MultiSelect label="Projects" values={[]} options={projects} onChange={() => undefined} />);
    const trigger = screen.getByRole("button", { name: "Projects, any" });
    await user.click(trigger);
    const picker = await screen.findByRole("dialog", { name: "Projects options" });
    expect(within(picker).getByText("Projects · match any selected")).toBeInTheDocument();
    expect(within(picker).getByText("Any projects")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});

const namespaceNodes: TaskNamespaceNode[] = [
  { path: "138", parent: null, label: "138", count: 1, direct_count: 1 },
  { path: "projects", parent: null, label: "projects", count: 8, direct_count: 1 },
  { path: "projects/", parent: "projects", label: "", count: 2, direct_count: 2 },
  { path: "projects/ecg", parent: "projects", label: "ecg", count: 5, direct_count: 0 },
  { path: "projects/ecg/analysis", parent: "projects/ecg", label: "analysis", count: 5, direct_count: 5 },
];

describe("Namespace hierarchy", () => {
  beforeEach(() => localStorage.removeItem("wb.tasks.namespace-expanded"));
  const tree = (onChange = vi.fn()) => <NamespaceRail nodes={namespaceNodes} selected={[]} exact={[]} noNamespaceCount={3} onChange={onChange} onHide={vi.fn()} onManage={vi.fn()} />;

  function StatefulRail() {
    const [selected, setSelected] = useState<readonly string[]>([]);
    const [exact, setExact] = useState<readonly string[]>([]);
    return <NamespaceRail nodes={namespaceNodes} selected={selected} exact={exact} onChange={(next, direct) => { setSelected(next); setExact(direct); }} onHide={vi.fn()} onManage={vi.fn()} />;
  }

  it("cycles branch, exact and unchecked with pointer or Space, then clears all namespace scopes together", async () => {
    const user = userEvent.setup();
    render(<StatefulRail />);
    const clear = screen.getByRole("button", { name: "Clear namespace filters" });
    expect(clear).toBeDisabled();
    const checkbox = screen.getByRole("checkbox", { name: "projects and descendants" });
    expect(checkbox).not.toBeChecked();
    await user.click(checkbox);
    expect(checkbox).toBeChecked();
    expect(checkbox).toHaveAttribute("aria-checked", "true");
    await user.keyboard(" ");
    expect(screen.getByRole("checkbox", { name: "projects only" })).toBePartiallyChecked();
    expect(checkbox).toHaveAttribute("aria-description", expect.stringContaining("This namespace only"));
    await user.keyboard(" ");
    expect(checkbox).not.toBeChecked();
    expect(checkbox).not.toBePartiallyChecked();
    expect(clear).toBeDisabled();
    await user.click(checkbox);
    await user.click(checkbox);
    await user.click(screen.getByRole("checkbox", { name: "138 and descendants" }));
    await user.click(screen.getByRole("checkbox", { name: "No namespace" }));
    await user.click(clear);
    expect(screen.getAllByRole("checkbox").every((input) => !(input as HTMLInputElement).checked && !(input as HTMLInputElement).indeterminate)).toBe(true);
    expect(clear).toBeDisabled();
    expect(screen.queryByText("Including descendants")).not.toBeInTheDocument();
    expect(screen.queryByText("Selecting a branch includes its descendants.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Change scope/ })).not.toBeInTheDocument();
  });

  it("starts every level collapsed and remembers deliberate expansions across remounts", async () => {
    const user = userEvent.setup(); const onChange = vi.fn();
    const view = render(tree(onChange));
    expect(screen.getByRole("checkbox", { name: "projects and descendants" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "138 and descendants" })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "projects/ecg and descendants" })).not.toBeInTheDocument();
    const expand = screen.getByRole("button", { name: "Expand projects" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    await user.click(expand);
    expect(screen.getByRole("checkbox", { name: "projects/ecg and descendants" })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "projects/ecg/analysis and descendants" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Expand projects/ecg" }));
    expect(screen.getByRole("checkbox", { name: "projects/ecg/analysis and descendants" })).toBeInTheDocument();
    view.unmount();
    render(tree(onChange));
    expect(screen.getByRole("button", { name: "Collapse projects" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "Collapse projects/ecg" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("checkbox", { name: "projects/ecg/analysis and descendants" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Collapse projects" }));
    expect(screen.queryByRole("checkbox", { name: "projects/ecg/analysis and descendants" })).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("reveals search matches temporarily and restores the saved expansion after clearing", async () => {
    const user = userEvent.setup(); const onChange = vi.fn();
    localStorage.setItem("wb.tasks.namespace-expanded", JSON.stringify(["projects"]));
    render(tree(onChange));
    const search = screen.getByRole("searchbox", { name: "Find namespaces" });
    expect(screen.getByRole("button", { name: "Expand projects/ecg" })).toBeInTheDocument();
    await user.type(search, "analysis");
    expect(screen.getByRole("checkbox", { name: "projects/ecg/analysis and descendants" })).toBeInTheDocument();
    const temporaryExpansion = screen.getByRole("button", { name: "Collapse projects/ecg" });
    expect(temporaryExpansion).toHaveAttribute("aria-disabled", "true");
    await user.click(temporaryExpansion);
    expect(JSON.parse(localStorage.getItem("wb.tasks.namespace-expanded")!)).toEqual(["projects"]);
    await user.clear(search);
    expect(screen.getByRole("button", { name: "Collapse projects" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Expand projects/ecg" })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "projects/ecg/analysis and descendants" })).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("labels imported empty segments without changing the exact namespace selected", async () => {
    const user = userEvent.setup(); const onChange = vi.fn();
    render(tree(onChange));
    await user.click(screen.getByRole("button", { name: "Expand projects" }));
    const malformed = screen.getByRole("checkbox", { name: "projects/ and descendants" });
    expect(malformed.closest("label")).toHaveTextContent("(empty segment)");
    expect(malformed.closest("label")).toHaveAttribute("title", expect.stringContaining("projects/: 2 direct"));
    await user.click(malformed);
    expect(onChange).toHaveBeenLastCalledWith(["projects/"], []);
    await user.click(screen.getByRole("checkbox", { name: "138 and descendants" }));
    expect(onChange).toHaveBeenLastCalledWith(["138"], []);
    expect(screen.getByRole("checkbox", { name: "No namespace" }).closest("label")).toHaveTextContent("3");
  });
});

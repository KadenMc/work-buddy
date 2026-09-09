import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { MultiSelect } from "./TaskFilters";

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

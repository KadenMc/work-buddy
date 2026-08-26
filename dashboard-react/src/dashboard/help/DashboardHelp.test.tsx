import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DashboardHelpProvider, HelpTarget } from "./DashboardHelp";

describe("HelpTarget", () => {
  it("keeps field validation associated while keyboard help opens and closes", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <DashboardHelpProvider enabled>
        <label htmlFor="help-title">Title</label>
        <HelpTarget content={{ summary: "Name the draft.", details: "Enter a short title before saving." }}>
          <input id="help-title" aria-invalid="true" aria-describedby="help-title-error" onChange={onChange} />
        </HelpTarget>
        <p id="help-title-error">A title is required.</p>
      </DashboardHelpProvider>,
    );
    const title = screen.getByRole("textbox", { name: "Title" });
    expect(title).toHaveAccessibleDescription("A title is required.");
    await user.tab();
    expect(title).toHaveFocus();
    const tooltip = await screen.findByRole("tooltip");
    expect(title.getAttribute("aria-describedby")?.split(" ")).toEqual(["help-title-error", tooltip.id]);
    expect(title).toHaveAccessibleDescription(/A title is required.*Enter a short title before saving/);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument());
    expect(title).toHaveAccessibleDescription("A title is required.");
    await user.keyboard("Reviewed title");
    expect(title).toHaveValue("Reviewed title");
    expect(onChange).toHaveBeenCalled();
  });
});

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DashboardHelpProvider } from "../../../dashboard/help";
import { TaskButton, TASK_HELP } from "./TaskHelp";

describe("Task action help", () => {
  it("keeps a native completion hint with Help off and uses contextual help when enabled", async () => {
    const user = userEvent.setup(); const onClick = vi.fn();
    const tree = (enabled: boolean) => <DashboardHelpProvider enabled={enabled}><TaskButton help={TASK_HELP.complete} aria-label="Complete example task" onClick={onClick}>✓</TaskButton></DashboardHelpProvider>;
    const view = render(tree(false));
    let button = screen.getByRole("button", { name: "Complete example task" });
    expect(button).toHaveAttribute("title", TASK_HELP.complete.summary);
    await user.hover(button);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    view.rerender(tree(true));
    button = screen.getByRole("button", { name: "Complete example task" });
    expect(button).toHaveAttribute("title", "");
    await user.hover(document.body); await user.hover(button);
    expect(await screen.findByRole("tooltip", {}, { timeout: 3000 })).toHaveTextContent("does not change anything until you confirm");
    expect(onClick).not.toHaveBeenCalled();
    view.rerender(tree(false));
    button = screen.getByRole("button", { name: "Complete example task" });
    await waitFor(() => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument());
    expect(button).toHaveAttribute("title", TASK_HELP.complete.summary);
    expect(onClick).not.toHaveBeenCalled();
  });
});

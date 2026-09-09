import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { Button } from "./Button";

describe("Button native hover hints", () => {
  it("passes title through to the native button and updates or removes it", async () => {
    const user = userEvent.setup(); const onClick = vi.fn(); const ref = createRef<HTMLButtonElement>();
    const tree = (title?: string, disabled = false) => <Button title={title} disabled={disabled} ref={ref} onClick={onClick}>Review action</Button>;
    const view = render(tree("Review before changing anything."));
    const button = screen.getByRole("button", { name: "Review action" });
    expect(button).toHaveAttribute("title", "Review before changing anything.");
    expect(ref.current).toBe(button);
    await user.click(button);
    await user.keyboard("{Enter}");
    expect(onClick).toHaveBeenCalledTimes(2);
    view.rerender(tree("Updated explanation.", true));
    expect(button).toHaveAttribute("title", "Updated explanation.");
    await user.click(button);
    expect(onClick).toHaveBeenCalledTimes(2);
    view.rerender(tree());
    expect(button).not.toHaveAttribute("title");
    expect(ref.current).toBe(button);
  });

  it("preserves caller DOM rendering, button props and the forwarded ref", () => {
    const ref = createRef<HTMLButtonElement>();
    render(<Button ref={ref} title="Native hint" render={(props, state) => <button {...props} data-custom-render={state.isDisabled ? "disabled" : "enabled"} />}>Custom action</Button>);
    const button = screen.getByRole("button", { name: "Custom action" });
    expect(button).toHaveAttribute("title", "Native hint");
    expect(button).toHaveAttribute("data-custom-render", "enabled");
    expect(ref.current).toBe(button);
  });
});

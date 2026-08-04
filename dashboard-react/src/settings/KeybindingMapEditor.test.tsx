import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { KeybindingMapEditor } from "./KeybindingMapEditor";

const commands = [
  { commandId: "previous", label: "Previous" },
  { commandId: "next", label: "Next" },
] as const;

describe("KeybindingMapEditor", () => {
  it("supports keyboard-only capture", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <KeybindingMapEditor
        commands={commands}
        value={{ previous: "j", next: "k" }}
        issues={[]}
        onChange={onChange}
      />,
    );

    const rebind = screen.getByRole("button", { name: "Rebind Next" });
    rebind.focus();
    await user.keyboard("{Enter}");
    expect(rebind).toHaveTextContent("Listening");
    await user.keyboard("n");

    expect(onChange).toHaveBeenCalledWith({ previous: "j", next: "n" });
    expect(rebind).toHaveTextContent("Rebind");
  });

  it("scopes error relationships per editor instance", () => {
    const issue = { commandId: "next", message: "Choose another shortcut." };
    render(
      <>
        <KeybindingMapEditor
          commands={commands}
          value={{ previous: "j", next: "j" }}
          issues={[issue]}
          onChange={vi.fn()}
        />
        <KeybindingMapEditor
          commands={commands}
          value={{ previous: "j", next: "j" }}
          issues={[issue]}
          onChange={vi.fn()}
        />
      </>,
    );

    const [first, second] = screen.getAllByRole("button", {
      name: "Rebind Next",
    });
    const firstErrorId = first?.getAttribute("aria-describedby");
    const secondErrorId = second?.getAttribute("aria-describedby");
    expect(firstErrorId).toBeTruthy();
    expect(secondErrorId).toBeTruthy();
    expect(firstErrorId).not.toBe(secondErrorId);
    expect(document.getElementById(firstErrorId ?? "")).toHaveTextContent(
      issue.message,
    );
    expect(document.getElementById(secondErrorId ?? "")).toHaveTextContent(
      issue.message,
    );
  });
});

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SelectField } from "./SelectField";

const TARGETS = [
  { value: "auto", label: "Auto", automatic: true },
  { value: "log", label: "Log" },
  { value: "running_notes", label: "Running Notes" },
] as const;

describe("SelectField automatic options", () => {
  it("marks the field when the selected option stands for a decision", () => {
    const { container } = render(
      <SelectField
        label="Destination"
        value="auto"
        options={[...TARGETS]}
        onChange={vi.fn()}
      />,
    );

    expect(container.querySelector(".wb-select-field--automatic")).not.toBeNull();
  });

  it("leaves the field unmarked when a literal destination is selected", () => {
    const { container } = render(
      <SelectField
        label="Destination"
        value="log"
        options={[...TARGETS]}
        onChange={vi.fn()}
      />,
    );

    expect(container.querySelector(".wb-select-field--automatic")).toBeNull();
  });

  it("marks the automatic row inside the open list", async () => {
    render(
      <SelectField
        label="Destination"
        value="log"
        options={[...TARGETS]}
        onChange={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button"));

    // The list is portalled out of the field, so an ancestor selector on the
    // field root cannot reach it. The row carries its own marker.
    const automatic = screen.getByRole("option", { name: /Auto/ });
    expect(automatic.className).toContain("wb-listbox__item--automatic");

    const literal = screen.getByRole("option", { name: /Running Notes/ });
    expect(literal.className).not.toContain("wb-listbox__item--automatic");
  });
});

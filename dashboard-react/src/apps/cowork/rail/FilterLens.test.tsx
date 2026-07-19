import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { FilterLens } from "./FilterLens";

const counts = { all: 5, suggestions: 3, flags: 1, claims: 1 };

describe("FilterLens", () => {
  it("renders every typed group with its count", () => {
    render(<FilterLens filter="all" counts={counts} onChange={vi.fn()} />);
    const all = screen.getByRole("button", { name: /All/ });
    const suggestions = screen.getByRole("button", { name: /Suggestions/ });
    const flags = screen.getByRole("button", { name: /Flags/ });
    const claims = screen.getByRole("button", { name: /Claims/ });
    expect(all.querySelector(".wb-cowork-rail__chip-count")).toHaveTextContent("5");
    expect(suggestions.querySelector(".wb-cowork-rail__chip-count")).toHaveTextContent("3");
    expect(flags.querySelector(".wb-cowork-rail__chip-count")).toHaveTextContent("1");
    expect(claims.querySelector(".wb-cowork-rail__chip-count")).toHaveTextContent("1");
  });

  it("marks the active filter pressed and the rest not", () => {
    render(
      <FilterLens filter="flags" counts={counts} onChange={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: /Flags/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: /All/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("emits the selected filter on click", async () => {
    const onChange = vi.fn();
    render(<FilterLens filter="all" counts={counts} onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: /Claims/ }));
    expect(onChange).toHaveBeenCalledWith("claims");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <FilterLens filter="all" counts={counts} onChange={vi.fn()} />,
    );
    await expectNoAccessibilityViolations(container);
  });
});

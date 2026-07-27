import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { CoworkViewChrome } from "./CoworkViewChrome";

const renderChrome = (ui: ReactElement) => render(<main>{ui}</main>);

describe("CoworkViewChrome", () => {
  it("names the view without adding a second persistence status system", () => {
    renderChrome(<CoworkViewChrome />);

    expect(
      screen.getByRole("heading", { level: 1, name: "Co-work" }),
    ).toBeVisible();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByText("Live")).toBeNull();
    expect(screen.queryByText("Local")).toBeNull();
    expect(screen.queryByText(/tracked AI proposals|review rail/i)).toBeNull();
  });

  it("places host-owned contextual actions in its actions region", () => {
    renderChrome(
      <CoworkViewChrome hostActions={<button type="button">Customize</button>} />,
    );
    expect(screen.getByRole("button", { name: "Customize" })).toBeVisible();
  });

  it("has no accessibility violations", async () => {
    const { container } = renderChrome(<CoworkViewChrome />);
    await expectNoAccessibilityViolations(container);
  });
});

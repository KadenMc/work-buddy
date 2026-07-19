import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DashboardAnnouncer } from "../accessibility/DashboardAnnouncer";
import { HelpTarget } from "./DashboardHelp";
import { HelpModeProvider } from "./HelpModeController";
import { HelpModeToggle } from "./HelpModeToggle";

const media = (matches: boolean): MediaQueryList =>
  ({
    matches,
    media: "",
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(() => true),
  }) as unknown as MediaQueryList;

afterEach(() => vi.unstubAllGlobals());

/** The toggle sits beside an ordinary HelpTarget, standing in for any view's region. */
function renderShell() {
  return render(
    <DashboardAnnouncer>
      <HelpModeProvider>
        <HelpModeToggle />
        <HelpTarget
          content={{
            summary: "Editor region.",
            details: "It is an editable document surface.",
          }}
          focusable
          ariaLabel="About the editor region"
        >
          <div>Editor region</div>
        </HelpTarget>
      </HelpModeProvider>
    </DashboardAnnouncer>,
  );
}

describe("HelpModeToggle", () => {
  it("flips shared hover help so a HelpTarget anywhere reveals on hover", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => media(false)));
    renderShell();

    const toggle = screen.getByRole("button", { name: "Hover help" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");

    // Off: the region carries no help affordance and reveals nothing on hover.
    await userEvent.hover(screen.getByText("Editor region"));
    expect(
      screen.queryByText("It is an editable document surface."),
    ).not.toBeInTheDocument();

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");

    const region = screen.getByLabelText("About the editor region");
    await userEvent.hover(region);
    expect(
      await screen.findByText("It is an editable document surface.", undefined, {
        timeout: 3000,
      }),
    ).toBeVisible();

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "false");
  });

  it("is disabled on narrow, hover-less viewports", () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => media(query === "(max-width: 767px)")),
    );
    renderShell();
    expect(screen.getByRole("button", { name: "Hover help" })).toBeDisabled();
  });
});

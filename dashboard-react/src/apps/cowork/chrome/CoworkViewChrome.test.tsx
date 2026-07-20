import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { CoworkViewChrome } from "./CoworkViewChrome";

/**
 * Render-site checks for the App-owned view chrome: the view identity, the honest live/local
 * provider badge (never "demo data"), the host-actions slot, and a clean axe pass. The chrome
 * renders inside a <main> here exactly as ViewHost mounts it, so its <header> is a section
 * header rather than a page banner.
 */
const renderChrome = (ui: ReactElement) => render(<main>{ui}</main>);

describe("CoworkViewChrome", () => {
  it("names the view and shows the local badge for a local scratch session", () => {
    renderChrome(<CoworkViewChrome providerState="local" />);

    expect(
      screen.getByRole("heading", { level: 1, name: "Co-work" }),
    ).toBeVisible();
    const badge = screen.getByRole("status");
    expect(badge).toHaveTextContent("Local");
    expect(badge).toHaveAttribute("data-state", "local");
  });

  it("shows the live badge for a store-scoped session", () => {
    renderChrome(<CoworkViewChrome providerState="live" />);

    const badge = screen.getByRole("status");
    expect(badge).toHaveTextContent("Live");
    expect(badge).toHaveAttribute("data-state", "live");
  });

  it("never says demo data in either provider state", () => {
    const { rerender } = renderChrome(<CoworkViewChrome providerState="local" />);
    expect(screen.queryByText(/demo/i)).toBeNull();
    rerender(
      <main>
        <CoworkViewChrome providerState="live" />
      </main>,
    );
    expect(screen.queryByText(/demo/i)).toBeNull();
  });

  it("places host-owned contextual actions in its actions region", () => {
    renderChrome(
      <CoworkViewChrome
        providerState="local"
        hostActions={<button type="button">Customize</button>}
      />,
    );
    expect(screen.getByRole("button", { name: "Customize" })).toBeVisible();
  });

  it("has no accessibility violations", async () => {
    const { container } = renderChrome(<CoworkViewChrome providerState="live" />);
    await expectNoAccessibilityViolations(container);
  });
});

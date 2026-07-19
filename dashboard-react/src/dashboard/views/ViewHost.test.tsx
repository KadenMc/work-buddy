import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InMemoryJournalProvider } from "../../apps/journal/providers/InMemoryJournalProvider";
import { JOURNAL_VIEW_DEFINITION } from "../../apps/journal/viewDefinition";
import { dashboardRegistry } from "../../app/dashboardRegistry";
import { ThemeProvider } from "../../theme/ThemeProvider";
import { DashboardTestRuntime } from "../../test/DashboardTestRuntime";
import { DashboardAnnouncer } from "../accessibility/DashboardAnnouncer";
import { DashboardEventProvider } from "../events/DashboardEventProvider";
import { DashboardHelpProvider } from "../help";
import { InMemoryPersonalizationRepository } from "../personalization/repository";
import { ViewHost } from "./ViewHost";

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

describe("ViewHost", () => {
  it("renders canonical mobile order without mounting the desktop grid", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => media(query === "(max-width: 767px)")),
    );
    const provider = new InMemoryJournalProvider();

    const rendered = render(
      <MemoryRouter initialEntries={["/journal"]}>
        <ThemeProvider initialPreference={{ scheme: "light", skinId: "wb.default" }}>
          <DashboardEventProvider>
            <DashboardAnnouncer>
              <DashboardTestRuntime>
                <ViewHost
                  registry={dashboardRegistry}
                  definition={JOURNAL_VIEW_DEFINITION}
                  provider={provider}
                  personalizationRepository={new InMemoryPersonalizationRepository()}
                />
              </DashboardTestRuntime>
            </DashboardAnnouncer>
          </DashboardEventProvider>
        </ThemeProvider>
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByRole("region", { name: "Quick Capture" })).toBeVisible(),
    );
    const headings = screen
      .getAllByRole("heading", { level: 2 })
      .map((heading) => heading.textContent);
    expect(headings).toEqual(["Quick Capture", "Day Timeline", "Running Notes"]);
    expect(rendered.container.querySelector(".react-grid-layout")).toBeNull();
    expect(
      await screen.findByRole(
        "region",
        { name: "Calendar surface for 2026-07-11" },
        { timeout: 15_000 },
      ),
    ).toBeVisible();
    expect(screen.getByRole("radio", { name: "Timeline" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "List" })).toBeVisible();
    await userEvent.click(screen.getByRole("radio", { name: "List" }));
    expect(
      await screen.findByRole("button", { name: /Mapped Journal data contracts/ }),
    ).toBeVisible();
    expect(rendered.container.querySelector(".wb-temporal-canvas")).toBeNull();
    await waitFor(() =>
      expect(rendered.container.querySelector(".wb-capture--compact")).not.toBeNull(),
    );
    await waitFor(
      () =>
        expect(
          rendered.container.querySelector(".wb-markdown-collection--compact"),
        ).not.toBeNull(),
      { timeout: 15_000 },
    );
    expect(screen.getByRole("button", { name: "Customize view" })).toBeDisabled();
  }, 20_000);

  it("keeps the desktop grid and standard timeline presentation", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => media(false)));

    const rendered = render(
      <MemoryRouter initialEntries={["/journal"]}>
        <ThemeProvider initialPreference={{ scheme: "light", skinId: "wb.default" }}>
          <DashboardEventProvider>
            <DashboardAnnouncer>
              <DashboardTestRuntime>
                <DashboardHelpProvider enabled>
                  <ViewHost
                    registry={dashboardRegistry}
                    definition={JOURNAL_VIEW_DEFINITION}
                    provider={new InMemoryJournalProvider()}
                    personalizationRepository={new InMemoryPersonalizationRepository()}
                  />
                </DashboardHelpProvider>
              </DashboardTestRuntime>
            </DashboardAnnouncer>
          </DashboardEventProvider>
        </ThemeProvider>
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole(
        "radio",
        { name: "Timeline" },
        { timeout: 15_000 },
      ),
    ).toBeVisible();
    expect(
      rendered.container.querySelector(".wb-day-timeline__toolbar .wb-segmented-field"),
    ).not.toBeNull();
    expect(rendered.container.querySelector(".react-grid-layout")).not.toBeNull();
    expect(rendered.container.querySelector(".wb-calendar-surface")).not.toBeNull();
    expect(rendered.container.querySelector(".wb-capture--standard")).not.toBeNull();
    await waitFor(
      () =>
        expect(
          rendered.container.querySelector(".wb-markdown-collection--standard"),
        ).not.toBeNull(),
      { timeout: 15_000 },
    );
    expect(
      screen.queryByRole("list", { name: "Day timeline items" }),
    ).not.toBeInTheDocument();

    // Hover help is driven by the app-shell provider here (the toggle now lives in the
    // navbar, outside this host). The Journal HelpTargets must still reveal on hover from
    // that shared context, proving no regression after the lift-out.
    const capturePurpose = screen.getByLabelText("About Quick Capture in this view");
    await userEvent.hover(capturePurpose);
    expect(
      await screen.findByText("Capture what is happening without leaving the Journal."),
    ).toBeVisible();
    expect(
      screen.getByText(/required Journal slot preserves exact text/i),
    ).toBeVisible();

    await userEvent.unhover(capturePurpose);
    await userEvent.click(screen.getByRole("button", { name: "Customize view" }));
    expect(screen.getByText("Arranging layout")).toBeVisible();
  }, 20_000);
});

import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InMemoryJournalProvider } from "../../apps/journal/providers/InMemoryJournalProvider";
import { JOURNAL_VIEW_DEFINITION } from "../../apps/journal/viewDefinition";
import { dashboardRegistry } from "../../app/dashboardRegistry";
import { ThemeProvider } from "../../theme/ThemeProvider";
import { DashboardAnnouncer } from "../accessibility/DashboardAnnouncer";
import { DashboardEventProvider } from "../events/DashboardEventProvider";
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
      <ThemeProvider initialPreference={{ scheme: "light", skinId: "wb.default" }}>
        <DashboardEventProvider>
          <DashboardAnnouncer>
            <ViewHost
              registry={dashboardRegistry}
              definition={JOURNAL_VIEW_DEFINITION}
              provider={provider}
              personalizationRepository={new InMemoryPersonalizationRepository()}
            />
          </DashboardAnnouncer>
        </DashboardEventProvider>
      </ThemeProvider>,
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
        "list",
        { name: "Day timeline items" },
        { timeout: 15_000 },
      ),
    ).toBeVisible();
    expect(screen.getByText("Mapped Journal data contracts")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Timeline" })).not.toBeInTheDocument();
    expect(rendered.container.querySelector(".wb-temporal-canvas")).toBeNull();
    expect(rendered.container.querySelector(".wb-capture--compact")).not.toBeNull();
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
      <ThemeProvider initialPreference={{ scheme: "light", skinId: "wb.default" }}>
        <DashboardEventProvider>
          <DashboardAnnouncer>
            <ViewHost
              registry={dashboardRegistry}
              definition={JOURNAL_VIEW_DEFINITION}
              provider={new InMemoryJournalProvider()}
              personalizationRepository={new InMemoryPersonalizationRepository()}
            />
          </DashboardAnnouncer>
        </DashboardEventProvider>
      </ThemeProvider>,
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
    expect(rendered.container.querySelector(".wb-temporal-canvas")).not.toBeNull();
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
  }, 20_000);
});

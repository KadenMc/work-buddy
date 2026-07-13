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
    expect(screen.getByRole("button", { name: "Customize view" })).toBeDisabled();
  });
});

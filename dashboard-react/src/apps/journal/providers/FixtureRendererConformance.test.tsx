import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { dashboardRegistry } from "../../../app/dashboardRegistry";
import { DashboardAnnouncer } from "../../../dashboard/accessibility/DashboardAnnouncer";
import { DashboardEventProvider } from "../../../dashboard/events/DashboardEventProvider";
import { InMemoryPersonalizationRepository } from "../../../dashboard/personalization/repository";
import { FixtureViewProvider } from "../../../dashboard/providers/FixtureViewProvider";
import { ViewHost } from "../../../dashboard/views/ViewHost";
import { ThemeProvider } from "../../../theme/ThemeProvider";
import {
  JOURNAL_APP_ID,
  JOURNAL_VIEW_DEFINITION_ID,
} from "../bindings";
import type { JournalFixtureState } from "../contracts";
import { JULY11_READY_FIXTURE } from "../fixtures/july11";
import {
  JOURNAL_READ_ONLY_FIXTURE,
  JOURNAL_STALE_FIXTURE,
} from "../fixtures/states";
import { JOURNAL_VIEW_DEFINITION } from "../viewDefinition";
import {
  InMemoryJournalProvider,
  type PopulatedJournalFixtureState,
} from "./InMemoryJournalProvider";

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

const populated = (
  fixture: JournalFixtureState,
): PopulatedJournalFixtureState => {
  if (fixture.model === null) {
    throw new Error(`Renderer fixture must have a model: ${fixture.fixtureId}`);
  }
  return fixture;
};

async function renderFixture(fixture: JournalFixtureState) {
  // InMemoryJournalProvider is the Journal-owned binding boundary. Freeze its
  // resulting snapshot in the generic fixture provider before rendering so this
  // suite proves the reusable ViewProvider path, not its stateful demo behavior.
  const source = new InMemoryJournalProvider(populated(fixture));
  const snapshot = await source.loadView(JOURNAL_VIEW_DEFINITION_ID, {
    reason: "mount",
  });
  const widgetSnapshots = await Promise.all(
    JOURNAL_VIEW_DEFINITION.defaultSlots.map((slot) =>
      source.loadWidget(slot.defaultWidgetTypeId, {
        viewId: JOURNAL_VIEW_DEFINITION_ID,
        instanceId: slot.defaultInstanceId,
        knownRevision: snapshot.revision,
        bindings: slot.defaultBindings,
      }),
    ),
  );
  const provider = new FixtureViewProvider({
    appId: JOURNAL_APP_ID,
    viewSnapshots: [snapshot],
    widgetSnapshots,
  });

  return render(
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
}

beforeEach(() => {
  vi.stubGlobal("matchMedia", vi.fn(() => media(false)));
});

afterEach(() => vi.unstubAllGlobals());

describe("FixtureViewProvider Journal renderer conformance", () => {
  it("mounts all three reusable Journal-selected renderers from a ready fixture", async () => {
    const rendered = await renderFixture(JULY11_READY_FIXTURE);

    expect(
      await screen.findByRole(
        "textbox",
        { name: "Capture text" },
        { timeout: 15_000 },
      ),
    ).toBeVisible();
    expect(screen.getByRole("region", { name: "Quick Capture" })).toBeVisible();
    expect(screen.getByRole("region", { name: "Day Timeline" })).toBeVisible();
    expect(screen.getByRole("region", { name: "Running Notes" })).toBeVisible();
    expect(
      await screen.findByText("Mapped Journal data contracts", {}, { timeout: 15_000 }),
    ).toBeVisible();
    expect(
      await within(screen.getByRole("region", { name: "Running Notes" })).findByText(
        "Prototype mobile timeline edge case",
        {},
        { timeout: 15_000 },
      ),
    ).toBeVisible();
    expect(rendered.container.querySelector(".wb-temporal-canvas")).not.toBeNull();
  }, 20_000);

  it("keeps all three real renderers visible beneath stale status banners", async () => {
    await renderFixture(JOURNAL_STALE_FIXTURE);

    expect(
      await screen.findByRole(
        "textbox",
        { name: "Capture text" },
        { timeout: 15_000 },
      ),
    ).toBeVisible();
    await waitFor(
      () => expect(screen.getAllByText("May be out of date:")).toHaveLength(3),
      { timeout: 15_000 },
    );
    expect(
      await screen.findByText("Mapped Journal data contracts", {}, { timeout: 15_000 }),
    ).toBeVisible();
    expect(
      await within(screen.getByRole("region", { name: "Running Notes" })).findByText(
        "Prototype mobile timeline edge case",
        {},
        { timeout: 15_000 },
      ),
    ).toBeVisible();
  }, 20_000);

  it("renders read-only data while disabling capture and notes mutations", async () => {
    await renderFixture(JOURNAL_READ_ONLY_FIXTURE);

    const capture = await screen.findByRole(
      "textbox",
      { name: "Capture text" },
      { timeout: 15_000 },
    );
    await waitFor(
      () => expect(screen.getAllByText("Read-only:")).toHaveLength(3),
      { timeout: 15_000 },
    );
    expect(capture).toBeDisabled();
    expect(screen.getByRole("button", { name: "Capture" })).toBeDisabled();
    expect(screen.getAllByRole("button", { name: "Edit" })).not.toHaveLength(0);
    for (const edit of screen.getAllByRole("button", { name: "Edit" })) {
      expect(edit).toBeDisabled();
    }
    expect(
      await screen.findByText("Mapped Journal data contracts", {}, { timeout: 15_000 }),
    ).toBeVisible();
    expect(
      await within(screen.getByRole("region", { name: "Running Notes" })).findByText(
        "Prototype mobile timeline edge case",
        {},
        { timeout: 15_000 },
      ),
    ).toBeVisible();
  }, 20_000);
});

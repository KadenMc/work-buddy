import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JOURNAL_VIEW_DEFINITION } from "../../apps/journal/viewDefinition";
import { SettingsRegistryProvider } from "../../settings";
import { asSettingsPageId, type ViewDefinition } from "../contributions/contracts";
import { ViewSettingsLauncher } from "./ViewSettingsLauncher";

function LocationProbe() {
  const location = useLocation();
  return (
    <output
      data-testid="location"
      data-navigation-state={JSON.stringify(location.state)}
    >
      {`${location.pathname}${location.search}${location.hash}`}
    </output>
  );
}

describe("ViewSettingsLauncher", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("degrades safely when an isolated renderer has no routing host", () => {
    const rendered = render(
      <ViewSettingsLauncher definition={JOURNAL_VIEW_DEFINITION} />,
    );

    expect(rendered.container).toBeEmptyDOMElement();
  });

  it("resolves the stable page ID and preserves the exact current URL", () => {
    render(
      <MemoryRouter
        initialEntries={[
          "/journal?provider=legacy&day=2026-07-11#day-timeline",
        ]}
      >
        <ViewSettingsLauncher definition={JOURNAL_VIEW_DEFINITION} />
        <LocationProbe />
      </MemoryRouter>,
    );

    const launcher = screen.getByRole("button", { name: "Journal settings" });
    expect(launcher.parentElement).toHaveAttribute("title", "Journal settings");
    fireEvent.click(launcher);

    const location = screen.getByTestId("location");
    expect(location).toHaveTextContent("/settings/apps/journal");
    expect(JSON.parse(location.dataset.navigationState ?? "null")).toEqual({
      settingsReturnTo:
        "/journal?provider=legacy&day=2026-07-11#day-timeline",
      settingsReturnLabel: "Back to Journal",
    });
  });

  it("does not let an unresolved contributed page ID become routing authority", () => {
    const unresolved = {
      ...JOURNAL_VIEW_DEFINITION,
      settings: {
        pageId: asSettingsPageId("example.settings.view.unknown"),
        label: "Unknown settings",
      },
    } satisfies ViewDefinition;

    render(
      <MemoryRouter initialEntries={["/journal"]}>
        <ViewSettingsLauncher definition={unresolved} />
      </MemoryRouter>,
    );

    expect(
      screen.queryByRole("button", { name: "Unknown settings" }),
    ).not.toBeInTheDocument();
  });

  it("resolves a contributed page through the loaded host registry", async () => {
    const contributed = {
      ...JOURNAL_VIEW_DEFINITION,
      settings: {
        pageId: asSettingsPageId("example.settings.app.weather"),
        label: "Weather settings",
      },
    } satisfies ViewDefinition;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          schema_version: 1,
          registry_revision: "settings-registry:weather",
          definitions: [],
          pages: [
            {
              page_id: "example.settings.app.weather",
              owner: { kind: "app", id: "example.weather" },
              route: "/app/settings/apps/weather",
              label: "Weather App",
              description: "Settings for the Weather App and its views.",
              navigation_group: "apps",
              navigation_category: "community",
              context: {
                kind: "app",
                id: "example.weather",
                label: "Weather App",
              },
              sections: [],
            },
          ],
          placements: [],
        }),
      ),
    );

    render(
      <MemoryRouter initialEntries={["/journal"]}>
        <SettingsRegistryProvider>
          <ViewSettingsLauncher definition={contributed} />
          <LocationProbe />
        </SettingsRegistryProvider>
      </MemoryRouter>,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Weather settings" }),
    );
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/settings/apps/weather",
    );
  });
});

import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { COWORK_APP_CONTRIBUTION } from "../../apps/cowork/contribution";
import { InMemoryCoworkProvider } from "../../apps/cowork/providers/InMemoryCoworkProvider";
import { COWORK_VIEW_DEFINITION } from "../../apps/cowork/viewDefinition";
import { COWORK_VIEW_MODULE } from "../../apps/cowork/viewModule";
import { DashboardEventProvider } from "../events/DashboardEventProvider";
import { ContributionRegistry } from "../contributions/registry";
import { InMemoryPersonalizationRepository } from "../personalization/repository";
import { ViewHost } from "./ViewHost";

const makeRegistry = (): ContributionRegistry => {
  const registry = new ContributionRegistry();
  registry.registerApp(COWORK_APP_CONTRIBUTION, [], [COWORK_VIEW_MODULE], {
    trust: "native",
  });
  return registry;
};

describe("ViewHost single-surface mounting", () => {
  it("mounts the App-owned surface and never the widget grid", async () => {
    const registry = makeRegistry();
    const { container } = render(
      <MemoryRouter initialEntries={["/app/cowork"]}>
        <DashboardEventProvider>
          <ViewHost
            registry={registry}
            definition={COWORK_VIEW_DEFINITION}
            provider={new InMemoryCoworkProvider()}
            personalizationRepository={new InMemoryPersonalizationRepository()}
          />
        </DashboardEventProvider>
      </MemoryRouter>,
    );

    // The App-owned surface renders its regions. The surface loads through a dynamic
    // module import, so allow for that resolution under full-suite parallel load.
    await waitFor(
      () => expect(screen.getByRole("tab", { name: "Review" })).toBeVisible(),
      { timeout: 10_000 },
    );
    await waitFor(
      () => expect(screen.getByText("Co-work demo document")).toBeVisible(),
      { timeout: 10_000 },
    );

    // None of the standard grid machinery is mounted for a single-surface view.
    expect(container.querySelector(".react-grid-layout")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Customize view" }),
    ).not.toBeInTheDocument();
  }, 15_000);

  it("surfaces a clear error when the module exports no surface renderer", async () => {
    const registry = makeRegistry();
    // Force the module to resolve without a surface export.
    Object.defineProperty(registry, "loadViewModule", {
      value: async () => ({ hostContractVersion: 1, createRuntime: () => ({}) }),
    });

    render(
      <MemoryRouter initialEntries={["/app/cowork"]}>
        <DashboardEventProvider>
          <ViewHost
            registry={registry}
            definition={COWORK_VIEW_DEFINITION}
            provider={new InMemoryCoworkProvider()}
            personalizationRepository={new InMemoryPersonalizationRepository()}
          />
        </DashboardEventProvider>
      </MemoryRouter>,
    );

    await waitFor(
      () => expect(screen.getByText(/no surface renderer/)).toBeVisible(),
      { timeout: 10_000 },
    );
  }, 15_000);
});

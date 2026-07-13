import { Suspense } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AppContribution } from "../dashboard/contributions/contracts";
import {
  asAppId,
  asViewId,
  asViewModuleId,
} from "../dashboard/contributions/contracts";
import { ContributionRegistry } from "../dashboard/contributions/registry";
import type { StandardWidgetViewModule } from "../dashboard/contributions/viewModules";
import type { PersonalizationRepository } from "../dashboard/personalization/repository";
import type { ViewProvider } from "../dashboard/providers/ViewProvider";
import { projectDashboardRoutes } from "./routes";

vi.mock("../dashboard/views/ViewHost", () => ({
  ViewHost: ({
    definition,
    providerLabel,
  }: {
    definition: { displayName: string };
    providerLabel?: string;
  }) => (
    <h1>
      {definition.displayName} · {providerLabel}
    </h1>
  ),
}));

describe("projectDashboardRoutes", () => {
  it("resolves a non-Journal standard module as ViewHost runtime configuration", async () => {
    const registry = new ContributionRegistry();
    const appId = asAppId("toy.weather");
    const viewId = asViewId("toy.weather.overview");
    const contribution: AppContribution = {
      schemaVersion: 1,
      appId,
      definitionVersion: 1,
      displayName: "Toy Weather",
      widgetRoles: [],
      widgetDefinitions: [],
      views: [
        {
          viewId,
          definitionVersion: 1,
          ownerAppId: appId,
          displayName: "Weather",
          route: "weather",
          navigation: { label: "Weather", order: 40, isDefault: true },
          primaryJob: "Understand today's weather.",
          grid: { columns: 24 },
          defaultSlots: [],
          readingOrder: [],
          mobileOrder: [],
        },
      ],
    };
    const createRuntime = vi.fn(() => ({
      provider: {} as ViewProvider,
      personalizationRepository: {} as PersonalizationRepository,
      providerLabel: "Toy provider",
    }));
    const load = vi.fn(async () => ({
      hostContractVersion: 1 as const,
      createRuntime,
    }));
    const viewModule: StandardWidgetViewModule = {
      kind: "standard-widget-view",
      hostContractVersion: 1,
      moduleId: asViewModuleId("toy.weather.overview.view-module"),
      viewId,
      load,
    };
    registry.registerApp(contribution, [], [viewModule]);

    const [route] = projectDashboardRoutes(registry);

    expect(route).toMatchObject({
      viewId,
      path: "weather",
      label: "Weather",
      isDefault: true,
    });
    expect(load).not.toHaveBeenCalled();

    render(
      <Suspense fallback={<p>Loading</p>}>
        <route.component />
      </Suspense>,
    );

    expect(
      await screen.findByRole("heading", {
        name: "Weather · Toy provider",
      }),
    ).toBeInTheDocument();
    expect(load).toHaveBeenCalledOnce();
    expect(createRuntime).toHaveBeenCalledWith({
      search: window.location.search,
      storage: window.localStorage,
    });
  });
});

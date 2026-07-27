import { Suspense } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { AppContribution } from "../dashboard/contributions/contracts";
import {
  asAppId,
  asViewId,
  asViewModuleId,
} from "../dashboard/contributions/contracts";
import { ContributionRegistry } from "../dashboard/contributions/registry";
import type {
  StandardViewRuntimeContext,
  StandardWidgetViewModule,
  ViewLocationAdapter,
} from "../dashboard/contributions/viewModules";
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
    const createRuntime = vi.fn((_context: StandardViewRuntimeContext) => ({
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
      <MemoryRouter initialEntries={["/weather?units=metric#today"]}>
        <Suspense fallback={<p>Loading</p>}>
          <route.component />
        </Suspense>
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", {
        name: "Weather · Toy provider",
      }),
    ).toBeInTheDocument();
    expect(load).toHaveBeenCalledOnce();
    expect(createRuntime).toHaveBeenCalledOnce();
    expect(createRuntime.mock.calls[0]?.[0]).toMatchObject({
      search: "?units=metric",
      storage: window.localStorage,
    });
    expect(createRuntime.mock.calls[0]?.[0].location).toEqual(
      expect.objectContaining({
        getSearch: expect.any(Function),
        pushSearch: expect.any(Function),
        replaceSearch: expect.any(Function),
        subscribe: expect.any(Function),
      }),
    );
  });

  it("keeps one runtime while its router-backed adapter publishes query changes", async () => {
    const registry = new ContributionRegistry();
    const appId = asAppId("toy.location");
    const viewId = asViewId("toy.location.overview");
    let adapter: ViewLocationAdapter | undefined;
    const listener = vi.fn();
    const createRuntime = vi.fn((context) => {
      adapter = context.location;
      context.location.subscribe(listener);
      return {
        provider: {} as ViewProvider,
        personalizationRepository: {} as PersonalizationRepository,
      };
    });
    const contribution: AppContribution = {
      schemaVersion: 1,
      appId,
      definitionVersion: 1,
      displayName: "Toy Location",
      widgetRoles: [],
      widgetDefinitions: [],
      views: [
        {
          viewId,
          definitionVersion: 1,
          ownerAppId: appId,
          displayName: "Location",
          route: "location",
          navigation: { label: "Location", order: 40, isDefault: true },
          primaryJob: "Exercise query navigation.",
          grid: { columns: 24 },
          defaultSlots: [],
          readingOrder: [],
          mobileOrder: [],
        },
      ],
    };
    registry.registerApp(contribution, [], [
      {
        kind: "standard-widget-view",
        hostContractVersion: 1,
        moduleId: asViewModuleId("toy.location.overview.view-module"),
        viewId,
        load: async () => ({ hostContractVersion: 1, createRuntime }),
      },
    ]);
    const [route] = projectDashboardRoutes(registry);

    function SearchProbe() {
      return (
        <>
          <output>{useLocation().search}</output>
          <button onClick={() => adapter?.pushSearch("?document_id=two")}>
            Open second document
          </button>
        </>
      );
    }

    render(
      <MemoryRouter initialEntries={["/location?document_id=one"]}>
        <Routes>
          <Route
            path="location"
            element={
              <>
                <SearchProbe />
                <Suspense fallback={<p>Loading</p>}>
                  <route.component />
                </Suspense>
              </>
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(adapter).toBeDefined());
    await screen.findByRole("heading", { name: "Location ·" });
    fireEvent.click(screen.getByRole("button", { name: "Open second document" }));
    await screen.findByText("?document_id=two");
    expect(listener).toHaveBeenCalledWith("?document_id=two");
    expect(adapter && adapter.getSearch()).toBe("?document_id=two");
    expect(createRuntime).toHaveBeenCalledOnce();
  });
});

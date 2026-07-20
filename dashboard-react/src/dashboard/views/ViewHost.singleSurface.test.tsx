import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import {
  asAppId,
  asViewId,
  asViewModuleId,
  asWidgetRoleId,
  type AppContribution,
  type DashboardIntent,
  type IntentResult,
  type ReconcileResult,
  type ViewDefinition,
  type ViewSnapshot,
  type WidgetSnapshot,
} from "../contributions/contracts";
import { ContributionRegistry } from "../contributions/registry";
import type { ViewModule } from "../contributions/viewModules";
import type { WidgetThemeDeclaration } from "../contributions/themeContract";
import { DashboardEventProvider } from "../events/DashboardEventProvider";
import { InMemoryPersonalizationRepository } from "../personalization/repository";
import type { ViewProvider } from "../providers/ViewProvider";
import { ViewHost } from "./ViewHost";

/**
 * The single-surface layout kind is a live Dashboard Core capability even though no shipped
 * App uses it after the Co-work re-platform. This proof keeps it exercised end to end
 * against a synthetic single-surface App: one region, one App-owned surface renderer, a
 * fresh registry. It asserts the two behaviours ViewHost must keep true for the kind: it
 * mounts the App-owned surface with none of the grid or Customize machinery, and it fails
 * loudly when a single-surface module resolves without a surface renderer.
 */
const APP_ID = asAppId("example.single-surface");
const VIEW_ID = asViewId("example.single-surface.workspace");
const MODULE_ID = asViewModuleId("example.single-surface.workspace.module");
const EDITOR_ROLE = asWidgetRoleId("example.widget-role.editor@1");

const STANDARD_THEME: WidgetThemeDeclaration = {
  contractVersion: 1,
  conformance: "standard",
  supports: ["light", "dark", "forced-colors", "reduced-motion"],
  styling: "semantic-tokens",
};

const SINGLE_SURFACE_VIEW: ViewDefinition = {
  viewId: VIEW_ID,
  definitionVersion: 1,
  ownerAppId: APP_ID,
  displayName: "Single surface",
  route: "single-surface",
  navigation: { label: "Single surface", order: 40 },
  primaryJob: "Prove the single-surface kind stays alive.",
  layoutKind: "single-surface",
  grid: { columns: 24 },
  defaultSlots: [],
  readingOrder: [],
  mobileOrder: [],
  surface: {
    regions: [
      {
        regionId: "editor",
        role: EDITOR_ROLE,
        presence: "required",
        help: { summary: "Edit the document.", details: "The one App-owned region." },
        theme: STANDARD_THEME,
      },
    ],
  },
};

const CONTRIBUTION: AppContribution = {
  schemaVersion: 1,
  appId: APP_ID,
  definitionVersion: 1,
  displayName: "Single surface",
  widgetRoles: [
    {
      roleId: EDITOR_ROLE,
      ownerAppId: APP_ID,
      displayName: "Editor",
      description: "Owns the App surface.",
    },
  ],
  widgetDefinitions: [],
  views: [SINGLE_SURFACE_VIEW],
};

/** A minimal coarse provider: the surface only needs a resolvable view snapshot. */
class SyntheticProvider implements ViewProvider {
  readonly appId = APP_ID;
  async loadView(): Promise<ViewSnapshot> {
    return {
      viewId: VIEW_ID,
      revision: 1,
      observedAt: new Date(0).toISOString(),
      status: "ready",
      quality: { kind: "demo", message: "synthetic single surface" },
      model: {},
      bindings: {},
      widgetInputs: {},
    };
  }
  async loadWidget(): Promise<WidgetSnapshot> {
    throw new Error("a single-surface view mounts no widgets");
  }
  async dispatch(intent: DashboardIntent): Promise<IntentResult> {
    return { intent_id: intent.intent_id, status: "accepted" };
  }
  async reconcile(): Promise<ReconcileResult> {
    return { changed: false };
  }
}

/** The App-owned surface renderer the module resolves for this view. */
function SyntheticSurface() {
  return <div data-testid="synthetic-surface">Synthetic single surface</div>;
}

const SURFACE_MODULE: ViewModule = {
  kind: "standard-widget-view",
  hostContractVersion: 1,
  moduleId: MODULE_ID,
  viewId: VIEW_ID,
  load: async () => ({
    hostContractVersion: 1 as const,
    createRuntime: () => ({
      provider: new SyntheticProvider(),
      personalizationRepository: new InMemoryPersonalizationRepository(),
    }),
    surface: SyntheticSurface,
  }),
};

const makeRegistry = (): ContributionRegistry => {
  const registry = new ContributionRegistry();
  registry.registerApp(CONTRIBUTION, [], [SURFACE_MODULE], { trust: "native" });
  return registry;
};

describe("ViewHost single-surface mounting", () => {
  it("mounts the App-owned surface and never the widget grid", async () => {
    const registry = makeRegistry();
    const { container } = render(
      <MemoryRouter initialEntries={["/app/single-surface"]}>
        <DashboardEventProvider>
          <ViewHost
            registry={registry}
            definition={SINGLE_SURFACE_VIEW}
            provider={new SyntheticProvider()}
            personalizationRepository={new InMemoryPersonalizationRepository()}
          />
        </DashboardEventProvider>
      </MemoryRouter>,
    );

    // The App-owned surface renders through a dynamic module import, so allow for that
    // resolution under full-suite parallel load.
    await waitFor(
      () => expect(screen.getByTestId("synthetic-surface")).toBeVisible(),
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
      <MemoryRouter initialEntries={["/app/single-surface"]}>
        <DashboardEventProvider>
          <ViewHost
            registry={registry}
            definition={SINGLE_SURFACE_VIEW}
            provider={new SyntheticProvider()}
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

import { describe, expect, it, vi } from "vitest";

import type {
  AppContribution,
  WidgetModule,
} from "./contracts";
import {
  asAppId,
  asViewModuleId,
  asViewId,
  asWidgetInstanceId,
  asWidgetModuleId,
  asWidgetRoleId,
  asWidgetSlotId,
  asWidgetTypeId,
} from "./contracts";
import type {
  StandardWidgetViewModule,
  ViewModule,
} from "./viewModules";
import type { PersonalizationRepository } from "../personalization/repository";
import type { ViewProvider } from "../providers/ViewProvider";
import { ContributionRegistry } from "./registry";
import { ContributionValidationError } from "./validate";

const appId = asAppId("toy.weather");
const roleId = asWidgetRoleId("toy.widget-role.summary@1");
const widgetTypeId = asWidgetTypeId("toy.weather.summary-card");
const moduleId = asWidgetModuleId("toy.weather.summary-card.renderer");
const viewModuleId = asViewModuleId("toy.weather.overview.view-module");

const makeToyContribution = (): AppContribution => ({
  schemaVersion: 1,
  appId,
  definitionVersion: 1,
  displayName: "Toy Weather",
  widgetRoles: [
    {
      roleId,
      ownerAppId: appId,
      displayName: "Summary",
      description: "Shows a concise summary.",
    },
  ],
  widgetDefinitions: [
    {
      typeId: widgetTypeId,
      definitionVersion: 1,
      publisherAppId: appId,
      displayName: "Weather Summary",
      description: "A deliberately non-Journal widget.",
      libraryPath: ["Weather", "Summary"],
      providesRoles: [roleId],
      settingsSchema: { schemaId: "toy.weather.summary.settings", version: 1 },
      inputSchema: { schemaId: "toy.weather.summary.input", version: 1 },
      outputIntentSchemas: [],
      sizeContract: {
        default: { w: 12, h: 4 },
        min: { w: 6, h: 3 },
        max: { w: 24, h: 12 },
        modes: ["compact", "standard", "expanded"],
      },
      multiplicity: "single_per_view",
      rendererModuleId: moduleId,
      theme: {
        contractVersion: 1,
        conformance: "standard",
        supports: ["light", "dark", "forced-colors", "reduced-motion"],
        styling: "semantic-tokens",
      },
    },
  ],
  views: [
    {
      viewId: asViewId("toy.weather.overview"),
      definitionVersion: 1,
      ownerAppId: appId,
      displayName: "Weather",
      route: "weather",
      navigation: { label: "Weather", order: 40, isDefault: true },
      primaryJob: "Understand today's weather.",
      grid: { columns: 24 },
      defaultSlots: [
        {
          slotId: asWidgetSlotId("summary"),
          defaultInstanceId: asWidgetInstanceId("default:summary"),
          requiredRole: roleId,
          defaultWidgetTypeId: widgetTypeId,
          presence: "required",
          help: { summary: "Summarize the weather.", details: "Explains this test view's weather." },
          defaultSettings: {},
          defaultLayout: { x: 0, y: 0, w: 12, h: 4 },
          lockedReason: "Without a summary the view cannot explain the weather.",
        },
      ],
      readingOrder: [asWidgetSlotId("summary")],
      mobileOrder: [asWidgetSlotId("summary")],
    },
  ],
});

const makeToyModule = (
  load: WidgetModule["load"] = vi.fn(async () => ({ default: () => null })),
): WidgetModule => ({
  moduleId,
  widgetTypeId,
  load,
});

const makeToyViewModule = (
  load: StandardWidgetViewModule["load"] = vi.fn(async () => ({
    hostContractVersion: 1 as const,
    createRuntime: () => ({
      provider: {} as ViewProvider,
      personalizationRepository: {} as PersonalizationRepository,
    }),
  })),
): StandardWidgetViewModule => ({
  kind: "standard-widget-view",
  hostContractVersion: 1,
  moduleId: viewModuleId,
  viewId: asViewId("toy.weather.overview"),
  load,
});

describe("ContributionRegistry", () => {
  it("registers and resolves a non-Journal contribution through the generic API", async () => {
    const registry = new ContributionRegistry();
    const contribution = makeToyContribution();
    const module = makeToyModule();

    const receipt = registry.registerApp(contribution, [module]);

    expect(receipt).toEqual({
      appId,
      viewIds: [asViewId("toy.weather.overview")],
      widgetTypeIds: [widgetTypeId],
      trust: "unverified",
    });
    expect(registry.requireView(asViewId("toy.weather.overview")).app.appId).toBe(appId);
    expect(registry.getViewByRoute("weather")?.definition.viewId).toBe(
      asViewId("toy.weather.overview"),
    );
    expect(registry.requireWidget(widgetTypeId).module).toBe(module);
    expect((await registry.loadWidgetModule(widgetTypeId)).default).toBeTypeOf("function");
    expect(module.load).toHaveBeenCalledOnce();
  });

  it("discovers and lazy-loads a page module through its contributed View ID", async () => {
    const registry = new ContributionRegistry();
    const viewModule = makeToyViewModule();

    registry.registerApp(makeToyContribution(), [makeToyModule()], [viewModule]);

    expect(registry.requireViewModule(asViewId("toy.weather.overview")).module).toBe(
      viewModule,
    );
    expect(viewModule.load).not.toHaveBeenCalled();
    expect(
      (await registry.loadViewModule(asViewId("toy.weather.overview"))).createRuntime,
    ).toBeTypeOf("function");
    expect(viewModule.load).toHaveBeenCalledOnce();
  });

  it("rejects an orphan page module without publishing partial metadata", () => {
    const registry = new ContributionRegistry();
    const orphanModule: ViewModule = {
      ...makeToyViewModule(),
      viewId: asViewId("another.app.view"),
    };

    expect(() =>
      registry.registerApp(makeToyContribution(), [makeToyModule()], [orphanModule]),
    ).toThrow(ContributionValidationError);
    expect(registry.listApps()).toEqual([]);
    expect(registry.listViews()).toEqual([]);
    expect(registry.listWidgets()).toEqual([]);
  });

  it("rejects a page module ID that collides with a widget module ID atomically", () => {
    const registry = new ContributionRegistry();
    const collidingModule = {
      ...makeToyViewModule(),
      moduleId: asViewModuleId(moduleId),
    };

    expect(() =>
      registry.registerApp(
        makeToyContribution(),
        [makeToyModule()],
        [collidingModule],
      ),
    ).toThrow(ContributionValidationError);
    expect(registry.listApps()).toEqual([]);
  });

  it("rejects raw developer roots from the standard view registry", () => {
    const registry = new ContributionRegistry();
    const developerRoot: ViewModule = {
      kind: "developer-root",
      trustGate: "developer-mode",
      moduleId: asViewModuleId("toy.weather.developer-root"),
      viewId: asViewId("toy.weather.overview"),
      load: vi.fn(async () => ({ default: () => null })),
    };

    expect(() =>
      registry.registerApp(makeToyContribution(), [makeToyModule()], [developerRoot]),
    ).toThrow(/separate trust-gated registry/);
    expect(registry.listApps()).toEqual([]);
  });

  it("does not normalize a bare page component into the standard host contract", async () => {
    const registry = new ContributionRegistry();
    const bareRootLoader = vi.fn(async () => ({ default: () => null }));
    const viewModule = makeToyViewModule(
      bareRootLoader as unknown as StandardWidgetViewModule["load"],
    );
    registry.registerApp(makeToyContribution(), [makeToyModule()], [viewModule]);

    await expect(
      registry.loadViewModule(asViewId("toy.weather.overview")),
    ).rejects.toThrow(/did not resolve the standard widget-view host contract/);
  });

  it("defaults spoofed wb-prefixed Apps to unverified trust", () => {
    const registry = new ContributionRegistry();
    const contribution = makeToyContribution();
    const spoofedAppId = asAppId("wb.evil");
    const spoofedContribution: AppContribution = {
      ...contribution,
      appId: spoofedAppId,
      widgetRoles: contribution.widgetRoles.map((role) => ({
        ...role,
        ownerAppId: spoofedAppId,
      })),
      widgetDefinitions: contribution.widgetDefinitions.map((widget) => ({
        ...widget,
        publisherAppId: spoofedAppId,
      })),
      views: contribution.views.map((view) => ({
        ...view,
        ownerAppId: spoofedAppId,
      })),
    };

    const receipt = registry.registerApp(spoofedContribution, [makeToyModule()]);

    expect(receipt.trust).toBe("unverified");
    expect(registry.requireAppTrust(spoofedAppId)).toBe("unverified");
    expect(registry.requireWidget(widgetTypeId).trust).toBe("unverified");
  });

  it("rejects a claimed role whose input schema does not exactly match", () => {
    const registry = new ContributionRegistry();
    const contribution = makeToyContribution();
    const mismatchedContribution: AppContribution = {
      ...contribution,
      widgetRoles: [
        {
          ...contribution.widgetRoles[0]!,
          inputSchema: { schemaId: "toy.weather.different-input", version: 1 },
        },
      ],
    };

    let thrown: unknown;
    try {
      registry.registerApp(mismatchedContribution, [makeToyModule()]);
    } catch (error) {
      thrown = error;
    }

    expect(thrown).toBeInstanceOf(ContributionValidationError);
    expect((thrown as ContributionValidationError).issues).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ code: "widget_role_input_schema_mismatch" }),
      ]),
    );
    expect(registry.listApps()).toEqual([]);
  });

  it("rejects a claimed role when the widget omits a required output intent", () => {
    const registry = new ContributionRegistry();
    const contribution = makeToyContribution();
    const missingIntentContribution: AppContribution = {
      ...contribution,
      widgetRoles: [
        {
          ...contribution.widgetRoles[0]!,
          inputSchema: { schemaId: "toy.weather.summary.input", version: 1 },
          outputIntentSchemas: [
            { schemaId: "toy.weather.refresh-requested", version: 1 },
          ],
        },
      ],
    };

    let thrown: unknown;
    try {
      registry.registerApp(missingIntentContribution, [makeToyModule()]);
    } catch (error) {
      thrown = error;
    }

    expect(thrown).toBeInstanceOf(ContributionValidationError);
    expect((thrown as ContributionValidationError).issues).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ code: "widget_role_output_intent_missing" }),
      ]),
    );
    expect(registry.listApps()).toEqual([]);
  });

  it("keeps registration atomic when a duplicate App is rejected", () => {
    const registry = new ContributionRegistry();
    const contribution = makeToyContribution();
    registry.registerApp(contribution, [makeToyModule()]);

    expect(() => registry.registerApp(contribution, [makeToyModule()])).toThrow(
      ContributionValidationError,
    );
    expect(registry.listApps()).toHaveLength(1);
    expect(registry.listViews()).toHaveLength(1);
    expect(registry.listWidgets()).toHaveLength(1);
  });

  it("rejects a missing renderer without publishing partial metadata", () => {
    const registry = new ContributionRegistry();

    expect(() => registry.registerApp(makeToyContribution(), [])).toThrowError(
      /no registered lazy module/,
    );
    expect(registry.listApps()).toEqual([]);
    expect(registry.listViews()).toEqual([]);
    expect(registry.listWidgets()).toEqual([]);
  });

  it("rejects a lazy module that resolves without a default renderer", async () => {
    const registry = new ContributionRegistry();
    registry.registerApp(
      makeToyContribution(),
      [makeToyModule(vi.fn(async () => ({ default: undefined })))],
    );

    await expect(registry.loadWidgetModule(widgetTypeId)).rejects.toThrow(
      /no default renderer export/,
    );
  });
});

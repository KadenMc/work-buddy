import { describe, expect, it, vi } from "vitest";

import type { AppContribution, WidgetModule } from "./contracts";
import {
  asAppId,
  asViewId,
  asWidgetInstanceId,
  asWidgetModuleId,
  asWidgetRoleId,
  asWidgetSlotId,
  asWidgetTypeId,
} from "./contracts";
import { ContributionRegistry } from "./registry";
import { ContributionValidationError } from "./validate";

const appId = asAppId("toy.weather");
const roleId = asWidgetRoleId("toy.widget-role.summary@1");
const widgetTypeId = asWidgetTypeId("toy.weather.summary-card");
const moduleId = asWidgetModuleId("toy.weather.summary-card.renderer");

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
    });
    expect(registry.requireView(asViewId("toy.weather.overview")).app.appId).toBe(appId);
    expect(registry.getViewByRoute("weather")?.definition.viewId).toBe(
      asViewId("toy.weather.overview"),
    );
    expect(registry.requireWidget(widgetTypeId).module).toBe(module);
    expect((await registry.loadWidgetModule(widgetTypeId)).default).toBeTypeOf("function");
    expect(module.load).toHaveBeenCalledOnce();
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

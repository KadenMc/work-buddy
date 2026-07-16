import { describe, expect, it } from "vitest";

import type {
  AppContribution,
  WidgetDefinition,
  WidgetModule,
} from "../contributions/contracts";
import {
  asAppId,
  asViewId,
  asWidgetInstanceId,
  asWidgetModuleId,
  asWidgetRoleId,
  asWidgetSlotId,
  asWidgetTypeId,
} from "../contributions/contracts";
import { ContributionRegistry } from "../contributions/registry";
import type { EffectiveWidgetInstance } from "../personalization/contracts";
import type { WidgetMigrationStep } from "../personalization/migrations";
import {
  beginViewEditSession,
  viewEditSessionReducer,
} from "../personalization/reducer";
import {
  findCompatibleWidgetReplacements,
  planWidgetReplacement,
} from "./replaceWidget";

const appId = asAppId("example.catalog");
const roleId = asWidgetRoleId("example.widget-role.summary@1");
const otherRoleId = asWidgetRoleId("example.widget-role.other@1");
const currentType = asWidgetTypeId("example.catalog.current");
const compatibleType = asWidgetTypeId("example.catalog.compatible");
const incompatibleType = asWidgetTypeId("example.catalog.incompatible");

const definition = (
  typeId: typeof currentType,
  role: typeof roleId,
  libraryPath: readonly string[],
  minW = 6,
): WidgetDefinition => ({
  typeId,
  definitionVersion: 1,
  publisherAppId: appId,
  displayName: libraryPath[libraryPath.length - 1]!,
  description: "Catalog test widget",
  libraryPath,
  providesRoles: [role],
  settingsSchema: { schemaId: `${typeId}.settings`, version: 1 },
  inputSchema: { schemaId: `${typeId}.input`, version: 1 },
  outputIntentSchemas: [],
  sizeContract: {
    default: { w: 8, h: 4 },
    min: { w: minW, h: 3 },
    max: { w: 12, h: 8 },
    modes: ["compact", "standard"],
  },
  multiplicity: "multiple_per_view",
  rendererModuleId: asWidgetModuleId(`${typeId}.renderer`),
  theme: {
    contractVersion: 1,
    conformance: "standard",
    supports: ["light", "dark", "forced-colors", "reduced-motion"],
    styling: "semantic-tokens",
  },
});

const buildRegistry = () => {
  const definitions = [
    definition(currentType, roleId, ["Summary", "Current"]),
    definition(compatibleType, roleId, ["Summary", "Compatible"]),
    definition(incompatibleType, otherRoleId, ["Other", "Incompatible"]),
  ];
  const contribution: AppContribution = {
    schemaVersion: 1,
    appId,
    definitionVersion: 1,
    displayName: "Catalog Publisher",
    widgetRoles: [
      { roleId, ownerAppId: appId, displayName: "Summary", description: "Summary role" },
      {
        roleId: otherRoleId,
        ownerAppId: appId,
        displayName: "Other",
        description: "Other role",
      },
    ],
    widgetDefinitions: definitions,
    views: [
      {
        viewId: asViewId("example.catalog.main"),
        definitionVersion: 1,
        ownerAppId: appId,
        displayName: "Catalog",
        route: "catalog",
        navigation: { label: "Catalog", order: 1 },
        primaryJob: "Test replacement",
        grid: { columns: 24 },
        defaultSlots: [
          {
            slotId: asWidgetSlotId("summary"),
            defaultInstanceId: asWidgetInstanceId("default:summary"),
            requiredRole: roleId,
            defaultWidgetTypeId: currentType,
            presence: "required",
            help: { summary: "Summarize the project.", details: "Provides this test view's required summary." },
            defaultSettings: { density: "compact" },
            defaultLayout: { x: 0, y: 0, w: 8, h: 4 },
            lockedReason: "Summary is required",
          },
        ],
        readingOrder: [asWidgetSlotId("summary")],
        mobileOrder: [asWidgetSlotId("summary")],
      },
    ],
  };
  const modules: WidgetModule[] = definitions.map((widget) => ({
    moduleId: widget.rendererModuleId,
    widgetTypeId: widget.typeId,
    load: async () => ({ default: () => null }),
  }));
  const registry = new ContributionRegistry();
  registry.registerApp(contribution, modules);
  return { registry, view: contribution.views[0]! };
};

const instance: EffectiveWidgetInstance = {
  instanceId: asWidgetInstanceId("default:summary"),
  slotId: asWidgetSlotId("summary"),
  widgetTypeId: currentType,
  widgetDefinitionVersion: 1,
  roleCompatibilityVersion: roleId,
  settings: { density: "compact" },
  settingsSchemaVersion: 1,
  bindings: { project: "northwind" },
  bindingVersion: 1,
  visibility: "shown",
  presence: "required",
  layout: { instanceId: asWidgetInstanceId("default:summary"), x: 0, y: 0, w: 8, h: 4 },
};

describe("widget replacement", () => {
  it("filters the registry by explicit role compatibility", () => {
    const { registry, view } = buildRegistry();
    expect(
      findCompatibleWidgetReplacements(registry, view, instance).map(
        (candidate) => candidate.definition.typeId,
      ),
    ).toEqual([compatibleType]);
  });

  it("preflights migration and emits one undo-friendly atomic reducer action", () => {
    const { registry, view } = buildRegistry();
    const migration: WidgetMigrationStep = {
      id: "current-to-compatible",
      from: {
        widgetTypeId: currentType,
        widgetDefinitionVersion: 1,
        settingsSchemaVersion: 1,
        bindingVersion: 1,
      },
      to: {
        widgetTypeId: compatibleType,
        widgetDefinitionVersion: 1,
        settingsSchemaVersion: 1,
        bindingVersion: 1,
      },
      description: "Carry summary configuration",
      migrate: (state) => state,
    };
    const planned = planWidgetReplacement({
      registry,
      view,
      instance,
      targetTypeId: compatibleType,
      migrations: [migration],
      allowExplicitReset: false,
    });
    expect(planned.ok).toBe(true);
    if (!planned.ok) return;
    expect(planned.plan.preserved).toEqual({
      instanceId: instance.instanceId,
      slotId: instance.slotId,
      layout: instance.layout,
    });

    const opening = { instances: [instance], mobileOrder: [instance.instanceId] };
    let state = viewEditSessionReducer(
      beginViewEditSession(opening),
      planned.plan.action,
    );
    expect(state.present.instances[0]).toMatchObject({
      instanceId: instance.instanceId,
      slotId: instance.slotId,
      widgetTypeId: compatibleType,
      layout: instance.layout,
    });
    state = viewEditSessionReducer(state, { type: "undo" });
    expect(state.present).toEqual(opening);
  });

  it("leaves the prior widget untouched when migration has no approved path", () => {
    const { registry, view } = buildRegistry();
    const result = planWidgetReplacement({
      registry,
      view,
      instance,
      targetTypeId: compatibleType,
      migrations: [],
      allowExplicitReset: false,
    });
    expect(result).toMatchObject({ ok: false, reason: "migration-failed" });
  });
});

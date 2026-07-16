import { describe, expect, it } from "vitest";

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
import { validateAppContribution, validateJsonValue } from "./validate";

const appId = asAppId("example.focus");
const roleId = asWidgetRoleId("example.widget-role.focus@1");
const typeId = asWidgetTypeId("example.focus.card");
const moduleId = asWidgetModuleId("example.focus.card.renderer");

const module: WidgetModule = {
  moduleId,
  widgetTypeId: typeId,
  load: async () => ({ default: () => null }),
};

const contribution = (): AppContribution => ({
  schemaVersion: 1,
  appId,
  definitionVersion: 1,
  displayName: "Focus",
  widgetRoles: [
    {
      roleId,
      ownerAppId: appId,
      displayName: "Focus",
      description: "Provides one focus item.",
    },
  ],
  widgetDefinitions: [
    {
      typeId,
      definitionVersion: 1,
      publisherAppId: appId,
      displayName: "Focus Card",
      description: "Shows one focus item.",
      libraryPath: ["Focus", "Card"],
      providesRoles: [roleId],
      settingsSchema: { schemaId: "example.focus.settings", version: 1 },
      inputSchema: { schemaId: "example.focus.input", version: 1 },
      outputIntentSchemas: [],
      sizeContract: {
        default: { w: 8, h: 4 },
        min: { w: 6, h: 3 },
        max: { w: 12, h: 8 },
        modes: ["compact", "standard"],
      },
      multiplicity: "multiple_per_view",
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
      viewId: asViewId("example.focus.today"),
      definitionVersion: 1,
      ownerAppId: appId,
      displayName: "Focus",
      route: "focus",
      navigation: { label: "Focus", order: 10 },
      primaryJob: "Keep one item visible.",
      grid: { columns: 24 },
      defaultSlots: [
        {
          slotId: asWidgetSlotId("focus"),
          defaultInstanceId: asWidgetInstanceId("default:focus"),
          requiredRole: roleId,
          defaultWidgetTypeId: typeId,
          presence: "required",
          defaultSettings: {},
          defaultLayout: { x: 0, y: 0, w: 8, h: 4 },
          lockedReason: "The view has no purpose without its focus item.",
        },
      ],
      readingOrder: [asWidgetSlotId("focus")],
      mobileOrder: [asWidgetSlotId("focus")],
    },
  ],
});

const validate = (value: AppContribution) =>
  validateAppContribution(value, { widgetModules: new Map([[moduleId, module]]) });

describe("validateAppContribution", () => {
  it("accepts a valid JSON-compatible contribution", () => {
    expect(validate(contribution())).toEqual([]);
  });

  it("rejects layouts outside the grid and the widget size contract", () => {
    const value = contribution();
    const slot = value.views[0]?.defaultSlots[0];
    expect(slot).toBeDefined();
    if (slot === undefined) return;
    const invalid: AppContribution = {
      ...value,
      views: [
        {
          ...value.views[0]!,
          defaultSlots: [{ ...slot, defaultLayout: { x: 20, y: 0, w: 8, h: 2 } }],
        },
      ],
    };

    expect(validate(invalid).map((issue) => issue.code)).toEqual(
      expect.arrayContaining(["invalid_default_layout", "layout_outside_widget_size"]),
    );
  });

  it("rejects a default widget that does not provide the slot role", () => {
    const value = contribution();
    const incompatibleRole = asWidgetRoleId("example.widget-role.other@1");
    const invalid: AppContribution = {
      ...value,
      widgetRoles: [
        ...value.widgetRoles,
        {
          roleId: incompatibleRole,
          ownerAppId: appId,
          displayName: "Other",
          description: "An incompatible purpose.",
        },
      ],
      widgetDefinitions: [
        { ...value.widgetDefinitions[0]!, providesRoles: [incompatibleRole] },
      ],
    };

    expect(validate(invalid)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ code: "incompatible_default_widget" }),
      ]),
    );
  });

  it("requires visible slots exactly once in mobile order", () => {
    const value = contribution();
    const invalid: AppContribution = {
      ...value,
      views: [{ ...value.views[0]!, mobileOrder: [] }],
    };

    expect(validate(invalid)).toEqual(
      expect.arrayContaining([expect.objectContaining({ code: "missing_order_slot" })]),
    );
  });

  it("rejects non-JSON settings including non-finite values and rich objects", () => {
    expect(validateJsonValue({ limit: Number.NaN, created: new Date() })).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ code: "non_json_number" }),
        expect.objectContaining({ code: "non_plain_json_object" }),
      ]),
    );
  });
});

import { describe, expect, it } from "vitest";

import type { AppContribution, WidgetModule } from "./contracts";
import {
  asAppId,
  asSettingsPageId,
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
          help: { summary: "Show the focus item.", details: "Provides the purpose of this test view." },
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

  it("validates a view's stable settings-page reference without coupling it to a route", () => {
    const value = contribution();
    const withSettings: AppContribution = {
      ...value,
      views: [
        {
          ...value.views[0]!,
          settings: {
            pageId: asSettingsPageId("example.settings.view.focus"),
            label: "Focus settings",
          },
        },
      ],
    };
    expect(validate(withSettings)).toEqual([]);

    const untrusted = structuredClone(withSettings) as unknown as {
      views: Array<{ settings: { pageId: string; label: string } }>;
    };
    untrusted.views[0]!.settings.pageId = "/settings/views/focus";
    untrusted.views[0]!.settings.label = "   ";
    expect(validate(untrusted as unknown as AppContribution)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          code: "invalid_namespaced_id",
          path: "views[0].settings.pageId",
        }),
        expect.objectContaining({
          code: "missing_view_settings_label",
          path: "views[0].settings.label",
        }),
      ]),
    );
  });

  it("requires one semantic effect policy for every outward widget intent", () => {
    const value = contribution();
    const widget = value.widgetDefinitions[0]!;
    const missing: AppContribution = {
      ...value,
      widgetDefinitions: [
        {
          ...widget,
          outputIntentSchemas: [{ schemaId: "example.focus.activate", version: 1 }],
        },
      ],
    };
    expect(validate(missing)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ code: "missing_widget_intent_effect" }),
      ]),
    );

    const declared: AppContribution = {
      ...missing,
      widgetDefinitions: [
        {
          ...missing.widgetDefinitions[0]!,
          outputIntentEffects: [
            {
              schema: { schemaId: "example.focus.activate", version: 1 },
              effect: "mutation",
              preview: "block",
            },
          ],
        },
      ],
    };
    expect(validate(declared)).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ code: "missing_widget_intent_effect" }),
      ]),
    );

    const untrusted = structuredClone(declared) as unknown as {
      widgetDefinitions: Array<{
        outputIntentEffects: Array<{ effect: string; preview: string }>;
      }>;
    };
    untrusted.widgetDefinitions[0]!.outputIntentEffects[0]!.effect = "network-write";
    untrusted.widgetDefinitions[0]!.outputIntentEffects[0]!.preview = "allow";
    expect(validate(untrusted as unknown as AppContribution)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ code: "unknown_widget_intent_effect_kind" }),
        expect.objectContaining({ code: "unknown_widget_intent_preview_policy" }),
      ]),
    );
  });

  it("requires meaningful contextual help for every default view slot", () => {
    const value = contribution();
    const view = value.views[0]!;
    const slot = view.defaultSlots[0]!;
    const invalid: AppContribution = {
      ...value,
      views: [
        {
          ...view,
          defaultSlots: [
            { ...slot, help: { summary: "   ", details: "" } },
          ],
        },
      ],
    };

    expect(validate(invalid)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ code: "missing_help_summary" }),
        expect.objectContaining({ code: "missing_help_details" }),
      ]),
    );
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

  it("accepts a durable widget that is single_per_view with no drafts", () => {
    const value = contribution();
    const widget = value.widgetDefinitions[0]!;
    const durable: AppContribution = {
      ...value,
      widgetDefinitions: [
        { ...widget, durable: true, multiplicity: "single_per_view" },
      ],
    };
    const codes = validate(durable).map((issue) => issue.code);
    expect(codes).not.toContain("durable_widget_multiplicity");
    expect(codes).not.toContain("durable_widget_drafts");
  });

  it("requires a durable widget to be single_per_view", () => {
    const value = contribution();
    const widget = value.widgetDefinitions[0]!;
    const durable: AppContribution = {
      ...value,
      widgetDefinitions: [
        { ...widget, durable: true, multiplicity: "multiple_per_view" },
      ],
    };
    expect(validate(durable)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          code: "durable_widget_multiplicity",
          path: "widgetDefinitions[0].multiplicity",
        }),
      ]),
    );
  });

  it("forbids a durable widget from declaring host-owned drafts", () => {
    const value = contribution();
    const widget = value.widgetDefinitions[0]!;
    const durable: AppContribution = {
      ...value,
      widgetDefinitions: [
        {
          ...widget,
          durable: true,
          multiplicity: "single_per_view",
          drafts: [
            {
              draftName: "body",
              schema: { schemaId: "example.focus.draft", version: 1 },
              persistence: "session",
              sensitivity: "ordinary",
              maxBytes: 4096,
              clearPolicy: "confirm",
              scope: { kind: "view" },
            },
          ],
        },
      ],
    };
    expect(validate(durable)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          code: "durable_widget_drafts",
          path: "widgetDefinitions[0].drafts",
        }),
      ]),
    );
  });

  it("leaves an ordinary non-durable widget free of the durable rules", () => {
    const codes = validate(contribution()).map((issue) => issue.code);
    expect(codes).not.toContain("durable_widget_multiplicity");
    expect(codes).not.toContain("durable_widget_drafts");
  });
});

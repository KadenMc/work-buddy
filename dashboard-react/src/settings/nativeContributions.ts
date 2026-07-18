import {
  asSettingsPageId,
  type SettingsPageId,
} from "../dashboard/contributions/contracts";
import {
  asSettingId,
  asSettingPlacementId,
  type SettingsContribution,
} from "./contracts";
import { coworkKeyboardSettingsContribution } from "../apps/cowork/keyboard";
import { SettingsRegistry } from "./registry";

export const ACCESSIBILITY_SETTINGS_PAGE_ID = asSettingsPageId(
  "wb.settings.system.accessibility",
);
export const JOURNAL_APP_SETTINGS_PAGE_ID = asSettingsPageId(
  "wb.settings.app.journal",
);
export const STATUS_SETTINGS_PAGE_ID = asSettingsPageId(
  "wb.settings.system.status",
);

export const TYPOGRAPHY_SCALE_SETTING_ID = asSettingId(
  "wb.core.accessibility.typography-scale",
);
export const JOURNAL_DAY_BOUNDARY_SETTING_ID = asSettingId(
  "wb.journal.day-boundary",
);

export const nativeSettingsContribution: SettingsContribution = {
  sourceId: "wb.core.native-settings",
  definitions: [
    {
      schemaVersion: 1,
      settingId: TYPOGRAPHY_SCALE_SETTING_ID,
      definitionVersion: 1,
      valueVersion: 1,
      ownerId: "wb.core",
      ownerLabel: "Work Buddy",
      provenance: {
        complementId: "wb.core",
        complementVersion: "0.x",
        trustTier: "native",
        label: "Built into Work Buddy",
      },
      title: "Interface text size",
      summary:
        "Choose a standardized text tier used by views, widgets, and dashboard controls.",
      details:
        "Widgets inherit the shared semantic type scale instead of choosing private fine-print sizes.",
      valueSchema: {
        type: "string",
        enum: ["standard", "large", "extra-large", "maximum"],
      },
      defaultValue: "standard",
      allowedScopes: ["device"],
      defaultScope: "device",
      control: {
        kind: "typography-scale",
        options: ["standard", "large", "extra-large", "maximum"],
      },
      appliesTo: [{ kind: "system", id: "wb.core", label: "Work Buddy" }],
      applyBehavior: "immediate",
      sensitivity: "ordinary",
      visibility: "frontend",
      searchKeywords: ["font", "type", "size", "readability", "vision"],
    },
    {
      schemaVersion: 1,
      settingId: JOURNAL_DAY_BOUNDARY_SETTING_ID,
      definitionVersion: 1,
      valueVersion: 1,
      ownerId: "wb.journal",
      ownerLabel: "Journal",
      provenance: {
        complementId: "wb.journal",
        complementVersion: "0.x",
        trustTier: "native",
        label: "Built into Journal",
      },
      title: "Journal day starts at",
      summary: "Times before this belong to the previous Journal day.",
      details:
        "This shared Journal rule affects day identity, captures, running notes, planner queries, Timeline, and List.",
      valueSchema: {
        type: "string",
        format: "local-time",
        pattern: "^(?:[01]\\d|2[0-3]):[0-5]\\d$",
      },
      defaultValue: "05:00",
      allowedScopes: ["profile"],
      defaultScope: "profile",
      control: { kind: "time", minuteStep: 15 },
      appliesTo: [
        { kind: "app", id: "wb.journal", label: "Journal" },
        { kind: "view", id: "wb.journal.main", label: "Journal view" },
      ],
      applyBehavior: "next-boundary",
      sensitivity: "ordinary",
      visibility: "frontend",
      searchKeywords: [
        "day boundary",
        "cutoff",
        "midnight",
        "late night",
        "5 am",
      ],
    },
  ],
  pages: [
    {
      schemaVersion: 1,
      pageId: ACCESSIBILITY_SETTINGS_PAGE_ID,
      ownerId: "wb.core",
      route: "/settings/system/accessibility",
      label: "Accessibility",
      description:
        "Make Work Buddy easier to read across views, widgets, and dashboard controls.",
      navigationGroup: "system",
      navigationLabel: "Accessibility",
      navigationOrder: 20,
      context: { kind: "system", id: "wb.core", label: "System" },
      sections: [
        {
          sectionId: "readability",
          label: "Readability",
          description: "Shared presentation preferences for the dashboard.",
          order: 10,
        },
      ],
    },
    {
      schemaVersion: 1,
      pageId: JOURNAL_APP_SETTINGS_PAGE_ID,
      ownerId: "wb.journal",
      route: "/settings/apps/journal",
      label: "Journal settings",
      description:
        "Configure shared Journal behavior used by its views and backend operations.",
      navigationGroup: "apps",
      navigationLabel: "Journal",
      navigationOrder: 100,
      appCategory: "built-in",
      context: { kind: "app", id: "wb.journal", label: "Journal" },
      sections: [
        {
          sectionId: "day-behavior",
          label: "Day behavior",
          description: "Define how Journal days are identified and resolved.",
          order: 10,
        },
      ],
      fallbackReturnPath: "/journal",
    },
    {
      schemaVersion: 1,
      pageId: STATUS_SETTINGS_PAGE_ID,
      ownerId: "wb.core",
      route: "/settings/status",
      label: "Status & repairs",
      description:
        "See setup, health, and repair information without mixing it into ordinary preferences.",
      navigationGroup: "status",
      navigationLabel: "Status & repairs",
      navigationOrder: 900,
      context: { kind: "status", id: "wb.control", label: "Setup & health" },
      sections: [],
    },
  ],
  placements: [
    {
      schemaVersion: 1,
      placementId: asSettingPlacementId(
        "wb.settings.placement.system.accessibility.typography-scale",
      ),
      settingId: TYPOGRAPHY_SCALE_SETTING_ID,
      pageId: ACCESSIBILITY_SETTINGS_PAGE_ID,
      sectionId: "readability",
      order: 10,
      preferredForSearch: true,
    },
    {
      schemaVersion: 1,
      placementId: asSettingPlacementId(
        "wb.settings.placement.app.journal.day-boundary",
      ),
      settingId: JOURNAL_DAY_BOUNDARY_SETTING_ID,
      pageId: JOURNAL_APP_SETTINGS_PAGE_ID,
      sectionId: "day-behavior",
      order: 10,
      preferredForSearch: true,
    },
  ],
};

export const nativeSettingsRegistry = new SettingsRegistry([
  nativeSettingsContribution,
  coworkKeyboardSettingsContribution,
]);

export function resolveSettingsPageRoute(
  pageId: SettingsPageId,
): string | undefined {
  return nativeSettingsRegistry.getPage(pageId)?.route;
}

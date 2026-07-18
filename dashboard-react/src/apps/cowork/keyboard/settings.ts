import {
  asSettingsPageId,
  type SettingsPageId,
} from "../../../dashboard/contributions/contracts";
import {
  asSettingId,
  asSettingPlacementId,
  type SettingId,
  type SettingsContribution,
  type SettingsValueSnapshot,
} from "../../../settings/contracts";
import { DEFAULT_NAV_BINDING_PRESET } from "./bindings";

/**
 * The Co-work review keyboard binding is a first-class dashboard setting, declared in the
 * house SettingDefinition shape exactly like the accessibility and Journal settings. This
 * contribution is the frontend half (definition metadata, page, placement) that the Settings
 * UI renders and the native fallback registry merges. The effective value is read at runtime
 * with useCoworkNavBinding, which degrades to the inverted default when the value is absent,
 * so the setting is safe to ship before its server registration lands.
 */

export const COWORK_SETTINGS_PAGE_ID: SettingsPageId = asSettingsPageId(
  "wb.settings.app.cowork",
);

export const COWORK_NAV_BINDING_SETTING_ID: SettingId = asSettingId(
  "wb.cowork.review.nav-binding",
);

export const coworkKeyboardSettingsContribution: SettingsContribution = {
  sourceId: "wb.cowork.keyboard-settings",
  definitions: [
    {
      schemaVersion: 1,
      settingId: COWORK_NAV_BINDING_SETTING_ID,
      definitionVersion: 1,
      valueVersion: 1,
      ownerId: "wb.cowork",
      ownerLabel: "Co-work",
      provenance: {
        complementId: "wb.cowork",
        complementVersion: "0.x",
        trustTier: "native",
        label: "Built into Co-work",
      },
      title: "Review navigation keys",
      summary:
        "Choose which keys walk to the previous and next item while reviewing a document.",
      details:
        "Inverted uses j for the previous item and k for the next item. Vim uses the conventional j down, k up pair.",
      valueSchema: { type: "string", enum: ["inverted", "vim"] },
      defaultValue: DEFAULT_NAV_BINDING_PRESET,
      allowedScopes: ["profile", "device"],
      defaultScope: "profile",
      control: {
        kind: "select",
        options: [
          {
            value: "inverted",
            label: "Inverted (j up, k down)",
            description: "The house binding: j moves to the previous item.",
          },
          {
            value: "vim",
            label: "Vim (j down, k up)",
            description: "Conventional vim: j moves to the next item.",
          },
        ],
      },
      appliesTo: [
        { kind: "app", id: "wb.cowork", label: "Co-work" },
        { kind: "view", id: "wb.cowork.workspace", label: "Co-work view" },
      ],
      applyBehavior: "immediate",
      sensitivity: "ordinary",
      visibility: "frontend",
      searchKeywords: [
        "keyboard",
        "shortcut",
        "navigation",
        "j",
        "k",
        "vim",
        "review",
      ],
    },
  ],
  pages: [
    {
      schemaVersion: 1,
      pageId: COWORK_SETTINGS_PAGE_ID,
      ownerId: "wb.cowork",
      route: "/settings/apps/cowork",
      label: "Co-work settings",
      description:
        "Configure the Co-work document review and writing surface.",
      navigationGroup: "apps",
      navigationLabel: "Co-work",
      navigationOrder: 120,
      appCategory: "built-in",
      context: { kind: "app", id: "wb.cowork", label: "Co-work" },
      sections: [
        {
          sectionId: "review-keyboard",
          label: "Review keyboard",
          description: "How the keyboard walks proposals and flags in Review.",
          order: 10,
        },
      ],
      fallbackReturnPath: "/app/cowork",
    },
  ],
  placements: [
    {
      schemaVersion: 1,
      placementId: asSettingPlacementId(
        "wb.settings.placement.app.cowork.nav-binding",
      ),
      settingId: COWORK_NAV_BINDING_SETTING_ID,
      pageId: COWORK_SETTINGS_PAGE_ID,
      sectionId: "review-keyboard",
      order: 10,
      preferredForSearch: true,
    },
  ],
};

/**
 * Read the configured binding preset id from a settings value snapshot, or undefined when the
 * setting is not present (server does not know it yet, or the fetch was unavailable). Pure, so
 * the resolution is testable without a live settings server.
 */
export function readNavBindingValue(
  snapshot: SettingsValueSnapshot | undefined,
): unknown {
  return snapshot?.values.get(COWORK_NAV_BINDING_SETTING_ID)?.effectiveValue;
}

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
import {
  COWORK_SHORTCUT_COMMANDS,
  DEFAULT_COWORK_SHORTCUT_BINDINGS,
  coworkShortcutValueSchema,
} from "./bindings";

/**
 * The Co-work review keyboard binding is a first-class dashboard setting, declared in the
 * house SettingDefinition shape exactly like the accessibility and Journal settings. This
 * contribution is the frontend half (definition metadata, page, placement) that the Settings
 * UI renders and the native fallback registry merges. The effective map is read at runtime
 * with useCoworkShortcutBindings, which degrades atomically to the house default when the
 * value is absent, so the setting is safe during rolling frontend/server upgrades.
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
      definitionVersion: 2,
      valueVersion: 2,
      ownerId: "wb.cowork",
      ownerLabel: "Co-work",
      provenance: {
        complementId: "wb.cowork",
        complementVersion: "0.x",
        trustTier: "native",
        label: "Built into Co-work",
      },
      title: "Review keyboard shortcuts",
      summary:
        "Choose the keys used to move through and decide Queue items.",
      details:
        "These shortcuts are active only while Queue is visible. They never take over while you are typing.",
      valueSchema: coworkShortcutValueSchema(),
      defaultValue: DEFAULT_COWORK_SHORTCUT_BINDINGS,
      allowedScopes: ["profile"],
      defaultScope: "profile",
      control: {
        kind: "keybinding-map",
        commands: COWORK_SHORTCUT_COMMANDS,
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
        "accept",
        "amend",
        "reject",
        "defer",
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
          description: "How the keyboard moves through and decides Queue items.",
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
 * Read the configured shortcut map from a settings value snapshot, or undefined when the
 * setting is not present (server does not know it yet, or the fetch was unavailable). Pure, so
 * the resolution is testable without a live settings server.
 */
export function readCoworkShortcutBindingValue(
  snapshot: SettingsValueSnapshot | undefined,
): unknown {
  return snapshot?.values.get(COWORK_NAV_BINDING_SETTING_ID)?.effectiveValue;
}

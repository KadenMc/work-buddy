import { describe, expect, it } from "vitest";

import type {
  EffectiveSettingValue,
  SettingId,
  SettingsValueSnapshot,
} from "../../../settings/contracts";
import {
  DEFAULT_COWORK_SHORTCUT_BINDINGS,
  resolveCoworkShortcutBindings,
} from "./bindings";
import {
  COWORK_NAV_BINDING_SETTING_ID,
  coworkKeyboardSettingsContribution,
  readCoworkShortcutBindingValue,
} from "./settings";

function snapshotWith(
  settingId: SettingId,
  effectiveValue: unknown,
): SettingsValueSnapshot {
  const value: EffectiveSettingValue = {
    settingId,
    scope: { kind: "profile" },
    effectiveValue,
    source: "profile",
    isModified: true,
    revision: "profile:1",
    diagnostics: [],
  };
  return {
    registryRevision: "test",
    observedAt: "2026-07-17T00:00:00Z",
    readOnly: false,
    diagnostics: [],
    values: new Map<SettingId, EffectiveSettingValue>([[settingId, value]]),
  };
}

describe("cowork keyboard settings contribution", () => {
  it("upgrades the existing setting identity to one v2 keybinding map", () => {
    const [definition] = coworkKeyboardSettingsContribution.definitions;
    expect(definition.settingId).toBe(COWORK_NAV_BINDING_SETTING_ID);
    expect(definition.definitionVersion).toBe(2);
    expect(definition.valueVersion).toBe(2);
    expect(definition.defaultValue).toEqual(DEFAULT_COWORK_SHORTCUT_BINDINGS);
    expect(definition.allowedScopes).toEqual(["profile"]);
    expect(definition.control.kind).toBe("keybinding-map");
    if (definition.control.kind !== "keybinding-map") {
      throw new Error("expected a keybinding-map control");
    }
    expect(definition.control.commands.map((command) => command.commandId)).toEqual([
      "previous",
      "next",
      "accept",
      "amend",
      "reject",
      "defer",
    ]);
  });

  it("places the setting on the contextual Co-work app settings page", () => {
    const placement = coworkKeyboardSettingsContribution.placements[0];
    expect(placement.settingId).toBe(COWORK_NAV_BINDING_SETTING_ID);
    const page = coworkKeyboardSettingsContribution.pages[0];
    expect(placement.pageId).toBe(page.pageId);
    expect(page.navigationGroup).toBe("apps");
  });
});

describe("readCoworkShortcutBindingValue", () => {
  it("resolves a configured map from the snapshot", () => {
    const configured = {
      ...DEFAULT_COWORK_SHORTCUT_BINDINGS,
      accept: "Enter",
    };
    const snapshot = snapshotWith(COWORK_NAV_BINDING_SETTING_ID, configured);
    expect(
      resolveCoworkShortcutBindings(readCoworkShortcutBindingValue(snapshot)),
    ).toEqual(configured);
  });

  it("falls back to defaults when the setting is absent", () => {
    expect(readCoworkShortcutBindingValue(undefined)).toBeUndefined();
    const otherSnapshot = snapshotWith("wb.other.setting" as SettingId, {});
    expect(readCoworkShortcutBindingValue(otherSnapshot)).toBeUndefined();
    expect(
      resolveCoworkShortcutBindings(readCoworkShortcutBindingValue(otherSnapshot)),
    ).toEqual(DEFAULT_COWORK_SHORTCUT_BINDINGS);
  });
});

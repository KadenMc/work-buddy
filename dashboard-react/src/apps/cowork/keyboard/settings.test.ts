import { describe, expect, it } from "vitest";

import type {
  EffectiveSettingValue,
  SettingId,
  SettingsValueSnapshot,
} from "../../../settings/contracts";
import { resolveNavBinding } from "./bindings";
import {
  COWORK_NAV_BINDING_SETTING_ID,
  coworkKeyboardSettingsContribution,
  readNavBindingValue,
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
  it("declares the binding setting with the inverted default and two presets", () => {
    const [definition] = coworkKeyboardSettingsContribution.definitions;
    expect(definition.settingId).toBe(COWORK_NAV_BINDING_SETTING_ID);
    expect(definition.defaultValue).toBe("inverted");
    expect(definition.control.kind).toBe("select");
    const control = definition.control;
    if (control.kind !== "select") throw new Error("expected a select control");
    expect(control.options.map((option) => option.value)).toEqual([
      "inverted",
      "vim",
    ]);
  });

  it("places the setting on a Co-work app settings page", () => {
    const placement = coworkKeyboardSettingsContribution.placements[0];
    expect(placement.settingId).toBe(COWORK_NAV_BINDING_SETTING_ID);
    const page = coworkKeyboardSettingsContribution.pages[0];
    expect(placement.pageId).toBe(page.pageId);
    expect(page.navigationGroup).toBe("apps");
  });
});

describe("readNavBindingValue + resolveNavBinding", () => {
  it("resolves a configured override from the snapshot", () => {
    const snapshot = snapshotWith(COWORK_NAV_BINDING_SETTING_ID, "vim");
    expect(resolveNavBinding(readNavBindingValue(snapshot))).toEqual({
      prev: "k",
      next: "j",
    });
  });

  it("falls back to the inverted default when the setting is absent", () => {
    expect(readNavBindingValue(undefined)).toBeUndefined();
    const otherSnapshot = snapshotWith(
      "wb.other.setting" as SettingId,
      "vim",
    );
    expect(readNavBindingValue(otherSnapshot)).toBeUndefined();
    expect(resolveNavBinding(readNavBindingValue(otherSnapshot))).toEqual({
      prev: "j",
      next: "k",
    });
  });
});

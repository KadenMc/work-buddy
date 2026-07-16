import { describe, expect, it } from "vitest";

import { asSettingsPageId } from "../dashboard/contributions/contracts";
import {
  asSettingId,
  asSettingPlacementId,
  type SettingsContribution,
} from "./contracts";
import {
  JOURNAL_APP_SETTINGS_PAGE_ID,
  JOURNAL_DAY_BOUNDARY_SETTING_ID,
  nativeSettingsContribution,
  nativeSettingsRegistry,
} from "./nativeContributions";
import { SettingsRegistry } from "./registry";

describe("SettingsRegistry", () => {
  it("projects one canonical definition on its App page and keeps view applicability", () => {
    const appSetting = nativeSettingsRegistry.projectPage(
      JOURNAL_APP_SETTINGS_PAGE_ID,
    )!.sections[0].settings[0].definition;

    expect(appSetting.settingId).toBe(JOURNAL_DAY_BOUNDARY_SETTING_ID);
    expect(appSetting.appliesTo.some((context) => context.kind === "view")).toBe(true);
    expect(nativeSettingsRegistry.search("late night")).toHaveLength(1);
    expect(
      nativeSettingsRegistry.searchPage(JOURNAL_APP_SETTINGS_PAGE_ID, "midnight")
        ?.sections[0].settings,
    ).toHaveLength(1);
    expect(
      nativeSettingsRegistry.searchPage(JOURNAL_APP_SETTINGS_PAGE_ID, "font")
        ?.sections,
    ).toHaveLength(0);
  });

  it("rejects generic section routes without mutating the valid registry", () => {
    const badPageId = asSettingsPageId("example.settings.bad");
    const badSettingId = asSettingId("example.bad.value");
    const contribution: SettingsContribution = {
      sourceId: "example.bad",
      definitions: [
        {
          schemaVersion: 1,
          settingId: badSettingId,
          definitionVersion: 1,
          valueVersion: 1,
          ownerId: "example.bad",
          ownerLabel: "Bad example",
          provenance: {
            complementId: "example.bad",
            complementVersion: "1",
            trustTier: "developer-local",
            label: "Test",
          },
          title: "Bad setting",
          summary: "A test setting.",
          defaultValue: true,
          allowedScopes: ["profile"],
          defaultScope: "profile",
          control: { kind: "switch" },
          appliesTo: [{ kind: "app", id: "example.bad", label: "Bad" }],
          applyBehavior: "immediate",
          sensitivity: "ordinary",
          visibility: "frontend",
        },
      ],
      pages: [
        {
          schemaVersion: 1,
          pageId: badPageId,
          ownerId: "example.bad",
          route: "/settings/sections/bad",
          label: "Bad",
          description: "Bad generic route.",
          navigationGroup: "apps",
          navigationLabel: "Bad",
          navigationOrder: 1,
          appCategory: "personal",
          context: { kind: "app", id: "example.bad", label: "Bad" },
          sections: [{ sectionId: "main", label: "Main", order: 1 }],
        },
      ],
      placements: [
        {
          schemaVersion: 1,
          placementId: asSettingPlacementId("example.bad.placement.value"),
          settingId: badSettingId,
          pageId: badPageId,
          sectionId: "main",
          order: 1,
        },
      ],
    };

    expect(() => nativeSettingsRegistry.merge(contribution)).toThrow(
      /system\/apps\/connections\/status/,
    );
    expect(
      nativeSettingsRegistry.getPage(JOURNAL_APP_SETTINGS_PAGE_ID),
    ).toBeDefined();
  });

  it("rejects duplicate setting placement on one page", () => {
    expect(
      nativeSettingsRegistry.projectPage(JOURNAL_APP_SETTINGS_PAGE_ID),
    ).toBeDefined();

    const duplicate = nativeSettingsContribution.placements[0];
    expect(() =>
      new SettingsRegistry([
        {
          ...nativeSettingsContribution,
          placements: [
            ...nativeSettingsContribution.placements,
            {
              ...duplicate,
              placementId: asSettingPlacementId(
                "wb.settings.placement.system.accessibility.typography-scale-duplicate",
              ),
            },
          ],
        },
      ]),
    ).toThrow(/Duplicate page setting placement ID/);
  });
});

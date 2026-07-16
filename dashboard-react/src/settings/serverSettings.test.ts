import { describe, expect, it } from "vitest";

import { SettingsRegistry } from "./registry";
import {
  normalizeServerRegistry,
  normalizeValueSnapshot,
  previewSettingValue,
} from "./serverSettings";

const serverRegistryPayload = {
  schema_version: 1,
  registry_revision: "settings-registry:1",
  definitions: [
    {
      setting_id: "wb.journal.day-boundary",
      definition_version: 1,
      value_version: 1,
      owner: { kind: "app", id: "wb.journal" },
      provenance: { complement_id: "wb.journal", trust_tier: "native" },
      title: "Day starts",
      short_description: "Choose when a Journal day starts.",
      long_description: "Times before this belong to the prior day.",
      keywords: ["midnight", "boundary"],
      default_value: "05:00",
      allowed_scopes: ["profile"],
      default_scope: "profile",
      applies_to: [
        { kind: "app", id: "wb.journal" },
        { kind: "subsystem", id: "wb.journal/day-lifecycle" },
        { kind: "view", id: "wb.journal.main" },
      ],
      presentation: {
        control: "time",
        minute_step: 15,
        apply_behavior: "next-boundary",
      },
      visibility: "frontend",
      sensitivity: "ordinary",
    },
  ],
  pages: [
    {
      page_id: "wb.settings.app.journal",
      context: { kind: "app", id: "wb.journal" },
      owner: { kind: "app", id: "wb.journal" },
      route: "/app/settings/apps/journal",
      label: "Journal",
      navigation_group: "apps",
      navigation_category: "built-in",
      sections: [{ section_id: "day-behavior", label: "Day behavior" }],
    },
  ],
  placements: [
    {
      placement_id: "wb.settings.placement.app.journal.day-boundary",
      setting_id: "wb.journal.day-boundary",
      page_id: "wb.settings.app.journal",
      section_id: "day-behavior",
    },
  ],
};

describe("server settings normalization", () => {
  it("normalizes the Python registry contract into a validated frontend contribution", () => {
    const result = normalizeServerRegistry(serverRegistryPayload);
    const registry = new SettingsRegistry([result.contribution]);
    const page = registry.listPages()[0];
    const definition = registry.listDefinitions()[0];

    expect(result.registryRevision).toBe("settings-registry:1");
    expect(page.route).toBe("/settings/apps/journal");
    expect(page.label).toBe("Journal settings");
    expect(page.appCategory).toBe("built-in");
    expect(definition.appliesTo.map((context) => context.kind)).toEqual([
      "app",
      "subsystem",
      "view",
    ]);
    expect(definition.control).toEqual({ kind: "time", minuteStep: 15 });
  });

  it("preserves revisions, pending transitions, and structured impact previews", () => {
    const snapshot = normalizeValueSnapshot({
      schema_version: 1,
      registry_revision: "settings-registry:1",
      timezone: "America/Toronto",
      configured_timezone: "America/New_York",
      observed_at: "2026-07-15T12:00:00Z",
      read_only: false,
      diagnostics: [{
        code: "timezone_config_drift",
        active_timezone: "America/Toronto",
        configured_timezone: "America/New_York",
        message: "The active and configured timezones differ.",
      }],
      values: [
        {
          setting_id: "wb.journal.day-boundary",
          scope: { kind: "profile", subject_id: "default" },
          effective_value: "05:00",
          configured_value: "04:00",
          source: "default",
          is_modified: true,
          revision: "value:1",
          pending_value: "04:00",
          effective_at: "2026-07-16T04:00:00-04:00",
          apply_status: "pending",
          impact_preview: { timezone: "America/Toronto" },
          policy_timezone: "America/Toronto",
          configured_timezone: "America/New_York",
          pending_timezone: "America/Toronto",
          diagnostics: [{
            code: "timezone_config_drift",
            active_timezone: "America/Toronto",
            configured_timezone: "America/New_York",
            message: "The active and configured timezones differ.",
          }],
        },
      ],
    });
    const [value] = snapshot.values.values();

    expect(snapshot.timezone).toBe("America/Toronto");
    expect(snapshot.configuredTimezone).toBe("America/New_York");
    expect(snapshot.diagnostics[0]?.code).toBe("timezone_config_drift");
    expect(value.pendingValue).toBe("04:00");
    expect(value.impactPreview).toEqual({ timezone: "America/Toronto" });
    expect(value.policyTimezone).toBe("America/Toronto");
    expect(value.configuredTimezone).toBe("America/New_York");
    expect(value.pendingTimezone).toBe("America/Toronto");
    expect(value.diagnostics[0]?.message).toBe(
      "The active and configured timezones differ.",
    );
  });

  it("preserves structured preview diagnostics returned by the broker", async () => {
    const fetchImpl = async () => Response.json({
      schema_version: 1,
      registry_revision: "settings-registry:1",
      timezone: "America/Toronto",
      configured_timezone: "America/New_York",
      value_revision: "value:0",
      preview: {
        setting_id: "wb.journal.day-boundary",
        scope: { kind: "profile", subject_id: "default" },
        value: "04:00",
        effective_at: "2026-07-16T04:00:00-04:00",
        apply_status: "pending",
        impact_preview: {},
      },
      diagnostics: [{
        code: "configured-timezone-mismatch",
        active_timezone: "America/Toronto",
        configured_timezone: "America/New_York",
        message: "The active and configured timezones differ.",
      }],
    });

    const preview = await previewSettingValue(
      "wb.journal.day-boundary",
      "04:00",
      "value:0",
      fetchImpl as typeof fetch,
    );

    expect(preview.diagnostics).toEqual([{
      code: "configured-timezone-mismatch",
      activeTimezone: "America/Toronto",
      configuredTimezone: "America/New_York",
      message: "The active and configured timezones differ.",
    }]);
  });
});

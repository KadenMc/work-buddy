import { asSettingsPageId } from "../dashboard/contributions/contracts";
import { asSettingId, asSettingPlacementId, type SettingsContribution } from "./contracts";

export const DASHBOARD_AI_SETTINGS_PAGE_ID = asSettingsPageId("wb.settings.system.dashboard-ai");
export const DASHBOARD_ASSISTANCE_SETTING_ID = asSettingId("wb.dashboard.assistance");
export const CHAT_EXECUTION_DEFAULT_SETTING_ID = asSettingId("wb.dashboard.chat-execution-default");

export const DASHBOARD_ASSISTANCE_HELP = {
  summary: "Enable assistance without starting an assistant.",
  details: "Assistance is off by default. Enabling it does not start a session or send data. "
    + "Each Start shows the provider/model and disclosure limits. Only supported fields and bounded chat context are shared; "
    + "later Send includes the form snapshot for that turn. Assistant edits stay visible and can be reviewed or conditionally undone. "
    + "The assistant cannot submit the form.",
};

const owner = {
  schemaVersion: 1 as const,
  definitionVersion: 1,
  valueVersion: 1,
  ownerId: "wb.dashboard",
  ownerLabel: "Dashboard AI",
  provenance: { complementId: "wb.dashboard", complementVersion: "0.x", trustTier: "native" as const, label: "Dashboard" },
  allowedScopes: ["profile" as const],
  defaultScope: "profile" as const,
  appliesTo: [{ kind: "system" as const, id: "wb.dashboard", label: "Dashboard AI" }],
  applyBehavior: "immediate" as const,
  sensitivity: "ordinary" as const,
  visibility: "frontend" as const,
};

/** Inert discoverability fallback; server registry and Settings values are authoritative. */
export const dashboardAiSettingsContribution: SettingsContribution = {
  sourceId: "wb.dashboard.native-settings",
  definitions: [
    {
      ...owner,
      settingId: DASHBOARD_ASSISTANCE_SETTING_ID,
      title: "Form assistance",
      summary: "Allow an assistant to help shape supported forms.",
      details: "Only Start authorizes sharing the disclosed form and chat with your selected model; submission stays yours.",
      defaultValue: "disabled",
      valueSchema: { type: "string", enum: ["disabled", "enabled"] },
      control: { kind: "select", options: [{ value: "disabled", label: "Off — no model assistance" }, { value: "enabled", label: "Allow form assistance" }] },
      searchKeywords: ["assistant", "draft", "privacy", "form"],
    },
    {
      ...owner,
      settingId: CHAT_EXECUTION_DEFAULT_SETTING_ID,
      title: "Default chat model",
      summary: "New chats start with this model; each chat keeps its own choice.",
      details: "Changing the default does not start a model or change existing chats.",
      defaultValue: { provider_id: "claude-code", model_id: "sonnet" },
      valueSchema: { type: "object", properties: { provider_id: { type: "string" }, model_id: { type: "string" } }, required: ["provider_id", "model_id"], additionalProperties: false },
      control: { kind: "execution-profile" },
      searchKeywords: ["assistant", "chat", "model", "provider", "default", "Claude", "Codex", "local"],
    },
  ],
  pages: [{
    schemaVersion: 1,
    pageId: DASHBOARD_AI_SETTINGS_PAGE_ID,
    ownerId: "wb.dashboard",
    route: "/settings/system/dashboard-ai",
    label: "Dashboard AI",
    description: "Shared chat defaults and form assistance across the dashboard.",
    navigationGroup: "system",
    navigationLabel: "Dashboard AI",
    navigationOrder: 30,
    context: { kind: "system", id: "wb.dashboard", label: "Dashboard AI" },
    sections: [{ sectionId: "assistance", label: "Chat and form assistance", order: 10 }],
  }],
  placements: [
    { schemaVersion: 1, placementId: asSettingPlacementId("wb.settings.placement.system.dashboard-ai.assistance"), settingId: DASHBOARD_ASSISTANCE_SETTING_ID, pageId: DASHBOARD_AI_SETTINGS_PAGE_ID, sectionId: "assistance", order: 10, preferredForSearch: true },
    { schemaVersion: 1, placementId: asSettingPlacementId("wb.settings.placement.system.dashboard-ai.chat-execution-default"), settingId: CHAT_EXECUTION_DEFAULT_SETTING_ID, pageId: DASHBOARD_AI_SETTINGS_PAGE_ID, sectionId: "assistance", order: 20, preferredForSearch: true },
  ],
};

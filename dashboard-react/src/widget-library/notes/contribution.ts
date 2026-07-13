import {
  asAppId,
  asWidgetModuleId,
  asWidgetRoleId,
  asWidgetTypeId,
  type AppContribution,
  type WidgetModule,
} from "../../dashboard/contributions/contracts";
import { STANDARD_WIDGET_THEME_SUPPORT } from "../../dashboard/contributions/themeContract";

export const NOTES_APP_ID = asAppId("wb.notes");
export const RUNNING_NOTES_ROLE_ID = asWidgetRoleId(
  "wb.widget-role.running-notes@1",
);
export const RUNNING_NOTES_TYPE_ID = asWidgetTypeId("wb.notes.running");
export const RUNNING_NOTES_MODULE_ID = asWidgetModuleId("wb.notes.running.renderer");

export const NOTES_APP_CONTRIBUTION = {
  schemaVersion: 1,
  appId: NOTES_APP_ID,
  definitionVersion: 1,
  displayName: "Work Buddy Notes",
  widgetRoles: [
    {
      roleId: RUNNING_NOTES_ROLE_ID,
      ownerAppId: NOTES_APP_ID,
      displayName: "Running Notes",
      description: "Review and version-edit a longitudinal Markdown item collection.",
      inputSchema: { schemaId: "wb.notes.running.input", version: 1 },
      outputIntentSchemas: [
        { schemaId: "wb.notes.edit-requested", version: 1 },
        { schemaId: "wb.notes.open-thread-requested", version: 1 },
      ],
    },
  ],
  widgetDefinitions: [
    {
      typeId: RUNNING_NOTES_TYPE_ID,
      definitionVersion: 1,
      publisherAppId: NOTES_APP_ID,
      displayName: "Running Notes",
      description: "Review and edit a chronological collection of Markdown notes.",
      libraryPath: ["Notes", "Running Notes"],
      providesRoles: [RUNNING_NOTES_ROLE_ID],
      settingsSchema: { schemaId: "wb.notes.running.settings", version: 1 },
      inputSchema: { schemaId: "wb.notes.running.input", version: 1 },
      outputIntentSchemas: [
        { schemaId: "wb.notes.edit-requested", version: 1 },
        { schemaId: "wb.notes.open-thread-requested", version: 1 },
      ],
      sizeContract: {
        default: { w: 8, h: 8 },
        min: { w: 6, h: 6 },
        max: { w: 24, h: 24 },
        modes: ["compact", "standard", "expanded"],
      },
      multiplicity: "single_per_view",
      rendererModuleId: RUNNING_NOTES_MODULE_ID,
      theme: {
        contractVersion: 1,
        conformance: "standard",
        supports: STANDARD_WIDGET_THEME_SUPPORT,
        styling: "semantic-tokens",
      },
    },
  ],
  views: [],
} as const satisfies AppContribution;

export const RUNNING_NOTES_MODULE: WidgetModule = {
  moduleId: RUNNING_NOTES_MODULE_ID,
  widgetTypeId: RUNNING_NOTES_TYPE_ID,
  load: async () => import("./RunningNotesWidget"),
};

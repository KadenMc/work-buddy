import {
  asAppId,
  asWidgetModuleId,
  asWidgetRoleId,
  asWidgetTypeId,
  type AppContribution,
  type WidgetModule,
} from "../../dashboard/contributions/contracts";
import { STANDARD_WIDGET_THEME_SUPPORT } from "../../dashboard/contributions/themeContract";

export const TIMELINE_APP_ID = asAppId("wb.timeline");
export const DAY_TIMELINE_ROLE_ID = asWidgetRoleId(
  "wb.widget-role.day-timeline@1",
);
export const DAY_TIMELINE_TYPE_ID = asWidgetTypeId("wb.timeline.day");
export const DAY_TIMELINE_MODULE_ID = asWidgetModuleId("wb.timeline.day.renderer");

export const TIMELINE_APP_CONTRIBUTION = {
  schemaVersion: 1,
  appId: TIMELINE_APP_ID,
  definitionVersion: 1,
  displayName: "Work Buddy Timeline",
  widgetRoles: [
    {
      roleId: DAY_TIMELINE_ROLE_ID,
      ownerAppId: TIMELINE_APP_ID,
      displayName: "Day Timeline",
      description: "Reconcile temporal records, commitments, and plans for one day.",
      inputSchema: { schemaId: "wb.timeline.day.input", version: 1 },
      outputIntentSchemas: [
        { schemaId: "wb.timeline.open-item", version: 1 },
        { schemaId: "wb.timeline.item-action-requested", version: 1 },
        { schemaId: "wb.timeline.render-mode-changed", version: 1 },
        { schemaId: "wb.timeline.replan-requested", version: 1 },
      ],
    },
  ],
  widgetDefinitions: [
    {
      typeId: DAY_TIMELINE_TYPE_ID,
      definitionVersion: 1,
      publisherAppId: TIMELINE_APP_ID,
      displayName: "Day Timeline",
      description: "See records, commitments, and plans together across a day.",
      libraryPath: ["Time", "Timelines", "Day Timeline"],
      providesRoles: [DAY_TIMELINE_ROLE_ID],
      settingsSchema: { schemaId: "wb.timeline.day.settings", version: 1 },
      inputSchema: { schemaId: "wb.timeline.day.input", version: 1 },
      outputIntentSchemas: [
        { schemaId: "wb.timeline.open-item", version: 1 },
        { schemaId: "wb.timeline.item-action-requested", version: 1 },
        { schemaId: "wb.timeline.render-mode-changed", version: 1 },
        { schemaId: "wb.timeline.replan-requested", version: 1 },
      ],
      sizeContract: {
        default: { w: 16, h: 16 },
        min: { w: 12, h: 8 },
        max: { w: 24, h: 24 },
        modes: ["compact", "standard", "expanded"],
      },
      multiplicity: "single_per_view",
      rendererModuleId: DAY_TIMELINE_MODULE_ID,
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

export const DAY_TIMELINE_MODULE: WidgetModule = {
  moduleId: DAY_TIMELINE_MODULE_ID,
  widgetTypeId: DAY_TIMELINE_TYPE_ID,
  load: async () => import("./DayTimelineWidget"),
};

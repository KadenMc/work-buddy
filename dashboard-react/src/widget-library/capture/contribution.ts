import {
  asAppId,
  asWidgetModuleId,
  asWidgetRoleId,
  asWidgetTypeId,
  type AppContribution,
  type WidgetModule,
} from "../../dashboard/contributions/contracts";
import { STANDARD_WIDGET_THEME_SUPPORT } from "../../dashboard/contributions/themeContract";

export const CAPTURE_APP_ID = asAppId("wb.capture");
export const CAPTURE_ROLE_ID = asWidgetRoleId("wb.widget-role.capture@1");
export const QUICK_TEXT_CAPTURE_TYPE_ID = asWidgetTypeId("wb.capture.quick-text");
export const QUICK_TEXT_CAPTURE_MODULE_ID = asWidgetModuleId(
  "wb.capture.quick-text.renderer",
);

export const CAPTURE_APP_CONTRIBUTION = {
  schemaVersion: 1,
  appId: CAPTURE_APP_ID,
  definitionVersion: 1,
  displayName: "Work Buddy Capture",
  widgetRoles: [
    {
      roleId: CAPTURE_ROLE_ID,
      ownerAppId: CAPTURE_APP_ID,
      displayName: "Capture",
      description: "Preserve exact user-supplied material at a visible destination.",
      inputSchema: { schemaId: "wb.capture.quick-text.input", version: 1 },
      outputIntentSchemas: [{ schemaId: "wb.capture.submit", version: 1 }],
    },
  ],
  widgetDefinitions: [
    {
      typeId: QUICK_TEXT_CAPTURE_TYPE_ID,
      definitionVersion: 1,
      publisherAppId: CAPTURE_APP_ID,
      displayName: "Quick Capture",
      description: "Capture exact text without leaving the current view.",
      libraryPath: ["Capture", "Quick Capture"],
      providesRoles: [CAPTURE_ROLE_ID],
      settingsSchema: { schemaId: "wb.capture.quick-text.settings", version: 1 },
      inputSchema: { schemaId: "wb.capture.quick-text.input", version: 1 },
      outputIntentSchemas: [{ schemaId: "wb.capture.submit", version: 1 }],
      sizeContract: {
        default: { w: 8, h: 4 },
        min: { w: 6, h: 3 },
        max: { w: 24, h: 12 },
        modes: ["compact", "standard", "expanded"],
      },
      multiplicity: "multiple_per_view",
      rendererModuleId: QUICK_TEXT_CAPTURE_MODULE_ID,
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

export const QUICK_TEXT_CAPTURE_MODULE: WidgetModule = {
  moduleId: QUICK_TEXT_CAPTURE_MODULE_ID,
  widgetTypeId: QUICK_TEXT_CAPTURE_TYPE_ID,
  load: async () => import("./QuickTextCaptureWidget"),
};

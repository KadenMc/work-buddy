import type {
  AppContribution,
  WidgetModule,
  WidgetRoleId,
  WidgetTypeId,
} from "../../dashboard/contributions/contracts";
import {
  JOURNAL_APP_ID,
  JOURNAL_GENERIC_ROLE_ID,
  JOURNAL_GENERIC_WIDGET_MODULE_ID,
  JOURNAL_GENERIC_WIDGET_TYPE_ID,
  JOURNAL_ROLE_IDS,
  JOURNAL_WIDGET_TYPE_IDS,
} from "./bindings";
import { JOURNAL_VIEW_DEFINITION } from "./viewDefinition";
import { STANDARD_WIDGET_THEME_SUPPORT } from "../../dashboard/contributions/themeContract";

/**
 * Registration prerequisites supplied by reusable widget-library contributions.
 * Journal owns the view purposes and selections, not these external renderers.
 */
export const JOURNAL_EXTERNAL_CONTRIBUTION_DEPENDENCIES = {
  roles: Object.values(JOURNAL_ROLE_IDS) as readonly WidgetRoleId[],
  widgetTypes: Object.values(JOURNAL_WIDGET_TYPE_IDS) as readonly WidgetTypeId[],
} as const;

export const JOURNAL_APP_CONTRIBUTION = {
  schemaVersion: 1,
  appId: JOURNAL_APP_ID,
  definitionVersion: 1,
  displayName: "Journal",
  widgetRoles: [
    {
      roleId: JOURNAL_GENERIC_ROLE_ID,
      ownerAppId: JOURNAL_APP_ID,
      displayName: "Journal module",
      description: "Render one trusted, provider-resolved Journal module and its typed fields.",
      inputSchema: { schemaId: "wb.journal.module.input", version: 1 },
      outputIntentSchemas: [
        { schemaId: "wb.journal.field-value.put", version: 1 },
      ],
    },
  ],
  widgetDefinitions: [
    {
      typeId: JOURNAL_GENERIC_WIDGET_TYPE_ID,
      definitionVersion: 1,
      publisherAppId: JOURNAL_APP_ID,
      displayName: "Journal section",
      description: "Shows a data-defined Journal section and typed values.",
      libraryPath: ["Journal", "Section"],
      providesRoles: [JOURNAL_GENERIC_ROLE_ID],
      settingsSchema: { schemaId: "wb.journal.module.settings", version: 1 },
      inputSchema: { schemaId: "wb.journal.module.input", version: 1 },
      outputIntentSchemas: [
        { schemaId: "wb.journal.field-value.put", version: 1 },
      ],
      outputIntentEffects: [
        {
          schema: { schemaId: "wb.journal.field-value.put", version: 1 },
          effect: "mutation",
          preview: "block",
        },
      ],
      sizeContract: {
        default: { w: 12, h: 8 },
        min: { w: 6, h: 4 },
        max: { w: 24, h: 24 },
        modes: ["compact", "standard", "expanded"],
      },
      multiplicity: "multiple_per_view",
      rendererModuleId: JOURNAL_GENERIC_WIDGET_MODULE_ID,
      theme: {
        contractVersion: 1,
        conformance: "standard",
        supports: STANDARD_WIDGET_THEME_SUPPORT,
        styling: "semantic-tokens",
      },
    },
  ],
  views: [JOURNAL_VIEW_DEFINITION],
} as const satisfies AppContribution;

export const JOURNAL_WIDGET_MODULES: readonly WidgetModule[] = [
  {
    moduleId: JOURNAL_GENERIC_WIDGET_MODULE_ID,
    widgetTypeId: JOURNAL_GENERIC_WIDGET_TYPE_ID,
    load: () => import("./JournalGenericModule"),
  },
];

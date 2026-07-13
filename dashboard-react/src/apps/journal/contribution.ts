import type {
  AppContribution,
  WidgetRoleId,
  WidgetTypeId,
} from "../../dashboard/contributions/contracts";
import {
  JOURNAL_APP_ID,
  JOURNAL_ROLE_IDS,
  JOURNAL_WIDGET_TYPE_IDS,
} from "./bindings";
import { JOURNAL_VIEW_DEFINITION } from "./viewDefinition";

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
  widgetRoles: [],
  widgetDefinitions: [],
  views: [JOURNAL_VIEW_DEFINITION],
} as const satisfies AppContribution;

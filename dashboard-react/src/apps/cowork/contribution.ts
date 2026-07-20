import type {
  AppContribution,
  JsonSchemaReference,
  WidgetDefinition,
  WidgetRoleContract,
} from "../../dashboard/contributions/contracts";
import {
  COWORK_APP_ID,
  COWORK_ROLE_IDS,
  COWORK_WORKSPACE_MODULE_ID,
  COWORK_WORKSPACE_ROLE_ID,
  COWORK_WORKSPACE_TYPE_ID,
} from "./bindings";
import { COWORK_VIEW_DEFINITION, COWORK_WORKSPACE_THEME } from "./viewDefinition";

/** The one input schema the workspace role and its filling widget both declare. */
const COWORK_WORKSPACE_INPUT_SCHEMA: JsonSchemaReference = {
  schemaId: "wb.cowork.workspace-card.input",
  version: 1,
};

/**
 * The roles the Co-work App owns (section 5.1). The workspace role is the functional
 * contract the composite durable card fills. The editor, review-rail, and health-strip
 * roles remain the App's stable identity units for the three regions the card composes
 * inside one live tree, rather than placing them on the grid as separate widgets.
 */
const COWORK_WIDGET_ROLES: readonly WidgetRoleContract[] = [
  {
    roleId: COWORK_WORKSPACE_ROLE_ID,
    ownerAppId: COWORK_APP_ID,
    displayName: "Co-work workspace",
    description:
      "The composite co-authoring surface: the editor, its review rail, and the health strip on one shared live session.",
    inputSchema: COWORK_WORKSPACE_INPUT_SCHEMA,
  },
  {
    roleId: COWORK_ROLE_IDS.editor,
    ownerAppId: COWORK_APP_ID,
    displayName: "Co-work editor",
    description: "Owns the live document, its suggestion decorations, and materialization.",
  },
  {
    roleId: COWORK_ROLE_IDS.reviewRail,
    ownerAppId: COWORK_APP_ID,
    displayName: "Co-work review rail",
    description: "Aligned proposal review and the document conversation, ledger-backed.",
  },
  {
    roleId: COWORK_ROLE_IDS.healthStrip,
    ownerAppId: COWORK_APP_ID,
    displayName: "Co-work health strip",
    description: "Read-only document identity, drift state, and open-proposal count.",
  },
];

/**
 * The composite Co-work workspace card: one app-owned durable widget. It is marked durable
 * so the dashboard keeps its live element mounted across every grid remount, customize
 * toggle, and interaction recovery instead of re-hydrating it from a snapshot each time.
 * Validation enforces the two durable invariants declared here, single_per_view and no
 * drafts. It emits no outward intents: the coarse session arrives as input, and the live
 * document, its Y.Doc, and the staged sitting take the direct route the durable exemption
 * sanctions.
 */
const COWORK_WORKSPACE_WIDGET: WidgetDefinition = {
  typeId: COWORK_WORKSPACE_TYPE_ID,
  definitionVersion: 1,
  publisherAppId: COWORK_APP_ID,
  displayName: "Co-work workspace",
  description:
    "Co-author a document with its tracked AI proposals and the review rail on one shared live session.",
  libraryPath: ["Co-work", "Workspace"],
  providesRoles: [COWORK_WORKSPACE_ROLE_ID],
  settingsSchema: { schemaId: "wb.cowork.workspace-card.settings", version: 1 },
  inputSchema: COWORK_WORKSPACE_INPUT_SCHEMA,
  outputIntentSchemas: [],
  sizeContract: {
    default: { w: 24, h: 20 },
    min: { w: 12, h: 10 },
    modes: ["compact", "standard", "expanded"],
  },
  multiplicity: "single_per_view",
  rendererModuleId: COWORK_WORKSPACE_MODULE_ID,
  durable: true,
  theme: COWORK_WORKSPACE_THEME,
};

/**
 * Pure, serializable Co-work App contribution (section 5). The executable renderer module
 * and the coarse provider live beside it and are registered separately by the dashboard
 * registry at the integration join.
 */
export const COWORK_APP_CONTRIBUTION = {
  schemaVersion: 1,
  appId: COWORK_APP_ID,
  definitionVersion: 1,
  displayName: "Co-work",
  widgetRoles: COWORK_WIDGET_ROLES,
  widgetDefinitions: [COWORK_WORKSPACE_WIDGET],
  views: [COWORK_VIEW_DEFINITION],
} as const satisfies AppContribution;

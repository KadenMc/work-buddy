import type {
  AppContribution,
  WidgetRoleContract,
} from "../../dashboard/contributions/contracts";
import { COWORK_APP_ID, COWORK_ROLE_IDS } from "./bindings";
import { COWORK_VIEW_DEFINITION } from "./viewDefinition";

/**
 * The three region roles the Co-work App owns (section 5.1). They are the stable
 * identity and theme units the App renderer composes. No widget provides them, because
 * a single-surface view mounts one App-owned renderer rather than grid widgets.
 */
const COWORK_WIDGET_ROLES: readonly WidgetRoleContract[] = [
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
 * Pure, serializable Co-work App contribution (section 5). The executable surface
 * renderer and coarse provider live in the view module, registered separately by the
 * dashboard registry at the integration join.
 */
export const COWORK_APP_CONTRIBUTION = {
  schemaVersion: 1,
  appId: COWORK_APP_ID,
  definitionVersion: 1,
  displayName: "Co-work",
  widgetRoles: COWORK_WIDGET_ROLES,
  widgetDefinitions: [],
  views: [COWORK_VIEW_DEFINITION],
} as const satisfies AppContribution;

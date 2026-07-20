import {
  asAppId,
  asViewId,
  asViewModuleId,
  asWidgetInstanceId,
  asWidgetModuleId,
  asWidgetRoleId,
  asWidgetSlotId,
  asWidgetTypeId,
} from "../../dashboard/contributions/contracts";

/**
 * Stable identities for the Co-work App contribution (C1 surface contract section 5).
 * The App id, view id, route, versioned role ids, and view-module id are the registry
 * safety-invariant units and never change without a version bump.
 */
export const COWORK_APP_ID = asAppId("wb.cowork");

export const COWORK_VIEW_ID = asViewId("wb.cowork.workspace");

/** Route segment beneath the dashboard `/app/` basename, so the full route is `/app/cowork`. */
export const COWORK_ROUTE = "cowork";

export const COWORK_VIEW_MODULE_ID = asViewModuleId("wb.cowork.workspace.module");

/**
 * Identities for the composite workspace card: the one durable widget the standard-grid
 * Co-work view places. The slot is the view's single stable purpose, the instance id is
 * its opaque placement (a colon separates the segments because instance ids forbid dots),
 * and the role, type, and renderer-module ids are the registry safety-invariant units.
 */
export const COWORK_WORKSPACE_SLOT_ID = asWidgetSlotId("workspace");
export const COWORK_WORKSPACE_INSTANCE_ID = asWidgetInstanceId("wb-cowork:workspace");
export const COWORK_WORKSPACE_ROLE_ID = asWidgetRoleId("wb.widget-role.cowork-workspace@1");
export const COWORK_WORKSPACE_TYPE_ID = asWidgetTypeId("wb.cowork.workspace-card");
export const COWORK_WORKSPACE_MODULE_ID = asWidgetModuleId(
  "wb.cowork.workspace-card.renderer",
);

/**
 * Versioned role ids for the three regions the workspace card composes internally
 * (section 5.1). They remain the App's stable identity units for the editor, the review
 * rail, and the health strip, now composed inside one durable widget rather than placed
 * on the grid as separate widgets.
 */
export const COWORK_ROLE_IDS = {
  editor: asWidgetRoleId("wb.widget-role.cowork-editor@1"),
  reviewRail: asWidgetRoleId("wb.widget-role.cowork-review-rail@1"),
  healthStrip: asWidgetRoleId("wb.widget-role.cowork-health-strip@1"),
} as const;

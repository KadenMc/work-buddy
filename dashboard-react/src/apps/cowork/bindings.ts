import {
  asAppId,
  asViewId,
  asViewModuleId,
  asWidgetRoleId,
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

/** Versioned role ids for the three App-composed regions (section 5.1). */
export const COWORK_ROLE_IDS = {
  editor: asWidgetRoleId("wb.widget-role.cowork-editor@1"),
  reviewRail: asWidgetRoleId("wb.widget-role.cowork-review-rail@1"),
  healthStrip: asWidgetRoleId("wb.widget-role.cowork-health-strip@1"),
} as const;

/** Local region ids composed by the App renderer (section 5.1 slot ids). */
export const COWORK_REGION_IDS = {
  editor: "editor",
  reviewRail: "review-rail",
  healthStrip: "health-strip",
} as const;

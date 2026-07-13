import type { ComponentType } from "react";

import JournalPanel from "../components/JournalPanel";

/**
 * Temporary route projection used while the contribution registry is landing.
 * The shell consumes only this generic shape; the integration step can project
 * registered ViewDefinitions into it without adding view-specific branches to
 * DashboardApp or TabBar.
 */
export interface DashboardRouteDefinition {
  readonly viewId: string;
  readonly path: string;
  readonly label: string;
  readonly component: ComponentType;
  readonly isDefault?: boolean;
}

function defineDashboardRoutes(
  definitions: readonly DashboardRouteDefinition[],
): readonly DashboardRouteDefinition[] {
  if (definitions.length === 0) {
    throw new Error("The dashboard route registry must contain at least one view");
  }

  const paths = new Set<string>();
  const viewIds = new Set<string>();
  let defaultCount = 0;

  for (const definition of definitions) {
    if (!/^[a-z0-9][a-z0-9_-]*$/i.test(definition.path)) {
      throw new Error(`Invalid dashboard route path: ${definition.path}`);
    }
    if (paths.has(definition.path) || viewIds.has(definition.viewId)) {
      throw new Error(`Duplicate dashboard route: ${definition.viewId}`);
    }
    paths.add(definition.path);
    viewIds.add(definition.viewId);
    defaultCount += definition.isDefault ? 1 : 0;
  }

  if (defaultCount !== 1) {
    throw new Error("The dashboard route registry must have exactly one default view");
  }

  return Object.freeze([...definitions]);
}

export const dashboardRoutes = defineDashboardRoutes([
  {
    viewId: "wb.journal.main",
    path: "journal",
    label: "Journal",
    component: JournalPanel,
    isDefault: true,
  },
]);

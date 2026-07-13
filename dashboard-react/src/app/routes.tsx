import { lazy, useMemo, type ComponentType } from "react";

import type { ViewDefinition } from "../dashboard/contributions/contracts";
import type { LoadedStandardWidgetViewModule } from "../dashboard/contributions/viewModules";
import type { ContributionRegistry } from "../dashboard/contributions/registry";
import { ViewHost } from "../dashboard/views/ViewHost";
import { dashboardRegistry } from "./dashboardRegistry";

/**
 * Generic route projection consumed by the dashboard shell. Both metadata and
 * the lazy page component are discovered by View ID from contribution registry.
 */
export interface DashboardRouteDefinition {
  readonly viewId: string;
  readonly path: string;
  readonly label: string;
  readonly component: ComponentType;
  readonly isDefault?: boolean;
}

export function defineDashboardRoutes(
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

interface StandardWidgetViewMountProps {
  readonly registry: ContributionRegistry;
  readonly definition: ViewDefinition;
  readonly loaded: LoadedStandardWidgetViewModule;
}

function StandardWidgetViewMount({
  registry,
  definition,
  loaded,
}: StandardWidgetViewMountProps) {
  const runtime = useMemo(
    () =>
      loaded.createRuntime({
        search: window.location.search,
        storage: window.localStorage,
      }),
    [loaded],
  );
  return (
    <ViewHost
      registry={registry}
      definition={definition}
      provider={runtime.provider}
      personalizationRepository={runtime.personalizationRepository}
      providerLabel={runtime.providerLabel}
      renderChrome={runtime.renderChrome}
    />
  );
}

export function projectDashboardRoutes(
  registry: ContributionRegistry,
): readonly DashboardRouteDefinition[] {
  return defineDashboardRoutes(
    registry.listViews().map(({ definition }) => {
      // Fail during registry projection, rather than after a user navigates, if an
      // App published route metadata without its executable page binding.
      registry.requireViewModule(definition.viewId);
      const component = lazy(async () => {
        const loaded = await registry.loadViewModule(definition.viewId);
        return {
          default: () => (
            <StandardWidgetViewMount
              registry={registry}
              definition={definition}
              loaded={loaded}
            />
          ),
        };
      });
      return {
        viewId: definition.viewId,
        path: definition.route,
        label: definition.navigation.label,
        component,
        isDefault: definition.navigation.isDefault,
      };
    }),
  );
}

export const dashboardRoutes = projectDashboardRoutes(dashboardRegistry);

import { COWORK_APP_CONTRIBUTION } from "../apps/cowork/contribution";
import { COWORK_VIEW_MODULE } from "../apps/cowork/viewModule";
import { JOURNAL_APP_CONTRIBUTION } from "../apps/journal/contribution";
import { JOURNAL_VIEW_MODULE } from "../apps/journal/viewModule";
import { createContributionRegistry } from "../dashboard/contributions/registry";
import {
  WIDGET_LIBRARY_CONTRIBUTIONS,
  WIDGET_LIBRARY_MODULES_BY_APP,
} from "../widget-library";

export const dashboardRegistry = createContributionRegistry();

for (const contribution of WIDGET_LIBRARY_CONTRIBUTIONS) {
  dashboardRegistry.registerApp(
    contribution,
    WIDGET_LIBRARY_MODULES_BY_APP.get(contribution.appId) ?? [],
    [],
    { trust: "native" },
  );
}

// Journal owns its ViewDefinition and lazy page module. The selected reusable widget
// roles and renderers must already exist so registration can validate the composition.
dashboardRegistry.registerApp(
  JOURNAL_APP_CONTRIBUTION,
  [],
  [JOURNAL_VIEW_MODULE],
  { trust: "native" },
);

// Co-work owns a single-surface ViewDefinition and its lazy view module. Routing
// /app/cowork auto-projects through this registration with no routes.tsx change.
dashboardRegistry.registerApp(
  COWORK_APP_CONTRIBUTION,
  [],
  [COWORK_VIEW_MODULE],
  { trust: "native" },
);

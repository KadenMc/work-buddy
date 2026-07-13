import { JOURNAL_APP_CONTRIBUTION } from "../apps/journal/contribution";
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
  );
}

// Journal intentionally owns only its ViewDefinition. The selected reusable widget
// roles and renderers must already exist so registration can validate the composition.
dashboardRegistry.registerApp(JOURNAL_APP_CONTRIBUTION, []);

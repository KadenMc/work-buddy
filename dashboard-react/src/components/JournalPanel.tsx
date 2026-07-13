import { useMemo } from "react";

import { JournalViewChrome } from "../apps/journal/chrome/JournalViewChrome";
import type { JournalViewModel } from "../apps/journal/contracts";
import { JOURNAL_VIEW_DEFINITION } from "../apps/journal/viewDefinition";
import { InMemoryJournalProvider } from "../apps/journal/providers/InMemoryJournalProvider";
import "../apps/journal/styles.css";
import { dashboardRegistry } from "../app/dashboardRegistry";
import type { ViewSnapshot } from "../dashboard/contributions/contracts";
import { selectViewProviderFromSearch } from "../dashboard/providers/providerSelection";
import { LocalStoragePersonalizationRepository } from "../dashboard/personalization/repository";
import { ViewHost } from "../dashboard/views/ViewHost";

export default function JournalPanel() {
  const provider = useMemo(() => new InMemoryJournalProvider(), []);
  const repository = useMemo(
    () => new LocalStoragePersonalizationRepository(window.localStorage),
    [],
  );
  const selection = selectViewProviderFromSearch(
    [
      {
        id: "demo",
        label: "Demo data · in-memory provider",
        isDemo: true,
        provider,
      },
    ],
    {
      search: window.location.search,
      defaultId: "demo",
    },
  );

  return (
    <ViewHost
      registry={dashboardRegistry}
      definition={JOURNAL_VIEW_DEFINITION}
      provider={selection.provider}
      personalizationRepository={repository}
      providerLabel={selection.label}
      renderChrome={(snapshot: ViewSnapshot) => {
        const model = snapshot.model as JournalViewModel;
        return (
          <JournalViewChrome
            day={model.day}
            access={model.access}
            quality={model.quality}
            source={model.source}
          />
        );
      }}
    />
  );
}

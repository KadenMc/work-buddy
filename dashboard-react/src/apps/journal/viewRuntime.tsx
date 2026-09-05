import { JournalViewChrome } from "./chrome/JournalViewChrome";
import type { JournalViewModel } from "./contracts";
import {
  journalSearchForDay,
  journalSearchHasDay,
  journalTodayLocalDate,
  shiftJournalLocalDate,
} from "./journalDay";
import { InMemoryJournalProvider } from "./providers/InMemoryJournalProvider";
import { LegacyFlaskViewAdapter } from "./providers/LegacyFlaskViewAdapter";
import { HttpJournalProvider } from "./providers/HttpJournalProvider";
import "./styles.css";
import type { ViewSnapshot } from "../../dashboard/contributions/contracts";
import type {
  StandardViewRuntimeConfiguration,
  StandardViewRuntimeContext,
} from "../../dashboard/contributions/viewModules";
import { LocalStoragePersonalizationRepository } from "../../dashboard/personalization/repository";
import { selectViewProviderFromSearch } from "../../dashboard/providers/providerSelection";
import { Button } from "../../ui";

export const hostContractVersion = 1 as const;

/** Journal contributes runtime adapters and chrome; Dashboard Core owns ViewHost. */
export function createRuntime(
  context: StandardViewRuntimeContext,
): StandardViewRuntimeConfiguration {
  const selection = selectViewProviderFromSearch(
    [
      {
        id: "demo",
        label: "Demo data · in-memory provider",
        isDemo: true,
        provider: new InMemoryJournalProvider(),
      },
      {
        id: "live",
        label: "Live Journal",
        isDemo: false,
        provider: new HttpJournalProvider({ location: context.location }),
      },
      {
        id: "legacy",
        label: "Older Today view · review only",
        isDemo: false,
        provider: new LegacyFlaskViewAdapter(),
      },
    ],
    { search: context.search, defaultId: "live" },
  );

  const returnToToday = () => {
    context.location.pushSearch(
      journalSearchForDay(context.location.getSearch(), null),
    );
  };

  return {
    provider: selection.provider,
    providerLabel: selection.label,
    personalizationRepository: new LocalStoragePersonalizationRepository(
      context.storage,
    ),
    renderChrome: (snapshot: ViewSnapshot, slots) => {
      const model = snapshot.model as JournalViewModel | null;
      if (model === null) {
        return (
          <header className="journal-view-chrome" aria-labelledby="journal-view-title">
            <div className="journal-view-chrome__main">
              <h1 id="journal-view-title">Journal</h1>
              <div className="journal-view-chrome__actions">
                {journalSearchHasDay(context.location.getSearch()) ? (
                  <Button size="small" variant="ghost" onClick={returnToToday}>
                    Today
                  </Button>
                ) : null}
                {slots.contextualActions}
              </div>
            </div>
            <p className="journal-view-chrome__notice" role="status">
              {snapshot.quality.message ??
                "The selected Journal provider is unavailable."}
            </p>
          </header>
        );
      }
      const today = journalTodayLocalDate(model.day);
      return (
        <JournalViewChrome
          day={model.day}
          access={model.access}
          quality={model.quality}
          source={model.source}
          hostActions={slots.contextualActions}
          {...(selection.id === "live"
            ? {
                onNavigateDay: (direction: "previous" | "next") => {
                  context.location.pushSearch(journalSearchForDay(
                    context.location.getSearch(),
                    shiftJournalLocalDate(
                      model.day.localDate,
                      direction === "previous" ? -1 : 1,
                      today,
                    ),
                  ));
                },
                onSelectDay: (localDate: string) => context.location.pushSearch(
                  journalSearchForDay(context.location.getSearch(), localDate),
                ),
                ...(journalSearchHasDay(context.location.getSearch())
                  ? { onReturnToToday: returnToToday }
                  : {}),
              }
            : {})}
        />
      );
    },
  };
}

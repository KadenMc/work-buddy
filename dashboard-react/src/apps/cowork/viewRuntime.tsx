import { CoworkViewChrome, type CoworkProviderState } from "./chrome/CoworkViewChrome";
import type {
  StandardViewRuntimeConfiguration,
  StandardViewRuntimeContext,
} from "../../dashboard/contributions/viewModules";
import { LocalStoragePersonalizationRepository } from "../../dashboard/personalization/repository";
import type { CoworkViewModel } from "./contracts";
import {
  HttpCoworkProvider,
  type CoworkLocationAdapter,
} from "./providers/HttpCoworkProvider";

export const hostContractVersion = 1 as const;

type LocationAwareRuntimeContext = StandardViewRuntimeContext & {
  readonly location?: CoworkLocationAdapter;
};

/** Temporary compatibility bridge while every standard view adopts UI-01. */
const browserLocationAdapter = (initialSearch: string): CoworkLocationAdapter => {
  let search = initialSearch;
  const listeners = new Set<(next: string) => void>();
  const publish = (): void => {
    search = window.location.search;
    for (const listener of listeners) listener(search);
  };
  window.addEventListener("popstate", publish);
  return {
    getSearch: () => search,
    pushSearch: (next) => {
      window.history.pushState({}, "", `${window.location.pathname}${next}${window.location.hash}`);
      publish();
    },
    replaceSearch: (next) => {
      window.history.replaceState({}, "", `${window.location.pathname}${next}${window.location.hash}`);
      publish();
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
};

const providerStateFromModel = (model: unknown): CoworkProviderState | null => {
  const state = model as Partial<CoworkViewModel> | null;
  if (state?.activeSession?.kind === "registered") return "live";
  if (state?.activeSession?.kind === "scratch") return "local";
  return null;
};

/** Production Co-work runtime: HTTP lifecycle provider plus router-owned location. */
export function createRuntime(
  context: StandardViewRuntimeContext,
): StandardViewRuntimeConfiguration {
  const aware = context as LocationAwareRuntimeContext;
  const location = aware.location ?? browserLocationAdapter(context.search);
  return {
    provider: new HttpCoworkProvider({
      location,
      storage: context.storage,
    }),
    providerLabel: "Co-work Folder and document session",
    personalizationRepository: new LocalStoragePersonalizationRepository(context.storage),
    renderChrome: (snapshot, slots) => (
      <CoworkViewChrome
        providerState={providerStateFromModel(snapshot.model)}
        hostActions={slots.contextualActions}
      />
    ),
  };
}

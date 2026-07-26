import { CoworkViewChrome } from "./chrome/CoworkViewChrome";
import type {
  StandardViewRuntimeConfiguration,
  StandardViewRuntimeContext,
} from "../../dashboard/contributions/viewModules";
import { LocalStoragePersonalizationRepository } from "../../dashboard/personalization/repository";
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
    renderChrome: (_snapshot, slots) => (
      <CoworkViewChrome hostActions={slots.contextualActions} />
    ),
  };
}

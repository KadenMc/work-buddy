import type { ViewProvider } from "./ViewProvider";

export interface ViewProviderRegistration {
  readonly id: string;
  readonly label: string;
  readonly isDemo: boolean;
  readonly provider: ViewProvider;
}

export interface SelectedViewProvider extends ViewProviderRegistration {
  readonly selectedExplicitly: boolean;
}

export class ProviderSelectionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ProviderSelectionError";
  }
}

export function readRequestedProviderId(
  search: string,
  parameter = "provider",
): string | undefined {
  const value = new URLSearchParams(search).get(parameter)?.trim();
  return value === undefined || value === "" ? undefined : value;
}

/**
 * Selects an explicitly registered provider. An unknown query value is an error, never
 * a silent fallback from failed live data to convincing demo data.
 */
export function selectViewProvider(
  registrations: readonly ViewProviderRegistration[],
  options: {
    readonly requestedId?: string;
    readonly defaultId: string;
  },
): SelectedViewProvider {
  const byId = new Map<string, ViewProviderRegistration>();
  for (const registration of registrations) {
    if (!/^[a-z0-9][a-z0-9:_-]*$/i.test(registration.id)) {
      throw new ProviderSelectionError(`Invalid provider ID: ${registration.id}`);
    }
    if (byId.has(registration.id)) {
      throw new ProviderSelectionError(`Duplicate provider ID: ${registration.id}`);
    }
    byId.set(registration.id, registration);
  }

  const selectedId = options.requestedId ?? options.defaultId;
  const selected = byId.get(selectedId);
  if (selected === undefined) {
    const source = options.requestedId === undefined ? "default" : "requested";
    throw new ProviderSelectionError(`Unknown ${source} provider: ${selectedId}`);
  }
  return {
    ...selected,
    selectedExplicitly: options.requestedId !== undefined,
  };
}

export function selectViewProviderFromSearch(
  registrations: readonly ViewProviderRegistration[],
  options: {
    readonly search: string;
    readonly defaultId: string;
    readonly parameter?: string;
  },
): SelectedViewProvider {
  return selectViewProvider(registrations, {
    requestedId: readRequestedProviderId(options.search, options.parameter),
    defaultId: options.defaultId,
  });
}


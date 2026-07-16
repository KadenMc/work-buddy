import type { ComponentType, ReactNode } from "react";

import type { PersonalizationRepository } from "../personalization/repository";
import type { ViewProvider } from "../providers/ViewProvider";
import type { ViewId, ViewModuleId, ViewSnapshot } from "./contracts";

export interface StandardViewRuntimeContext {
  readonly search: string;
  readonly storage: Storage;
}

/** Host-owned controls which an App may place in its chrome without owning behavior. */
export interface StandardViewChromeSlots {
  readonly contextualActions?: ReactNode;
}

/** The only executable seams available to a standard, shareable widget view. */
export interface StandardViewRuntimeConfiguration {
  readonly provider: ViewProvider;
  readonly personalizationRepository: PersonalizationRepository;
  readonly providerLabel?: string;
  readonly renderChrome?: (
    snapshot: ViewSnapshot,
    slots: StandardViewChromeSlots,
  ) => ReactNode;
}

export interface LoadedStandardWidgetViewModule {
  readonly hostContractVersion: 1;
  createRuntime(
    context: StandardViewRuntimeContext,
  ): StandardViewRuntimeConfiguration;
}

/**
 * A standard module contributes provider/repository/chrome configuration only.
 * Dashboard Core—not the App—always instantiates ViewHost and WidgetHost.
 */
export interface StandardWidgetViewModule {
  readonly kind: "standard-widget-view";
  readonly hostContractVersion: 1;
  readonly moduleId: ViewModuleId;
  readonly viewId: ViewId;
  load(): Promise<LoadedStandardWidgetViewModule>;
}

/**
 * Escape hatch deliberately kept distinct from a standard shareable view.
 * The standard contribution registry rejects this kind; a future developer-mode
 * registry must enforce the explicit trust gate before evaluating its code.
 */
export interface DeveloperRootViewModule {
  readonly kind: "developer-root";
  readonly trustGate: "developer-mode";
  readonly moduleId: ViewModuleId;
  readonly viewId: ViewId;
  load(): Promise<{ readonly default: ComponentType }>;
}

export type ViewModule = StandardWidgetViewModule | DeveloperRootViewModule;

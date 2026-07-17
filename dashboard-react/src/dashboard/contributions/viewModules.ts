import type { ComponentType, ReactNode } from "react";

import type { PersonalizationRepository } from "../personalization/repository";
import type { ViewProvider } from "../providers/ViewProvider";
import type { ViewDefinition, ViewId, ViewModuleId, ViewSnapshot } from "./contracts";

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

/**
 * Props Dashboard Core passes to a single-surface view's App-owned renderer. The
 * renderer receives the same coarse ViewProvider a standard view does (which document
 * is open, its content-meta, drift, and SSE-driven invalidations) and owns everything
 * below the coarse session itself: its live local state and any direct route calls.
 * It is not a hydrated widget, so the Widget Renderer Contract's URL/SSE/direct-call
 * exclusions do not apply to it.
 */
export interface SingleSurfaceRuntimeProps {
  readonly definition: ViewDefinition;
  readonly provider: ViewProvider;
  readonly providerLabel?: string;
}

/** The App-owned renderer a single-surface view mounts through ViewHost. */
export type SingleSurfaceComponent = ComponentType<SingleSurfaceRuntimeProps>;

export interface LoadedStandardWidgetViewModule {
  readonly hostContractVersion: 1;
  createRuntime(
    context: StandardViewRuntimeContext,
  ): StandardViewRuntimeConfiguration;
  /**
   * Present only when the bound view declares `layoutKind: "single-surface"`. ViewHost
   * mounts this App-owned renderer instead of the widget grid. Absent for standard
   * grid views, whose module contributes only runtime configuration.
   */
  readonly surface?: SingleSurfaceComponent;
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

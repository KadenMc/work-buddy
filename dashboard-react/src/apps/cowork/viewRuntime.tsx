import type {
  SingleSurfaceComponent,
  StandardViewRuntimeConfiguration,
  StandardViewRuntimeContext,
} from "../../dashboard/contributions/viewModules";
import { LocalStoragePersonalizationRepository } from "../../dashboard/personalization/repository";
import { InMemoryCoworkProvider } from "./providers/InMemoryCoworkProvider";
import { CoworkWorkspaceSurface } from "./surface/CoworkWorkspaceSurface";

export const hostContractVersion = 1 as const;

/**
 * Co-work contributes the coarse document-session provider. Dashboard Core mounts the
 * single App-owned surface (below) rather than a widget grid, so the personalization
 * repository is present only to satisfy the standard runtime shape and is never used by
 * a single-surface view.
 */
export function createRuntime(
  context: StandardViewRuntimeContext,
): StandardViewRuntimeConfiguration {
  return {
    provider: new InMemoryCoworkProvider(),
    providerLabel: "Demo data · in-memory Co-work provider",
    personalizationRepository: new LocalStoragePersonalizationRepository(
      context.storage,
    ),
  };
}

/**
 * The App-owned renderer ViewHost mounts for this single-surface view (section 5.3, N8).
 * It legitimately sits outside the Widget Renderer Contract's URL/SSE/direct-operation
 * exclusion because it is one cohesive root, not a hydrated widget.
 */
export const surface: SingleSurfaceComponent = CoworkWorkspaceSurface;

import type {
  StandardViewRuntimeConfiguration,
  StandardViewRuntimeContext,
} from "../../dashboard/contributions/viewModules";
import { LocalStoragePersonalizationRepository } from "../../dashboard/personalization/repository";
import { InMemoryCoworkProvider } from "./providers/InMemoryCoworkProvider";

export const hostContractVersion = 1 as const;

/**
 * Co-work contributes the coarse document-session provider and the personalization
 * repository for its standard-grid view. Dashboard Core mounts the widget grid and hydrates
 * the composite workspace card from this provider, and persists the view's layout through
 * the personalization repository like any other standard view.
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

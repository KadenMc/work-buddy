import { CoworkViewChrome, type CoworkProviderState } from "./chrome/CoworkViewChrome";
import type {
  StandardViewRuntimeConfiguration,
  StandardViewRuntimeContext,
} from "../../dashboard/contributions/viewModules";
import { LocalStoragePersonalizationRepository } from "../../dashboard/personalization/repository";
import { InMemoryCoworkProvider } from "./providers/InMemoryCoworkProvider";

export const hostContractVersion = 1 as const;

/**
 * Co-work contributes the coarse document-session provider, the personalization repository
 * for its standard-grid view, and the App-owned view chrome. Dashboard Core mounts the widget
 * grid, hydrates the composite workspace card from this provider, persists the view's layout
 * through the personalization repository like any other standard view, and renders the chrome
 * above the toolbar. Supplying renderChrome also suppresses the raw provider-label text the
 * toolbar would otherwise show for Co-work.
 */
export function createRuntime(
  context: StandardViewRuntimeContext,
): StandardViewRuntimeConfiguration {
  // A store_id on the view URL scopes the session to a live, ledger-backed document. With no
  // store scope the workspace is the local scratch document the editor persists in the browser.
  // The chrome badge reflects that honest distinction and never says "demo data" (Ruling 1).
  const providerState: CoworkProviderState =
    new URLSearchParams(context.search).get("store_id") !== null ? "live" : "local";

  return {
    provider: new InMemoryCoworkProvider(),
    providerLabel: "Local Co-work document session",
    personalizationRepository: new LocalStoragePersonalizationRepository(
      context.storage,
    ),
    renderChrome: (_snapshot, slots) => (
      <CoworkViewChrome
        providerState={providerState}
        hostActions={slots.contextualActions}
      />
    ),
  };
}

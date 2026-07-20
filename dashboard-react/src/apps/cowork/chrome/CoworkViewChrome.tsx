import { Broadcast } from "@phosphor-icons/react/Broadcast";
import { HardDrives } from "@phosphor-icons/react/HardDrives";
import { NotePencil } from "@phosphor-icons/react/NotePencil";
import type { ReactNode } from "react";

import "./styles.css";

/**
 * Whether the view is bound to a live, ledger-backed document (a store_id scopes the
 * session) or to the local scratch document the editor persists in the browser. The badge
 * names the two honestly and never claims fabricated demo data (Ruling 1).
 */
export type CoworkProviderState = "live" | "local";

export interface CoworkViewChromeProps {
  readonly providerState: CoworkProviderState;
  /** Placement slot for Dashboard-host-owned contextual controls. */
  readonly hostActions?: ReactNode;
}

const PROVIDER_STATE: Record<
  CoworkProviderState,
  { readonly label: string; readonly hint: string }
> = {
  live: {
    label: "Live",
    hint: "This document is store-scoped and ledger-backed, so edits sync to the server.",
  },
  local: {
    label: "Local",
    hint: "This is a local scratch document the editor keeps in your browser across reloads.",
  },
};

/**
 * The Co-work view chrome, the App-owned header Dashboard Core renders above the view
 * toolbar through the same seam Journal uses. It carries the view identity and a
 * provider-state badge that reads live or local. The live document identity, drift, and
 * open-proposal count stay on the in-card health strip, which reads them from the
 * widget-local session the view-level chrome cannot observe, so the two never show the same
 * fact twice. Supplying this chrome also suppresses the raw provider-label text the toolbar
 * would otherwise render for Co-work.
 */
export function CoworkViewChrome({ providerState, hostActions }: CoworkViewChromeProps) {
  const state = PROVIDER_STATE[providerState];
  return (
    <header className="cowork-view-chrome" aria-labelledby="cowork-view-title">
      <div className="cowork-view-chrome__main">
        <div className="cowork-view-chrome__identity">
          <div className="cowork-view-chrome__mark" aria-hidden="true">
            <NotePencil weight="duotone" />
          </div>
          <div className="cowork-view-chrome__copy">
            <div className="cowork-view-chrome__title-row">
              <h1 id="cowork-view-title">Co-work</h1>
            </div>
            <p className="cowork-view-chrome__primary-job">
              Co-author a document with its tracked AI proposals and review rail.
            </p>
          </div>
        </div>

        <div className="cowork-view-chrome__actions">
          <span
            className="cowork-view-chrome__provider"
            role="status"
            data-state={providerState}
            title={state.hint}
          >
            {providerState === "live" ? (
              <Broadcast weight="duotone" aria-hidden="true" />
            ) : (
              <HardDrives weight="duotone" aria-hidden="true" />
            )}
            {state.label}
          </span>
          {hostActions}
        </div>
      </div>
    </header>
  );
}

export default CoworkViewChrome;

import { NotePencil } from "@phosphor-icons/react/NotePencil";
import type { ReactNode } from "react";

import "./styles.css";

export interface CoworkViewChromeProps {
  /** Placement slot for Dashboard-host-owned contextual controls. */
  readonly hostActions?: ReactNode;
}

/**
 * The Co-work view chrome, the App-owned header Dashboard Core renders above the view
 * toolbar through the same seam Journal uses. Persistence and file state stay in the
 * document bar, where they can be expressed as one concrete status instead of a second
 * live/local badge system.
 */
export function CoworkViewChrome({ hostActions }: CoworkViewChromeProps) {
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
          </div>
        </div>

        <div className="cowork-view-chrome__actions">
          {hostActions}
        </div>
      </div>
    </header>
  );
}

export default CoworkViewChrome;

import { useState } from "react";

import type { SingleSurfaceRuntimeProps } from "../../../dashboard/contributions/viewModules";
import { useViewSession } from "../../../dashboard/views/useViewSession";
import type { CoworkViewModel } from "../contracts";
import { CoworkEditorPane } from "../editor/CoworkEditorPane";
import "./styles.css";

type RailTab = "review" | "chat";

const DRIFT_LABEL: Record<string, string> = {
  clean: "In sync",
  drifted: "Drifted from file",
  missing: "File missing",
};

/**
 * Health strip region (`wb.widget-role.cowork-health-strip@1`). Read-only chrome:
 * document name, drift state, and open-proposal count. Drift is encoded with a text
 * label as well as a data attribute, so its meaning survives forced-colors (SP-6 G3).
 */
function CoworkHealthStrip({ model }: { model: CoworkViewModel | null }) {
  const document = model?.document ?? null;
  return (
    <header className="wb-cowork__health" aria-label="Document health">
      <span className="wb-cowork__health-title">
        {document?.title ?? "No document open"}
      </span>
      {document !== null ? (
        <span className="wb-cowork__health-facts">
          <span
            className="wb-cowork__drift"
            data-drift={document.driftState}
          >
            {DRIFT_LABEL[document.driftState] ?? document.driftState}
          </span>
          <span className="wb-cowork__count">
            {document.openProposalCount} open proposal
            {document.openProposalCount === 1 ? "" : "s"}
          </span>
        </span>
      ) : null}
    </header>
  );
}

/**
 * Placeholder Review rail content. The real variant-A-hybrid rail (aligned-stream cards,
 * filter lens, queue mode, mark bar) is wave-2 territory and lives under
 * `apps/cowork/suggestions/`.
 */
function ReviewRailStub({ hidden }: { hidden: boolean }) {
  return (
    <div
      className="wb-cowork__rail-body"
      role="tabpanel"
      id="wb-cowork-rail-review"
      aria-labelledby="wb-cowork-tab-review"
      hidden={hidden}
    >
      <p className="wb-cowork__rail-placeholder">
        Aligned proposal review appears here. The review rail is delivered in a later
        wave.
      </p>
    </div>
  );
}

/**
 * Placeholder Chat rail content. In a later wave this mounts the house
 * `conversation_chat` renderer in pane mode (one conversation per document), rather than
 * a fourth widget or the floating chat sidebar.
 */
function ChatRailStub({ hidden }: { hidden: boolean }) {
  return (
    <div
      className="wb-cowork__rail-body"
      role="tabpanel"
      id="wb-cowork-rail-chat"
      aria-labelledby="wb-cowork-tab-chat"
      hidden={hidden}
    >
      <p className="wb-cowork__rail-placeholder">
        The document conversation appears here. Chat reuses the house conversation
        machinery in a later wave.
      </p>
    </div>
  );
}

/**
 * The App-owned Co-work surface renderer (section 5, variant-A-hybrid skeleton). It
 * composes the three regions inside ONE React tree that shares the coarse document
 * session: the header health strip on top, the editor pane center-left, and the
 * Review / Chat tabbed rail on the right. The coarse session flows through the
 * ViewProvider snapshot, and the live Y.Doc and the sitting take the direct route and are
 * owned by the editor pane and the wave-2 rail.
 */
export function CoworkWorkspaceSurface({
  definition,
  provider,
}: SingleSurfaceRuntimeProps) {
  const session = useViewSession({ provider, viewId: definition.viewId });
  const [tab, setTab] = useState<RailTab>("review");
  const model = (session.snapshot?.model as CoworkViewModel | undefined) ?? null;

  return (
    <main className="wb-cowork" aria-label={definition.displayName}>
      <CoworkHealthStrip model={model} />
      <div className="wb-cowork__body">
        <div className="wb-cowork__editor-region">
          <CoworkEditorPane />
        </div>
        <aside className="wb-cowork__rail" aria-label="Review and chat">
          <div className="wb-cowork__rail-tabs" role="tablist" aria-label="Rail">
            <button
              type="button"
              role="tab"
              id="wb-cowork-tab-review"
              aria-selected={tab === "review"}
              aria-controls="wb-cowork-rail-review"
              className="wb-cowork__rail-tab"
              onClick={() => setTab("review")}
            >
              Review
            </button>
            <button
              type="button"
              role="tab"
              id="wb-cowork-tab-chat"
              aria-selected={tab === "chat"}
              aria-controls="wb-cowork-rail-chat"
              className="wb-cowork__rail-tab"
              onClick={() => setTab("chat")}
            >
              Chat
            </button>
          </div>
          <ReviewRailStub hidden={tab !== "review"} />
          <ChatRailStub hidden={tab !== "chat"} />
        </aside>
      </div>
    </main>
  );
}

export default CoworkWorkspaceSurface;

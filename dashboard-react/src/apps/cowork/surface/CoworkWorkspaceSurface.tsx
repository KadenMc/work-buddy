import { useMemo } from "react";

import type { SingleSurfaceRuntimeProps } from "../../../dashboard/contributions/viewModules";
import { useViewSession } from "../../../dashboard/views/useViewSession";
import type { CoworkViewModel } from "../contracts";
import { CoworkEditorPane } from "../editor/CoworkEditorPane";
import {
  CoworkRail,
  InMemoryReviewProvider,
  createDemoChatProvider,
} from "../rail";
import "./styles.css";

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
 * The App-owned Co-work surface renderer (section 5, variant-A-hybrid). It composes the
 * three regions inside ONE React tree that shares the coarse document session: the header
 * health strip on top, the editor pane center-left, and the Review / Chat tabbed rail on
 * the right. The coarse session flows through the ViewProvider snapshot, and the live
 * Y.Doc and the sitting take the direct route and are owned by the editor pane and the
 * rail. The rail is fed the in-memory review and demo chat providers until the live R2 and
 * conversation transports are wired behind those same seams.
 */
export function CoworkWorkspaceSurface({
  definition,
  provider,
}: SingleSurfaceRuntimeProps) {
  const session = useViewSession({ provider, viewId: definition.viewId });
  const model = (session.snapshot?.model as CoworkViewModel | undefined) ?? null;
  const documentId = model?.document?.documentId ?? "demo-doc";
  const conversationId = `cowork-doc-${documentId}`;

  const reviewProvider = useMemo(() => new InMemoryReviewProvider(), []);
  const chatProvider = useMemo(
    () => createDemoChatProvider(conversationId),
    [conversationId],
  );

  return (
    <main className="wb-cowork" aria-label={definition.displayName}>
      <CoworkHealthStrip model={model} />
      <div className="wb-cowork__body">
        <div className="wb-cowork__editor-region">
          <CoworkEditorPane />
        </div>
        <aside className="wb-cowork__rail" aria-label="Review and chat">
          <CoworkRail
            documentId={documentId}
            reviewProvider={reviewProvider}
            chatProvider={chatProvider}
            conversationId={conversationId}
          />
        </aside>
      </div>
    </main>
  );
}

export default CoworkWorkspaceSurface;

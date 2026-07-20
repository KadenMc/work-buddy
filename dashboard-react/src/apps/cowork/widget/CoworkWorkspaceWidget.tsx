import { useMemo } from "react";

import type { WidgetRendererProps } from "../../../dashboard/contributions/contracts";
import type { CoworkViewModel, CoworkWorkspaceInput } from "../contracts";
import {
  CoworkDemoWorkspace,
  CoworkEmptyWorkspace,
  CoworkLiveWorkspace,
  healthFromModel,
  resolveFixtureMode,
} from "../surface/CoworkWorkspaceSurface";

/**
 * The composite Co-work workspace renderer: the lazy default export the durable WidgetHost
 * mounts once and keeps alive across every grid remount, customize toggle, and
 * interaction-recovery remount of the grid below it. It composes the editor, the review
 * rail, and the health strip inside one live React tree that shares the document session.
 *
 * The coarse session arrives as hydrated input, so which document is open and the session
 * quality both come from `input`. Reading `window.location.search` directly for `store_id`
 * and `cowork_fixture` is sanctioned by the app-owned durable exemption: like a
 * single-surface renderer, a durable widget owns the live local state below its frame and
 * may read its own URL, listen to its own streams, and call its own routes directly, so
 * the Widget Renderer Contract's URL, SSE, and direct-call exclusions do not apply to it.
 * It never calls useViewSession. The live Y.Doc and the staged sitting take the direct
 * route to `/api/truth/doc/*` from inside the live workspace.
 */
export default function CoworkWorkspaceWidget({
  input,
}: WidgetRendererProps<CoworkWorkspaceInput>) {
  const model: CoworkViewModel = { document: input.document };
  const documentId = input.document?.documentId;

  const search = typeof window === "undefined" ? "" : window.location.search;
  const { storeId, override } = useMemo(() => {
    const params = new URLSearchParams(search);
    return {
      storeId: params.get("store_id") ?? undefined,
      override: params.get("cowork_fixture"),
    };
  }, [search]);

  const mode = resolveFixtureMode(input.sessionQuality, documentId, storeId, override);

  if (mode === "live" && documentId !== undefined && storeId !== undefined) {
    return (
      <CoworkLiveWorkspace
        documentId={documentId}
        storeId={storeId}
        fallbackHealth={healthFromModel(model)}
      />
    );
  }

  if (mode === "demo") {
    return <CoworkDemoWorkspace model={model} />;
  }

  return <CoworkEmptyWorkspace />;
}

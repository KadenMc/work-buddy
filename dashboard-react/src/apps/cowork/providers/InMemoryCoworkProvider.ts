import type {
  DashboardIntent,
  IntentResult,
  ReconcileResult,
  ViewSnapshot,
  WidgetLoadRequest,
  WidgetSnapshot,
  WidgetTypeId,
} from "../../../dashboard/contributions/contracts";
import type { ViewProvider } from "../../../dashboard/providers/ViewProvider";
import { COWORK_APP_ID, COWORK_VIEW_ID } from "../bindings";
import type { CoworkViewModel, CoworkWorkspaceInput } from "../contracts";

const DEMO_MODEL: CoworkViewModel = {
  document: {
    documentId: "demo-doc",
    path: "docs/demo/co-work-demo.md",
    title: "Co-work demo document",
    profile: "co_authored",
    driftState: "clean",
    openProposalCount: 0,
    openFlagCount: 0,
  },
};

/**
 * A deterministic in-memory coarse provider for the Co-work view (section 5.2). It
 * delivers only the JSON-compatible document session (which document is open, its
 * path / title / profile, drift, and open-proposal counts). It never carries the Yjs
 * binary or the sitting, which take the direct route to `/api/truth/doc/*`. The composite
 * workspace card hydrates from `loadWidget`. The live binary state stays widget-local.
 */
export class InMemoryCoworkProvider implements ViewProvider {
  readonly appId = COWORK_APP_ID;
  readonly #model: CoworkViewModel;

  constructor(model: CoworkViewModel = DEMO_MODEL) {
    this.#model = model;
  }

  async loadView(): Promise<ViewSnapshot<CoworkViewModel>> {
    return {
      viewId: COWORK_VIEW_ID,
      revision: 1,
      observedAt: new Date(0).toISOString(),
      status: "ready",
      quality: { kind: "demo", message: "In-memory Co-work document session." },
      model: this.#model,
      bindings: {},
      widgetInputs: {},
    };
  }

  async loadWidget(
    widgetTypeId: WidgetTypeId,
    request: WidgetLoadRequest,
  ): Promise<WidgetSnapshot<CoworkWorkspaceInput>> {
    // The composite workspace card is the one durable widget this view places. Its input
    // is the coarse document session plus the demo session quality it resolves its mode
    // from. The live Y.Doc and the sitting take the direct route, never this snapshot.
    const input: CoworkWorkspaceInput = {
      document: this.#model.document,
      sessionQuality: "demo",
    };
    return {
      widgetTypeId,
      instanceId: request.instanceId,
      revision: request.knownRevision ?? 1,
      observedAt: new Date(0).toISOString(),
      status: "ready",
      quality: { kind: "demo", message: "In-memory Co-work document session." },
      input,
    };
  }

  async dispatch(intent: DashboardIntent): Promise<IntentResult> {
    // Coarse document-session intents (open / close / register / reimport / materialize)
    // are accepted at this seam, and the live editor talks to the routes directly.
    return { intent_id: intent.intent_id, status: "accepted" };
  }

  async reconcile(): Promise<ReconcileResult> {
    return { changed: false };
  }
}

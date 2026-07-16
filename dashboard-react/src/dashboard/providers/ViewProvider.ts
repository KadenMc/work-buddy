import type {
  AppId,
  AppInvalidation,
  DashboardIntent,
  IntentResult,
  ReconcileResult,
  ViewId,
  ViewLoadRequest,
  ViewSnapshot,
  WidgetLoadRequest,
  WidgetSnapshot,
  WidgetTypeId,
} from "../contributions/contracts";

/**
 * The Dashboard View API implementation boundary.
 *
 * Providers translate App-owned data and behavior into validated UI snapshots and
 * intents. Renderers never receive a provider, transport URL, or EventSource.
 */
export interface ViewProvider {
  readonly appId: AppId;

  /** Optional in-process invalidations for deterministic/local providers. */
  subscribeInvalidations?(listener: (invalidation: AppInvalidation) => void): () => void;

  /**
   * Widget types this provider can hydrate as new personal instances in a view.
   *
   * Absence means no catalog additions are supported. The dashboard never infers
   * add support merely because a renderer is installed in the contribution
   * registry; the data/intent provider must opt in at this boundary.
   */
  getAddableWidgetTypeIds?(viewId: ViewId): readonly WidgetTypeId[];

  loadView(viewId: ViewId, request: ViewLoadRequest): Promise<ViewSnapshot>;

  loadWidget(
    widgetTypeId: WidgetTypeId,
    request: WidgetLoadRequest,
  ): Promise<WidgetSnapshot>;

  dispatch(intent: DashboardIntent): Promise<IntentResult>;

  reconcile(invalidation: AppInvalidation): Promise<ReconcileResult>;
}

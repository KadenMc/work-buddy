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

  loadView(viewId: ViewId, request: ViewLoadRequest): Promise<ViewSnapshot>;

  loadWidget(
    widgetTypeId: WidgetTypeId,
    request: WidgetLoadRequest,
  ): Promise<WidgetSnapshot>;

  dispatch(intent: DashboardIntent): Promise<IntentResult>;

  reconcile(invalidation: AppInvalidation): Promise<ReconcileResult>;
}


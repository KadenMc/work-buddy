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
import type { ViewProvider } from "./ViewProvider";

export type FixtureIntentHandler = (
  intent: DashboardIntent,
) => IntentResult | Promise<IntentResult>;
export type FixtureReconcileHandler = (
  invalidation: AppInvalidation,
) => ReconcileResult | Promise<ReconcileResult>;

export interface FixtureViewProviderOptions {
  readonly appId: AppId;
  readonly viewSnapshots: readonly ViewSnapshot[];
  readonly widgetSnapshots?: readonly WidgetSnapshot[];
  readonly intentHandlers?: Readonly<Record<string, FixtureIntentHandler>>;
  readonly reconcile?: FixtureReconcileHandler;
}

export class FixtureNotFoundError extends Error {
  constructor(kind: "view" | "widget", id: string) {
    super(`Unknown fixture ${kind}: ${id}`);
    this.name = "FixtureNotFoundError";
  }
}

const widgetKey = (widgetTypeId: WidgetTypeId, instanceId: string): string =>
  `${widgetTypeId}\u0000${instanceId}`;

const cloneFixture = <Value,>(value: Value): Value => structuredClone(value);

/**
 * Deterministic Dashboard View API implementation for unit, visual, and Widget Lab
 * states. It is immutable unless a test explicitly supplies intent/reconcile handlers.
 */
export class FixtureViewProvider implements ViewProvider {
  readonly appId: AppId;
  readonly #views = new Map<ViewId, ViewSnapshot>();
  readonly #widgets = new Map<string, WidgetSnapshot>();
  readonly #intentHandlers: Readonly<Record<string, FixtureIntentHandler>>;
  readonly #reconcile?: FixtureReconcileHandler;

  constructor(options: FixtureViewProviderOptions) {
    this.appId = options.appId;
    this.#intentHandlers = options.intentHandlers ?? {};
    this.#reconcile = options.reconcile;
    options.viewSnapshots.forEach((snapshot) => {
      if (this.#views.has(snapshot.viewId)) {
        throw new Error(`Duplicate fixture view snapshot: ${snapshot.viewId}`);
      }
      this.#views.set(snapshot.viewId, cloneFixture(snapshot));
    });
    options.widgetSnapshots?.forEach((snapshot) => {
      const key = widgetKey(snapshot.widgetTypeId, snapshot.instanceId);
      if (this.#widgets.has(key)) {
        throw new Error(`Duplicate fixture widget snapshot: ${snapshot.instanceId}`);
      }
      this.#widgets.set(key, cloneFixture(snapshot));
    });
  }

  async loadView(viewId: ViewId, request: ViewLoadRequest): Promise<ViewSnapshot> {
    void request;
    const snapshot = this.#views.get(viewId);
    if (snapshot === undefined) {
      throw new FixtureNotFoundError("view", viewId);
    }
    return cloneFixture(snapshot);
  }

  async loadWidget(
    widgetTypeId: WidgetTypeId,
    request: WidgetLoadRequest,
  ): Promise<WidgetSnapshot> {
    const snapshot = this.#widgets.get(widgetKey(widgetTypeId, request.instanceId));
    if (snapshot === undefined) {
      throw new FixtureNotFoundError("widget", request.instanceId);
    }
    return cloneFixture(snapshot);
  }

  async dispatch(intent: DashboardIntent): Promise<IntentResult> {
    const handler = this.#intentHandlers[intent.intent_type];
    if (handler === undefined) {
      return {
        intent_id: intent.intent_id,
        ...(intent.client_mutation_id === undefined
          ? {}
          : { client_mutation_id: intent.client_mutation_id }),
        status: "unavailable",
        message: `Fixture provider has no handler for ${intent.intent_type}`,
      };
    }
    const result = await handler(intent);
    return {
      ...result,
      intent_id: intent.intent_id,
      ...(intent.client_mutation_id === undefined
        ? {}
        : { client_mutation_id: intent.client_mutation_id }),
    };
  }

  async reconcile(invalidation: AppInvalidation): Promise<ReconcileResult> {
    if (invalidation.appId !== this.appId) {
      return { changed: false };
    }
    if (this.#reconcile !== undefined) {
      return cloneFixture(await this.#reconcile(invalidation));
    }

    const viewId = invalidation.viewIds?.[0];
    const snapshot =
      viewId === undefined ? this.#views.values().next().value : this.#views.get(viewId);
    return snapshot === undefined
      ? { changed: false }
      : { changed: false, revision: snapshot.revision };
  }
}

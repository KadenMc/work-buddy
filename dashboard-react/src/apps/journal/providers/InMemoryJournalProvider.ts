import type {
  AppInvalidation,
  DashboardIntent,
  IntentResult,
  ReconcileResult,
  SnapshotStatus,
  ViewId,
  ViewLoadRequest,
  ViewSnapshot,
  WidgetLoadRequest,
  WidgetSnapshot,
  WidgetTypeId,
} from "../../../dashboard/contributions/contracts";
import type { ViewProvider } from "../../../dashboard/providers/ViewProvider";
import {
  JOURNAL_APP_ID,
  JOURNAL_INSTANCE_IDS,
  JOURNAL_VIEW_DEFINITION_ID,
  JOURNAL_WIDGET_TYPE_BY_INSTANCE,
  createJournalViewBindings,
  type JournalBindingValue,
  type JournalWidgetInput,
} from "../bindings";
import {
  JOURNAL_WIDGET_INSTANCE_IDS,
  type IsoDateTime,
  type JournalFixtureState,
  type JournalViewModel,
} from "../contracts";
import {
  JULY11_DUMB_CAPTURE_INTENT,
  JULY11_DUMB_PERSISTED_FIXTURE,
  JULY11_READY_FIXTURE,
  JULY11_REVISED_TIMELINE_ITEMS,
  JULY11_SMART_CAPTURE_INTENT,
  JULY11_SMART_PENDING_FIXTURE,
  JULY11_SMART_SETTLED_FIXTURE,
} from "../fixtures/july11";

export type PopulatedJournalFixtureState = Extract<
  JournalFixtureState,
  { readonly model: JournalViewModel }
>;

export type JournalProviderViewSnapshot = ViewSnapshot<
  JournalViewModel,
  JournalBindingValue,
  JournalWidgetInput
>;

export type JournalProviderWidgetSnapshot = WidgetSnapshot<JournalWidgetInput | null>;

function snapshotStatus(fixture: PopulatedJournalFixtureState): SnapshotStatus {
  if (fixture.loadStatus === "stale" || fixture.loadStatus === "offline") {
    return fixture.loadStatus;
  }
  return fixture.model.access.mode === "read_only" ? "read-only" : "ready";
}

function asInMemoryFixture(
  fixture: PopulatedJournalFixtureState,
): PopulatedJournalFixtureState {
  return {
    ...fixture,
    model: {
      ...fixture.model,
      source: {
        kind: "in_memory",
        fixtureId: fixture.fixtureId,
        label: "Demo data",
      },
    },
  };
}

function toViewSnapshot(
  fixture: PopulatedJournalFixtureState,
): JournalProviderViewSnapshot {
  const widgetInputs: Readonly<Record<string, JournalWidgetInput>> = {
    [JOURNAL_INSTANCE_IDS.capture]:
      fixture.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture],
    [JOURNAL_INSTANCE_IDS.timeline]:
      fixture.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline],
    [JOURNAL_INSTANCE_IDS.runningNotes]:
      fixture.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes],
  };
  return {
    viewId: JOURNAL_VIEW_DEFINITION_ID,
    revision: fixture.model.revision,
    observedAt: fixture.observedAt,
    status: snapshotStatus(fixture),
    quality: { kind: "demo", message: "Deterministic Journal demo data" },
    model: fixture.model,
    bindings: createJournalViewBindings(fixture.model),
    widgetInputs,
  };
}

function inputForInstance(
  model: JournalViewModel,
  instanceId: string,
): JournalWidgetInput | undefined {
  if (instanceId === JOURNAL_WIDGET_INSTANCE_IDS.capture) {
    return model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture];
  }
  if (instanceId === JOURNAL_WIDGET_INSTANCE_IDS.timeline) {
    return model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline];
  }
  if (instanceId === JOURNAL_WIDGET_INSTANCE_IDS.runningNotes) {
    return model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes];
  }
  return undefined;
}

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null;
}

function captureIntentMatches(
  intent: DashboardIntent,
  expected: typeof JULY11_SMART_CAPTURE_INTENT | typeof JULY11_DUMB_CAPTURE_INTENT,
): boolean {
  if (
    intent.intent_type !== expected.intent_type ||
    !("instance_id" in intent) ||
    intent.instance_id !== JOURNAL_INSTANCE_IDS.capture ||
    !isRecord(intent.payload)
  ) {
    return false;
  }
  return (
    intent.payload.day_id === expected.payload.day_id &&
    intent.payload.target_id === expected.payload.target_id &&
    intent.payload.mode === expected.payload.mode &&
    intent.payload.exact_text === expected.payload.exact_text &&
    (intent.payload.stated_at === undefined ||
      intent.payload.stated_at === expected.payload.stated_at)
  );
}

function bindCaptureMutationId(
  fixture: PopulatedJournalFixtureState,
  clientMutationId: string,
): PopulatedJournalFixtureState {
  const capture = fixture.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture];
  const submissions = capture.recentSubmissions;
  const last = submissions[submissions.length - 1];
  if (last === undefined) return fixture;
  return {
    ...fixture,
    model: {
      ...fixture.model,
      widgetInputs: {
        ...fixture.model.widgetInputs,
        [JOURNAL_WIDGET_INSTANCE_IDS.capture]: {
          ...capture,
          recentSubmissions: [
            ...submissions.slice(0, -1),
            { ...last, clientMutationId },
          ],
        },
      },
    },
  };
}

/**
 * Deterministic UI-first Journal provider.
 *
 * It coordinates Capture, Timeline, and Running Notes only by swapping an immutable
 * provider snapshot to a new revision. Renderers receive no sibling callbacks, timers,
 * data clients, or provider reference. Smart processing advances explicitly in tests or
 * through a matching synthetic invalidation; it never waits on wall-clock time.
 */
export class InMemoryJournalProvider implements ViewProvider {
  readonly appId = JOURNAL_APP_ID;

  #current: PopulatedJournalFixtureState;
  #pendingSettlement: PopulatedJournalFixtureState | null = null;
  #intentResults = new Map<string, IntentResult>();
  #revisionSequence = 0;

  constructor(initialFixture: PopulatedJournalFixtureState = JULY11_READY_FIXTURE) {
    this.#current = asInMemoryFixture(initialFixture);
  }

  async loadView(
    viewId: ViewId,
    _request: ViewLoadRequest,
  ): Promise<JournalProviderViewSnapshot> {
    if (viewId !== JOURNAL_VIEW_DEFINITION_ID) {
      throw new Error(`InMemoryJournalProvider cannot load view ${viewId}`);
    }
    return toViewSnapshot(this.#current);
  }

  async loadWidget(
    widgetTypeId: WidgetTypeId,
    request: WidgetLoadRequest,
  ): Promise<JournalProviderWidgetSnapshot> {
    if (request.viewId !== JOURNAL_VIEW_DEFINITION_ID) {
      throw new Error(`InMemoryJournalProvider cannot load widgets for ${request.viewId}`);
    }

    const expectedType = JOURNAL_WIDGET_TYPE_BY_INSTANCE.get(request.instanceId);
    const input = inputForInstance(this.#current.model, request.instanceId);
    if (expectedType === undefined || expectedType !== widgetTypeId || input === undefined) {
      return {
        widgetTypeId,
        instanceId: request.instanceId,
        revision: this.#current.model.revision,
        observedAt: this.#current.observedAt,
        status: "unavailable",
        quality: { kind: "demo", message: "Widget is not bound to this Journal slot" },
        input: null,
      };
    }

    return {
      widgetTypeId,
      instanceId: request.instanceId,
      revision: this.#current.model.revision,
      observedAt: this.#current.observedAt,
      status: snapshotStatus(this.#current),
      quality: { kind: "demo", message: "Deterministic Journal demo data" },
      input,
    };
  }

  async dispatch(intent: DashboardIntent): Promise<IntentResult> {
    if (intent.view_id !== JOURNAL_VIEW_DEFINITION_ID) {
      return this.#result(intent, "rejected", "Intent targets a different view");
    }
    if (this.#current.model.access.mode === "read_only") {
      return this.#result(intent, "unavailable", this.#current.model.access.reason);
    }

    const mutationId = intent.client_mutation_id;
    if (mutationId !== undefined) {
      const prior = this.#intentResults.get(mutationId);
      if (prior !== undefined) return prior;
    }

    if (captureIntentMatches(intent, JULY11_SMART_CAPTURE_INTENT)) {
      if (mutationId === undefined) {
        return this.#result(intent, "rejected", "Capture requires client_mutation_id");
      }
      if (this.#current.model.revision !== JULY11_READY_FIXTURE.model.revision) {
        return this.#result(intent, "conflict", "The demo Journal has already changed");
      }
      this.#current = asInMemoryFixture(
        bindCaptureMutationId(JULY11_SMART_PENDING_FIXTURE, mutationId),
      );
      this.#pendingSettlement = asInMemoryFixture(
        bindCaptureMutationId(JULY11_SMART_SETTLED_FIXTURE, mutationId),
      );
      return this.#remember(
        intent,
        this.#result(
          intent,
          "accepted",
          "Exact text persisted; smart processing is pending",
          this.#current.model.revision,
        ),
      );
    }

    if (captureIntentMatches(intent, JULY11_DUMB_CAPTURE_INTENT)) {
      if (mutationId === undefined) {
        return this.#result(intent, "rejected", "Capture requires client_mutation_id");
      }
      if (this.#current.model.revision !== JULY11_READY_FIXTURE.model.revision) {
        return this.#result(intent, "conflict", "The demo Journal has already changed");
      }
      this.#current = asInMemoryFixture(
        bindCaptureMutationId(JULY11_DUMB_PERSISTED_FIXTURE, mutationId),
      );
      this.#pendingSettlement = null;
      return this.#remember(
        intent,
        this.#result(
          intent,
          "accepted",
          "Exact text persisted with no per-entry processing",
          this.#current.model.revision,
        ),
      );
    }

    if (
      intent.intent_type === "wb.timeline.open-item" &&
      "instance_id" in intent &&
      intent.instance_id === JOURNAL_INSTANCE_IDS.timeline &&
      isRecord(intent.payload) &&
      typeof intent.payload.item_id === "string"
    ) {
      const itemId = intent.payload.item_id;
      const item = this.#current.model.widgetInputs[
        JOURNAL_WIDGET_INSTANCE_IDS.timeline
      ].items.find((candidate) => candidate.itemId === itemId);
      return item === undefined
        ? this.#result(intent, "rejected", "Timeline item is not present in this revision")
        : this.#result(intent, "accepted", `Open ${item.title}`);
    }

    if (
      intent.intent_type === "wb.timeline.render-mode-changed" &&
      "instance_id" in intent &&
      intent.instance_id === JOURNAL_INSTANCE_IDS.timeline &&
      isRecord(intent.payload) &&
      (intent.payload.render_mode === "timeline" || intent.payload.render_mode === "list")
    ) {
      const renderMode = intent.payload.render_mode;
      const revision = this.#commit("timeline-mode", (model) => ({
        ...model,
        widgetInputs: {
          ...model.widgetInputs,
          [JOURNAL_WIDGET_INSTANCE_IDS.timeline]: {
            ...model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline],
            renderMode,
          },
        },
      }));
      return this.#result(intent, "accepted", `Timeline mode changed to ${renderMode}`, revision);
    }

    if (
      intent.intent_type === "wb.timeline.replan-requested" &&
      "instance_id" in intent &&
      intent.instance_id === JOURNAL_INSTANCE_IDS.timeline &&
      isRecord(intent.payload) &&
      intent.payload.day_id === this.#current.model.day.dayId &&
      typeof intent.payload.preserve_before === "string"
    ) {
      const revision = this.#commit("timeline-replan", (model) => ({
        ...model,
        widgetInputs: {
          ...model.widgetInputs,
          [JOURNAL_WIDGET_INSTANCE_IDS.timeline]: {
            ...model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline],
            items: JULY11_REVISED_TIMELINE_ITEMS,
          },
        },
      }));
      return this.#result(
        intent,
        "accepted",
        `Replanned only editable items after ${intent.payload.preserve_before}`,
        revision,
      );
    }

    if (
      intent.intent_type === "wb.notes.edit-requested" &&
      "instance_id" in intent &&
      intent.instance_id === JOURNAL_INSTANCE_IDS.runningNotes &&
      isRecord(intent.payload) &&
      typeof intent.payload.item_id === "string" &&
      typeof intent.payload.expected_version === "number" &&
      typeof intent.payload.markdown === "string"
    ) {
      const itemId = intent.payload.item_id;
      const expectedVersion = intent.payload.expected_version;
      const markdown = intent.payload.markdown;
      const items = this.#current.model.widgetInputs[
        JOURNAL_WIDGET_INSTANCE_IDS.runningNotes
      ].items;
      const existing = items.find((item) => item.itemId === itemId);
      if (existing === undefined) {
        return this.#result(intent, "rejected", "Running note is not present");
      }
      if (existing.version !== expectedVersion) {
        return this.#result(intent, "conflict", "Running note version changed");
      }
      const revision = this.#commit("notes-edit", (model) => ({
        ...model,
        widgetInputs: {
          ...model.widgetInputs,
          [JOURNAL_WIDGET_INSTANCE_IDS.runningNotes]: {
            ...model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes],
            items: model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes].items.map(
              (item) =>
                item.itemId === itemId
                  ? {
                      ...item,
                      markdown,
                      updatedAt: model.day.now,
                      version: item.version + 1,
                    }
                  : item,
            ),
          },
        },
      }));
      return this.#result(intent, "accepted", "Running note updated", revision);
    }

    if (
      intent.intent_type === "wb.notes.open-thread-requested" &&
      "instance_id" in intent &&
      intent.instance_id === JOURNAL_INSTANCE_IDS.runningNotes &&
      isRecord(intent.payload) &&
      typeof intent.payload.item_id === "string" &&
      typeof intent.payload.thread_id === "string"
    ) {
      const itemId = intent.payload.item_id;
      const threadId = intent.payload.thread_id;
      const item = this.#current.model.widgetInputs[
        JOURNAL_WIDGET_INSTANCE_IDS.runningNotes
      ].items.find((candidate) => candidate.itemId === itemId);
      return item === undefined
        ? this.#result(intent, "rejected", "Running note is not present")
        : this.#result(intent, "accepted", `Open thread ${threadId}`);
    }

    return this.#result(
      intent,
      "rejected",
      "This deterministic provider accepts only the named July 11 capture scenarios",
    );
  }

  async reconcile(invalidation: AppInvalidation): Promise<ReconcileResult> {
    const viewMatches =
      invalidation.appId === JOURNAL_APP_ID &&
      (invalidation.viewIds === undefined ||
        invalidation.viewIds.includes(JOURNAL_VIEW_DEFINITION_ID));
    if (!viewMatches) {
      return { changed: false, revision: this.#current.model.revision };
    }

    if (
      this.#pendingSettlement !== null &&
      invalidation.revision === this.#pendingSettlement.model.revision
    ) {
      this.#current = this.#pendingSettlement;
      this.#pendingSettlement = null;
      return {
        changed: true,
        revision: this.#current.model.revision,
        snapshot: toViewSnapshot(this.#current),
      };
    }

    // A matching reconcile call is also the queryable-truth recovery path used after
    // dispatch, reconnect, and foreground return. The caller may hold an older snapshot
    // even when the provider has already advanced to `#current`, so return the current
    // authoritative snapshot instead of relying on the lossy event transport.
    return {
      changed: true,
      revision: this.#current.model.revision,
      snapshot: toViewSnapshot(this.#current),
    };
  }

  /** Advances the deterministic async phase without scheduling a timer. */
  advanceDemoProcessing(): boolean {
    if (this.#pendingSettlement === null) return false;
    this.#current = this.#pendingSettlement;
    this.#pendingSettlement = null;
    return true;
  }

  /** Advances only the provider's injected clock; never reads the wall clock. */
  advanceClock(now: IsoDateTime): string {
    if (!Number.isFinite(Date.parse(now))) {
      throw new Error(`Invalid Journal clock value: ${now}`);
    }
    return this.#commit("clock", (model) => {
      const day = { ...model.day, now };
      return {
        ...model,
        day,
        quality: { ...model.quality, observedAt: now },
        widgetInputs: {
          ...model.widgetInputs,
          [JOURNAL_WIDGET_INSTANCE_IDS.timeline]: {
            ...model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline],
            day,
          },
        },
      };
    });
  }

  get revision(): string {
    return this.#current.model.revision;
  }

  #remember(intent: DashboardIntent, result: IntentResult): IntentResult {
    if (intent.client_mutation_id !== undefined) {
      this.#intentResults.set(intent.client_mutation_id, result);
    }
    return result;
  }

  #commit(
    label: string,
    update: (model: JournalViewModel) => JournalViewModel,
  ): string {
    this.#revisionSequence += 1;
    const candidate = update(this.#current.model);
    const revision = `${this.#current.model.revision}:${label}:${this.#revisionSequence}`;
    const capture = candidate.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture];
    const timeline = candidate.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline];
    const runningNotes = candidate.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes];
    const model: JournalViewModel = {
      ...candidate,
      revision,
      widgetInputs: {
        [JOURNAL_WIDGET_INSTANCE_IDS.capture]: { ...capture, revision },
        [JOURNAL_WIDGET_INSTANCE_IDS.timeline]: { ...timeline, revision },
        [JOURNAL_WIDGET_INSTANCE_IDS.runningNotes]: { ...runningNotes, revision },
      },
    };
    this.#current = {
      ...this.#current,
      fixtureId: `${this.#current.fixtureId}:${label}:${this.#revisionSequence}`,
      observedAt: model.day.now,
      model,
    };
    return revision;
  }

  #result(
    intent: DashboardIntent,
    status: IntentResult["status"],
    message: string,
    revision: string = this.#current.model.revision,
  ): IntentResult {
    return {
      intent_id: intent.intent_id,
      client_mutation_id: intent.client_mutation_id,
      status,
      revision,
      message,
    };
  }
}

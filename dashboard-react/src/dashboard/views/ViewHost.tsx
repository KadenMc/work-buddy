import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { useDashboardAnnouncer } from "../accessibility/DashboardAnnouncer";
import type {
  ViewDefinition,
  ViewSnapshot,
  WidgetDefinition,
  WidgetInstanceId,
  WidgetIntent,
  WidgetSnapshot,
} from "../contributions/contracts";
import { asWidgetInstanceId } from "../contributions/contracts";
import type { ContributionRegistry } from "../contributions/registry";
import type { RegisteredWidget } from "../contributions/registry";
import { ReactGridLayoutAdapter } from "../layout/ReactGridLayoutAdapter";
import type { DashboardLayout, LayoutCommand } from "../layout/contracts";
import type { ViewProvider } from "../providers/ViewProvider";
import { assertWidgetSnapshot } from "../providers/validateProviderBoundary";
import type { PersonalizationRepository } from "../personalization/repository";
import {
  beginViewEditSession,
  createPersonalizationPatch,
  resolveViewPersonalization,
  viewEditSessionReducer,
} from "../personalization/reducer";
import type {
  EffectiveWidgetInstance,
  ViewEditAction,
  ViewEditSessionState,
  ViewPersonalizationPatch,
} from "../personalization/contracts";
import { WidgetFrame } from "../widgets/WidgetFrame";
import { WidgetCatalogDrawer } from "../widgets/WidgetCatalogDrawer";
import { WidgetHost } from "../widgets/WidgetHost";
import { WidgetState } from "../widgets/WidgetStates";
import {
  findCompatibleWidgetReplacements,
  planWidgetReplacement,
} from "../widgets/replaceWidget";
import { MobileOrderEditor } from "./MobileOrderEditor";
import { useViewSession } from "./useViewSession";

export interface ViewHostProps {
  readonly registry: ContributionRegistry;
  readonly definition: ViewDefinition;
  readonly provider: ViewProvider;
  readonly personalizationRepository: PersonalizationRepository;
  readonly renderChrome?: (snapshot: ViewSnapshot) => ReactNode;
  readonly providerLabel?: string;
}

const useMediaQuery = (query: string): boolean => {
  const read = () =>
    typeof window.matchMedia === "function" && window.matchMedia(query).matches;
  const [matches, setMatches] = useState(read);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);
  return matches;
};

const layoutFor = (state: ViewEditSessionState): DashboardLayout =>
  state.present.instances
    .filter((instance) => instance.visibility === "shown")
    .map((instance) => instance.layout);

const patchHasUserState = (patch: ViewPersonalizationPatch): boolean =>
  Object.keys(patch.defaultSlotOverrides).length > 0 ||
  patch.addedInstances.length > 0 ||
  patch.orphanedInstances.length > 0 ||
  patch.mobileOrderOverride !== null;

const sizeModeFor = (
  instance: EffectiveWidgetInstance,
  defaultWidth: number,
): "compact" | "standard" | "expanded" => {
  if (instance.layout.w < defaultWidth) return "compact";
  if (instance.layout.w >= Math.min(24, defaultWidth + 6)) return "expanded";
  return "standard";
};

export function ViewHost({
  registry,
  definition,
  provider,
  personalizationRepository,
  renderChrome,
  providerLabel,
}: ViewHostProps) {
  const session = useViewSession({ provider, viewId: definition.viewId });
  const { announce } = useDashboardAnnouncer();
  const isMobile = useMediaQuery("(max-width: 767px)");
  const definitions = useMemo(
    () =>
      new Map(
        registry
          .listWidgets()
          .map(({ definition: widget }) => [widget.typeId, widget] as const),
      ),
    [registry],
  );
  const defaults = useMemo(
    () => resolveViewPersonalization(definition, definitions),
    [definition, definitions],
  );
  const [storedPatch, setStoredPatch] = useState<ViewPersonalizationPatch | undefined>();
  const [personalizationLoaded, setPersonalizationLoaded] = useState(false);
  const [personalizationError, setPersonalizationError] = useState<string>();
  const resolved = useMemo(
    () => resolveViewPersonalization(definition, definitions, storedPatch),
    [definition, definitions, storedPatch],
  );
  const [editState, setEditState] = useState<ViewEditSessionState>(() =>
    beginViewEditSession(defaults),
  );
  const [customizing, setCustomizing] = useState(false);
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [mobileOrderOpen, setMobileOrderOpen] = useState(false);
  const [resetPatchRequested, setResetPatchRequested] = useState(false);
  const [widgetSnapshots, setWidgetSnapshots] = useState<
    ReadonlyMap<WidgetInstanceId, WidgetSnapshot>
  >(() => new Map());
  const [widgetSnapshotErrors, setWidgetSnapshotErrors] = useState<
    ReadonlyMap<WidgetInstanceId, string>
  >(() => new Map());
  const addableWidgetTypeIds =
    provider.getAddableWidgetTypeIds?.(definition.viewId) ?? [];
  const widgetHydrationKey = editState.present.instances
    .filter((instance) => instance.visibility === "shown")
    .map(
      (instance) =>
        `${instance.instanceId}\u0000${instance.widgetTypeId}\u0000${JSON.stringify(instance.bindings)}`,
    )
    .join("\u0001");

  useEffect(() => {
    let active = true;
    setPersonalizationLoaded(false);
    setPersonalizationError(undefined);
    void personalizationRepository
      .load(definition.viewId)
      .then((patch) => {
        if (!active) return;
        setStoredPatch(patch ?? undefined);
        setPersonalizationLoaded(true);
      })
      .catch((error: unknown) => {
        if (!active) return;
        setPersonalizationError(String(error));
        setStoredPatch(undefined);
        setPersonalizationLoaded(true);
      });
    return () => {
      active = false;
    };
  }, [definition.viewId, personalizationRepository]);

  useEffect(() => {
    if (!customizing) setEditState(beginViewEditSession(resolved));
  }, [customizing, resolved]);

  useEffect(() => {
    const viewSnapshot = session.snapshot;
    if (viewSnapshot === undefined) {
      setWidgetSnapshots(new Map());
      setWidgetSnapshotErrors(new Map());
      return;
    }

    let active = true;
    const instances = editState.present.instances.filter(
      (instance) => instance.visibility === "shown",
    );
    const visibleIds = new Set(instances.map((instance) => instance.instanceId));

    void Promise.all(
      instances.map(async (instance) => {
        try {
          const loaded = await provider.loadWidget(instance.widgetTypeId, {
            viewId: definition.viewId,
            instanceId: instance.instanceId,
            ...(viewSnapshot.revision === undefined
              ? {}
              : { knownRevision: viewSnapshot.revision }),
            bindings: instance.bindings,
          });
          assertWidgetSnapshot(
            loaded,
            instance.widgetTypeId,
            instance.instanceId,
            viewSnapshot.revision,
          );
          return {
            ok: true,
            instanceId: instance.instanceId,
            snapshot: loaded,
          } as const;
        } catch (error) {
          return {
            ok: false,
            instanceId: instance.instanceId,
            error: error instanceof Error ? error.message : String(error),
          } as const;
        }
      }),
    ).then((results) => {
      if (!active) return;
      setWidgetSnapshots((current) => {
        const next = new Map(
          [...current].filter(([instanceId]) => visibleIds.has(instanceId)),
        );
        results.forEach((result) => {
          if (result.ok) next.set(result.instanceId, result.snapshot);
        });
        return next;
      });
      setWidgetSnapshotErrors(() => {
        const next = new Map<WidgetInstanceId, string>();
        results.forEach((result) => {
          if (!result.ok) next.set(result.instanceId, result.error);
        });
        return next;
      });
    });

    return () => {
      active = false;
    };
  }, [
    definition.viewId,
    provider,
    session.snapshot,
    session.snapshot?.revision,
    widgetHydrationKey,
  ]);

  const act = useCallback((action: ViewEditAction) => {
    setEditState((current) => viewEditSessionReducer(current, action));
  }, []);

  const beginCustomize = () => {
    setEditState(beginViewEditSession(resolved));
    setCustomizing(true);
    setResetPatchRequested(false);
    announce("Customize view mode started");
  };

  const openCatalog = () => {
    if (!customizing) {
      setEditState(beginViewEditSession(resolved));
      setCustomizing(true);
      setResetPatchRequested(false);
      announce("Customize view mode started");
    }
    setCatalogOpen(true);
  };

  const cancelCustomize = () => {
    setEditState((current) => viewEditSessionReducer(current, { type: "cancel" }));
    setCustomizing(false);
    setCatalogOpen(false);
    setMobileOrderOpen(false);
    setResetPatchRequested(false);
    announce("View changes cancelled");
  };

  const saveCustomize = async () => {
    const atCurrentDefaults = JSON.stringify(editState.present) === JSON.stringify(defaults);
    const patch = createPersonalizationPatch(
      definition,
      definitions,
      editState.present,
      resetPatchRequested && atCurrentDefaults ? undefined : storedPatch,
    );
    try {
      if (!patchHasUserState(patch)) {
        await personalizationRepository.reset(definition.viewId);
        setStoredPatch(undefined);
      } else {
        await personalizationRepository.save(patch);
        setStoredPatch(patch);
      }
      setEditState(beginViewEditSession(editState.present));
      setCustomizing(false);
      setCatalogOpen(false);
      setMobileOrderOpen(false);
      setResetPatchRequested(false);
      setPersonalizationError(undefined);
      announce(patchHasUserState(patch) ? "View layout saved" : "View reset to App defaults");
    } catch (error) {
      setPersonalizationError(String(error));
      announce("View layout could not be saved", "assertive");
    }
  };

  const addWidget = async (widget: WidgetDefinition) => {
    if (!addableWidgetTypeIds.includes(widget.typeId)) {
      announce(
        `${widget.displayName} cannot be added because this view provider does not support it`,
        "assertive",
      );
      return;
    }
    const instanceId = asWidgetInstanceId(
      `personal:${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`,
    );
    let hydrated: WidgetSnapshot;
    try {
      hydrated = await provider.loadWidget(widget.typeId, {
        viewId: definition.viewId,
        instanceId,
        ...(session.snapshot?.revision === undefined
          ? {}
          : { knownRevision: session.snapshot.revision }),
        bindings: {},
      });
      assertWidgetSnapshot(
        hydrated,
        widget.typeId,
        instanceId,
        session.snapshot?.revision,
      );
    } catch (error) {
      announce(
        `${widget.displayName} could not be added: ${
          error instanceof Error ? error.message : String(error)
        }`,
        "assertive",
      );
      return;
    }
    if (
      hydrated.status === "unavailable" ||
      hydrated.status === "permission-denied" ||
      hydrated.status === "error"
    ) {
      announce(
        `${widget.displayName} could not be added: ${
          hydrated.quality.message ?? hydrated.status
        }`,
        "assertive",
      );
      return;
    }
    const size = widget.sizeContract.default;
    setWidgetSnapshots((current) => new Map(current).set(instanceId, hydrated));
    setWidgetSnapshotErrors((current) => {
      const next = new Map(current);
      next.delete(instanceId);
      return next;
    });
    act({
      type: "add",
      instance: {
        instanceId,
        widgetTypeId: widget.typeId,
        widgetDefinitionVersion: widget.definitionVersion,
        roleCompatibilityVersion: widget.providesRoles[0],
        settings: {},
        settingsSchemaVersion: widget.settingsSchema.version,
        bindings: {},
        bindingVersion: 1,
        visibility: "shown",
        presence: "personal",
        layout: {
          instanceId,
          x: 0,
          y: 0,
          w: size.w,
          h: size.h,
          minW: widget.sizeContract.min.w,
          minH: widget.sizeContract.min.h,
          ...(widget.sizeContract.max === undefined
            ? {}
            : {
                maxW: widget.sizeContract.max.w,
                maxH: widget.sizeContract.max.h,
              }),
        },
      },
    });
    announce(`${widget.displayName} added to the draft view`);
  };

  const replaceWidget = (
    instance: EffectiveWidgetInstance,
    target: RegisteredWidget,
  ) => {
    const request = (allowExplicitReset: boolean) =>
      planWidgetReplacement({
        registry,
        view: definition,
        instance,
        targetTypeId: target.definition.typeId,
        migrations: [],
        targetDefaults: { settings: {}, bindings: instance.bindings },
        allowExplicitReset,
      });
    let result = request(false);
    if (
      !result.ok &&
      result.reason === "migration-failed" &&
      window.confirm(
        `${target.definition.displayName} cannot preserve all settings. Reset incompatible settings and continue?`,
      )
    ) {
      result = request(true);
    }
    if (!result.ok) {
      announce(result.message, "assertive");
      return;
    }
    act(result.plan.action);
    announce(`Replaced widget with ${target.definition.displayName}`);
  };

  const issueLayoutCommand = (
    instanceId: WidgetInstanceId,
    command: Omit<LayoutCommand, "instanceId">,
  ) => {
    act({ type: "layout-command", command: { ...command, instanceId } as LayoutCommand });
    announce(`Widget ${command.kind === "move" ? "moved" : "resized"}`);
  };

  if (session.snapshot === undefined) {
    return (
      <main className="wb-view-host" aria-label={definition.displayName}>
        <WidgetState
          state={session.status === "error" ? "error" : "loading"}
          message={session.error?.message}
          onRetry={() => void session.reload("refresh")}
        />
      </main>
    );
  }

  if (!personalizationLoaded) {
    return (
      <main className="wb-view-host" aria-label={definition.displayName}>
        <WidgetState state="loading" message="Loading your saved view layout." />
      </main>
    );
  }

  const snapshot = session.snapshot;
  const visibleInstances = editState.present.instances.filter(
    (instance) => instance.visibility === "shown",
  );
  const byId = new Map(visibleInstances.map((instance) => [instance.instanceId, instance]));
  const orderedMobile = editState.present.mobileOrder
    .map((instanceId) => byId.get(instanceId))
    .filter((instance): instance is EffectiveWidgetInstance => instance !== undefined);

  const renderWidget = (instance: EffectiveWidgetInstance) => {
    const registered = registry.getWidget(instance.widgetTypeId);
    if (registered === undefined) {
      return (
        <WidgetFrame title="Unavailable widget">
          <WidgetState state="unavailable" message={instance.unavailableReason} />
        </WidgetFrame>
      );
    }
    const slot = definition.defaultSlots.find((candidate) => candidate.slotId === instance.slotId);
    const widgetSnapshot = widgetSnapshots.get(instance.instanceId);
    const widgetSnapshotError = widgetSnapshotErrors.get(instance.instanceId);
    const input = widgetSnapshot?.input;
    const defaultWidth = registered.definition.sizeContract.default.w;
    const status =
      instance.unavailableReason !== undefined
        ? "unavailable"
        : widgetSnapshotError !== undefined
          ? "error"
          : widgetSnapshot?.status ?? "loading";
    return (
      <WidgetHost
        definition={registered.definition}
        module={registered.module}
        instanceId={instance.instanceId}
        viewId={definition.viewId}
        input={input}
        status={status}
        statusMessage={
          instance.unavailableReason ?? widgetSnapshotError ?? widgetSnapshot?.quality.message
        }
        width={instance.layout.w * 54}
        height={instance.layout.h * 32}
        sizeMode={isMobile ? "compact" : sizeModeFor(instance, defaultWidth)}
        editing={customizing}
        emit={(intent: WidgetIntent) => {
          void session.dispatch(intent).catch((error: unknown) => {
            announce(`Widget action failed: ${String(error)}`, "assertive");
          });
        }}
        presence={instance.presence === "personal" ? undefined : instance.presence}
        lockedReason={slot?.lockedReason}
        onRetry={() => void session.reload("refresh")}
        onHide={customizing ? () => act({ type: "hide", instanceId: instance.instanceId }) : undefined}
        onRemove={customizing ? () => act({ type: "remove", instanceId: instance.instanceId }) : undefined}
        onMove={
          customizing
            ? (direction) => issueLayoutCommand(instance.instanceId, { kind: "move", direction })
            : undefined
        }
        onResize={
          customizing
            ? (direction) => issueLayoutCommand(instance.instanceId, { kind: "resize", direction })
            : undefined
        }
      />
    );
  };

  return (
    <main className={`wb-view-host${customizing ? " is-customizing" : ""}`}>
      {renderChrome?.(snapshot)}
      <div className="wb-view-toolbar" aria-label="View controls">
        {providerLabel ? <span className="wb-view-toolbar__provider">{providerLabel}</span> : null}
        {session.reconciling ? <span role="status">Refreshing…</span> : null}
        {customizing ? (
          <>
            <button type="button" onClick={() => setCatalogOpen(true)}>Widgets</button>
            <button type="button" onClick={() => setMobileOrderOpen((open) => !open)} aria-expanded={mobileOrderOpen}>Mobile order</button>
            <button type="button" onClick={() => { act({ type: "undo" }); setResetPatchRequested(false); }} disabled={editState.past.length === 0}>Undo</button>
            <button type="button" onClick={() => act({ type: "redo" })} disabled={editState.future.length === 0}>Redo</button>
            <button type="button" onClick={() => act({ type: "tidy" })}>Tidy</button>
            <button type="button" onClick={() => { act({ type: "reset", defaults }); setResetPatchRequested(true); }}>Reset</button>
            <button type="button" onClick={cancelCustomize}>Cancel</button>
            <button type="button" className="wb-view-toolbar__primary" onClick={() => void saveCustomize()} disabled={!editState.dirty && !resetPatchRequested}>Done</button>
          </>
        ) : (
          <>
            <button type="button" onClick={openCatalog} disabled={isMobile}>Widgets</button>
            <button type="button" onClick={beginCustomize} disabled={isMobile}>Customize view</button>
          </>
        )}
      </div>
      {customizing && mobileOrderOpen ? (
        <MobileOrderEditor
          registry={registry}
          instances={editState.present.instances}
          order={editState.present.mobileOrder}
          onChange={(order) => {
            act({ type: "set-mobile-order", order });
            announce("Mobile widget order updated");
          }}
          onClose={() => setMobileOrderOpen(false)}
        />
      ) : null}
      {personalizationError ? (
        <p className="wb-view-host__warning" role="alert">{personalizationError}</p>
      ) : null}
      {editState.lastFailure ? (
        <p className="wb-view-host__warning" role="status">{editState.lastFailure}</p>
      ) : null}
      {isMobile ? (
        <div className="wb-dashboard-mobile-stack">
          {orderedMobile.map((instance) => (
            <div key={instance.instanceId}>{renderWidget(instance)}</div>
          ))}
        </div>
      ) : (
        <ReactGridLayoutAdapter
          items={layoutFor(editState)}
          editMode={customizing}
          onDraftChange={(layout) => customizing && act({ type: "preview-layout", layout })}
          onInteractionStart={() => act({ type: "begin-interaction" })}
          onInteractionEnd={() => act({ type: "commit-interaction" })}
          renderItem={(layoutItem) => {
            const instance = byId.get(layoutItem.instanceId);
            return instance === undefined ? null : renderWidget(instance);
          }}
        />
      )}
      {catalogOpen ? (
        <WidgetCatalogDrawer
          registry={registry}
          view={definition}
          instances={editState.present.instances}
          addableWidgetTypeIds={addableWidgetTypeIds}
          getPublisherPresentation={(widget) => ({
            label: widget.app.displayName,
            appId: widget.app.appId,
            trust: widget.trust,
          })}
          onAction={act}
          onAddRequested={addWidget}
          onReplaceRequested={replaceWidget}
          onRecoverRequested={(instance) => {
            const replacement = findCompatibleWidgetReplacements(
              registry,
              definition,
              instance,
            )[0];
            if (replacement === undefined) {
              announce("No compatible replacement is installed", "assertive");
            } else {
              replaceWidget(instance, replacement);
            }
          }}
          onClose={() => setCatalogOpen(false)}
        />
      ) : null}
    </main>
  );
}

export default ViewHost;

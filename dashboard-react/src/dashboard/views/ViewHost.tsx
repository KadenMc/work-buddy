import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { ArrowCounterClockwise } from "@phosphor-icons/react/ArrowCounterClockwise";
import { ArrowUDownLeft } from "@phosphor-icons/react/ArrowUDownLeft";
import { Check } from "@phosphor-icons/react/Check";
import { DeviceMobile } from "@phosphor-icons/react/DeviceMobile";
import { Eye } from "@phosphor-icons/react/Eye";
import { GridFour } from "@phosphor-icons/react/GridFour";
import { Info } from "@phosphor-icons/react/Info";
import { Layout } from "@phosphor-icons/react/Layout";
import { PencilSimple } from "@phosphor-icons/react/PencilSimple";
import { SquaresFour } from "@phosphor-icons/react/SquaresFour";
import { X } from "@phosphor-icons/react/X";

import { useDashboardAnnouncer } from "../accessibility/DashboardAnnouncer";
import { Button } from "../../ui";
import type {
  ViewDefinition,
  ViewSnapshot,
  WidgetDefinition,
  WidgetInstanceId,
  WidgetIntent,
  WidgetSnapshot,
} from "../contributions/contracts";
import type { StandardViewChromeSlots } from "../contributions/viewModules";
import { asWidgetInstanceId } from "../contributions/contracts";
import type { ContributionRegistry } from "../contributions/registry";
import type { RegisteredWidget } from "../contributions/registry";
import { ReactGridLayoutAdapter } from "../layout/ReactGridLayoutAdapter";
import { DashboardHelpProvider, HelpTarget } from "../help";
import type { DashboardLayout, LayoutCommand } from "../layout/contracts";
import { applyLayoutCommand } from "../layout/operations";
import { useInteractionSurfaces } from "../interactions";
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
import { ViewSettingsLauncher } from "./ViewSettingsLauncher";

export interface ViewHostProps {
  readonly registry: ContributionRegistry;
  readonly definition: ViewDefinition;
  readonly provider: ViewProvider;
  readonly personalizationRepository: PersonalizationRepository;
  readonly renderChrome?: (
    snapshot: ViewSnapshot,
    slots: StandardViewChromeSlots,
  ) => ReactNode;
  readonly providerLabel?: string;
}

const formatCustomizationFailure = (failure: string): string => {
  const normalized = failure.toLowerCase();
  if (normalized.includes("out-of-bounds")) {
    return "That change would place a widget outside the 24-column canvas.";
  }
  if (normalized.includes("collision")) {
    return "That change would overlap another widget. Empty space is allowed; overlap is not.";
  }
  if (normalized.includes("size-limit")) {
    return "That change would exceed this widget's allowed size.";
  }
  if (normalized.includes("locked")) {
    return "That part of this widget's layout is locked by the view.";
  }
  return failure;
};

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
  const { notify, confirm } = useInteractionSurfaces();
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
  const [helpMode, setHelpMode] = useState(false);
  const [customizeMode, setCustomizeMode] = useState<"arrange" | "preview">("arrange");
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

  const showDashboardNotice = useCallback((message: string) => {
    notify({
      message,
      tone: "warning",
      dedupeKey: "dashboard-layout-feedback",
    });
  }, [notify]);

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

  useEffect(() => {
    if (!customizing || editState.lastFailure === undefined) return;
    showDashboardNotice(formatCustomizationFailure(editState.lastFailure));
    act({ type: "clear-failure" });
  }, [act, customizing, editState.lastFailure, showDashboardNotice]);

  const beginCustomize = () => {
    setEditState(beginViewEditSession(resolved));
    setHelpMode(false);
    setCustomizeMode("arrange");
    setCustomizing(true);
    setResetPatchRequested(false);
    announce("Customize view mode started");
  };

  const cancelCustomize = () => {
    setEditState((current) => viewEditSessionReducer(current, { type: "cancel" }));
    setCustomizing(false);
    setCustomizeMode("arrange");
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
      setCustomizeMode("arrange");
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

  const replaceWidget = async (
    instance: EffectiveWidgetInstance,
    target: RegisteredWidget,
  ): Promise<void> => {
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
    if (!result.ok && result.reason === "migration-failed") {
      const accepted = await confirm({
        title: "Reset incompatible widget settings?",
        description: `${target.definition.displayName} cannot preserve all settings. The current widget's saved content and other widgets will not be affected.`,
        confirmLabel: "Reset and replace",
        cancelLabel: "Keep current widget",
        tone: "danger",
      });
      if (accepted) result = request(true);
    }
    if (!result.ok) {
      announce(result.message, "assertive");
      return;
    }
    act(result.plan.action);
    announce(`Replaced widget with ${target.definition.displayName}`);
  };

  const issueLayoutCommand = (command: LayoutCommand) => {
    const result = applyLayoutCommand(layoutFor(editState), command);
    if (!result.accepted) {
      showDashboardNotice(
        formatCustomizationFailure(result.reason ?? "Layout change rejected"),
      );
      return;
    }
    act({ type: "layout-command", command });
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
  const chromeSlots = {
    contextualActions:
      definition.settings === undefined ? undefined : (
        <ViewSettingsLauncher definition={definition} />
      ),
  } satisfies StandardViewChromeSlots;
  const visibleInstances = editState.present.instances.filter(
    (instance) => instance.visibility === "shown",
  );
  const byId = new Map(visibleInstances.map((instance) => [instance.instanceId, instance]));
  const orderedMobile = editState.present.mobileOrder
    .map((instanceId) => byId.get(instanceId))
    .filter((instance): instance is EffectiveWidgetInstance => instance !== undefined);

  const layoutConstraintMessage = (
    kind: "move" | "resize",
    instanceId: WidgetInstanceId,
  ): string => {
    const instance = byId.get(instanceId);
    const registered =
      instance === undefined ? undefined : registry.getWidget(instance.widgetTypeId);
    const label = registered?.definition.displayName ?? "This widget";
    if (kind === "move") {
      return `Placement unchanged for ${label}. Keep it inside the 24-column canvas without overlapping another widget. Empty space is allowed.`;
    }

    const layout = instance?.layout;
    const minimum = `${layout?.minW ?? 1}×${layout?.minH ?? 1}`;
    const maximum =
      layout?.maxW === undefined || layout.maxH === undefined
        ? "the canvas bounds"
        : `${layout.maxW}×${layout.maxH}`;
    return `Size unchanged for ${label}. Allowed size: ${minimum}–${maximum} grid units. Keep it inside the 24-column canvas without overlapping another widget. Empty space is allowed.`;
  };

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
        interactionMode={customizing ? customizeMode : "operate"}
        help={
          slot?.help ??
          registered.definition.help ?? {
            summary: registered.definition.description,
            details:
              "This personally added widget is not assigned to a standard view purpose yet. Its reusable type description is shown as a fallback.",
          }
        }
        gridSize={{ w: instance.layout.w, h: instance.layout.h }}
        emit={(intent: WidgetIntent) =>
          session.dispatch(intent).catch((error: unknown) => {
            announce(`Widget action failed: ${String(error)}`, "assertive");
            return {
              intent_id: intent.intent_id,
              ...(intent.client_mutation_id === undefined
                ? {}
                : { client_mutation_id: intent.client_mutation_id }),
              status: "unavailable" as const,
              message: error instanceof Error ? error.message : String(error),
            };
          })
        }
        presence={instance.presence === "personal" ? undefined : instance.presence}
        lockedReason={slot?.lockedReason}
        onRetry={() => void session.reload("refresh")}
        onHide={customizing ? () => act({ type: "hide", instanceId: instance.instanceId }) : undefined}
        onRemove={customizing ? () => act({ type: "remove", instanceId: instance.instanceId }) : undefined}
      />
    );
  };

  return (
    <DashboardHelpProvider enabled={helpMode}>
    <main
      className={`wb-view-host${customizing ? " is-customizing" : ""}${
        customizing && customizeMode === "preview" ? " is-previewing-layout" : ""
      }${helpMode ? " is-helping" : ""}`}
    >
      {renderChrome !== undefined ? (
        renderChrome(snapshot, chromeSlots)
      ) : chromeSlots.contextualActions !== undefined ? (
        <div className="wb-view-context-actions">
          {chromeSlots.contextualActions}
        </div>
      ) : null}
      <div className="wb-view-toolbar" aria-label="View controls">
        {providerLabel && renderChrome === undefined ? (
          <span className="wb-view-toolbar__provider">{providerLabel}</span>
        ) : null}
        {session.reconciling ? <span role="status" aria-label="Refreshing…">Refreshing…</span> : null}
        {customizing ? (
          <>
            <span className="wb-view-toolbar__mode">
              {customizeMode === "arrange" ? (
                <Layout weight="duotone" aria-hidden="true" />
              ) : (
                <Eye weight="duotone" aria-hidden="true" />
              )}
              {customizeMode === "arrange" ? "Arranging layout" : "Previewing interactions"}
              <span className="wb-view-toolbar__constraint">
                {customizeMode === "arrange"
                  ? "24 columns · gaps allowed · no overlap · resize from any edge"
                  : "Widget actions are simulated or blocked · preview input is discarded"}
              </span>
            </span>
            {customizeMode === "arrange" ? (
              <>
                <span className="wb-view-toolbar__group">
                  <Button size="small" onClick={() => setCatalogOpen(true)}>
                    <SquaresFour aria-hidden="true" /> Widgets
                  </Button>
                  <Button
                    size="small"
                    onClick={() => setMobileOrderOpen((open) => !open)}
                    aria-expanded={mobileOrderOpen}
                  >
                    <DeviceMobile aria-hidden="true" /> Mobile order
                  </Button>
                  <Button
                    size="small"
                    onClick={() => {
                      setCatalogOpen(false);
                      setMobileOrderOpen(false);
                      setCustomizeMode("preview");
                      announce("Interaction preview started; widget actions will not be saved");
                    }}
                  >
                    <Eye aria-hidden="true" /> Preview interactions
                  </Button>
                </span>
                <span className="wb-view-toolbar__group">
              <Button
                size="small"
                variant="ghost"
                onClick={() => {
                  act({ type: "undo" });
                  setResetPatchRequested(false);
                }}
                disabled={editState.past.length === 0}
              >
                <ArrowCounterClockwise aria-hidden="true" /> Undo
              </Button>
              <Button
                size="small"
                variant="ghost"
                onClick={() => act({ type: "redo" })}
                disabled={editState.future.length === 0}
              >
                <ArrowUDownLeft aria-hidden="true" /> Redo
              </Button>
              <Button
                size="small"
                variant="ghost"
                title="Move widgets upward to remove vertical gaps without changing their columns or sizes"
                onClick={() => act({ type: "tidy" })}
              >
                <GridFour aria-hidden="true" /> Tidy upward
              </Button>
              <Button
                size="small"
                variant="ghost"
                title="Restore the App's recommended widgets, layout, settings, and mobile order"
                onClick={() => {
                  act({ type: "reset", defaults });
                  setResetPatchRequested(true);
                }}
              >
                Restore view defaults
              </Button>
                </span>
              </>
            ) : (
              <span className="wb-view-toolbar__group">
                <Button
                  size="small"
                  onClick={() => {
                    setCustomizeMode("arrange");
                    announce("Returned to arranging the view");
                  }}
                >
                  <PencilSimple aria-hidden="true" /> Back to arranging
                </Button>
              </span>
            )}
            <span className="wb-view-toolbar__group">
              <Button size="small" variant="ghost" onClick={cancelCustomize}>
                <X aria-hidden="true" /> Cancel
              </Button>
              <Button
                size="small"
                variant="primary"
                onClick={() => void saveCustomize()}
                disabled={!editState.dirty && !resetPatchRequested}
              >
                <Check weight="bold" aria-hidden="true" /> Done
              </Button>
            </span>
          </>
        ) : (
          <>
            <Button
              size="small"
              variant={helpMode ? "primary" : "secondary"}
              aria-pressed={helpMode}
              onClick={() => {
                setHelpMode((enabled) => {
                  announce(enabled ? "Hover help turned off" : "Hover help turned on");
                  return !enabled;
                });
              }}
              disabled={isMobile}
            >
              <Info weight="duotone" aria-hidden="true" /> Hover help
            </Button>
            <HelpTarget
              content={{
                summary: "Rearrange and resize the widgets in this view.",
                details:
                  "Customize view opens a dedicated desktop layout editor. You can move, resize, add, hide, or remove eligible widgets, preview the result safely, and then save or cancel the entire layout change.",
              }}
              placement="bottom end"
              reactAriaComposite
            >
              <Button size="small" onClick={beginCustomize} disabled={isMobile}>
                <Layout aria-hidden="true" /> Customize view
              </Button>
            </HelpTarget>
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
      {isMobile ? (
        <div className="wb-dashboard-mobile-stack">
          {orderedMobile.map((instance) => (
            <div key={instance.instanceId}>{renderWidget(instance)}</div>
          ))}
        </div>
      ) : (
        <ReactGridLayoutAdapter
          items={layoutFor(editState)}
          editMode={customizing && customizeMode === "arrange"}
          onDraftChange={(layout) =>
            customizing && customizeMode === "arrange" && act({ type: "preview-layout", layout })
          }
          onInteractionStart={() => act({ type: "begin-interaction" })}
          onKeyboardCommand={issueLayoutCommand}
          onInteractionRejected={(kind, instanceId) =>
            showDashboardNotice(layoutConstraintMessage(kind, instanceId))
          }
          onInteractionCancel={(kind, _instanceId, reason) => {
            act({ type: "cancel-interaction" });
            if (reason !== "edit-mode-ended") {
              showDashboardNotice(
                `${kind === "resize" ? "Resize" : "Move"} canceled because the pointer interaction ended outside the dashboard.`,
              );
            }
          }}
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
    </DashboardHelpProvider>
  );
}

export default ViewHost;

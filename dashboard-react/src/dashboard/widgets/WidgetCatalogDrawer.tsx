import { useEffect, useRef, type SyntheticEvent } from "react";

import { Button } from "../../ui";
import type {
  AppId,
  ViewDefinition,
  WidgetDefinition,
  WidgetTypeId,
} from "../contributions/contracts";
import type {
  ContributionTrustProvenance,
  ContributionRegistry,
  RegisteredWidget,
} from "../contributions/registry";
import type {
  EffectiveWidgetInstance,
  ViewEditAction,
} from "../personalization/contracts";
import { findCompatibleWidgetReplacements } from "./replaceWidget";

export type WidgetPublisherTrust = ContributionTrustProvenance;

export interface WidgetPublisherPresentation {
  readonly label: string;
  readonly appId: AppId;
  readonly trust: WidgetPublisherTrust;
}

export interface WidgetCatalogDrawerProps {
  readonly registry: ContributionRegistry;
  readonly view: ViewDefinition;
  readonly instances: readonly EffectiveWidgetInstance[];
  /** Provider-approved types that can be hydrated for this specific view. */
  readonly addableWidgetTypeIds: readonly WidgetTypeId[];
  readonly title?: string;
  getPublisherPresentation?(widget: RegisteredWidget): WidgetPublisherPresentation;
  onAction(action: ViewEditAction): void;
  onAddRequested(definition: WidgetDefinition): void;
  onReplaceRequested(
    instance: EffectiveWidgetInstance,
    target: RegisteredWidget,
  ): void;
  onRecoverRequested?(instance: EffectiveWidgetInstance): void;
  onClose(): void;
}

export interface WidgetLibraryGroup {
  readonly path: readonly string[];
  readonly widgets: readonly RegisteredWidget[];
}

export function groupWidgetsByLibraryPath(
  widgets: readonly RegisteredWidget[],
): readonly WidgetLibraryGroup[] {
  const groups = new Map<string, { path: readonly string[]; widgets: RegisteredWidget[] }>();
  widgets.forEach((widget) => {
    const path = widget.definition.libraryPath.slice(0, -1);
    const key = path.join("\u0000");
    const group = groups.get(key) ?? { path, widgets: [] };
    group.widgets.push(widget);
    groups.set(key, group);
  });
  return [...groups.values()]
    .sort((left, right) => left.path.join("/").localeCompare(right.path.join("/")))
    .map((group) => ({
      path: group.path,
      widgets: group.widgets.sort((left, right) =>
        left.definition.displayName.localeCompare(right.definition.displayName),
      ),
    }));
}

const defaultPublisherPresentation = (
  widget: RegisteredWidget,
): WidgetPublisherPresentation => ({
  label: widget.app.displayName,
  appId: widget.app.appId,
  trust: widget.trust,
});

function PublisherLine({
  widget,
  presentation,
}: {
  readonly widget: RegisteredWidget;
  readonly presentation: WidgetPublisherPresentation;
}) {
  return (
    <p className="wb-widget-catalog__provenance">
      Published by {presentation.label} ({presentation.appId}) · Trust: {presentation.trust}
      {widget.definition.definitionVersion > 0
        ? ` · Version ${widget.definition.definitionVersion}`
        : null}
    </p>
  );
}

interface InstanceCardProps {
  readonly instance: EffectiveWidgetInstance;
  readonly registered?: RegisteredWidget;
  readonly replacements: readonly RegisteredWidget[];
  readonly publisher: (widget: RegisteredWidget) => WidgetPublisherPresentation;
  readonly unavailable: boolean;
  onAction(action: ViewEditAction): void;
  onReplace(target: RegisteredWidget): void;
  onRecover?(): void;
}

function InstanceCard({
  instance,
  registered,
  replacements,
  publisher,
  unavailable,
  onAction,
  onReplace,
  onRecover,
}: InstanceCardProps) {
  const required = instance.presence === "required";
  const title = registered?.definition.displayName ?? instance.widgetTypeId;
  return (
    <li className="wb-widget-catalog__card" data-instance-id={instance.instanceId}>
      <h4>{title}</h4>
      {registered === undefined ? (
        <p>Publisher unavailable · {instance.widgetTypeId}</p>
      ) : (
        <>
          <p>{registered.definition.libraryPath.join(" / ")}</p>
          <PublisherLine widget={registered} presentation={publisher(registered)} />
        </>
      )}
      {instance.slotId !== undefined ? <p>View slot: {instance.slotId}</p> : <p>Personal widget</p>}
      {unavailable ? (
        <p role="status">{instance.unavailableReason ?? "This widget type is unavailable."}</p>
      ) : null}

      <div className="wb-widget-catalog__actions">
        {unavailable ? (
          <Button onClick={onRecover} disabled={onRecover === undefined}>
            Find replacement
          </Button>
        ) : instance.visibility === "shown" ? (
          <Button
            onClick={() => onAction({ type: "hide", instanceId: instance.instanceId })}
            disabled={required}
          >
            Hide
          </Button>
        ) : (
          <Button onClick={() => onAction({ type: "show", instanceId: instance.instanceId })}>
            Show
          </Button>
        )}
        <Button
          variant="danger"
          onClick={() => onAction({ type: "remove", instanceId: instance.instanceId })}
          disabled={required}
        >
          Remove
        </Button>
      </div>

      {required ? <p>This slot is required for the view's primary purpose.</p> : null}
      {replacements.length > 0 ? (
        <details>
          <summary>Replace widget</summary>
          <ul>
            {replacements.map((candidate) => {
              const provenance = publisher(candidate);
              return (
                <li key={candidate.definition.typeId}>
                  <span>
                    {candidate.definition.displayName} · {candidate.definition.libraryPath.join(" / ")} ·{" "}
                    {provenance.label} · {provenance.trust}
                  </span>{" "}
                  <Button onClick={() => onReplace(candidate)}>
                    Replace with {candidate.definition.displayName}
                  </Button>
                </li>
              );
            })}
          </ul>
        </details>
      ) : null}
    </li>
  );
}

export function WidgetCatalogDrawer({
  registry,
  view,
  instances,
  addableWidgetTypeIds,
  title = "Widgets",
  getPublisherPresentation = defaultPublisherPresentation,
  onAction,
  onAddRequested,
  onReplaceRequested,
  onRecoverRequested,
  onClose,
}: WidgetCatalogDrawerProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const restoreFocus = (): void => {
    const previouslyFocused = previouslyFocusedRef.current;
    if (previouslyFocused?.isConnected) previouslyFocused.focus();
    globalThis.setTimeout(() => {
      if (previouslyFocused?.isConnected) previouslyFocused.focus();
    }, 0);
  };
  useEffect(() => {
    previouslyFocusedRef.current = document.activeElement as HTMLElement | null;
    const dialog = dialogRef.current;
    if (dialog !== null && !dialog.open) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    }
    closeRef.current?.focus();
    return restoreFocus;
  }, []);
  const requestClose = (): void => {
    const dialog = dialogRef.current;
    if (dialog?.open && typeof dialog.close === "function") dialog.close();
    else dialog?.removeAttribute("open");
    onClose();
    restoreFocus();
  };
  const onDialogCancel = (event: SyntheticEvent<HTMLDialogElement>): void => {
    event.preventDefault();
    requestClose();
  };
  const unavailable = instances.filter(
    (instance) =>
      instance.unavailableReason !== undefined || registry.getWidget(instance.widgetTypeId) === undefined,
  );
  const unavailableIds = new Set(unavailable.map((instance) => instance.instanceId));
  const shown = instances.filter(
    (instance) => instance.visibility === "shown" && !unavailableIds.has(instance.instanceId),
  );
  const hidden = instances.filter(
    (instance) => instance.visibility === "hidden" && !unavailableIds.has(instance.instanceId),
  );
  const installedTypeIds = new Set(instances.map((instance) => instance.widgetTypeId));
  const providerAddableTypeIds = new Set(addableWidgetTypeIds);
  const available = registry.listWidgets().filter(
    (widget) =>
      providerAddableTypeIds.has(widget.definition.typeId) &&
      (widget.definition.multiplicity === "multiple_per_view" ||
        !installedTypeIds.has(widget.definition.typeId)),
  );
  const publisher = (widget: RegisteredWidget) => getPublisherPresentation(widget);

  const renderInstances = (entries: readonly EffectiveWidgetInstance[]) => (
    <ul className="wb-widget-catalog__list">
      {entries.map((instance) => {
        const registered = registry.getWidget(instance.widgetTypeId);
        const replacements = findCompatibleWidgetReplacements(registry, view, instance);
        return (
          <InstanceCard
            key={instance.instanceId}
            instance={instance}
            registered={registered}
            replacements={replacements}
            publisher={publisher}
            unavailable={unavailableIds.has(instance.instanceId)}
            onAction={onAction}
            onReplace={(target) => onReplaceRequested(instance, target)}
            onRecover={
              onRecoverRequested === undefined
                ? undefined
                : () => onRecoverRequested(instance)
            }
          />
        );
      })}
    </ul>
  );

  return (
    <dialog
      ref={dialogRef}
      className="wb-widget-catalog"
      aria-labelledby="wb-widget-catalog-title"
      onCancel={onDialogCancel}
    >
      <header>
        <h2 id="wb-widget-catalog-title">{title}</h2>
        <Button
          ref={closeRef}
          onClick={requestClose}
          aria-label="Close Widgets drawer"
        >
          Close
        </Button>
      </header>

      <section aria-labelledby="wb-widget-catalog-shown">
        <h3 id="wb-widget-catalog-shown">Shown ({shown.length})</h3>
        {shown.length === 0 ? <p>No widgets are currently shown.</p> : renderInstances(shown)}
      </section>
      <section aria-labelledby="wb-widget-catalog-hidden">
        <h3 id="wb-widget-catalog-hidden">Hidden ({hidden.length})</h3>
        {hidden.length === 0 ? <p>No hidden widgets.</p> : renderInstances(hidden)}
      </section>
      <section aria-labelledby="wb-widget-catalog-unavailable">
        <h3 id="wb-widget-catalog-unavailable">Unavailable ({unavailable.length})</h3>
        {unavailable.length === 0 ? (
          <p>No unavailable or orphaned widgets.</p>
        ) : (
          renderInstances(unavailable)
        )}
      </section>
      <section aria-labelledby="wb-widget-catalog-available">
        <h3 id="wb-widget-catalog-available">Available ({available.length})</h3>
        {available.length === 0 ? (
          <p>No additional widget types are available.</p>
        ) : (
          groupWidgetsByLibraryPath(available).map((group) => (
            <section key={group.path.join("/") || "other"}>
              <h4>{group.path.length === 0 ? "Other" : group.path.join(" / ")}</h4>
              <ul className="wb-widget-catalog__list">
                {group.widgets.map((widget) => {
                  const provenance = publisher(widget);
                  return (
                    <li key={widget.definition.typeId} className="wb-widget-catalog__card">
                      <h5>{widget.definition.displayName}</h5>
                      <p>{widget.definition.description}</p>
                      <p>{widget.definition.libraryPath.join(" / ")}</p>
                      <PublisherLine widget={widget} presentation={provenance} />
                      <Button onClick={() => onAddRequested(widget.definition)}>
                        Add {widget.definition.displayName}
                      </Button>
                    </li>
                  );
                })}
              </ul>
            </section>
          ))
        )}
      </section>
    </dialog>
  );
}

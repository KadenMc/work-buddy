import { useEffect, useRef, type KeyboardEvent as ReactKeyboardEvent } from "react";

import { Button } from "../../ui";
import type { AppId, ViewDefinition, WidgetDefinition } from "../contributions/contracts";
import type {
  ContributionRegistry,
  RegisteredWidget,
} from "../contributions/registry";
import type {
  EffectiveWidgetInstance,
  ViewEditAction,
} from "../personalization/contracts";
import { findCompatibleWidgetReplacements } from "./replaceWidget";

export type WidgetPublisherTrust =
  | "native"
  | "standard"
  | "personal-proposed"
  | "developer-local"
  | "unverified";

export interface WidgetPublisherPresentation {
  readonly label: string;
  readonly appId: AppId;
  readonly trust: WidgetPublisherTrust;
}

export interface WidgetCatalogDrawerProps {
  readonly registry: ContributionRegistry;
  readonly view: ViewDefinition;
  readonly instances: readonly EffectiveWidgetInstance[];
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
  trust: "unverified",
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
  title = "Widgets",
  getPublisherPresentation = defaultPublisherPresentation,
  onAction,
  onAddRequested,
  onReplaceRequested,
  onRecoverRequested,
  onClose,
}: WidgetCatalogDrawerProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previouslyFocused = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    return () => {
      if (previouslyFocused?.isConnected) previouslyFocused.focus();
    };
  }, []);
  const onDialogKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>): void => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...(dialogRef.current?.querySelectorAll<HTMLElement>(
      'button:not([disabled]),summary,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])',
    ) ?? [])];
    if (focusable.length === 0) return;
    const first = focusable[0]!;
    const last = focusable[focusable.length - 1]!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
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
  const available = registry.listWidgets().filter(
    (widget) =>
      widget.definition.multiplicity === "multiple_per_view" ||
      !installedTypeIds.has(widget.definition.typeId),
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
    <div
      ref={dialogRef}
      className="wb-widget-catalog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="wb-widget-catalog-title"
      onKeyDown={onDialogKeyDown}
    >
      <header>
        <h2 id="wb-widget-catalog-title">{title}</h2>
        <Button ref={closeRef} onClick={onClose} aria-label="Close Widgets drawer">
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
    </div>
  );
}

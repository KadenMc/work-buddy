import type {
  CalendarSurfaceItem,
  CalendarSurfaceModel,
  CalendarSurfaceSource,
} from "./contracts";

export type CalendarItemActionGroup = "primary" | "edit" | "danger";
export type CalendarItemActionIcon =
  | "open"
  | "source"
  | "edit-time"
  | "duration"
  | "remove"
  | "custom";

/**
 * Data-only action contribution. Apps may add namespaced action ids without
 * passing arbitrary React or FullCalendar objects through the View API.
 */
export interface CalendarItemActionDescriptor {
  readonly id: string;
  readonly label: string;
  readonly group: CalendarItemActionGroup;
  readonly icon?: CalendarItemActionIcon;
  readonly tone?: "default" | "danger";
  readonly disabledReason?: string;
  readonly dispatch: "open" | "action";
  readonly closeOnAction?: boolean;
}

export interface CalendarItemActionResolution {
  readonly actions: readonly CalendarItemActionDescriptor[];
  readonly note?: string;
}

export interface CalendarItemActionContext {
  readonly item: CalendarSurfaceItem;
  readonly source?: CalendarSurfaceSource;
  readonly access: CalendarSurfaceModel["access"];
  readonly base: CalendarItemActionResolution;
}

/**
 * Extension seam for an App/provider to amend the standard kind-aware menu.
 * Returning data keeps rendering, focus behavior, and skinning Work Buddy-owned.
 */
export type CalendarItemActionResolver = (
  context: CalendarItemActionContext,
) => CalendarItemActionResolution;

const openAction = (
  item: CalendarSurfaceItem,
): CalendarItemActionDescriptor => ({
  id: `wb.calendar.${item.kind}.open`,
  label:
    item.kind === "record"
      ? "Open record"
      : item.kind === "plan"
        ? "Open plan"
        : item.kind === "calendar"
          ? "Open event"
          : "Open item",
  group: "primary",
  icon: "open",
  dispatch: "open",
  closeOnAction: true,
});

const sourceAction = (
  item: CalendarSurfaceItem,
): CalendarItemActionDescriptor | undefined => {
  if (!item.navigation) return undefined;
  return {
    id: `wb.calendar.${item.kind}.open-source`,
    label:
      item.kind === "calendar"
        ? "View in source calendar"
        : item.kind === "record"
          ? "Go to record source"
          : item.kind === "plan"
            ? "Go to plan source"
            : "Go to item source",
    group: "primary",
    icon: "source",
    dispatch: "action",
    closeOnAction: true,
  };
};

export function defaultCalendarItemActions(
  item: CalendarSurfaceItem,
  access: CalendarSurfaceModel["access"],
): CalendarItemActionResolution {
  const actions: CalendarItemActionDescriptor[] = [openAction(item)];
  const source = sourceAction(item);
  if (source) actions.push(source);

  const canEdit = access.mode === "read_write";
  if (item.kind !== "record" && canEdit && item.capabilities.move) {
    actions.push({
      id: `wb.calendar.${item.kind}.edit-time`,
      label: item.kind === "calendar" ? "Edit event time" : "Edit scheduled time",
      group: "edit",
      icon: "edit-time",
      dispatch: "action",
    });
  }
  if (item.kind !== "record" && canEdit && item.capabilities.resize) {
    actions.push({
      id: `wb.calendar.${item.kind}.change-duration`,
      label: "Change duration",
      group: "edit",
      icon: "duration",
      dispatch: "action",
    });
  }
  if (item.kind !== "record" && canEdit && item.capabilities.remove) {
    actions.push({
      id: `wb.calendar.${item.kind}.remove`,
      label:
        item.kind === "calendar"
          ? "Remove calendar event"
          : item.kind === "plan"
            ? "Remove plan"
            : "Remove item",
      group: "danger",
      icon: "remove",
      tone: "danger",
      dispatch: "action",
    });
  }

  const mutable =
    item.capabilities.move || item.capabilities.resize || item.capabilities.remove;
  const note =
    access.mode === "read_only"
      ? access.reason ?? "This calendar surface is read-only."
      : item.kind === "calendar" && !mutable
        ? "This calendar event is read-only here. Provider editing is not connected."
        : item.kind === "record"
          ? "Records describe observed work and are not rescheduled from the calendar."
          : undefined;

  return { actions, ...(note ? { note } : {}) };
}

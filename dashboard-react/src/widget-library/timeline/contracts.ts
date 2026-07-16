import type { WidgetIntent } from "../../dashboard/contributions/contracts";
import type { WidgetAccess, WidgetProvenance } from "../shared";

export type TimelineItemKind = "record" | "calendar" | "plan";
export type TimelineItemStatus = "observed" | "planned" | "completed" | "cancelled";
export type TimelineItemMutability = "past_protected" | "fixed" | "editable";
export type TimelinePrecision = "exact" | "derived" | "approximate";
export type TimelineRenderMode = "timeline" | "list";
export type TimelineDensity = "comfortable" | "compact";

export type TimelineTemporalPlacement =
  | { readonly shape: "point"; readonly at: string }
  | { readonly shape: "span"; readonly startAt: string; readonly endAt: string };

export interface TimelineNavigationTarget {
  readonly targetType: string;
  readonly targetId: string;
}

export type DayTimelineItem = TimelineTemporalPlacement & {
  readonly itemId: string;
  readonly kind: TimelineItemKind;
  readonly title: string;
  readonly detail?: string;
  readonly status: TimelineItemStatus;
  readonly mutability: TimelineItemMutability;
  readonly precision: TimelinePrecision;
  readonly provenance: WidgetProvenance;
  readonly navigation?: TimelineNavigationTarget;
};

export interface TimelineDayWindow {
  readonly dayId: string;
  readonly localDate: string;
  readonly timezone: string;
  readonly dayBoundaryStart: string;
  readonly windowStart: string;
  readonly windowEnd: string;
  readonly now: string;
}

export interface DayTimelineInput {
  readonly instanceId: string;
  readonly revision: string;
  readonly day: TimelineDayWindow;
  readonly access?: WidgetAccess;
  readonly renderMode: TimelineRenderMode;
  readonly density: TimelineDensity;
  readonly items: readonly DayTimelineItem[];
}

export interface TimelineOpenItemIntent
  extends WidgetIntent<{ readonly item_id: string }> {
  readonly intent_type: "wb.timeline.open-item";
}

export interface TimelineRenderModeChangedIntent
  extends WidgetIntent<{ readonly render_mode: TimelineRenderMode }> {
  readonly intent_type: "wb.timeline.render-mode-changed";
}

export interface TimelineReplanRequestedIntent
  extends WidgetIntent<{
    readonly day_id: string;
    readonly preserve_before: string;
  }> {
  readonly intent_type: "wb.timeline.replan-requested";
}

export type DayTimelineIntent =
  | TimelineOpenItemIntent
  | TimelineRenderModeChangedIntent
  | TimelineReplanRequestedIntent;

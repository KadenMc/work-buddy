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
  /**
   * Exact stored text of the authoring record. The title and detail above are a
   * display split of it, so an edit round-trips the text the owning store holds
   * instead of a reformatted rejoin.
   */
  readonly text?: string;
  /** Numeric content revision of the authoring record, used as its compare-and-set token. */
  readonly version?: number;
  /**
   * The owning provider's vocabulary for who authors this item's content. Only
   * items authored by the provider itself accept text and time changes here.
   */
  readonly authorityKind?: string;
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
  /** `view` means a containing surface already renders the access notice. */
  readonly accessNotice?: "widget" | "view";
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

export interface TimelineItemActionRequestedIntent
  extends WidgetIntent<{
    readonly item_id: string;
    readonly action_id: string;
    readonly expected_revision: string;
  }> {
  readonly intent_type: "wb.timeline.item-action-requested";
  readonly client_mutation_id: string;
}

/**
 * Correct one record in place. `stated_at` is present only when the reader
 * changed the occurrence time, so an untouched time is never restated.
 */
export interface TimelineItemEditRequestedIntent
  extends WidgetIntent<{
    readonly item_id: string;
    readonly expected_version: number;
    readonly text: string;
    readonly stated_at?: string;
  }> {
  readonly intent_type: "wb.timeline.item-edit-requested";
  readonly client_mutation_id: string;
}

export interface TimelineItemDeleteRequestedIntent
  extends WidgetIntent<{
    readonly item_id: string;
    readonly expected_version: number;
  }> {
  readonly intent_type: "wb.timeline.item-delete-requested";
  readonly client_mutation_id: string;
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
  | TimelineItemActionRequestedIntent
  | TimelineItemEditRequestedIntent
  | TimelineItemDeleteRequestedIntent
  | TimelineRenderModeChangedIntent
  | TimelineReplanRequestedIntent;

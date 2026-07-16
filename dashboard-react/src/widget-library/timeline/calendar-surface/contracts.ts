import type { WidgetProvenance } from "../../shared";
import type { TimelineNavigationTarget } from "../contracts";

export type CalendarSurfaceRange = "day" | "week" | "month";
export type CalendarSurfacePresentation = "calendar" | "list";

/**
 * Range and presentation are deliberately independent. "List" is a way to
 * render a day, week, or month—not a second name for a one-day agenda.
 */
export interface CalendarSurfaceView {
  readonly range: CalendarSurfaceRange;
  readonly presentation: CalendarSurfacePresentation;
}
export type CalendarSurfaceTone =
  | "data-1"
  | "data-2"
  | "data-3"
  | "data-4"
  | "data-5"
  | "data-6"
  | "data-7"
  | "data-8";
export type CalendarSurfaceItemKind =
  | "record"
  | "plan"
  | "calendar"
  | `app:${string}`;

export type CalendarPlacement =
  | { readonly shape: "point"; readonly at: string }
  | { readonly shape: "span"; readonly startAt: string; readonly endAt: string }
  | {
      readonly shape: "all_day";
      readonly startDate: string;
      readonly endDateExclusive: string;
    };

export interface CalendarSurfaceSource {
  readonly sourceId: string;
  readonly label: string;
  readonly tone: CalendarSurfaceTone;
}

export interface CalendarSurfaceItemCapabilities {
  readonly open: boolean;
  readonly move: boolean;
  readonly resize: boolean;
  readonly remove: boolean;
}

export interface CalendarSurfaceItem {
  readonly id: string;
  readonly revision: string;
  readonly sourceId: string;
  readonly placement: CalendarPlacement;
  readonly kind: CalendarSurfaceItemKind;
  readonly kindLabel?: string;
  readonly title: string;
  readonly detail?: string;
  readonly status: string;
  readonly provenance: WidgetProvenance;
  readonly capabilities: CalendarSurfaceItemCapabilities;
  /** Provider-owned human language for constraints that capabilities alone cannot explain. */
  readonly policy?: { readonly label: string; readonly description?: string };
  readonly navigation?: TimelineNavigationTarget;
  readonly appearance?: {
    readonly tone?: CalendarSurfaceTone;
    readonly emphasis?: "quiet" | "normal" | "strong";
  };
}

export interface CalendarSurfaceModel {
  readonly revision: string;
  readonly timezone: string;
  readonly now: string;
  readonly selectedDate: string;
  readonly view: CalendarSurfaceView;
  readonly visibleRange: { readonly start: string; readonly endExclusive: string };
  readonly access: { readonly mode: "read_write" | "read_only"; readonly reason?: string };
  readonly capabilities?: { readonly create: boolean };
  readonly sources: readonly CalendarSurfaceSource[];
  readonly items: readonly CalendarSurfaceItem[];
  readonly loading?: boolean;
  readonly error?: { readonly code: string; readonly message: string };
}

export type CalendarSurfaceIntent =
  | {
      readonly type: "calendar.range-requested";
      readonly view: CalendarSurfaceView;
      readonly start: string;
      readonly endExclusive: string;
      readonly timezone: string;
    }
  | { readonly type: "calendar.item-open-requested"; readonly itemId: string }
  | {
      readonly type: "calendar.item-action-requested";
      readonly requestId: string;
      readonly actionId: string;
      readonly itemId: string;
      readonly expectedRevision: string;
    }
  | {
      readonly type: "calendar.item-create-requested";
      readonly requestId: string;
      readonly placement: CalendarPlacement;
      readonly sourceId?: string;
    }
  | {
      readonly type: "calendar.item-move-requested";
      readonly requestId: string;
      readonly itemId: string;
      readonly expectedRevision: string;
      readonly placement: CalendarPlacement;
    }
  | {
      readonly type: "calendar.item-resize-requested";
      readonly requestId: string;
      readonly itemId: string;
      readonly expectedRevision: string;
      readonly placement: CalendarPlacement;
    }
  | {
      readonly type: "calendar.item-remove-requested";
      readonly requestId: string;
      readonly itemId: string;
      readonly expectedRevision: string;
    };

export interface CalendarSurfaceIntentResult {
  readonly status: "accepted" | "rejected" | "conflict" | "unavailable";
  readonly revision?: string;
  readonly message?: string;
}

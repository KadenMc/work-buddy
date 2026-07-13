import type { ReactNode } from "react";

import type {
  DashboardLayoutItem,
  GridSize,
  WidgetInstanceId,
  WidgetTypeId,
} from "../contributions/contracts";

export const DASHBOARD_COLUMNS = 24 as const;

/** Work Buddy-owned portable layout record. RGL fields never escape the adapter. */
export interface WidgetLayoutItem extends DashboardLayoutItem {
  readonly minW?: number;
  readonly maxW?: number;
  readonly minH?: number;
  readonly maxH?: number;
  readonly positionLocked?: boolean;
  readonly sizeLocked?: boolean;
}

export type DashboardLayout = readonly WidgetLayoutItem[];
export type LayoutInteractionKind = "move" | "resize";

export type LayoutCommand =
  | {
      readonly kind: "move";
      readonly instanceId: WidgetInstanceId;
      readonly direction: "left" | "right" | "up" | "down";
      readonly amount?: number;
    }
  | {
      readonly kind: "resize";
      readonly instanceId: WidgetInstanceId;
      readonly direction:
        | "grow-width"
        | "shrink-width"
        | "grow-height"
        | "shrink-height";
      readonly amount?: number;
    };

export interface LayoutMutationResult {
  readonly accepted: boolean;
  readonly items: DashboardLayout;
  readonly reason?: "not-found" | "locked" | "out-of-bounds" | "collision" | "size-limit";
}

export interface ExternalWidgetDropSpec extends GridSize {
  readonly widgetTypeId: WidgetTypeId;
  readonly instanceId: WidgetInstanceId;
  readonly minW?: number;
  readonly maxW?: number;
  readonly minH?: number;
  readonly maxH?: number;
}

export interface ReactGridLayoutAdapterProps {
  readonly items: DashboardLayout;
  readonly editMode: boolean;
  readonly rowHeight?: number;
  readonly margin?: readonly [number, number];
  readonly containerPadding?: readonly [number, number];
  renderItem(item: WidgetLayoutItem): ReactNode;
  onDraftChange(items: DashboardLayout): void;
  onInteractionStart?(kind: LayoutInteractionKind, instanceId: WidgetInstanceId): void;
  onInteractionEnd?(
    kind: LayoutInteractionKind,
    items: DashboardLayout,
    instanceId: WidgetInstanceId,
  ): void;
  readonly externalDrop?: ExternalWidgetDropSpec;
  onExternalWidgetDrop?(
    widgetTypeId: WidgetTypeId,
    placement: WidgetLayoutItem,
  ): void;
}


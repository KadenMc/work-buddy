import {
  GridLayout,
  getCompactor,
  useContainerWidth,
  type EventCallback,
  type Layout as RglLayout,
  type LayoutItem as RglLayoutItem,
} from "react-grid-layout";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";

import {
  asWidgetInstanceId,
  type WidgetInstanceId,
} from "../contributions/contracts";
import {
  DASHBOARD_COLUMNS,
  type DashboardLayout,
  type ReactGridLayoutAdapterProps,
  type WidgetLayoutItem,
} from "./contracts";

export const WORK_BUDDY_NO_COMPACTION = getCompactor(null, false, true);

export const toRglLayout = (items: DashboardLayout): RglLayout =>
  items.map((item) => ({
    i: item.instanceId,
    x: item.x,
    y: item.y,
    w: item.w,
    h: item.h,
    ...(item.minW === undefined ? {} : { minW: item.minW }),
    ...(item.maxW === undefined ? {} : { maxW: item.maxW }),
    ...(item.minH === undefined ? {} : { minH: item.minH }),
    ...(item.maxH === undefined ? {} : { maxH: item.maxH }),
    isDraggable: item.positionLocked !== true,
    isResizable: item.sizeLocked !== true,
  }));

export const fromRglLayout = (
  layout: RglLayout,
  source: DashboardLayout,
): DashboardLayout => {
  const byId = new Map(layout.map((item) => [item.i, item]));
  return source.map((item) => {
    const translated = byId.get(item.instanceId);
    return translated === undefined
      ? item
      : {
          ...item,
          x: translated.x,
          y: translated.y,
          w: translated.w,
          h: translated.h,
        };
  });
};

const interactionId = (
  oldItem: RglLayoutItem | null,
  newItem: RglLayoutItem | null,
): WidgetInstanceId | undefined => {
  const id = newItem?.i ?? oldItem?.i;
  return id === undefined ? undefined : asWidgetInstanceId(id);
};

export function ReactGridLayoutAdapter({
  items,
  editMode,
  rowHeight = 32,
  margin = [12, 12],
  containerPadding = [0, 0],
  renderItem,
  onDraftChange,
  onInteractionStart,
  onInteractionEnd,
  externalDrop,
  onExternalWidgetDrop,
}: ReactGridLayoutAdapterProps) {
  const { width, containerRef } = useContainerWidth({
    initialWidth: 1_280,
    measureBeforeMount: false,
  });
  const rglLayout = toRglLayout(items);
  const onStart =
    (kind: "move" | "resize"): EventCallback =>
    (_layout, oldItem, newItem) => {
      const instanceId = interactionId(oldItem, newItem);
      if (instanceId !== undefined) onInteractionStart?.(kind, instanceId);
    };
  const onEnd =
    (kind: "move" | "resize"): EventCallback =>
    (layout, oldItem, newItem) => {
      const instanceId = interactionId(oldItem, newItem);
      const translated = fromRglLayout(layout, items);
      onDraftChange(translated);
      if (instanceId !== undefined) onInteractionEnd?.(kind, translated, instanceId);
    };

  return (
    <div ref={containerRef} className="wb-dashboard-grid-container">
      <GridLayout
        width={width}
        layout={rglLayout}
        gridConfig={{
          cols: DASHBOARD_COLUMNS,
          rowHeight,
          margin,
          containerPadding,
        }}
        dragConfig={{
          enabled: editMode,
          handle: ".wb-widget-drag-handle",
          cancel:
            ".wb-widget-frame__content,.wb-widget-body,button,input,textarea,select,a",
        }}
        resizeConfig={{ enabled: editMode, handles: ["se"] }}
        dropConfig={{
          enabled: editMode && externalDrop !== undefined,
          defaultItem: {
            w: externalDrop?.w ?? 1,
            h: externalDrop?.h ?? 1,
          },
        }}
        droppingItem={
          externalDrop === undefined
            ? undefined
            : {
                i: externalDrop.instanceId,
                x: 0,
                y: 0,
                w: externalDrop.w,
                h: externalDrop.h,
                ...(externalDrop.minW === undefined ? {} : { minW: externalDrop.minW }),
                ...(externalDrop.maxW === undefined ? {} : { maxW: externalDrop.maxW }),
                ...(externalDrop.minH === undefined ? {} : { minH: externalDrop.minH }),
                ...(externalDrop.maxH === undefined ? {} : { maxH: externalDrop.maxH }),
              }
        }
        compactor={WORK_BUDDY_NO_COMPACTION}
        onLayoutChange={(layout) => {
          if (editMode) onDraftChange(fromRglLayout(layout, items));
        }}
        onDragStart={onStart("move")}
        onDragStop={onEnd("move")}
        onResizeStart={onStart("resize")}
        onResizeStop={onEnd("resize")}
        onDrop={(_layout, dropped) => {
          if (externalDrop === undefined || dropped === undefined) return;
          const placement: WidgetLayoutItem = {
            instanceId: externalDrop.instanceId,
            x: dropped.x,
            y: dropped.y,
            w: dropped.w,
            h: dropped.h,
            ...(externalDrop.minW === undefined ? {} : { minW: externalDrop.minW }),
            ...(externalDrop.maxW === undefined ? {} : { maxW: externalDrop.maxW }),
            ...(externalDrop.minH === undefined ? {} : { minH: externalDrop.minH }),
            ...(externalDrop.maxH === undefined ? {} : { maxH: externalDrop.maxH }),
          };
          onExternalWidgetDrop?.(externalDrop.widgetTypeId, placement);
        }}
      >
        {items.map((item) => (
          <div
            key={item.instanceId}
            className="wb-dashboard-grid-item"
            data-widget-instance-id={item.instanceId}
          >
            {editMode ? (
              <span className="wb-widget-drag-handle" aria-hidden="true" />
            ) : null}
            {renderItem(item)}
          </div>
        ))}
      </GridLayout>
    </div>
  );
}

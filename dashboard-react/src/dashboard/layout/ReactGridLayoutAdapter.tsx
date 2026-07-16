import {
  GridLayout,
  getCompactor,
  useContainerWidth,
  type EventCallback,
  type Layout as RglLayout,
  type LayoutItem as RglLayoutItem,
} from "react-grid-layout";
import { GridBackground } from "react-grid-layout/extras";
import { DotsSixVertical } from "@phosphor-icons/react/DotsSixVertical";
import { useCallback, useEffect, useRef, useState, type Ref } from "react";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";

import {
  asWidgetInstanceId,
  type WidgetInstanceId,
} from "../contributions/contracts";
import {
  DASHBOARD_COLUMNS,
  type DashboardLayout,
  type LayoutInteractionCancelReason,
  type ReactGridLayoutAdapterProps,
  type WidgetLayoutItem,
} from "./contracts";

export const WORK_BUDDY_NO_COMPACTION = getCompactor(null, false, true);
export const WORK_BUDDY_RESIZE_HANDLES = [
  "n",
  "e",
  "s",
  "w",
  "ne",
  "nw",
  "se",
  "sw",
] as const;

export const toRglLayout = (
  items: DashboardLayout,
  interactionEnabled = true,
): RglLayout =>
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
    isDraggable: interactionEnabled && item.positionLocked !== true,
    isResizable: interactionEnabled && item.sizeLocked !== true,
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
  onKeyboardCommand,
  onInteractionRejected,
  onInteractionCancel,
  onInteractionEnd,
  externalDrop,
  onExternalWidgetDrop,
}: ReactGridLayoutAdapterProps) {
  const { width, containerRef, mounted } = useContainerWidth({
    initialWidth: 1_280,
    measureBeforeMount: true,
  });
  const rglLayout = toRglLayout(items, editMode);
  const [interactionEpoch, setInteractionEpoch] = useState(0);
  const latestPreviewRef = useRef(items);
  const gridRows = Math.max(
    1,
    items.reduce((bottom, item) => Math.max(bottom, item.y + item.h), 0),
  );
  const interactionRef = useRef<{
    readonly kind: "move" | "resize";
    readonly instanceId: WidgetInstanceId;
    readonly origin: Pick<RglLayoutItem, "x" | "y" | "w" | "h">;
    readonly pointer?: { readonly x: number; readonly y: number };
  } | null>(null);
  const pointerFor = (event: Event) =>
    "clientX" in event && "clientY" in event
      ? { x: Number(event.clientX), y: Number(event.clientY) }
      : undefined;
  const cancelActiveInteraction = useCallback(
    (reason: LayoutInteractionCancelReason) => {
      const interaction = interactionRef.current;
      if (interaction === null) return;
      interactionRef.current = null;
      setInteractionEpoch((current) => current + 1);
      onInteractionCancel?.(interaction.kind, interaction.instanceId, reason);
    },
    [onInteractionCancel],
  );
  const finishReleasedInteraction = useCallback(
    (endPointer?: { readonly x: number; readonly y: number }) => {
      const interaction = interactionRef.current;
      if (interaction === null) return;
      const currentItems = latestPreviewRef.current;
      const currentItem = currentItems.find(
        (item) => item.instanceId === interaction.instanceId,
      );
      const movedPointer =
        interaction.pointer !== undefined && endPointer !== undefined
          ? Math.hypot(
              endPointer.x - interaction.pointer.x,
              endPointer.y - interaction.pointer.y,
            ) >= 8
          : false;
      const unchanged =
        currentItem !== undefined &&
        currentItem.x === interaction.origin.x &&
        currentItem.y === interaction.origin.y &&
        currentItem.w === interaction.origin.w &&
        currentItem.h === interaction.origin.h;

      interactionRef.current = null;
      // RGL/react-resizable can occasionally miss its document-level stop
      // callback even though the window observed the release. Preserve the
      // last valid preview, then remount to clear library-private state.
      onDraftChange(currentItems);
      setInteractionEpoch((current) => current + 1);
      if (movedPointer && unchanged) {
        onInteractionRejected?.(interaction.kind, interaction.instanceId);
      }
      onInteractionEnd?.(interaction.kind, currentItems, interaction.instanceId);
    },
    [onDraftChange, onInteractionEnd, onInteractionRejected],
  );

  useEffect(() => {
    if (!editMode) return;

    const scheduleReleaseCheck = (event: MouseEvent | PointerEvent) => {
      const endPointer = pointerFor(event);
      window.queueMicrotask(() => finishReleasedInteraction(endPointer));
    };
    const cancelWhenReleased = (event: MouseEvent | PointerEvent) => {
      if (event.buttons === 0) finishReleasedInteraction(pointerFor(event));
    };
    const cancelWhenHidden = () => {
      if (document.visibilityState === "hidden") {
        cancelActiveInteraction("document-hidden");
      }
    };
    const cancelPointer = () => cancelActiveInteraction("pointer-cancelled");
    const cancelBlurred = () => cancelActiveInteraction("window-blurred");

    window.addEventListener("mouseup", scheduleReleaseCheck, true);
    window.addEventListener("pointerup", scheduleReleaseCheck, true);
    window.addEventListener("mousemove", cancelWhenReleased, true);
    window.addEventListener("pointermove", cancelWhenReleased, true);
    window.addEventListener("pointercancel", cancelPointer, true);
    window.addEventListener("touchcancel", cancelPointer, true);
    window.addEventListener("blur", cancelBlurred);
    document.addEventListener("visibilitychange", cancelWhenHidden);

    return () => {
      window.removeEventListener("mouseup", scheduleReleaseCheck, true);
      window.removeEventListener("pointerup", scheduleReleaseCheck, true);
      window.removeEventListener("mousemove", cancelWhenReleased, true);
      window.removeEventListener("pointermove", cancelWhenReleased, true);
      window.removeEventListener("pointercancel", cancelPointer, true);
      window.removeEventListener("touchcancel", cancelPointer, true);
      window.removeEventListener("blur", cancelBlurred);
      document.removeEventListener("visibilitychange", cancelWhenHidden);
    };
  }, [cancelActiveInteraction, editMode, finishReleasedInteraction]);

  useEffect(() => {
    if (!editMode && interactionRef.current !== null) {
      cancelActiveInteraction("edit-mode-ended");
    }
  }, [cancelActiveInteraction, editMode]);
  const onStart =
    (kind: "move" | "resize"): EventCallback =>
    (_layout, oldItem, newItem, _placeholder, event) => {
      const instanceId = interactionId(oldItem, newItem);
      const origin = oldItem ?? newItem;
      if (instanceId !== undefined && origin !== null) {
        latestPreviewRef.current = items;
        const pointer = pointerFor(event);
        interactionRef.current = {
          kind,
          instanceId,
          origin: { x: origin.x, y: origin.y, w: origin.w, h: origin.h },
          ...(pointer === undefined ? {} : { pointer }),
        };
        onInteractionStart?.(kind, instanceId);
      }
    };
  const onEnd =
    (kind: "move" | "resize"): EventCallback =>
    (layout, oldItem, newItem, _placeholder, event) => {
      const instanceId = interactionId(oldItem, newItem);
      const translated = fromRglLayout(layout, items);
      const interaction = interactionRef.current;
      // End the adapter-owned interaction before invoking consumer callbacks.
      // Those callbacks synchronously update ViewHost state and can re-enter the
      // lost-pointer recovery effect during React's external event processing.
      interactionRef.current = null;
      const endPointer = pointerFor(event);
      const movedPointer =
        interaction?.pointer !== undefined && endPointer !== undefined
          ? Math.hypot(
              endPointer.x - interaction.pointer.x,
              endPointer.y - interaction.pointer.y,
            ) >= 8
          : false;
      const unchanged =
        newItem !== null &&
        interaction !== null &&
        newItem.x === interaction.origin.x &&
        newItem.y === interaction.origin.y &&
        newItem.w === interaction.origin.w &&
        newItem.h === interaction.origin.h;
      onDraftChange(translated);
      if (movedPointer && unchanged && instanceId !== undefined) {
        onInteractionRejected?.(kind, instanceId);
      }
      if (instanceId !== undefined) onInteractionEnd?.(kind, translated, instanceId);
    };
  const onPreview: EventCallback = (layout) => {
    latestPreviewRef.current = fromRglLayout(layout, items);
  };

  return (
    <div
      ref={containerRef}
      className="wb-dashboard-grid-container"
      data-grid-measured={mounted ? "true" : "false"}
    >
      {mounted && editMode ? (
        <GridBackground
          width={width}
          cols={DASHBOARD_COLUMNS}
          rowHeight={rowHeight}
          margin={[margin[0], margin[1]]}
          containerPadding={[containerPadding[0], containerPadding[1]]}
          rows={gridRows}
          color="color-mix(in srgb, var(--wb-color-edit-grid) 42%, transparent)"
          borderRadius={4}
          className="wb-dashboard-grid-guide"
        />
      ) : null}
      {mounted ? (
        <GridLayout
          key={`${editMode ? "editing" : "viewing"}:${interactionEpoch}`}
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
              ".wb-widget-frame__content,.wb-widget-body,button:not(.wb-widget-drag-handle),input,textarea,select,a",
          }}
          resizeConfig={{
            enabled: editMode,
            handles: editMode ? [...WORK_BUDDY_RESIZE_HANDLES] : [],
            handleComponent: editMode
              ? (axis, ref) => (
                  <span
                    ref={ref as Ref<HTMLSpanElement>}
                    className={`react-resizable-handle react-resizable-handle-${axis} wb-widget-resize-handle wb-widget-resize-handle--${axis}`}
                    data-wb-resize-axis={axis}
                    aria-hidden="true"
                  />
                )
              : undefined,
          }}
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
          onDrag={onPreview}
          onDragStop={onEnd("move")}
          onResizeStart={onStart("resize")}
          onResize={onPreview}
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
                <button
                  type="button"
                  className="wb-widget-drag-handle"
                  aria-label="Move or resize widget. Arrow keys move; Shift plus arrow keys resize."
                  aria-keyshortcuts="ArrowLeft ArrowRight ArrowUp ArrowDown Shift+ArrowLeft Shift+ArrowRight Shift+ArrowUp Shift+ArrowDown"
                  title="Arrow keys move. Shift + arrow keys resize."
                  onKeyDown={(event) => {
                    const direction = {
                      ArrowLeft: "left",
                      ArrowRight: "right",
                      ArrowUp: "up",
                      ArrowDown: "down",
                    }[event.key] as "left" | "right" | "up" | "down" | undefined;
                    if (direction === undefined) return;
                    event.preventDefault();
                    if (event.shiftKey) {
                      const resizeDirection = {
                        left: "shrink-width",
                        right: "grow-width",
                        up: "shrink-height",
                        down: "grow-height",
                      }[direction] as
                        | "grow-width"
                        | "shrink-width"
                        | "grow-height"
                        | "shrink-height";
                      onKeyboardCommand?.({
                        kind: "resize",
                        instanceId: item.instanceId,
                        direction: resizeDirection,
                      });
                      return;
                    }
                    onKeyboardCommand?.({
                      kind: "move",
                      instanceId: item.instanceId,
                      direction,
                    });
                  }}
                >
                  <DotsSixVertical weight="bold" />
                </button>
              ) : null}
              {renderItem(item)}
            </div>
          ))}
        </GridLayout>
      ) : null}
    </div>
  );
}

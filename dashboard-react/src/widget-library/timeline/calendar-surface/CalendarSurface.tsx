import { useEffect, useMemo, useRef, useState } from "react";

import type {
  CalendarItemActionDescriptor,
  CalendarItemActionResolver,
} from "./actions";
import { defaultCalendarItemActions } from "./actions";
import { CalendarItemInspector } from "./CalendarItemInspector";
import type {
  CalendarSurfaceIntent,
  CalendarSurfaceIntentResult,
} from "./contracts";
import {
  FullCalendarSurfaceAdapter,
  type FullCalendarSurfaceAdapterProps,
} from "./fullcalendar/FullCalendarSurfaceAdapter";

export interface CalendarSurfaceProps
  extends Omit<FullCalendarSurfaceAdapterProps, "onItemActivate"> {
  readonly resolveItemActions?: CalendarItemActionResolver;
}

export function CalendarSurface({
  model,
  density,
  onIntent,
  onAnnouncement,
  createRequestId,
  resolveItemActions,
}: CalendarSurfaceProps) {
  const [activeItemId, setActiveItemId] = useState<string | null>(null);
  const triggerRef = useRef<Element | null>(null);
  const requestSequenceRef = useRef(0);
  const activeItem = model.items.find((item) => item.id === activeItemId);
  const activeSource = model.sources.find(
    (source) => source.sourceId === activeItem?.sourceId,
  );

  useEffect(() => {
    if (activeItemId !== null && !activeItem) {
      setActiveItemId(null);
      triggerRef.current = null;
    }
  }, [activeItem, activeItemId]);

  const resolution = useMemo(() => {
    if (!activeItem) return undefined;
    const base = defaultCalendarItemActions(activeItem, model.access);
    return resolveItemActions?.({
      item: activeItem,
      source: activeSource,
      access: model.access,
      base,
    }) ?? base;
  }, [activeItem, activeSource, model.access, resolveItemActions]);

  const nextRequestId = () => {
    if (createRequestId) return createRequestId();
    requestSequenceRef.current += 1;
    return `calendar-item-action:${model.revision}:${requestSequenceRef.current}`;
  };

  const closeInspector = () => {
    setActiveItemId(null);
  };

  const handleAction = async (action: CalendarItemActionDescriptor) => {
    if (!activeItem) return;
    const intent: CalendarSurfaceIntent =
      action.dispatch === "open"
        ? { type: "calendar.item-open-requested", itemId: activeItem.id }
        : {
            type: "calendar.item-action-requested",
            requestId: nextRequestId(),
            actionId: action.id,
            itemId: activeItem.id,
            expectedRevision: activeItem.revision,
          };
    try {
      const result: CalendarSurfaceIntentResult = await onIntent(intent);
      if (result.status === "accepted") {
        onAnnouncement?.(`${action.label} requested for ${activeItem.title}.`, "polite");
        if (action.closeOnAction) closeInspector();
        return;
      }
      onAnnouncement?.(
        result.message ?? `${action.label} is not available for this item.`,
        "assertive",
      );
    } catch {
      onAnnouncement?.("The calendar action could not be requested.", "assertive");
    }
  };

  return (
    <>
      <FullCalendarSurfaceAdapter
        model={model}
        density={density}
        onIntent={onIntent}
        onAnnouncement={onAnnouncement}
        createRequestId={createRequestId}
        onItemActivate={(item, triggerElement) => {
          triggerRef.current = triggerElement;
          setActiveItemId(item.id);
        }}
      />
      {activeItem && resolution ? (
        <CalendarItemInspector
          item={activeItem}
          source={activeSource}
          timezone={model.timezone}
          resolution={resolution}
          triggerRef={triggerRef}
          onAction={(action) => void handleAction(action)}
          onClose={closeInspector}
        />
      ) : null}
    </>
  );
}

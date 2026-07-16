import { useEffect, useMemo, useState } from "react";
import { ArrowsClockwise } from "@phosphor-icons/react/ArrowsClockwise";

import type { WidgetRendererProps } from "../../dashboard/contributions/contracts";
import { Button, InlineAlert, SegmentedControl } from "../../ui";
import { createWidgetIntent } from "../shared";
import type {
  CalendarSurfaceIntent,
  CalendarSurfaceIntentResult,
} from "./calendar-surface/contracts";
import { CalendarSurface } from "./calendar-surface/CalendarSurface";
import { toCalendarSurfaceModel } from "./calendar-surface/fromDayTimeline";
import type {
  DayTimelineInput,
  DayTimelineIntent,
  TimelineRenderMode,
} from "./contracts";
import "./styles.css";

export default function DayTimelineWidget({
  input,
  emit,
  presentation,
}: WidgetRendererProps<DayTimelineInput, DayTimelineIntent>) {
  const [renderMode, setRenderMode] = useState(input.renderMode);
  const [announcement, setAnnouncement] = useState("");
  const compact = presentation.sizeMode === "compact";
  const density = compact ? "compact" : input.density;
  const readOnly = input.access?.mode === "read_only";
  const calendarModel = useMemo(
    () => toCalendarSurfaceModel({ ...input, renderMode }),
    [input, renderMode],
  );

  useEffect(() => setRenderMode(input.renderMode), [input.renderMode]);

  const setMode = (next: TimelineRenderMode) => {
    setRenderMode(next);
    emit(
      createWidgetIntent(presentation, "wb.timeline.render-mode-changed", {
        render_mode: next,
      }) as DayTimelineIntent,
    );
  };
  const requestReplan = () => {
    void emit(
      createWidgetIntent(presentation, "wb.timeline.replan-requested", {
        day_id: input.day.dayId,
        preserve_before: input.day.now,
      }) as DayTimelineIntent,
    );
  };

  const handleCalendarIntent = async (
    intent: CalendarSurfaceIntent,
  ): Promise<CalendarSurfaceIntentResult> => {
    if (intent.type === "calendar.range-requested") {
      return { status: "accepted", revision: input.revision };
    }
    if (intent.type === "calendar.item-open-requested") {
      const result = await emit(
        createWidgetIntent(presentation, "wb.timeline.open-item", {
          item_id: intent.itemId,
        }) as DayTimelineIntent,
      );
      return {
        status: result.status,
        ...(typeof result.revision === "string" ? { revision: result.revision } : {}),
        ...(result.message === undefined ? {} : { message: result.message }),
      };
    }
    if (intent.type === "calendar.item-action-requested") {
      const result = await emit(
        createWidgetIntent(
          presentation,
          "wb.timeline.item-action-requested",
          {
            item_id: intent.itemId,
            action_id: intent.actionId,
            expected_revision: intent.expectedRevision,
          },
          {
            intentId: intent.requestId,
            clientMutationId: intent.requestId,
          },
        ) as DayTimelineIntent,
      );
      return {
        status: result.status,
        ...(typeof result.revision === "string" ? { revision: result.revision } : {}),
        ...(result.message === undefined ? {} : { message: result.message }),
      };
    }
    return {
      status: "unavailable",
      revision: input.revision,
      message: "This Journal timeline action is not available yet.",
    };
  };

  return (
    <div className="wb-day-timeline">
      <div className="wb-day-timeline__toolbar">
        <SegmentedControl
          label="Timeline display mode"
          value={renderMode}
          options={[
            { value: "timeline", label: "Timeline" },
            { value: "list", label: "List" },
          ]}
          onChange={setMode}
        />
        {presentation.sizeMode === "expanded" && (
          <Button size="small" disabled={readOnly} onClick={requestReplan}>
            <ArrowsClockwise aria-hidden="true" /> Request replan
          </Button>
        )}
      </div>
      {readOnly && <InlineAlert tone="warning">{input.access?.reason}</InlineAlert>}
      {input.items.length === 0 ? (
        <p className="wb-day-timeline__empty">No temporal items for this day.</p>
      ) : (
        <CalendarSurface
          model={calendarModel}
          density={density}
          onIntent={handleCalendarIntent}
          onAnnouncement={(message) => setAnnouncement(message)}
        />
      )}
      <p className="wb-visually-hidden" role="status">
        {announcement}
      </p>
      <p className="wb-visually-hidden">
        Every item includes textual kind, status, provenance, and mutability; color is
        supplementary.
      </p>
    </div>
  );
}

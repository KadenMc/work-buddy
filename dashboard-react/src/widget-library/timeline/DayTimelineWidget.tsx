import { useEffect, useState } from "react";

import type { WidgetRendererProps } from "../../dashboard/contributions/contracts";
import { Button, InlineAlert } from "../../ui";
import { createWidgetIntent } from "../shared";
import type {
  DayTimelineInput,
  DayTimelineIntent,
  DayTimelineItem,
  TimelineRenderMode,
} from "./contracts";
import { TemporalCanvas, TemporalList } from "./TemporalCanvas";

export default function DayTimelineWidget({
  input,
  emit,
  presentation,
}: WidgetRendererProps<DayTimelineInput, DayTimelineIntent>) {
  const [renderMode, setRenderMode] = useState(input.renderMode);
  const compact = presentation.sizeMode === "compact";
  const effectiveMode = compact ? "list" : renderMode;
  const readOnly = input.access?.mode === "read_only";

  useEffect(() => setRenderMode(input.renderMode), [input.renderMode]);

  const setMode = (next: TimelineRenderMode) => {
    setRenderMode(next);
    emit(
      createWidgetIntent(presentation, "wb.timeline.render-mode-changed", {
        render_mode: next,
      }) as DayTimelineIntent,
    );
  };
  const openItem = (item: DayTimelineItem) => {
    emit(
      createWidgetIntent(presentation, "wb.timeline.open-item", {
        item_id: item.itemId,
      }) as DayTimelineIntent,
    );
  };
  const requestReplan = () => {
    emit(
      createWidgetIntent(presentation, "wb.timeline.replan-requested", {
        day_id: input.day.dayId,
        preserve_before: input.day.now,
      }) as DayTimelineIntent,
    );
  };

  return (
    <section className="wb-day-timeline" aria-label="Day timeline">
      <div className="wb-day-timeline__toolbar">
        {!compact && (
          <div className="wb-day-timeline__mode" aria-label="Timeline display mode">
            <Button
              variant={renderMode === "timeline" ? "primary" : "ghost"}
              aria-pressed={renderMode === "timeline"}
              onClick={() => setMode("timeline")}
            >
              Timeline
            </Button>
            <Button
              variant={renderMode === "list" ? "primary" : "ghost"}
              aria-pressed={renderMode === "list"}
              onClick={() => setMode("list")}
            >
              List
            </Button>
          </div>
        )}
        {presentation.sizeMode === "expanded" && (
          <Button disabled={readOnly} onClick={requestReplan}>
            Request replan
          </Button>
        )}
      </div>
      {readOnly && <InlineAlert tone="warning">{input.access?.reason}</InlineAlert>}
      {input.items.length === 0 ? (
        <p className="wb-day-timeline__empty">No temporal items for this day.</p>
      ) : effectiveMode === "timeline" ? (
        <TemporalCanvas
          day={input.day}
          items={input.items}
          density={input.density}
          onOpenItem={openItem}
        />
      ) : (
        <TemporalList day={input.day} items={input.items} onOpenItem={openItem} />
      )}
      <p className="wb-day-timeline__legend">
        Every item includes textual kind, status, provenance, and mutability; color is
        supplementary.
      </p>
    </section>
  );
}
